// Copyright (c) 2026 Sylvain Niles. MIT License.

//! MCP server mode — `c9k-engine mcp`
//!
//! Pure Rust, no Python, no external scripts. Uses `gh` CLI for GitHub API.

use std::process::Command;

use anyhow::Result;
use rmcp::handler::server::router::tool::ToolRouter;
use rmcp::handler::server::wrapper::Parameters;
use rmcp::model::{ServerCapabilities, ServerInfo};
use rmcp::{schemars, tool, tool_handler, tool_router, ServerHandler, ServiceExt};

use crate::ingest;
use crate::solver::SolverHandle;

#[derive(Debug, Clone)]
pub struct C9kMcpServer {
    solver: SolverHandle,
    tool_router: ToolRouter<Self>,
}

impl C9kMcpServer {
    pub fn new(solver: SolverHandle) -> Self {
        Self {
            solver,
            tool_router: Self::tool_router(),
        }
    }
}

#[derive(Debug, serde::Deserialize, schemars::JsonSchema)]
pub struct IngestGithubParams {
    #[schemars(description = "GitHub repository in owner/name format (e.g. dapr/dapr)")]
    pub repo: String,
    #[schemars(description = "Hours to look back (default: 48)")]
    pub hours: Option<u32>,
}

#[derive(Debug, serde::Deserialize, schemars::JsonSchema)]
pub struct DiagnoseAllParams {
    #[schemars(description = "Minimum confidence threshold 0-100 (default: 10)")]
    pub min_confidence: Option<f64>,
}

#[derive(Debug, serde::Deserialize, schemars::JsonSchema)]
pub struct CommitInfoParams {
    #[schemars(description = "GitHub repository (owner/name)")]
    pub repo: String,
    #[schemars(description = "Commit SHA (full or abbreviated)")]
    pub sha: String,
}

#[tool_router]
impl C9kMcpServer {
    #[tool(description = "Check engine health and graph statistics")]
    fn c9k_health(&self) -> String {
        match self.solver.stats() {
            Ok((nodes, edges, mutations, signals)) => format!(
                "Engine: running (v0.1.0)\n- Nodes: {nodes}\n- Edges: {edges}\n\
                 - Active mutations: {mutations}\n- Active signals: {signals}"
            ),
            Err(e) => format!("Error: {e}"),
        }
    }

    #[tool(description = "Clear the entire causal graph")]
    fn c9k_clear(&self) -> String {
        match self.solver.clear_events() {
            Ok(_) => "Graph cleared.".to_string(),
            Err(e) => format!("Error: {e}"),
        }
    }

    #[tool(description = "Ingest GitHub Actions CI failures for a repo. Uses the gh CLI for auth.")]
    fn c9k_ingest_github(&self, Parameters(p): Parameters<IngestGithubParams>) -> String {
        let hours = p.hours.unwrap_or(48);
        match ingest::ingest_github(&self.solver, &p.repo, hours) {
            Ok(report) => {
                let mut text = format!("## GitHub Actions Ingestion: {}\n\n{report}\n", p.repo);
                // Append alert groups
                if let Ok(groups) = self.solver.alert_groups() {
                    if !groups.is_empty() {
                        text.push_str("\n### Alert Groups\n\n| Root Cause | Confidence | Members |\n|---|---|---|\n");
                        for g in &groups {
                            text.push_str(&format!("| {} | {:.0}% | {} |\n",
                                &g.root_cause, g.confidence * 100.0, g.members.len()));
                        }
                    }
                }
                text
            }
            Err(e) => format!("Ingestion failed: {e}"),
        }
    }

    #[tool(description = "Get all active diagnoses sorted by confidence")]
    fn c9k_diagnose_all(&self, Parameters(p): Parameters<DiagnoseAllParams>) -> String {
        let min = p.min_confidence.unwrap_or(10.0) / 100.0;
        match self.solver.diagnose_all(min) {
            Ok(d) if d.is_empty() => "No diagnoses above threshold.".to_string(),
            Ok(d) => {
                let mut text = format!("## Diagnoses ({} results)\n\n| Confidence | Target | Root Cause |\n|---|---|---|\n", d.len());
                for diag in d.iter().take(30) {
                    text.push_str(&format!("| {:.0}% | {} | {} |\n",
                        diag.confidence * 100.0, diag.target_node,
                        diag.root_cause.as_deref().unwrap_or("?")));
                }
                text
            }
            Err(e) => format!("Error: {e}"),
        }
    }

    #[tool(description = "Get correlated alert groups — failures grouped by shared root cause")]
    fn c9k_alert_groups(&self) -> String {
        match self.solver.alert_groups() {
            Ok(g) if g.is_empty() => "No alert groups.".to_string(),
            Ok(g) => {
                let mut text = format!("## Alert Groups ({} groups)\n\n| Root Cause | Confidence | Members |\n|---|---|---|\n", g.len());
                for group in &g {
                    text.push_str(&format!("| {} | {:.0}% | {} |\n",
                        &group.root_cause, group.confidence * 100.0, group.members.len()));
                }
                text
            }
            Err(e) => format!("Error: {e}"),
        }
    }

    #[tool(description = "Get commit details (message, author, date) for a SHA")]
    fn c9k_commit_info(&self, Parameters(p): Parameters<CommitInfoParams>) -> String {
        let output = match Command::new("gh")
            .args(["api", &format!("repos/{}/commits/{}", p.repo, p.sha),
                   "--jq", r#"{sha: .sha[0:8], message: .commit.message, author: .commit.author.name, date: .commit.author.date}"#])
            .env("GH_PAGER", "cat")
            .output()
        {
            Ok(o) if o.status.success() => o,
            Ok(o) => return format!("Failed: {}", String::from_utf8_lossy(&o.stderr)),
            Err(e) => return format!("gh cli error: {e}"),
        };
        let data: serde_json::Value = match serde_json::from_slice(&output.stdout) {
            Ok(d) => d,
            Err(e) => return format!("Parse error: {e}"),
        };
        format!("**{}** by {} ({})\n\n{}",
            data["sha"].as_str().unwrap_or(&p.sha),
            data["author"].as_str().unwrap_or("?"),
            data["date"].as_str().unwrap_or("?"),
            data["message"].as_str().unwrap_or("?"))
    }
}

#[tool_handler(router = self.tool_router)]
impl ServerHandler for C9kMcpServer {
    fn get_info(&self) -> ServerInfo {
        ServerInfo::new(ServerCapabilities::builder().enable_tools().build())
            .with_instructions(
                "Causinator 9000 — Bayesian causal inference engine for CI/CD failure diagnosis. \
                 Use c9k_ingest_github to load failures, then c9k_diagnose_all or c9k_alert_groups \
                 to analyze root causes.",
            )
    }
}

pub async fn serve_mcp(solver: SolverHandle) -> Result<()> {
    let server = C9kMcpServer::new(solver);
    let transport = rmcp::transport::io::stdio();
    let mcp = server.serve(transport).await
        .map_err(|e| anyhow::anyhow!("MCP init failed: {e}"))?;
    mcp.waiting().await
        .map_err(|e| anyhow::anyhow!("MCP server error: {e}"))?;
    Ok(())
}
