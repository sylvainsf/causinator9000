// Copyright (c) 2026 Sylvain Niles. MIT License.

//! Drasi Integration — Embedded continuous query engine via drasi-lib
//!
//! Sets up:
//! - PostgreSQL CDC source (watches mutations, signals tables)
//! - Continuous queries (mutation-tracker, signal-tracker)
//! - Application reaction (feeds CQ results into the Bayesian solver)

use std::sync::Arc;

use anyhow::{Context, Result};
use drasi_index_rocksdb::RocksDbIndexProvider;
use drasi_lib::{DrasiLib, Query};
use drasi_reaction_application::ApplicationReaction;
// FIX 1: We only need PostgresSourceBuilder — it has a .build() that returns
// the source directly. No need to import PostgresReplicationSource separately.
use drasi_source_postgres::{PostgresSourceBuilder, TableKeyConfig};
// FIX 3: Import the result types so we can pattern-match on CQ output.
// QueryResult contains Vec<ResultDiff>, and ResultDiff::Add { data } holds
// the actual row data as serde_json::Value.
use drasi_lib::channels::ResultDiff;

use crate::solver::SolverHandle;

// ── CQ Definitions ──────────────────────────────────────────────────────

/// Mutation tracker: emits when new mutations are inserted.
const MUTATION_TRACKING_CQ: &str = r#"
MATCH (m:mutations)
RETURN m.id AS mutation_id,
       m.node_id AS node_id,
       m.mutation_type AS mutation_type,
       m.source AS source,
       m.timestamp AS timestamp
"#;

/// Signal tracker: emits when new signals are inserted.
const SIGNAL_TRACKING_CQ: &str = r#"
MATCH (s:signals)
RETURN s.id AS signal_id,
       s.node_id AS node_id,
       s.signal_type AS signal_type,
       s.value AS value,
       s.severity AS severity,
       s.timestamp AS timestamp
"#;

// ── Configuration ────────────────────────────────────────────────────────

pub struct DrasiConfig {
    pub pg_host: String,
    pub pg_port: u16,
    pub pg_database: String,
    pub pg_user: String,
    pub pg_password: String,
    pub slot_name: String,
    pub publication_name: String,
    pub rocksdb_path: String,
}

impl Default for DrasiConfig {
    fn default() -> Self {
        Self {
            pg_host: "localhost".to_string(),
            pg_port: crate::PG_PORT,
            pg_database: "c9k_poc".to_string(),
            pg_user: std::env::var("USER").unwrap_or_else(|_| "postgres".to_string()),
            pg_password: String::new(),
            slot_name: "drasi_slot".to_string(),
            publication_name: "drasi_pub".to_string(),
            rocksdb_path: "data/drasi-rocksdb".to_string(),
        }
    }
}

// ── Setup ────────────────────────────────────────────────────────────────

/// Initialize the embedded Drasi runtime and wire it to the solver.
///
/// Returns the DrasiLib handle and a tokio JoinHandle for the reaction
/// consumer task.
pub async fn init_drasi(
    config: DrasiConfig,
    solver: SolverHandle,
) -> Result<(DrasiLib, tokio::task::JoinHandle<()>)> {
    // FIX 1 & 2: PostgresSourceBuilder::new() requires an `id` argument.
    // In Rust, all function parameters are mandatory — there are no optional
    // or default args like Python/TypeScript. The id names this source within
    // DrasiLib so queries can reference it via .from_source("pg-source").
    //
    // FIX 2: The builder's .build() returns Result<PostgresReplicationSource>
    // directly — it constructs the source in one step. Our original code was
    // incorrectly calling build() then passing the result to a second
    // constructor. The builder IS the constructor.
    let pg_source = PostgresSourceBuilder::new("pg-source")
        .with_host(&config.pg_host)
        .with_port(config.pg_port)
        .with_database(&config.pg_database)
        .with_user(&config.pg_user)
        .with_password(&config.pg_password)
        .with_tables(vec![
            "mutations".to_string(),
            "signals".to_string(),
        ])
        .with_slot_name(&config.slot_name)
        .with_publication_name(&config.publication_name)
        // Tell Drasi which column is the PK for each table — without this,
        // the PG WAL decoder can't derive element IDs from replication events.
        .with_table_keys(vec![
            TableKeyConfig { table: "mutations".to_string(), key_columns: vec!["id".to_string()] },
            TableKeyConfig { table: "signals".to_string(), key_columns: vec!["id".to_string()] },
        ])
        .build()
        .context("creating PostgreSQL replication source")?;

    // 2. Build Application reaction (in-process consumer)
    let (reaction, handle) = ApplicationReaction::builder("solver-sink")
        .with_queries(vec![
            "mutation-tracker".to_string(),
            "signal-tracker".to_string(),
        ])
        .build();

    // 3. Build RocksDB index provider for persistent CQ state
    let rocksdb_provider = RocksDbIndexProvider::new(&config.rocksdb_path, true, false);

    // 4. Build DrasiLib
    let drasi = DrasiLib::builder()
        .with_id("c9k-engine")
        .with_source(pg_source)
        .with_reaction(reaction)
        .with_index_provider(Arc::new(rocksdb_provider))
        .with_query(
            Query::cypher("mutation-tracker")
                .query(MUTATION_TRACKING_CQ)
                .from_source("pg-source")
                .auto_start(true)
                .build(),
        )
        .with_query(
            Query::cypher("signal-tracker")
                .query(SIGNAL_TRACKING_CQ)
                .from_source("pg-source")
                .auto_start(true)
                .build(),
        )
        .build()
        .await
        .context("building DrasiLib")?;

    // 5. Start all components
    drasi.start().await.context("starting DrasiLib")?;

    tracing::info!("Drasi runtime started — watching PostgreSQL for mutations and signals");

    // 6. Spawn reaction consumer task
    let consumer_handle = tokio::spawn(consume_results(handle, solver));

    Ok((drasi, consumer_handle))
}

/// Extract a string field from a serde_json::Value object.
/// Returns "" if the field is missing or not a string.
fn json_str(data: &serde_json::Value, key: &str) -> String {
    data.get(key)
        .and_then(|v: &serde_json::Value| v.as_str())
        .unwrap_or("")
        .to_string()
}

/// Extract an optional f64 field from a serde_json::Value object.
fn json_f64(data: &serde_json::Value, key: &str) -> Option<f64> {
    data.get(key).and_then(|v: &serde_json::Value| v.as_f64())
}

/// Background task: reads CQ results from the ApplicationReaction and
/// feeds them into the Bayesian solver.
///
/// FIX 3: QueryResult is NOT a serde_json::Value — it's a struct with fields:
///   - query_id: String (which CQ produced this)
///   - results: Vec<ResultDiff> (the actual changes)
///   - timestamp, metadata, profiling
///
/// ResultDiff is an enum:
///   - Add { data: serde_json::Value }    — new row matched the CQ
///   - Delete { data: serde_json::Value }  — row no longer matches
///   - Update { data, before, after }     — row changed but still matches
///   - Noop                               — no change
///
/// We pattern-match on query_id to know whether it's a mutation or signal,
/// then extract fields from the `data` Value inside each ResultDiff::Add.
async fn consume_results(
    handle: drasi_reaction_application::ApplicationReactionHandle,
    solver: SolverHandle,
) {
    use chrono::{DateTime, Utc};

    // FIX 4: subscribe_with_options returns Result<Subscription>, where
    // Subscription has .recv() -> Option<QueryResult>. The Default::default()
    // gives us SubscriptionOptions with sensible defaults.
    let mut subscription = match handle.subscribe_with_options(Default::default()).await {
        Ok(sub) => sub,
        Err(e) => {
            tracing::error!(error = %e, "Failed to subscribe to reaction results");
            return;
        }
    };

    tracing::info!("Drasi reaction consumer started");

    while let Some(result) = subscription.recv().await {
        // Route based on which CQ produced this result
        for diff in &result.results {
            // We only care about new rows (Add). Deletes happen when rows
            // leave the CQ result set (e.g., mutation expired from window).
            let data = match diff {
                ResultDiff::Add { data } => data,
                _ => continue,
            };

            match result.query_id.as_str() {
                "mutation-tracker" => {
                    let mutation = crate::solver::Mutation {
                        id: json_str(data, "mutation_id"),
                        node_id: json_str(data, "node_id"),
                        mutation_type: json_str(data, "mutation_type"),
                        source: {
                            let s = json_str(data, "source");
                            if s.is_empty() { "drasi".to_string() } else { s }
                        },
                        timestamp: data
                            .get("timestamp")
                            .and_then(|v: &serde_json::Value| v.as_str())
                            .and_then(|s: &str| s.parse::<DateTime<Utc>>().ok())
                            .unwrap_or_else(Utc::now),
                        properties: serde_json::json!({}),
                    };
                    if let Err(e) = solver.ingest_mutation(mutation) {
                        tracing::warn!(error = %e, "Failed to ingest mutation from Drasi");
                    }
                }
                "signal-tracker" => {
                    let signal = crate::solver::Signal {
                        id: json_str(data, "signal_id"),
                        node_id: json_str(data, "node_id"),
                        signal_type: json_str(data, "signal_type"),
                        value: json_f64(data, "value"),
                        severity: data
                            .get("severity")
                            .and_then(|v: &serde_json::Value| v.as_str())
                            .map(|s: &str| s.to_string()),
                        timestamp: data
                            .get("timestamp")
                            .and_then(|v: &serde_json::Value| v.as_str())
                            .and_then(|s: &str| s.parse::<DateTime<Utc>>().ok())
                            .unwrap_or_else(Utc::now),
                        properties: serde_json::json!({}),
                    };
                    if let Err(e) = solver.ingest_signal(signal) {
                        tracing::warn!(error = %e, "Failed to ingest signal from Drasi");
                    }
                }
                other => {
                    tracing::debug!(query_id = other, "Ignoring result from unknown CQ");
                }
            }
        }
    }

    tracing::warn!("Drasi reaction consumer ended — subscription closed");
}
