// Copyright (c) 2026 Sylvain Niles. MIT License.

//! RCIE Load Test Suite — Rust port of scripts/load_test.py
//!
//! Four stress tests that probe the engine's performance boundaries:
//!
//!   1. Fan-out:    1 upstream mutation → diagnose N downstream pods
//!   2. Concurrent: N threads × M queries hammering /diagnosis in parallel
//!   3. Window:     Large active window (configurable mutations + signals)
//!   4. Flood:      Sustained injection + diagnosis simultaneously
//!
//! Usage:
//!   cargo run -p rcie-tests --bin rcie-load-test -- --help
//!   cargo run -p rcie-tests --bin rcie-load-test -- --test all
//!   cargo run -p rcie-tests --bin rcie-load-test -- --test fan --fan-pods 200
//!
//! Prerequisites:
//!   Engine running with topology loaded.

use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use clap::Parser;
use rcie_tests::{InjectMutation, InjectSignal, LatencyStats, TestClient};

// ── CLI ──────────────────────────────────────────────────────────────────

#[derive(Parser)]
#[command(name = "rcie-load-test", about = "RCIE Engine Stress Test Suite")]
struct Cli {
    /// Which test to run
    #[arg(long, default_value = "all")]
    test: TestSelection,

    /// Engine URL
    #[arg(long, env = "RCIE_ENGINE_URL", default_value = "http://localhost:8080")]
    engine_url: String,

    // ── Fan-out parameters ──
    /// Number of downstream pods for fan-out test
    #[arg(long, default_value = "100")]
    fan_pods: usize,

    /// Upstream node for fan-out test
    #[arg(long, default_value = "kv-eastus-01")]
    fan_upstream: String,

    // ── Concurrent parameters ──
    /// Number of concurrent threads
    #[arg(long, default_value = "8")]
    threads: usize,

    /// Queries per thread
    #[arg(long, default_value = "50")]
    queries_per_thread: usize,

    // ── Window parameters ──
    /// Number of mutations for large-window test
    #[arg(long, default_value = "10000")]
    window_mutations: usize,

    /// Number of signals for large-window test
    #[arg(long, default_value = "10000")]
    window_signals: usize,

    /// Number of diagnosis queries for large-window test
    #[arg(long, default_value = "50")]
    window_queries: usize,

    // ── Flood parameters ──
    /// Duration of flood test in seconds
    #[arg(long, default_value = "10")]
    flood_duration_secs: u64,

    // ── Thresholds ──
    /// p95 latency threshold for fan-out (ms)
    #[arg(long, default_value = "50")]
    threshold_fan: f64,

    /// p95 latency threshold for concurrent (ms)
    #[arg(long, default_value = "50")]
    threshold_concurrent: f64,

    /// p95 latency threshold for window (ms)
    #[arg(long, default_value = "100")]
    threshold_window: f64,

    /// p95 latency threshold for flood (ms)
    #[arg(long, default_value = "100")]
    threshold_flood: f64,
}

#[derive(Clone, Debug)]
enum TestSelection {
    All,
    Fan,
    Concurrent,
    Window,
    Flood,
}

impl std::str::FromStr for TestSelection {
    type Err = String;
    fn from_str(s: &str) -> std::result::Result<Self, Self::Err> {
        match s.to_lowercase().as_str() {
            "all" => Ok(Self::All),
            "fan" | "fanout" | "fan-out" => Ok(Self::Fan),
            "concurrent" | "conc" => Ok(Self::Concurrent),
            "window" | "large-window" => Ok(Self::Window),
            "flood" | "sustained" => Ok(Self::Flood),
            _ => Err(format!("unknown test: {s} (try: all, fan, concurrent, window, flood)")),
        }
    }
}

// ── Helpers ──────────────────────────────────────────────────────────────

const REGIONS: &[&str] = &[
    "eastus",
    "westeurope",
    "japaneast",
    "westus2",
    "centralus",
    "northeurope",
    "southeastasia",
    "australiaeast",
    "eastus2",
    "canadacentral",
];

fn pod_node(region: &str, app: usize, pod: usize) -> String {
    format!("pod-{region}-app{app:03}-{pod:02}")
}

fn banner(title: &str) {
    let width = title.len().max(60) + 6;
    println!("\n{}", "━".repeat(width));
    println!("   {title}");
    println!("{}\n", "━".repeat(width));
}

// ── Test 1: Fan-out ─────────────────────────────────────────────────────

async fn test_fanout(cli: &Cli) -> Result<f64> {
    banner(&format!(
        "Test 1 — Fan-out: 1 Upstream Mutation → {} Downstream Diagnoses",
        cli.fan_pods
    ));

    let client = TestClient::new(&cli.engine_url);
    client.clear().await?;

    // Inject upstream mutation
    println!("  ▸ Inject: 1 upstream mutation on {}", cli.fan_upstream);
    client
        .inject_mutation(InjectMutation {
            node_id: cli.fan_upstream.clone(),
            mutation_type: "SecretRotation".into(),
            timestamp: None,
        })
        .await?;

    // Inject signals on downstream pods
    println!(
        "  ▸ Inject: AccessDenied_403 on {} downstream pods",
        cli.fan_pods
    );
    let apps = cli.fan_pods / 2;
    for app in 0..apps {
        for pod in 0..2 {
            let node = pod_node("eastus", app, pod);
            client
                .inject_signal(InjectSignal {
                    node_id: node,
                    signal_type: "AccessDenied_403".into(),
                    value: Some(1.0),
                    severity: Some("critical".into()),
                })
                .await?;
        }
    }

    // Diagnose all pods
    println!("  ▸ Diagnose all {} pods", cli.fan_pods);
    let mut latencies = Vec::with_capacity(cli.fan_pods);
    let mut root_causes: std::collections::HashMap<String, usize> = std::collections::HashMap::new();

    for app in 0..apps {
        for pod in 0..2 {
            let node = pod_node("eastus", app, pod);
            let (diag, ms) = client.diagnose_timed(&node).await?;
            latencies.push(ms);
            let rc = diag.root_cause.unwrap_or_else(|| "none".into());
            *root_causes.entry(rc).or_default() += 1;
        }
    }

    let stats = LatencyStats::from_measurements(&mut latencies).context("no measurements")?;
    stats.print(&format!("Fan-out ({} diagnoses)", cli.fan_pods));

    println!("\n  ▸ Root cause distribution:");
    let mut rc_sorted: Vec<_> = root_causes.into_iter().collect();
    rc_sorted.sort_by(|a, b| b.1.cmp(&a.1));
    for (rc, count) in rc_sorted.iter().take(5) {
        println!("    {rc}: {count} pods");
    }

    let verdict = if stats.p95_ms < cli.threshold_fan {
        println!(
            "\n    ✓ p95 = {:.1} ms — fan-out handled efficiently",
            stats.p95_ms
        );
        "PASS"
    } else {
        println!(
            "\n    ✗ p95 = {:.1} ms — exceeds threshold ({} ms)",
            stats.p95_ms, cli.threshold_fan
        );
        "FAIL"
    };
    println!("    Verdict: {verdict}");

    Ok(stats.p95_ms)
}

// ── Test 2: Concurrent Clients ──────────────────────────────────────────

async fn test_concurrent(cli: &Cli) -> Result<f64> {
    banner(&format!(
        "Test 2 — Concurrent: {} Threads × {} Queries",
        cli.threads, cli.queries_per_thread
    ));

    let client = TestClient::new(&cli.engine_url);
    client.clear().await?;

    // Seed evidence
    println!("  ▸ Seeding evidence: 20 mutations + 20 signals");
    for i in 0..20 {
        let region = REGIONS[i % 4];
        let app = i % 100;
        let node = pod_node(region, app, 1);
        client
            .inject_mutation(InjectMutation {
                node_id: node.clone(),
                mutation_type: "ImageUpdate".into(),
                timestamp: None,
            })
            .await?;
        client
            .inject_signal(InjectSignal {
                node_id: node,
                signal_type: "CrashLoopBackOff".into(),
                value: Some(1.0),
                severity: Some("critical".into()),
            })
            .await?;
    }

    println!(
        "  ▸ Running {} threads × {} queries",
        cli.threads, cli.queries_per_thread
    );

    let engine_url = cli.engine_url.clone();
    let thread_count = cli.threads;
    let queries_per_thread = cli.queries_per_thread;

    let wall_start = Instant::now();

    let mut handles = Vec::new();
    for tid in 0..thread_count {
        let url = engine_url.clone();
        let handle = tokio::spawn(async move {
            let tc = TestClient::new(&url);
            let mut lats = Vec::with_capacity(queries_per_thread);
            for i in 0..queries_per_thread {
                let region = REGIONS[i % 4];
                let app = (tid * 7 + i) % 100;
                let node = pod_node(region, app, 1);
                match tc.diagnose_timed(&node).await {
                    Ok((_, ms)) => lats.push(ms),
                    Err(_) => {} // skip failed requests
                }
            }
            lats
        });
        handles.push(handle);
    }

    let mut all_lats = Vec::new();
    for handle in handles {
        let lats = handle.await?;
        all_lats.extend(lats);
    }

    let wall_time = wall_start.elapsed().as_secs_f64();
    let total_queries = thread_count * queries_per_thread;
    let qps = total_queries as f64 / wall_time;

    let stats = LatencyStats::from_measurements(&mut all_lats).context("no measurements")?;
    stats.print(&format!("Concurrent ({thread_count} threads)"));

    println!("\n    total queries: {total_queries}");
    println!("    wall time:     {wall_time:.2} s");
    println!("    throughput:    {qps:.0} queries/sec");

    let verdict = if stats.p95_ms < cli.threshold_concurrent {
        println!(
            "\n    ✓ p95 = {:.1} ms — lock contention is negligible",
            stats.p95_ms
        );
        "PASS"
    } else {
        println!(
            "\n    ✗ p95 = {:.1} ms — exceeds threshold ({} ms)",
            stats.p95_ms, cli.threshold_concurrent
        );
        "FAIL"
    };
    println!("    Verdict: {verdict}");

    Ok(stats.p95_ms)
}

// ── Test 3: Large Active Window ─────────────────────────────────────────

async fn test_large_window(cli: &Cli) -> Result<f64> {
    banner(&format!(
        "Test 3 — Large Window: {}k Mutations + {}k Signals",
        cli.window_mutations / 1000,
        cli.window_signals / 1000
    ));

    let client = TestClient::new(&cli.engine_url);
    client.clear().await?;

    // Inject mutations
    println!("  ▸ Injecting {} mutations", cli.window_mutations);
    let t0 = Instant::now();
    for i in 0..cli.window_mutations {
        let region = REGIONS[i % REGIONS.len()];
        let app = i % 100;
        let pod = i % 4;
        let node = pod_node(region, app, pod);
        client
            .inject_mutation(InjectMutation {
                node_id: node,
                mutation_type: "ImageUpdate".into(),
                timestamp: None,
            })
            .await?;
        if (i + 1) % 2000 == 0 {
            println!("    {}/{} mutations...", i + 1, cli.window_mutations);
        }
    }
    let mut_secs = t0.elapsed().as_secs_f64();
    println!(
        "    mutation injection: {mut_secs:.1}s ({:.0}/s)",
        cli.window_mutations as f64 / mut_secs
    );

    // Inject signals
    println!("  ▸ Injecting {} signals", cli.window_signals);
    let t0 = Instant::now();
    for i in 0..cli.window_signals {
        let region = REGIONS[i % REGIONS.len()];
        let app = i % 100;
        let pod = i % 4;
        let node = pod_node(region, app, pod);
        client
            .inject_signal(InjectSignal {
                node_id: node,
                signal_type: "CrashLoopBackOff".into(),
                value: Some(1.0),
                severity: Some("critical".into()),
            })
            .await?;
        if (i + 1) % 2000 == 0 {
            println!("    {}/{} signals...", i + 1, cli.window_signals);
        }
    }
    let sig_secs = t0.elapsed().as_secs_f64();
    println!(
        "    signal injection: {sig_secs:.1}s ({:.0}/s)",
        cli.window_signals as f64 / sig_secs
    );

    // Diagnose
    println!("  ▸ Diagnosing {} nodes", cli.window_queries);
    let mut latencies = Vec::with_capacity(cli.window_queries);
    for i in 0..cli.window_queries {
        let region = REGIONS[i % 3];
        let app = i % 100;
        let node = pod_node(region, app, 1);
        let (_, ms) = client.diagnose_timed(&node).await?;
        latencies.push(ms);
    }

    let total_events = cli.window_mutations + cli.window_signals;
    let stats =
        LatencyStats::from_measurements(&mut latencies).context("no measurements")?;
    stats.print(&format!("Large window ({total_events} active events)"));

    let verdict = if stats.p95_ms < cli.threshold_window {
        println!(
            "\n    ✓ p95 = {:.1} ms — large window handled efficiently",
            stats.p95_ms
        );
        "PASS"
    } else {
        println!(
            "\n    ✗ p95 = {:.1} ms — exceeds threshold ({} ms)",
            stats.p95_ms, cli.threshold_window
        );
        "FAIL"
    };
    println!("    Verdict: {verdict}");

    Ok(stats.p95_ms)
}

// ── Test 4: Sustained Flood ─────────────────────────────────────────────

async fn test_flood(cli: &Cli) -> Result<f64> {
    banner(&format!(
        "Test 4 — Sustained Flood: Inject + Diagnose for {}s",
        cli.flood_duration_secs
    ));

    let client = TestClient::new(&cli.engine_url);
    client.clear().await?;

    // Seed initial evidence
    println!("  ▸ Seeding 50 initial events");
    for i in 0..50 {
        let region = REGIONS[i % 2];
        let node = pod_node(region, i, 1);
        client
            .inject_mutation(InjectMutation {
                node_id: node.clone(),
                mutation_type: "ImageUpdate".into(),
                timestamp: None,
            })
            .await?;
        client
            .inject_signal(InjectSignal {
                node_id: node,
                signal_type: "CrashLoopBackOff".into(),
                value: Some(1.0),
                severity: Some("critical".into()),
            })
            .await?;
    }

    let stop = Arc::new(AtomicBool::new(false));
    let inject_count = Arc::new(AtomicUsize::new(0));
    let diag_count = Arc::new(AtomicUsize::new(0));

    let duration = Duration::from_secs(cli.flood_duration_secs);
    let engine_url = cli.engine_url.clone();

    println!(
        "  ▸ Running inject + diagnose for {}s",
        cli.flood_duration_secs
    );
    let wall_start = Instant::now();

    // Injector task
    let stop_inj = stop.clone();
    let inject_count_inj = inject_count.clone();
    let url_inj = engine_url.clone();
    let injector = tokio::spawn(async move {
        let tc = TestClient::new(&url_inj);
        let mut i = 0usize;
        while !stop_inj.load(Ordering::Relaxed) {
            let region = REGIONS[i % 4];
            let app = i % 100;
            let pod = i % 4;
            let node = pod_node(region, app, pod);
            let _ = tc
                .inject_mutation(InjectMutation {
                    node_id: node.clone(),
                    mutation_type: "ConfigChange".into(),
                    timestamp: None,
                })
                .await;
            let _ = tc
                .inject_signal(InjectSignal {
                    node_id: node,
                    signal_type: "error_rate".into(),
                    value: Some(0.8),
                    severity: Some("critical".into()),
                })
                .await;
            inject_count_inj.fetch_add(2, Ordering::Relaxed);
            i += 1;
        }
    });

    // Diagnoser task
    let stop_diag = stop.clone();
    let diag_count_diag = diag_count.clone();
    let url_diag = engine_url.clone();
    let diagnoser = tokio::spawn(async move {
        let tc = TestClient::new(&url_diag);
        let mut lats = Vec::new();
        let mut i = 0usize;
        while !stop_diag.load(Ordering::Relaxed) {
            let region = REGIONS[i % 3];
            let app = i % 100;
            let node = pod_node(region, app, 1);
            if let Ok((_, ms)) = tc.diagnose_timed(&node).await {
                lats.push(ms);
                diag_count_diag.fetch_add(1, Ordering::Relaxed);
            }
            i += 1;
        }
        lats
    });

    // Wait for duration
    tokio::time::sleep(duration).await;
    stop.store(true, Ordering::Relaxed);

    // Wait for tasks to finish
    let _ = injector.await;
    let mut diag_lats = diagnoser.await?;

    let elapsed = wall_start.elapsed().as_secs_f64();
    let total_injected = inject_count.load(Ordering::Relaxed);
    let total_diagnosed = diag_count.load(Ordering::Relaxed);

    println!("\n    duration:          {elapsed:.1}s");
    println!(
        "    events injected:   {total_injected} ({:.0}/s)",
        total_injected as f64 / elapsed
    );
    println!(
        "    diagnoses:         {total_diagnosed} ({:.0}/s)",
        total_diagnosed as f64 / elapsed
    );

    let stats = LatencyStats::from_measurements(&mut diag_lats).context("no measurements")?;
    stats.print("Sustained flood (diagnosis under injection)");

    let verdict = if stats.p95_ms < cli.threshold_flood {
        println!(
            "\n    ✓ p95 = {:.1} ms — concurrent inject + diagnose works well",
            stats.p95_ms
        );
        "PASS"
    } else {
        println!(
            "\n    ✗ p95 = {:.1} ms — exceeds threshold ({} ms)",
            stats.p95_ms, cli.threshold_flood
        );
        "FAIL"
    };
    println!("    Verdict: {verdict}");

    Ok(stats.p95_ms)
}

// ── Summary ─────────────────────────────────────────────────────────────

fn print_summary(results: &[(&str, f64, f64)]) {
    banner("Load Test Summary");
    let mut all_pass = true;
    for (name, p95, threshold) in results {
        let pass = p95 < threshold;
        let status = if pass { "PASS" } else { "FAIL" };
        if !pass {
            all_pass = false;
        }
        println!("    {name}: p95 = {p95:.1} ms (target < {threshold} ms) [{status}]");
    }
    println!();
    if all_pass {
        println!("    ✓ All stress tests passed");
    } else {
        println!("    ⚠ Some tests exceeded targets — see details above");
    }
}

// ── Main ────────────────────────────────────────────────────────────────

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();

    // Preflight check
    let client = TestClient::new(&cli.engine_url);
    let health = client
        .health()
        .await
        .context("Engine not responding — is it running?")?;
    println!(
        "✓ Engine: {} nodes, {} edges",
        health.nodes, health.edges
    );

    let mut results: Vec<(&str, f64, f64)> = Vec::new();

    match cli.test {
        TestSelection::All => {
            let p95 = test_fanout(&cli).await?;
            results.push(("Fan-out", p95, cli.threshold_fan));
            let p95 = test_concurrent(&cli).await?;
            results.push(("Concurrent", p95, cli.threshold_concurrent));
            let p95 = test_large_window(&cli).await?;
            results.push(("Large window", p95, cli.threshold_window));
            let p95 = test_flood(&cli).await?;
            results.push(("Sustained flood", p95, cli.threshold_flood));
        }
        TestSelection::Fan => {
            let p95 = test_fanout(&cli).await?;
            results.push(("Fan-out", p95, cli.threshold_fan));
        }
        TestSelection::Concurrent => {
            let p95 = test_concurrent(&cli).await?;
            results.push(("Concurrent", p95, cli.threshold_concurrent));
        }
        TestSelection::Window => {
            let p95 = test_large_window(&cli).await?;
            results.push(("Large window", p95, cli.threshold_window));
        }
        TestSelection::Flood => {
            let p95 = test_flood(&cli).await?;
            results.push(("Sustained flood", p95, cli.threshold_flood));
        }
    }

    print_summary(&results);
    Ok(())
}
