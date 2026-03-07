// Copyright (c) 2026 Sylvain Niles. MIT License.

use std::path::Path;

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use serde::{Deserialize, Serialize};

#[derive(Parser)]
#[command(name = "c9k", about = "Causinator 9000 CLI — Reactive Causal Inference Engine")]
struct Cli {
    /// Engine base URL
    #[arg(long, default_value = "http://localhost:8080", env = "C9K_URL")]
    url: String,

    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Diagnose a node
    Check {
        /// Node ID to diagnose
        node_id: String,
    },
    /// Inject a signal
    Inject {
        #[arg(long)]
        node: String,
        #[arg(long, default_value = "heartbeat")]
        signal: String,
        #[arg(long, default_value = "critical")]
        severity: String,
        #[arg(long, default_value = "1.0")]
        value: f64,
    },
    /// Inject a mutation
    Mutate {
        #[arg(long)]
        node: String,
        #[arg(long)]
        mutation: String,
    },
    /// Show engine health
    Status,
    /// Export graph as Cytoscape JSON
    Graph {
        #[arg(long, default_value = "default")]
        island: String,
    },

    /// CPT management commands
    Cpt {
        #[command(subcommand)]
        action: CptAction,
    },
}

#[derive(Subcommand)]
enum CptAction {
    /// List all CPT classes and their mutation→signal pairs
    List,
    /// Show the full CPT for a specific class
    Show {
        /// Resource class name (e.g., Container, Gateway, KeyVault)
        class: String,
    },
    /// Export current CPTs to YAML or JSON
    Export {
        /// Output file path (extension determines format: .yaml or .json)
        #[arg(short, long, default_value = "cpts-export.yaml")]
        output: String,
    },
    /// Import CPTs from a YAML or JSON file (replaces current config)
    Import {
        /// Input file path (.yaml or .json)
        file: String,
        /// Skip creating a version backup
        #[arg(long)]
        no_version: bool,
    },
    /// Add or update a single CPT entry
    Set {
        /// Resource class (e.g., Container)
        #[arg(long)]
        class: String,
        /// Mutation type (e.g., ImageUpdate)
        #[arg(long)]
        mutation: String,
        /// Signal type (e.g., CrashLoopBackOff)
        #[arg(long)]
        signal: String,
        /// P(signal | mutation present), e.g., 0.75
        #[arg(long)]
        p_present: f64,
        /// P(signal | mutation absent), e.g., 0.03
        #[arg(long)]
        p_absent: f64,
    },
    /// Remove a CPT entry
    Remove {
        #[arg(long)]
        class: String,
        #[arg(long)]
        mutation: String,
        #[arg(long)]
        signal: String,
    },
    /// List all saved versions
    Versions,
    /// Rollback to a previous version
    Rollback {
        /// Version number to roll back to
        version: u32,
    },
    /// Reload CPTs in the running engine (hot-reload via API)
    Reload,
    /// Validate a CPT file without applying it
    Validate {
        /// File to validate (.yaml or .json)
        file: String,
    },
}

// ── CPT Types (mirror the engine's types) ────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
struct CptEntry {
    mutation: String,
    signal: String,
    table: Vec<Vec<f64>>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct PriorConfig {
    #[serde(rename = "P_failure")]
    p_failure: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct ClassHeuristic {
    class: String,
    default_prior: PriorConfig,
    cpts: Vec<CptEntry>,
}

// ── CPT File Management ──────────────────────────────────────────────────

const CPT_FILE: &str = "config/heuristics.manifest.yaml";
const CPT_VERSIONS_DIR: &str = "config/versions";

fn load_cpts(path: &str) -> Result<Vec<ClassHeuristic>> {
    let content = std::fs::read_to_string(path)
        .context(format!("reading CPT file: {path}"))?;
    if path.ends_with(".json") {
        serde_json::from_str(&content).context("parsing JSON CPTs")
    } else {
        serde_yaml_ng::from_str(&content).context("parsing YAML CPTs")
    }
}

fn save_cpts(classes: &[ClassHeuristic], path: &str) -> Result<()> {
    let content = if path.ends_with(".json") {
        serde_json::to_string_pretty(classes).context("serializing JSON")?
    } else {
        serde_yaml_ng::to_string(classes).context("serializing YAML")?
    };
    std::fs::write(path, &content).context(format!("writing {path}"))?;
    Ok(())
}

fn next_version() -> Result<u32> {
    std::fs::create_dir_all(CPT_VERSIONS_DIR).ok();
    let mut max = 0u32;
    if let Ok(entries) = std::fs::read_dir(CPT_VERSIONS_DIR) {
        for entry in entries.flatten() {
            let name = entry.file_name().to_string_lossy().to_string();
            if let Some(num_str) = name.strip_prefix("v").and_then(|s| s.strip_suffix(".yaml"))
                && let Ok(n) = num_str.parse::<u32>()
            {
                max = max.max(n);
            }
        }
    }
    Ok(max + 1)
}



fn save_version(classes: &[ClassHeuristic]) -> Result<u32> {
    let ver = next_version()?;
    let path = format!("{CPT_VERSIONS_DIR}/v{ver}.yaml");
    save_cpts(classes, &path)?;
    println!("  Version {ver} saved → {path}");
    Ok(ver)
}

fn validate_cpts(classes: &[ClassHeuristic]) -> Vec<String> {
    let mut warnings = Vec::new();
    for class in classes {
        if class.cpts.is_empty() {
            warnings.push(format!("{}: no CPT entries defined", class.class));
        }
        for cpt in &class.cpts {
            if cpt.table.len() != 2 || cpt.table[0].len() != 2 || cpt.table[1].len() != 2 {
                warnings.push(format!(
                    "{}.{} → {}: table must be 2×2, got {}×{}",
                    class.class, cpt.mutation, cpt.signal,
                    cpt.table.len(),
                    cpt.table.first().map(|r| r.len()).unwrap_or(0)
                ));
                continue;
            }
            let row0_sum = cpt.table[0][0] + cpt.table[1][0];
            let row1_sum = cpt.table[0][1] + cpt.table[1][1];
            if (row0_sum - 1.0).abs() > 0.01 {
                warnings.push(format!(
                    "{}.{} → {}: column 0 sums to {row0_sum:.3} (should be 1.0)",
                    class.class, cpt.mutation, cpt.signal
                ));
            }
            if (row1_sum - 1.0).abs() > 0.01 {
                warnings.push(format!(
                    "{}.{} → {}: column 1 sums to {row1_sum:.3} (should be 1.0)",
                    class.class, cpt.mutation, cpt.signal
                ));
            }
            let lr = if cpt.table[0][1] > 0.0 {
                cpt.table[0][0] / cpt.table[0][1]
            } else {
                f64::INFINITY
            };
            if lr < 1.0 {
                warnings.push(format!(
                    "{}.{} → {}: LR = {lr:.2} (< 1.0 means mutation PREVENTS signal — is this intended?)",
                    class.class, cpt.mutation, cpt.signal
                ));
            }
        }
    }
    warnings
}

// ── API Response Types ───────────────────────────────────────────────────

#[derive(Deserialize)]
struct DiagnosisResponse {
    target_node: String,
    confidence: f64,
    root_cause: Option<String>,
    causal_path: Vec<String>,
    competing_causes: Vec<(String, f64)>,
    #[allow(dead_code)]
    timestamp: String,
}

#[derive(Deserialize)]
struct HealthResponse {
    status: String,
    version: String,
    nodes: Option<usize>,
    edges: Option<usize>,
    active_mutations: Option<usize>,
    active_signals: Option<usize>,
}

#[derive(Deserialize)]
struct InjectResponse {
    status: String,
    id: String,
}

// ── Main ─────────────────────────────────────────────────────────────────

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("warn")),
        )
        .init();

    let cli = Cli::parse();
    let client = reqwest::Client::new();

    match cli.command {
        Commands::Check { node_id } => {
            let url = format!("{}/api/diagnosis?target={}", cli.url, node_id);
            let d: DiagnosisResponse = client.get(&url).send().await?.error_for_status()?.json().await?;

            println!("Node:       {}", d.target_node);
            println!("Confidence: {:.1}%", d.confidence * 100.0);
            if let Some(rc) = &d.root_cause {
                println!("Root Cause: {rc}");
            }
            if !d.causal_path.is_empty() {
                println!("Path:       {}", d.causal_path.join(" → "));
            }
            if !d.competing_causes.is_empty() {
                println!("Competing:");
                for (cause, conf) in &d.competing_causes {
                    println!("  - {cause}: {:.1}%", conf * 100.0);
                }
            }
        }

        Commands::Inject { node, signal, severity, value } => {
            let r: InjectResponse = client
                .post(format!("{}/api/signals", cli.url))
                .json(&serde_json::json!({
                    "node_id": node,
                    "signal_type": signal,
                    "value": value,
                    "severity": severity,
                }))
                .send().await?.error_for_status()?.json().await?;
            println!("Signal injected: {} ({})", r.id, r.status);
        }

        Commands::Mutate { node, mutation } => {
            let r: InjectResponse = client
                .post(format!("{}/api/mutations", cli.url))
                .json(&serde_json::json!({
                    "node_id": node,
                    "mutation_type": mutation,
                }))
                .send().await?.error_for_status()?.json().await?;
            println!("Mutation injected: {} ({})", r.id, r.status);
        }

        Commands::Status => {
            let h: HealthResponse = client
                .get(format!("{}/api/health", cli.url))
                .send().await?.error_for_status()?.json().await?;
            println!("Status:    {}", h.status);
            println!("Version:   {}", h.version);
            if let Some(n) = h.nodes { println!("Nodes:     {n}"); }
            if let Some(e) = h.edges { println!("Edges:     {e}"); }
            if let Some(m) = h.active_mutations { println!("Mutations: {m} (active)"); }
            if let Some(s) = h.active_signals { println!("Signals:   {s} (active)"); }
        }

        Commands::Graph { island } => {
            let text = client
                .get(format!("{}/api/graph/{island}", cli.url))
                .send().await?.error_for_status()?.text().await?;
            println!("{text}");
        }

        // ── CPT Commands ─────────────────────────────────────────────

        Commands::Cpt { action } => match action {
            CptAction::List => {
                let classes = load_cpts(CPT_FILE)?;
                println!("{} resource classes:\n", classes.len());
                for class in &classes {
                    let lr_range: String = if class.cpts.is_empty() {
                        "no entries".into()
                    } else {
                        let lrs: Vec<f64> = class.cpts.iter().map(|c| {
                            if c.table[0][1] > 0.0 { c.table[0][0] / c.table[0][1] } else { f64::INFINITY }
                        }).collect();
                        let min = lrs.iter().cloned().fold(f64::INFINITY, f64::min);
                        let max = lrs.iter().cloned().fold(0.0_f64, f64::max);
                        format!("LR {min:.0}×–{max:.0}×")
                    };
                    println!("  {} ({} entries, {lr_range})", class.class, class.cpts.len());
                    for cpt in &class.cpts {
                        let lr = if cpt.table[0][1] > 0.0 {
                            cpt.table[0][0] / cpt.table[0][1]
                        } else { f64::INFINITY };
                        println!("    {} → {}  (LR = {lr:.1}×)", cpt.mutation, cpt.signal);
                    }
                }
            }

            CptAction::Show { class } => {
                let classes = load_cpts(CPT_FILE)?;
                let cls = classes.iter().find(|c| c.class.eq_ignore_ascii_case(&class));
                match cls {
                    Some(c) => {
                        let yaml = serde_yaml_ng::to_string(&[c])?;
                        println!("{yaml}");
                    }
                    None => {
                        eprintln!("Class '{class}' not found. Available:");
                        for c in &classes {
                            eprintln!("  {}", c.class);
                        }
                        std::process::exit(1);
                    }
                }
            }

            CptAction::Export { output } => {
                let classes = load_cpts(CPT_FILE)?;
                save_cpts(&classes, &output)?;
                println!("Exported {} classes to {output}", classes.len());
            }

            CptAction::Import { file, no_version } => {
                let new_classes = load_cpts(&file)?;

                // Validate
                let warnings = validate_cpts(&new_classes);
                if !warnings.is_empty() {
                    println!("Warnings:");
                    for w in &warnings {
                        println!("  ⚠ {w}");
                    }
                }

                // Version the current config before overwriting
                if !no_version && Path::new(CPT_FILE).exists() {
                    let current = load_cpts(CPT_FILE)?;
                    save_version(&current)?;
                }

                save_cpts(&new_classes, CPT_FILE)?;
                println!("Imported {} classes from {file} → {CPT_FILE}", new_classes.len());
                println!("Reload the engine: c9k cpt reload");
            }

            CptAction::Set { class, mutation, signal, p_present, p_absent } => {
                let mut classes = load_cpts(CPT_FILE)?;

                // Version before modifying
                save_version(&classes)?;

                // Find or create the class
                let cls = if let Some(c) = classes.iter_mut().find(|c| c.class == class) {
                    c
                } else {
                    classes.push(ClassHeuristic {
                        class: class.clone(),
                        default_prior: PriorConfig { p_failure: 0.005 },
                        cpts: vec![],
                    });
                    classes.last_mut().unwrap()
                };

                // Find or create the entry
                if let Some(entry) = cls.cpts.iter_mut().find(|e| e.mutation == mutation && e.signal == signal) {
                    entry.table = vec![vec![p_present, p_absent], vec![1.0 - p_present, 1.0 - p_absent]];
                    println!("Updated: {class}.{mutation} → {signal}");
                } else {
                    cls.cpts.push(CptEntry {
                        mutation: mutation.clone(),
                        signal: signal.clone(),
                        table: vec![vec![p_present, p_absent], vec![1.0 - p_present, 1.0 - p_absent]],
                    });
                    println!("Added: {class}.{mutation} → {signal}");
                }

                let lr = if p_absent > 0.0 { p_present / p_absent } else { f64::INFINITY };
                println!("  P(signal|mutation) = {p_present}");
                println!("  P(signal|absent)   = {p_absent}");
                println!("  Likelihood ratio   = {lr:.1}×");

                save_cpts(&classes, CPT_FILE)?;
                println!("Saved to {CPT_FILE}. Reload: c9k cpt reload");
            }

            CptAction::Remove { class, mutation, signal } => {
                let mut classes = load_cpts(CPT_FILE)?;
                save_version(&classes)?;

                if let Some(cls) = classes.iter_mut().find(|c| c.class == class) {
                    let before = cls.cpts.len();
                    cls.cpts.retain(|e| !(e.mutation == mutation && e.signal == signal));
                    if cls.cpts.len() < before {
                        save_cpts(&classes, CPT_FILE)?;
                        println!("Removed: {class}.{mutation} → {signal}");
                    } else {
                        eprintln!("Entry not found: {class}.{mutation} → {signal}");
                        std::process::exit(1);
                    }
                } else {
                    eprintln!("Class not found: {class}");
                    std::process::exit(1);
                }
            }

            CptAction::Versions => {
                std::fs::create_dir_all(CPT_VERSIONS_DIR).ok();
                let mut versions: Vec<(u32, String, u64)> = Vec::new();
                if let Ok(entries) = std::fs::read_dir(CPT_VERSIONS_DIR) {
                    for entry in entries.flatten() {
                        let name = entry.file_name().to_string_lossy().to_string();
                        if let Some(Ok(n)) = name.strip_prefix("v").and_then(|s| s.strip_suffix(".yaml")).map(|s| s.parse::<u32>()) {
                            let size = entry.metadata().map(|m| m.len()).unwrap_or(0);
                            versions.push((n, name, size));
                        }
                    }
                }
                versions.sort_by_key(|v| v.0);

                if versions.is_empty() {
                    println!("No versions saved yet. Versions are created automatically on import/set/remove.");
                } else {
                    println!("{} versions:\n", versions.len());
                    for (ver, name, size) in &versions {
                        let current_marker = if *ver == versions.last().unwrap().0 { " (latest)" } else { "" };
                        println!("  v{ver}  {CPT_VERSIONS_DIR}/{name}  ({size} bytes){current_marker}");
                    }
                }
            }

            CptAction::Rollback { version } => {
                let version_path = format!("{CPT_VERSIONS_DIR}/v{version}.yaml");
                if !Path::new(&version_path).exists() {
                    eprintln!("Version {version} not found at {version_path}");
                    eprintln!("Run 'c9k cpt versions' to see available versions.");
                    std::process::exit(1);
                }

                // Save current as a new version before rolling back
                if Path::new(CPT_FILE).exists() {
                    let current = load_cpts(CPT_FILE)?;
                    save_version(&current)?;
                }

                let rollback_classes = load_cpts(&version_path)?;
                save_cpts(&rollback_classes, CPT_FILE)?;
                println!("Rolled back to version {version} ({} classes)", rollback_classes.len());
                println!("Reload the engine: c9k cpt reload");
            }

            CptAction::Reload => {
                let resp = client
                    .post(format!("{}/api/reload-cpts", cli.url))
                    .send()
                    .await;
                match resp {
                    Ok(r) if r.status().is_success() => {
                        println!("CPTs reloaded in running engine.");
                    }
                    Ok(r) => {
                        eprintln!("Reload failed: HTTP {}", r.status());
                        if let Ok(body) = r.text().await {
                            eprintln!("{body}");
                        }
                        std::process::exit(1);
                    }
                    Err(e) => {
                        eprintln!("Could not reach engine: {e}");
                        eprintln!("Is the engine running? The CPT file was updated on disk.");
                        eprintln!("Restart the engine to pick up changes.");
                    }
                }
            }

            CptAction::Validate { file } => {
                match load_cpts(&file) {
                    Ok(classes) => {
                        let warnings = validate_cpts(&classes);
                        println!("Parsed {} classes, {} total entries",
                            classes.len(),
                            classes.iter().map(|c| c.cpts.len()).sum::<usize>());

                        if warnings.is_empty() {
                            println!("✓ All entries valid");
                        } else {
                            println!("\n{} warnings:", warnings.len());
                            for w in &warnings {
                                println!("  ⚠ {w}");
                            }
                        }

                        // Show summary
                        println!("\nClasses:");
                        for class in &classes {
                            println!("  {} ({} entries)", class.class, class.cpts.len());
                        }
                    }
                    Err(e) => {
                        eprintln!("✗ Parse error: {e}");
                        std::process::exit(1);
                    }
                }
            }
        },
    }

    Ok(())
}
