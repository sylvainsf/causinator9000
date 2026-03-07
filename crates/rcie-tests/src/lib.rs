// Copyright (c) 2026 Sylvain Niles. MIT License.

//! RCIE Test Utilities — shared helpers for HTTP integration and load tests.
//!
//! This crate provides common HTTP client wrappers and test helpers
//! for testing the RCIE engine's REST API.

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};

/// Default engine URL.
pub const DEFAULT_ENGINE_URL: &str = "http://localhost:8080";

/// Get the engine URL from the environment or use the default.
pub fn engine_url() -> String {
    std::env::var("RCIE_ENGINE_URL").unwrap_or_else(|_| DEFAULT_ENGINE_URL.to_string())
}

// ── Request / Response types ─────────────────────────────────────────────

#[derive(Debug, Serialize)]
pub struct InjectMutation {
    pub node_id: String,
    pub mutation_type: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub timestamp: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct InjectSignal {
    pub node_id: String,
    pub signal_type: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub value: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub severity: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct InjectResponse {
    pub status: String,
    pub id: String,
}

#[derive(Debug, Deserialize)]
pub struct HealthResponse {
    pub status: String,
    pub version: String,
    pub nodes: usize,
    pub edges: usize,
    pub active_mutations: usize,
    pub active_signals: usize,
}

#[derive(Debug, Deserialize)]
pub struct DiagnosisResponse {
    pub target_node: String,
    pub confidence: f64,
    pub root_cause: Option<String>,
    pub causal_path: Vec<String>,
    pub competing_causes: Vec<(String, f64)>,
    pub timestamp: String,
}

// ── HTTP Client ──────────────────────────────────────────────────────────

/// RCIE test client for interacting with the engine's REST API.
pub struct TestClient {
    base_url: String,
    client: reqwest::Client,
}

impl TestClient {
    /// Create a new test client.
    pub fn new(base_url: &str) -> Self {
        Self {
            base_url: base_url.trim_end_matches('/').to_string(),
            client: reqwest::Client::new(),
        }
    }

    /// Create a test client using the engine URL from the environment.
    pub fn from_env() -> Self {
        Self::new(&engine_url())
    }

    /// Check if the engine is healthy.
    pub async fn health(&self) -> Result<HealthResponse> {
        let resp = self
            .client
            .get(format!("{}/api/health", self.base_url))
            .timeout(std::time::Duration::from_secs(5))
            .send()
            .await
            .context("health check failed")?;
        resp.json().await.context("parsing health response")
    }

    /// Check if the engine is reachable.
    pub async fn is_healthy(&self) -> bool {
        self.health().await.is_ok()
    }

    /// Inject a mutation.
    pub async fn inject_mutation(&self, mutation: InjectMutation) -> Result<InjectResponse> {
        let resp = self
            .client
            .post(format!("{}/api/mutations", self.base_url))
            .json(&mutation)
            .timeout(std::time::Duration::from_secs(5))
            .send()
            .await
            .context("inject mutation failed")?;
        resp.json().await.context("parsing mutation response")
    }

    /// Inject a signal.
    pub async fn inject_signal(&self, signal: InjectSignal) -> Result<InjectResponse> {
        let resp = self
            .client
            .post(format!("{}/api/signals", self.base_url))
            .json(&signal)
            .timeout(std::time::Duration::from_secs(5))
            .send()
            .await
            .context("inject signal failed")?;
        resp.json().await.context("parsing signal response")
    }

    /// Diagnose a specific node.
    pub async fn diagnose(&self, node_id: &str) -> Result<DiagnosisResponse> {
        let resp = self
            .client
            .get(format!("{}/api/diagnosis?target={node_id}", self.base_url))
            .timeout(std::time::Duration::from_secs(10))
            .send()
            .await
            .context("diagnosis failed")?;
        let diag: DiagnosisResponse = resp.json().await.context("parsing diagnosis response")?;
        Ok(diag)
    }

    /// Diagnose a node and measure latency in milliseconds.
    pub async fn diagnose_timed(&self, node_id: &str) -> Result<(DiagnosisResponse, f64)> {
        let start = std::time::Instant::now();
        let resp = self.diagnose(node_id).await?;
        let elapsed_ms = start.elapsed().as_secs_f64() * 1000.0;
        Ok((resp, elapsed_ms))
    }

    /// Clear all events.
    pub async fn clear(&self) -> Result<()> {
        self.client
            .post(format!("{}/api/clear", self.base_url))
            .timeout(std::time::Duration::from_secs(5))
            .send()
            .await
            .context("clear failed")?;
        Ok(())
    }

    /// Get the underlying reqwest client for custom requests.
    pub fn inner(&self) -> &reqwest::Client {
        &self.client
    }

    /// Get the base URL.
    pub fn base_url(&self) -> &str {
        &self.base_url
    }
}

// ── Latency reporting ────────────────────────────────────────────────────

/// Latency statistics computed from a collection of measurements.
#[derive(Debug, Clone)]
pub struct LatencyStats {
    pub count: usize,
    pub avg_ms: f64,
    pub p50_ms: f64,
    pub p95_ms: f64,
    pub p99_ms: f64,
    pub max_ms: f64,
}

impl LatencyStats {
    /// Compute latency statistics from a slice of millisecond measurements.
    pub fn from_measurements(latencies: &mut [f64]) -> Option<Self> {
        if latencies.is_empty() {
            return None;
        }
        latencies.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let n = latencies.len();
        let avg = latencies.iter().sum::<f64>() / n as f64;
        Some(Self {
            count: n,
            avg_ms: avg,
            p50_ms: latencies[n / 2],
            p95_ms: latencies[(n as f64 * 0.95) as usize],
            p99_ms: latencies[(n as f64 * 0.99) as usize],
            max_ms: latencies[n - 1],
        })
    }

    /// Print a formatted latency report.
    pub fn print(&self, label: &str) {
        println!("\n  ▸ Results: {label}");
        println!("    count: {} queries", self.count);
        println!("    avg:   {:.2} ms", self.avg_ms);
        println!("    p50:   {:.2} ms", self.p50_ms);
        println!("    p95:   {:.2} ms", self.p95_ms);
        println!("    p99:   {:.2} ms", self.p99_ms);
        println!("    max:   {:.2} ms", self.max_ms);
    }
}

impl std::fmt::Display for LatencyStats {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "n={} avg={:.1}ms p50={:.1}ms p95={:.1}ms p99={:.1}ms max={:.1}ms",
            self.count, self.avg_ms, self.p50_ms, self.p95_ms, self.p99_ms, self.max_ms
        )
    }
}
