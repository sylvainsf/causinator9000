// Copyright (c) 2026 Sylvain Niles. MIT License.

use anyhow::Result;
use c9k_engine::{api, drasi, embedded_heuristics, ingest, mcp, solver};
use tracing_subscriber::EnvFilter;

/// Format a node ID as a markdown link to the GitHub Actions run.
/// e.g. job://owner/repo/12345/lint -> [12345/lint](https://github.com/owner/repo/actions/runs/12345)
fn format_target(id: &str) -> String {
    if let Some(rest) = id.strip_prefix("job://") {
        // rest = "owner/repo/run_id/job_slug"
        let parts: Vec<&str> = rest.splitn(4, '/').collect();
        if parts.len() == 4 {
            let owner = parts[0];
            let repo = parts[1];
            let run_id = parts[2];
            let job_slug = parts[3];
            return format!("[{run_id}/{job_slug}](https://github.com/{owner}/{repo}/actions/runs/{run_id})");
        }
        if parts.len() == 3 {
            let owner = parts[0];
            let repo = parts[1];
            let run_id = parts[2];
            return format!("[{run_id}](https://github.com/{owner}/{repo}/actions/runs/{run_id})");
        }
    }
    id.to_string()
}

/// Convert a root cause ID to a markdown-linked display string.
/// e.g. "commit://owner/repo/abc12345 (CodeChange)" -> "[`abc12345`](https://github.com/owner/repo/commit/abc12345) (CodeChange)"
fn format_root_cause(rc: &str) -> String {
    if let Some(rest) = rc.strip_prefix("commit://") {
        // rest = "owner/repo/sha8 (MutationType)" or "owner/repo/sha8"
        let (path, suffix) = if let Some(idx) = rest.find(" (") {
            (&rest[..idx], &rest[idx..])
        } else {
            (rest, "")
        };
        let parts: Vec<&str> = path.splitn(3, '/').collect();
        if parts.len() == 3 {
            let owner_repo = format!("{}/{}", parts[0], parts[1]);
            let sha = parts[2];
            return format!("[`{sha}`](https://github.com/{owner_repo}/commit/{sha}){suffix}");
        }
    }
    if let Some(rest) = rc.strip_prefix("latent://") {
        let (name, suffix) = if let Some(idx) = rest.find(" (") {
            (&rest[..idx], &rest[idx..])
        } else {
            (rest, "")
        };
        return format!("{name}{suffix}");
    }
    rc.to_string()
}

/// Load heuristics into the solver.
///
/// Priority:
///   1. `--heuristics <path>` CLI flag  → load from that file
///   2. `C9K_HEURISTICS` env var        → load from that file
///   3. `config/heuristics.manifest.yaml` on disk → load from that file
///   4. Embedded heuristics compiled into the binary (always available)
///
/// Pass `--embedded-heuristics` to skip disk entirely and use the built-in set.
fn load_heuristics(solver: &mut solver::BayesianSolver) -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    let use_embedded = args.iter().any(|a| a == "--embedded-heuristics");

    // Explicit CLI path takes priority over env var
    let custom_path = args
        .iter()
        .position(|a| a == "--heuristics")
        .and_then(|i| args.get(i + 1))
        .cloned();

    if use_embedded {
        let handle = solver.handle();
        for yaml in embedded_heuristics::ALL {
            handle.load_heuristics_str(yaml)?;
        }
        tracing::info!("Loaded embedded heuristics");
        return Ok(());
    }

    // Try custom path, then env var, then default disk location
    let path = custom_path
        .or_else(|| std::env::var("C9K_HEURISTICS").ok())
        .unwrap_or_else(|| "config/heuristics.manifest.yaml".to_string());

    if std::path::Path::new(&path).exists() {
        solver.load_heuristics(&path)?;
        tracing::info!(path = %path, "Loaded heuristics from disk");
    } else {
        // Fall back to embedded
        let handle = solver.handle();
        for yaml in embedded_heuristics::ALL {
            handle.load_heuristics_str(yaml)?;
        }
        tracing::info!("Disk heuristics not found at {path}, using embedded");
    }

    Ok(())
}

#[tokio::main]
async fn main() -> Result<()> {
    let mode = std::env::args().nth(1).unwrap_or_default();

    // MCP mode: minimal logging (stderr is MCP's transport), no Drasi, no REST API
    if mode == "mcp" {
        tracing_subscriber::fmt()
            .with_env_filter(EnvFilter::new("warn"))
            .with_writer(std::io::stderr)
            .init();

        let mut solver = solver::BayesianSolver::new()?;
        load_heuristics(&mut solver)?;

        let handle = solver.handle();
        return mcp::serve_mcp(handle).await;
    }

    // Report mode: ingest GitHub Actions failures, diagnose, print markdown report, exit
    if mode == "report" {
        tracing_subscriber::fmt()
            .with_env_filter(EnvFilter::new("warn"))
            .with_writer(std::io::stderr)
            .init();

        let args: Vec<String> = std::env::args().collect();
        let repo = args.iter().position(|a| a == "--repo")
            .and_then(|i| args.get(i + 1))
            .cloned()
            .or_else(|| std::env::var("GITHUB_REPOSITORY").ok())
            .expect("--repo <owner/name> is required (or set GITHUB_REPOSITORY)");

        let hours: u32 = args.iter().position(|a| a == "--hours")
            .and_then(|i| args.get(i + 1))
            .and_then(|v| v.parse().ok())
            .unwrap_or(168);

        let min_confidence: f64 = args.iter().position(|a| a == "--min-confidence")
            .and_then(|i| args.get(i + 1))
            .and_then(|v| v.parse::<f64>().ok())
            .unwrap_or(50.0) / 100.0;

        let mut solver = solver::BayesianSolver::new()?;
        load_heuristics(&mut solver)?;
        let handle = solver.handle();

        // Ingest
        let ingest_report = ingest::ingest_github(&handle, &repo, hours)?;
        eprintln!("{ingest_report}");

        // Diagnose
        let diagnoses = handle.diagnose_all(min_confidence)?;
        let groups = handle.alert_groups()?;

        // Health stats
        let (nodes, edges, mutations, signals) = handle.stats()?;

        // Format report
        if diagnoses.is_empty() {
            println!("## Causinator 9000 — No Failures Detected\n");
            println!("No CI failures found for `{repo}` in the last {hours}h above {:.0}% confidence.", min_confidence * 100.0);
            return Ok(());
        }

        println!("## Causinator 9000 — CI Failure Analysis\n");
        println!("**{} failures** diagnosed above {:.0}% confidence | {} nodes | {} edges | {} mutations | {} signals\n",
            diagnoses.len(), min_confidence * 100.0, nodes, edges, mutations, signals);

        // Alert groups table
        if !groups.is_empty() {
            println!("### Alert Groups\n");
            println!("| Root Cause | Confidence | Failures | Signal Types |");
            println!("|---|---|---|---|");
            for g in &groups {
                let signals: Vec<&str> = g.signal_types.iter().map(|s| s.as_str()).collect();
                println!("| {} | {:.0}% | {} | {} |",
                    format_root_cause(&g.root_cause), g.confidence * 100.0, g.members.len(),
                    signals.join(", "));
            }
            println!();
        }

        // Diagnoses table
        println!("### Diagnoses\n");
        println!("| Confidence | Target | Root Cause |");
        println!("|---|---|---|");
        for d in diagnoses.iter().take(50) {
            let target = format_target(&d.target_node);
            let rc = d.root_cause.as_deref().unwrap_or("?");
            println!("| {:.0}% | {} | {} |", d.confidence * 100.0, target, format_root_cause(rc));
        }
        if diagnoses.len() > 50 {
            println!("\n*...and {} more diagnoses*\n", diagnoses.len() - 50);
        }

        println!("\n---");
        println!("*Generated by [Causinator 9000](https://github.com/sylvainsf/causinator9000)*");
        println!();
        println!("<sub>Want help analyzing this report? Copy it and paste it to Copilot with this prompt: \
                  \"Summarize the top CI issues from this C9K report. \
                  For each alert group, explain the root cause, whether it's a real regression or flaky, \
                  and recommend a fix. Prioritize by number of failures caused.\"</sub>");

        return Ok(());
    }

    // Normal server mode
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .init();

    tracing::info!("Causinator 9000 Engine starting");

    let checkpoint_path = std::env::args().skip_while(|a| a != "--checkpoint").nth(1);

    // Initialize the solver
    let mut solver = solver::BayesianSolver::new()?;

    // Load heuristics (CPTs)
    load_heuristics(&mut solver)?;

    // Optionally load checkpoint
    if let Some(ref cp_path) = checkpoint_path {
        solver.load_checkpoint(cp_path)?;
        tracing::info!(path = %cp_path, "Restored from checkpoint");
    }

    // Load blueprint graph if available
    let blueprint_path =
        std::env::var("C9K_BLUEPRINT").unwrap_or_else(|_| "data/blueprint.bin".to_string());
    if std::path::Path::new(&blueprint_path).exists() {
        solver.load_blueprint(&blueprint_path)?;
        tracing::info!(path = %blueprint_path, "Loaded blueprint graph");
    }

    let solver_handle = solver.handle();

    // Initialize drasi-lib runtime (PostgreSQL CDC → CQs → solver)
    let drasi_enabled = std::env::var("C9K_DRASI_ENABLED")
        .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
        .unwrap_or(true);

    let _drasi_handles = if drasi_enabled {
        match drasi::init_drasi(drasi::DrasiConfig::default(), solver_handle.clone()).await {
            Ok((drasi_lib, consumer_handle)) => {
                tracing::info!("Drasi runtime initialized");
                Some((drasi_lib, consumer_handle))
            }
            Err(e) => {
                tracing::warn!(error = %e, "Drasi initialization failed — running without CDC. \
                    Set C9K_DRASI_ENABLED=false to suppress this warning.");
                None
            }
        }
    } else {
        tracing::info!("Drasi disabled via C9K_DRASI_ENABLED=false");
        None
    };

    // Start REST API
    let api_addr = std::env::var("C9K_BIND").unwrap_or_else(|_| "0.0.0.0:8080".to_string());
    tracing::info!(addr = %api_addr, "Starting REST API");
    api::serve(solver_handle, &api_addr).await?;

    Ok(())
}
