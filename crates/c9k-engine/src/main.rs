// Copyright (c) 2026 Sylvain Niles. MIT License.

use anyhow::Result;
use c9k_engine::{api, drasi, embedded_heuristics, mcp, solver};
use tracing_subscriber::EnvFilter;

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
