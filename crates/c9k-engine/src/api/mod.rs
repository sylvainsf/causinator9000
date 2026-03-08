// Copyright (c) 2026 Sylvain Niles. MIT License.

//! REST API — Axum HTTP endpoints for Causinator 9000 diagnostics + web UI.

use std::sync::Arc;

use axum::{
    Json, Router,
    extract::{DefaultBodyLimit, Path, Query, State},
    http::StatusCode,
    routing::{get, post},
};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use tower_http::services::ServeDir;

use crate::solver::{AlertGroup, GraphPayload, Mutation, Signal, SolverHandle};

// ── State ────────────────────────────────────────────────────────────────

type AppState = Arc<ApiState>;

struct ApiState {
    solver: SolverHandle,
}

// ── Request / Response types ─────────────────────────────────────────────

#[derive(Deserialize)]
struct DiagnosisQuery {
    target: String,
}

#[derive(Serialize)]
struct DiagnosisResponse {
    target_node: String,
    confidence: f64,
    root_cause: Option<String>,
    causal_path: Vec<String>,
    competing_causes: Vec<(String, f64)>,
    timestamp: String,
}

#[derive(Serialize)]
struct HealthResponse {
    status: String,
    version: String,
    nodes: usize,
    edges: usize,
    active_mutations: usize,
    active_signals: usize,
}

#[derive(Deserialize)]
struct InjectMutation {
    id: Option<String>,
    node_id: String,
    mutation_type: String,
    source: Option<String>,
    timestamp: Option<DateTime<Utc>>,
}

#[derive(Deserialize)]
struct InjectSignal {
    id: Option<String>,
    node_id: String,
    signal_type: String,
    value: Option<f64>,
    severity: Option<String>,
    timestamp: Option<DateTime<Utc>>,
}

#[derive(Serialize)]
struct InjectResponse {
    status: String,
    id: String,
}

// ── Handlers ─────────────────────────────────────────────────────────────

async fn get_diagnosis(
    State(state): State<AppState>,
    Query(params): Query<DiagnosisQuery>,
) -> Result<Json<DiagnosisResponse>, (StatusCode, String)> {
    let diagnosis = state
        .solver
        .diagnose(&params.target)
        .map_err(|e| (StatusCode::NOT_FOUND, e.to_string()))?;

    Ok(Json(DiagnosisResponse {
        target_node: diagnosis.target_node,
        confidence: diagnosis.confidence,
        root_cause: diagnosis.root_cause,
        causal_path: diagnosis.causal_path,
        competing_causes: diagnosis.competing_causes,
        timestamp: diagnosis.timestamp.to_rfc3339(),
    }))
}

async fn get_all_diagnoses(
    State(state): State<AppState>,
) -> Result<Json<Vec<DiagnosisResponse>>, (StatusCode, String)> {
    let diagnoses = state
        .solver
        .diagnose_all(0.3)
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;

    let responses: Vec<DiagnosisResponse> = diagnoses
        .into_iter()
        .map(|d| DiagnosisResponse {
            target_node: d.target_node,
            confidence: d.confidence,
            root_cause: d.root_cause,
            causal_path: d.causal_path,
            competing_causes: d.competing_causes,
            timestamp: d.timestamp.to_rfc3339(),
        })
        .collect();

    Ok(Json(responses))
}

async fn post_mutation(
    State(state): State<AppState>,
    Json(body): Json<InjectMutation>,
) -> Result<Json<InjectResponse>, (StatusCode, String)> {
    let id = body.id.unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
    let mutation = Mutation {
        id: id.clone(),
        node_id: body.node_id,
        mutation_type: body.mutation_type,
        source: body.source.unwrap_or_else(|| "api".to_string()),
        timestamp: body.timestamp.unwrap_or_else(Utc::now),
        properties: serde_json::json!({}),
    };
    state
        .solver
        .ingest_mutation(mutation)
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    Ok(Json(InjectResponse {
        status: "accepted".to_string(),
        id,
    }))
}

async fn post_signal(
    State(state): State<AppState>,
    Json(body): Json<InjectSignal>,
) -> Result<Json<InjectResponse>, (StatusCode, String)> {
    let id = body.id.unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
    let signal = Signal {
        id: id.clone(),
        node_id: body.node_id,
        signal_type: body.signal_type,
        value: body.value,
        severity: body.severity,
        timestamp: body.timestamp.unwrap_or_else(Utc::now),
        properties: serde_json::json!({}),
    };
    state
        .solver
        .ingest_signal(signal)
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    Ok(Json(InjectResponse {
        status: "accepted".to_string(),
        id,
    }))
}

async fn get_graph(
    State(state): State<AppState>,
    Path(_island_id): Path<String>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    state
        .solver
        .export_cytoscape()
        .map(Json)
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))
}

#[derive(Deserialize)]
struct NeighborhoodQuery {
    node: String,
    #[serde(default = "default_depth")]
    depth: usize,
}
fn default_depth() -> usize {
    2
}

async fn get_neighborhood(
    State(state): State<AppState>,
    Query(params): Query<NeighborhoodQuery>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    state
        .solver
        .neighborhood(&params.node, params.depth)
        .map(Json)
        .map_err(|e| (StatusCode::NOT_FOUND, e.to_string()))
}

async fn get_alerts(
    State(state): State<AppState>,
) -> Result<Json<Vec<serde_json::Value>>, (StatusCode, String)> {
    state
        .solver
        .alerts()
        .map(Json)
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))
}

async fn get_alert_graph(
    State(state): State<AppState>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    state
        .solver
        .alert_subgraphs()
        .map(Json)
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))
}

async fn get_alert_groups(
    State(state): State<AppState>,
) -> Result<Json<Vec<AlertGroup>>, (StatusCode, String)> {
    state
        .solver
        .alert_groups()
        .map(Json)
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))
}

async fn health(State(state): State<AppState>) -> Json<HealthResponse> {
    let (nodes, edges, mutations, signals) = state.solver.stats().unwrap_or((0, 0, 0, 0));
    Json(HealthResponse {
        status: "ok".to_string(),
        version: env!("CARGO_PKG_VERSION").to_string(),
        nodes,
        edges,
        active_mutations: mutations,
        active_signals: signals,
    })
}

async fn post_clear(
    State(state): State<AppState>,
) -> Result<Json<InjectResponse>, (StatusCode, String)> {
    state
        .solver
        .clear_events()
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    Ok(Json(InjectResponse {
        status: "cleared".to_string(),
        id: "all".to_string(),
    }))
}

async fn post_reload_cpts(
    State(state): State<AppState>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let path = std::env::var("C9K_HEURISTICS")
        .unwrap_or_else(|_| "config/heuristics.manifest.yaml".to_string());
    let count = state
        .solver
        .reload_heuristics(&path)
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    Ok(Json(serde_json::json!({
        "status": "reloaded",
        "classes": count,
        "path": path,
    })))
}

#[derive(Deserialize)]
struct WindowConfig {
    minutes: i64,
}

async fn get_window(
    State(state): State<AppState>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let mins = state
        .solver
        .get_temporal_window()
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    Ok(Json(serde_json::json!({ "temporal_window_minutes": mins })))
}

async fn post_window(
    State(state): State<AppState>,
    Json(body): Json<WindowConfig>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    state
        .solver
        .set_temporal_window(body.minutes)
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    Ok(Json(serde_json::json!({
        "status": "updated",
        "temporal_window_minutes": body.minutes,
    })))
}

async fn post_graph_load(
    State(state): State<AppState>,
    Json(payload): Json<GraphPayload>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let (nodes, edges) = state
        .solver
        .load_graph(payload)
        .map_err(|e| (StatusCode::BAD_REQUEST, e.to_string()))?;
    Ok(Json(serde_json::json!({
        "status": "loaded",
        "nodes": nodes,
        "edges": edges,
    })))
}

async fn get_graph_export(
    State(state): State<AppState>,
) -> Result<Json<GraphPayload>, (StatusCode, String)> {
    state
        .solver
        .export_graph()
        .map(Json)
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))
}

async fn get_memory(
    State(state): State<AppState>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let info = state
        .solver
        .memory_info()
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    Ok(Json(serde_json::json!(info)))
}

// ── Router ───────────────────────────────────────────────────────────────

pub async fn serve(solver: SolverHandle, addr: &str) -> anyhow::Result<()> {
    let state = Arc::new(ApiState { solver });

    let app = Router::new()
        // API endpoints
        .route("/api/health", get(health))
        .route("/api/diagnosis", get(get_diagnosis))
        .route("/api/diagnosis/all", get(get_all_diagnoses))
        .route("/api/mutations", post(post_mutation))
        .route("/api/signals", post(post_signal))
        .route("/api/clear", post(post_clear))
        .route("/api/graph/{island_id}", get(get_graph))
        .route("/api/neighborhood", get(get_neighborhood))
        .route("/api/alerts", get(get_alerts))
        .route("/api/alert-groups", get(get_alert_groups))
        .route("/api/alert-graph", get(get_alert_graph))
        .route("/api/reload-cpts", post(post_reload_cpts))
        .route("/api/window", get(get_window).post(post_window))
        .route("/api/graph/load", post(post_graph_load))
        .route("/api/graph/export", get(get_graph_export))
        .route("/api/memory", get(get_memory))
        // Allow large payloads for graph loading (up to 512MB)
        .layer(DefaultBodyLimit::max(512 * 1024 * 1024))
        // Legacy endpoints (backward compat)
        .route("/health", get(health))
        .route("/diagnosis", get(get_diagnosis))
        .route("/diagnosis/all", get(get_all_diagnoses))
        .route("/mutations", post(post_mutation))
        .route("/signals", post(post_signal))
        .route("/clear", post(post_clear))
        .with_state(state)
        // Static web UI
        .fallback_service(ServeDir::new("web"));

    let listener = tokio::net::TcpListener::bind(addr).await?;
    tracing::info!("API listening on {addr}");
    axum::serve(listener, app).await?;

    Ok(())
}
