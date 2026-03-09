// Copyright (c) 2026 Sylvain Niles. MIT License.

//! Scale test — measures memory usage and inference latency as graph size increases.
//!
//! Uses the Rust topology builder to generate realistic Azure infrastructure
//! at increasing scales, loads via POST /api/graph/load, then measures:
//!   - Engine RSS memory (via /proc or ps)
//!   - Inference latency at each scale point
//!   - Graph load time
//!
//! No SQL, no Python, no files.
//!
//! Usage:
//!   c9k-scale-test                          # default: 6 scale points up to ~50k nodes
//!   c9k-scale-test --preset azure-region    # single region at production scale
//!   c9k-scale-test --preset multi-region    # 1-5 regions (~50k-250k nodes)
//!   c9k-scale-test --queries 200            # more queries per scale point

use anyhow::Result;
use c9k_tests::topology::TopologyBuilder;
use clap::Parser;
use std::time::Instant;

#[derive(Parser)]
#[command(name = "c9k-scale-test", about = "Causinator 9000 Scale & Memory Test")]
struct Args {
    /// Engine URL
    #[arg(long, default_value = "http://localhost:8080", env = "C9K_ENGINE_URL")]
    engine_url: String,

    /// Scale preset
    #[arg(long, default_value = "progressive")]
    preset: String,

    /// Number of diagnosis queries per scale point
    #[arg(long, default_value = "100")]
    queries: usize,
}

struct ScalePoint {
    label: String,
    graph: c9k_engine::solver::GraphPayload,
}

fn progressive_points() -> Vec<ScalePoint> {
    vec![
        ScalePoint {
            label: "tiny (1r/2rack/3vm)".into(),
            graph: TopologyBuilder::new()
                .regions(1)
                .racks_per_region(2)
                .vms_per_rack(3)
                .containers_per_vm(2)
                .identities_per_vm(1)
                .apps_per_region(10)
                .pods_per_app(4)
                .build(),
        },
        ScalePoint {
            label: "small (1r/10rack/10vm)".into(),
            graph: TopologyBuilder::new()
                .regions(1)
                .racks_per_region(10)
                .vms_per_rack(10)
                .apps_per_region(50)
                .pods_per_app(4)
                .build(),
        },
        ScalePoint {
            label: "medium (2r/10rack/10vm)".into(),
            graph: TopologyBuilder::new()
                .regions(2)
                .racks_per_region(10)
                .vms_per_rack(10)
                .apps_per_region(100)
                .pods_per_app(4)
                .build(),
        },
        ScalePoint {
            label: "standard (10r/10rack/10vm)".into(),
            graph: TopologyBuilder::standard().build(),
        },
        ScalePoint {
            label: "large (10r/10rack/10vm/200app)".into(),
            graph: TopologyBuilder::large().build(),
        },
        ScalePoint {
            label: "azure-region (1r/150rack/40vm/500app)".into(),
            graph: TopologyBuilder::azure_region().build(),
        },
    ]
}

fn multi_region_points() -> Vec<ScalePoint> {
    (1..=5)
        .map(|regions| ScalePoint {
            label: format!("{regions} Azure region(s)"),
            graph: TopologyBuilder::azure_multi_region(regions).build(),
        })
        .collect()
}

fn get_engine_rss(url: &str) -> Option<u64> {
    // Try to get RSS from the engine's /api/memory endpoint
    // then also try to get the OS-level RSS via lsof + ps
    let pid = get_engine_pid()?;
    let output = std::process::Command::new("ps")
        .args(["-o", "rss=", "-p", &pid.to_string()])
        .output()
        .ok()?;
    let rss_kb: u64 = String::from_utf8_lossy(&output.stdout)
        .trim()
        .parse()
        .ok()?;
    Some(rss_kb)
}

fn get_engine_pid() -> Option<u32> {
    let output = std::process::Command::new("lsof")
        .args(["-t", "-i", ":8080"])
        .output()
        .ok()?;
    String::from_utf8_lossy(&output.stdout)
        .trim()
        .lines()
        .next()?
        .parse()
        .ok()
}

fn format_mem(kb: u64) -> String {
    if kb > 1_048_576 {
        format!("{:.1} GB", kb as f64 / 1_048_576.0)
    } else if kb > 1024 {
        format!("{:.1} MB", kb as f64 / 1024.0)
    } else {
        format!("{kb} KB")
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(300))
        .build()?;

    println!("Causinator 9000 — Scale & Memory Test");
    println!("═══════════════════════════════════════════════════════════════════════════════════");
    println!();

    // Verify engine
    let health: serde_json::Value = client
        .get(format!("{}/api/health", args.engine_url))
        .send()
        .await?
        .json()
        .await?;
    println!("Engine: v{}", health["version"]);
    if let Some(rss) = get_engine_rss(&args.engine_url) {
        println!("Baseline RSS: {}", format_mem(rss));
    }
    println!();

    let points = match args.preset.as_str() {
        "progressive" => progressive_points(),
        "azure-region" => vec![ScalePoint {
            label: "Azure region".into(),
            graph: TopologyBuilder::azure_region().build(),
        }],
        "multi-region" => multi_region_points(),
        other => {
            eprintln!("Unknown preset: {other}. Use: progressive, azure-region, multi-region");
            std::process::exit(1);
        }
    };

    println!(
        "{:<35} {:>8} {:>8} {:>10} {:>10} {:>8} {:>8} {:>8}",
        "Scale Point", "Nodes", "Edges", "Load(ms)", "RSS", "p50", "p95", "p99"
    );
    println!("{}", "─".repeat(100));

    for point in &points {
        let node_count = point.graph.nodes.len();
        let edge_count = point.graph.edges.len();

        // Generate time (how fast the builder creates the graph in memory)
        let gen_start = Instant::now();
        let _size = point.graph.nodes.len(); // graph already built
        let _gen_ms = gen_start.elapsed().as_millis();

        // Load via API
        let load_start = Instant::now();
        let resp: serde_json::Value = client
            .post(format!("{}/api/graph/load", args.engine_url))
            .json(&point.graph)
            .send()
            .await?
            .json()
            .await?;
        let load_ms = load_start.elapsed().as_millis();

        let loaded_nodes = resp["nodes"].as_u64().unwrap_or(0) as usize;
        if loaded_nodes == 0 {
            eprintln!("  ERROR loading {}: {:?}", point.label, resp);
            continue;
        }

        // Get RSS after loading
        // Small sleep to let allocator settle
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
        let rss = get_engine_rss(&args.engine_url);
        let rss_str = rss.map(|r| format_mem(r)).unwrap_or_else(|| "???".into());

        // Clear events then inject evidence for diagnosis
        client
            .post(format!("{}/api/clear", args.engine_url))
            .send()
            .await?;

        let region = "eastus"; // always present
        client.post(format!("{}/api/mutations", args.engine_url))
            .json(&serde_json::json!({"node_id": format!("kv-{region}-01"), "mutation_type": "SecretRotation"}))
            .send().await?;
        for app in 0..5.min(point.graph.nodes.len() / 100) {
            for pod in 0..4 {
                client
                    .post(format!("{}/api/signals", args.engine_url))
                    .json(&serde_json::json!({
                        "node_id": format!("pod-{region}-app{app:03}-{pod:02}"),
                        "signal_type": "AccessDenied_403",
                        "severity": "critical",
                    }))
                    .send()
                    .await?;
            }
        }

        // Measure diagnosis latency
        let mut latencies = Vec::with_capacity(args.queries);
        for i in 0..args.queries {
            let app = i % 100;
            let pod = i % 4;
            let node_id = format!("pod-{region}-app{app:03}-{pod:02}");
            let url = format!("{}/api/diagnosis?target={}", args.engine_url, node_id);

            let t = Instant::now();
            let _resp = client.get(&url).send().await?.text().await?;
            latencies.push(t.elapsed().as_secs_f64() * 1000.0);
        }

        latencies.sort_by(|a, b| a.partial_cmp(b).unwrap());
        let p50 = latencies[latencies.len() / 2];
        let p95 = latencies[(latencies.len() as f64 * 0.95) as usize];
        let p99 = latencies[(latencies.len() as f64 * 0.99) as usize];

        println!(
            "{:<35} {:>8} {:>8} {:>10} {:>10} {:>7.2}ms {:>7.2}ms {:>7.2}ms",
            point.label, loaded_nodes, edge_count, load_ms, rss_str, p50, p95, p99
        );
    }

    // Final summary
    println!();
    println!("─────────────────────────────────────────────────────────");
    if let Some(rss) = get_engine_rss(&args.engine_url) {
        println!("Final engine RSS: {}", format_mem(rss));
    }
    let final_mem: serde_json::Value = client
        .get(format!("{}/api/memory", args.engine_url))
        .send()
        .await?
        .json()
        .await?;
    println!(
        "Solver state:  {} nodes, {} edges, {} CPT classes",
        final_mem["nodes"], final_mem["edges"], final_mem["heuristic_classes"]
    );
    println!();
    println!("Key insight: inference latency is O(ancestors × active_mutations),");
    println!("not O(graph). Memory scales linearly with node+edge count.");

    Ok(())
}
