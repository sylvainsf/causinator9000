// Copyright (c) 2026 Sylvain Niles. MIT License.

//! Bayesian Solver — Variable Elimination on petgraph
//!
//! Core inference engine for Causinator 9000. Maintains a causal DAG (petgraph),
//! applies CPTs from the heuristic registry, and runs Variable Elimination
//! to compute posterior probabilities for root-cause identification.

pub mod ve;

use std::collections::HashMap;
use std::path::Path;
use std::sync::{Arc, Mutex};

use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use petgraph::Direction;
use petgraph::graph::{DiGraph, NodeIndex};
use petgraph::visit::EdgeRef;
use serde::{Deserialize, Serialize};

// ── Types ────────────────────────────────────────────────────────────────

/// A node in the causal DAG.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CausalNode {
    pub id: String,
    pub label: String,
    pub class: String,
    pub region: Option<String>,
    pub rack_id: Option<String>,
    pub properties: serde_json::Value,
}

/// A directed edge in the causal DAG.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CausalEdge {
    pub id: String,
    pub edge_type: EdgeType,
    pub properties: serde_json::Value,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum EdgeType {
    Containment,
    Dependency,
    Connection,
}

/// A mutation event (deployment, config change, etc.)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Mutation {
    pub id: String,
    pub node_id: String,
    pub mutation_type: String,
    pub source: String,
    pub timestamp: DateTime<Utc>,
    pub properties: serde_json::Value,
}

/// A degradation signal (error spike, heartbeat loss, etc.)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Signal {
    pub id: String,
    pub node_id: String,
    pub signal_type: String,
    pub value: Option<f64>,
    pub severity: Option<String>,
    pub timestamp: DateTime<Utc>,
    pub properties: serde_json::Value,
}

/// A Conditional Probability Table entry.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CptEntry {
    pub mutation: String,
    pub signal: String,
    /// 2×2 table: [[P(signal_high|mutation), P(signal_high|no_mutation)],
    ///              [P(signal_low|mutation),  P(signal_low|no_mutation)]]
    pub table: Vec<Vec<f64>>,
}

/// Heuristic definition for a resource class.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ClassHeuristic {
    pub class: String,
    pub default_prior: PriorConfig,
    pub cpts: Vec<CptEntry>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PriorConfig {
    #[serde(rename = "P_failure")]
    pub p_failure: f64,
}

// ── Modular heuristics manifest ──────────────────────────────────────────

/// Default background failure probability used when a new class is defined
/// in an override layer without specifying `default_prior`.
const DEFAULT_PRIOR_P_FAILURE: f64 = 0.005;

/// Manifest that lists heuristic layer files to load in order.
/// Later layers override earlier ones with most-specific granularity.
#[derive(Debug, Clone, Deserialize)]
struct HeuristicsManifest {
    layers: Vec<ManifestLayer>,
}

/// A single layer entry in a heuristics manifest.
#[derive(Debug, Clone, Deserialize)]
struct ManifestLayer {
    path: String,
    #[serde(default)]
    optional: bool,
}

/// A class entry in a heuristic layer file.
/// `default_prior` is optional to support lean patching — omit it to
/// inherit from an earlier layer.
#[derive(Debug, Clone, Deserialize)]
struct LayerClassEntry {
    class: String,
    #[serde(default)]
    default_prior: Option<PriorConfig>,
    #[serde(default)]
    cpts: Vec<CptEntry>,
}

/// Merge a set of layer class entries into the heuristics map.
///
/// For each entry:
/// - If the class already exists, merge into it:
///   - `default_prior` is replaced only if the layer specifies it.
///   - Individual CPT entries are keyed by `(mutation, signal)`.
///     Matching entries are replaced; new entries are appended.
/// - If the class is new, insert it (using a default prior of 0.005
///   if none is specified).
fn merge_layer(heuristics: &mut HashMap<String, ClassHeuristic>, entries: Vec<LayerClassEntry>) {
    for entry in entries {
        match heuristics.get_mut(&entry.class) {
            Some(existing) => {
                if let Some(prior) = entry.default_prior {
                    existing.default_prior = prior;
                }
                for cpt in entry.cpts {
                    if let Some(pos) = existing
                        .cpts
                        .iter()
                        .position(|c| c.mutation == cpt.mutation && c.signal == cpt.signal)
                    {
                        existing.cpts[pos] = cpt;
                    } else {
                        existing.cpts.push(cpt);
                    }
                }
            }
            None => {
                heuristics.insert(
                    entry.class.clone(),
                    ClassHeuristic {
                        class: entry.class,
                        default_prior: entry.default_prior.unwrap_or(PriorConfig {
                            p_failure: DEFAULT_PRIOR_P_FAILURE,
                        }),
                        cpts: entry.cpts,
                    },
                );
            }
        }
    }
}

/// Load heuristics from a file path.  The format is auto-detected:
///
/// - **Manifest** (YAML mapping with a `layers` key): each layer file is
///   loaded in order, with later layers overriding earlier ones.
///   Layer paths are resolved relative to the manifest file's directory.
///
/// - **Flat list** (YAML sequence): parsed directly as a single layer.
///
/// Both formats support lean patching via [`LayerClassEntry`].
fn load_heuristics_from_path(
    heuristics: &mut HashMap<String, ClassHeuristic>,
    path: &str,
) -> Result<()> {
    let contents =
        std::fs::read_to_string(path).context(format!("reading heuristics file: {path}"))?;

    // Try manifest format first (YAML mapping with "layers" key)
    if let Ok(manifest) = serde_yaml_ng::from_str::<HeuristicsManifest>(&contents) {
        let base_dir = Path::new(path).parent().unwrap_or_else(|| Path::new("."));
        for layer in &manifest.layers {
            let layer_path = base_dir.join(&layer.path);
            let layer_path_str = layer_path.display().to_string();
            match std::fs::read_to_string(&layer_path) {
                Ok(layer_contents) => {
                    let entries: Vec<LayerClassEntry> = serde_yaml_ng::from_str(&layer_contents)
                        .context(format!("parsing layer file: {layer_path_str}"))?;
                    tracing::debug!(
                        path = %layer_path_str,
                        classes = entries.len(),
                        "Loaded heuristic layer"
                    );
                    merge_layer(heuristics, entries);
                }
                Err(_) if layer.optional => {
                    tracing::debug!(
                        path = %layer_path_str,
                        "Optional heuristic layer not found, skipping"
                    );
                }
                Err(e) => {
                    return Err(e)
                        .context(format!("reading heuristic layer file: {layer_path_str}"));
                }
            }
        }
    } else {
        // Flat format: YAML list of class entries
        let entries: Vec<LayerClassEntry> =
            serde_yaml_ng::from_str(&contents).context("parsing heuristics YAML")?;
        merge_layer(heuristics, entries);
    }

    Ok(())
}

/// Diagnosis result from the solver.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Diagnosis {
    pub target_node: String,
    pub confidence: f64,
    pub root_cause: Option<String>,
    pub causal_path: Vec<String>,
    pub competing_causes: Vec<(String, f64)>,
    pub timestamp: DateTime<Utc>,
}

/// A group of alerts sharing the same root cause.
/// Collapses N individual alerts into a single incident.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AlertGroup {
    /// The shared root cause (e.g., "kv-eastus-01 (SecretRotation)")
    pub root_cause: String,
    /// Highest confidence score among all members
    pub confidence: f64,
    /// Number of affected nodes
    pub count: usize,
    /// All signal types observed across the group
    pub signal_types: Vec<String>,
    /// Causal path from root cause to affected nodes (from first member)
    pub causal_path: Vec<String>,
    /// Most recent signal timestamp in the group
    pub latest_signal: String,
    /// List of affected node IDs
    pub affected_nodes: Vec<String>,
    /// Full diagnosis details for each affected node
    pub members: Vec<AlertMember>,
}

/// Individual alert within a group.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AlertMember {
    pub node_id: String,
    pub confidence: f64,
    pub signal_types: Vec<String>,
    pub signal_count: usize,
    pub latest_signal: String,
}

// ── Blueprint (binary interchange format from transpiler) ────────────────

/// Serializable representation of the full graph for blueprint.bin.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Blueprint {
    pub nodes: Vec<CausalNode>,
    pub edges: Vec<BlueprintEdge>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BlueprintEdge {
    pub id: String,
    pub source_id: String,
    pub target_id: String,
    pub edge_type: String,
    pub properties: serde_json::Value,
}

/// Serializable snapshot of solver state for checkpoint persistence.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SolverSnapshot {
    pub nodes: Vec<CausalNode>,
    pub edges: Vec<BlueprintEdge>,
    pub active_mutations: Vec<Mutation>,
    pub active_signals: Vec<Signal>,
}

/// Structured graph payload for loading/exporting via API.
/// This is the primary format for programmatic graph management —
/// no SQL, no files, just JSON in and out.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GraphPayload {
    pub nodes: Vec<CausalNode>,
    pub edges: Vec<BlueprintEdge>,
}

/// Memory usage info for diagnostics.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryInfo {
    pub nodes: usize,
    pub edges: usize,
    pub active_mutations: usize,
    pub active_signals: usize,
    pub node_index_entries: usize,
    pub heuristic_classes: usize,
}

// ── Solver ───────────────────────────────────────────────────────────────

/// Thread-safe handle to the solver for use from API/reaction handlers.
#[derive(Clone)]
pub struct SolverHandle {
    inner: Arc<Mutex<SolverState>>,
}

impl SolverHandle {
    /// Run inference for a specific node and return the diagnosis.
    pub fn diagnose(&self, node_id: &str) -> Result<Diagnosis> {
        let state = self
            .inner
            .lock()
            .map_err(|e| anyhow::anyhow!("lock: {e}"))?;
        state.diagnose(node_id)
    }

    /// Return all active diagnoses above the confidence threshold.
    pub fn diagnose_all(&self, min_confidence: f64) -> Result<Vec<Diagnosis>> {
        let state = self
            .inner
            .lock()
            .map_err(|e| anyhow::anyhow!("lock: {e}"))?;
        state.diagnose_all(min_confidence)
    }

    /// Ingest a CQ result from drasi (called by ApplicationReaction).
    pub fn ingest_signal(&self, signal: Signal) -> Result<()> {
        let mut state = self
            .inner
            .lock()
            .map_err(|e| anyhow::anyhow!("lock: {e}"))?;
        state.ingest_signal(signal)
    }

    /// Ingest a mutation event.
    pub fn ingest_mutation(&self, mutation: Mutation) -> Result<()> {
        let mut state = self
            .inner
            .lock()
            .map_err(|e| anyhow::anyhow!("lock: {e}"))?;
        state.ingest_mutation(mutation)
    }

    /// Export the graph as a DOT/Graphviz string.
    pub fn export_dot(&self) -> Result<String> {
        let state = self
            .inner
            .lock()
            .map_err(|e| anyhow::anyhow!("lock: {e}"))?;
        Ok(state.export_dot())
    }

    /// Write a checkpoint to disk.
    pub fn write_checkpoint(&self, path: &str) -> Result<()> {
        let state = self
            .inner
            .lock()
            .map_err(|e| anyhow::anyhow!("lock: {e}"))?;
        state.write_checkpoint(path)
    }

    /// Get counts for health endpoint.
    pub fn stats(&self) -> Result<(usize, usize, usize, usize)> {
        let state = self
            .inner
            .lock()
            .map_err(|e| anyhow::anyhow!("lock: {e}"))?;
        Ok((
            state.graph.node_count(),
            state.graph.edge_count(),
            state.active_mutations.len(),
            state.active_signals.len(),
        ))
    }

    /// Clear all active mutations and signals (for demo/testing).
    pub fn clear_events(&self) -> Result<()> {
        let mut state = self
            .inner
            .lock()
            .map_err(|e| anyhow::anyhow!("lock: {e}"))?;
        state.active_mutations.clear();
        state.active_signals.clear();
        state.cached_diagnoses.clear();
        Ok(())
    }

    /// Add a node to the causal DAG.
    pub fn add_node(&self, node: CausalNode) -> Result<()> {
        let mut state = self
            .inner
            .lock()
            .map_err(|e| anyhow::anyhow!("lock: {e}"))?;
        state.add_node(node);
        Ok(())
    }

    /// Add an edge between two nodes in the causal DAG.
    pub fn add_edge(&self, edge: CausalEdge, source_id: &str, target_id: &str) -> Result<()> {
        let mut state = self
            .inner
            .lock()
            .map_err(|e| anyhow::anyhow!("lock: {e}"))?;
        state.add_edge(edge, source_id, target_id)
    }

    /// Load a complete graph from structured data, replacing the current graph.
    /// Accepts a `GraphPayload` with nodes and edges arrays.
    pub fn load_graph(&self, payload: GraphPayload) -> Result<(usize, usize)> {
        let mut state = self.inner.lock().map_err(|e| anyhow::anyhow!("lock: {e}"))?;
        state.graph.clear();
        state.node_index.clear();

        for node in &payload.nodes {
            state.add_node(node.clone());
        }

        let mut edge_count = 0;
        for edge in &payload.edges {
            let edge_type = match edge.edge_type.as_str() {
                "containment" => EdgeType::Containment,
                "dependency" => EdgeType::Dependency,
                "connection" => EdgeType::Connection,
                _ => EdgeType::Dependency,
            };
            state.add_edge(
                CausalEdge {
                    id: edge.id.clone(),
                    edge_type,
                    properties: edge.properties.clone(),
                },
                &edge.source_id,
                &edge.target_id,
            )?;
            edge_count += 1;
        }

        tracing::info!(
            nodes = state.graph.node_count(),
            edges = edge_count,
            "Graph loaded from payload"
        );
        Ok((state.graph.node_count(), edge_count))
    }

    /// Export the current graph as a structured `GraphPayload`.
    pub fn export_graph(&self) -> Result<GraphPayload> {
        let state = self.inner.lock().map_err(|e| anyhow::anyhow!("lock: {e}"))?;

        let nodes: Vec<CausalNode> = state
            .graph
            .node_indices()
            .map(|idx| state.graph[idx].clone())
            .collect();

        let edges: Vec<BlueprintEdge> = state
            .graph
            .edge_references()
            .map(|e| {
                let weight = e.weight();
                BlueprintEdge {
                    id: weight.id.clone(),
                    source_id: state.graph[e.source()].id.clone(),
                    target_id: state.graph[e.target()].id.clone(),
                    edge_type: format!("{:?}", weight.edge_type).to_lowercase(),
                    properties: weight.properties.clone(),
                }
            })
            .collect();

        Ok(GraphPayload { nodes, edges })
    }

    /// Get memory stats: node count, edge count, active mutations, active signals.
    pub fn memory_info(&self) -> Result<MemoryInfo> {
        let state = self.inner.lock().map_err(|e| anyhow::anyhow!("lock: {e}"))?;
        Ok(MemoryInfo {
            nodes: state.graph.node_count(),
            edges: state.graph.edge_count(),
            active_mutations: state.active_mutations.len(),
            active_signals: state.active_signals.len(),
            node_index_entries: state.node_index.len(),
            heuristic_classes: state.heuristics.len(),
        })
    }

    /// Load heuristics (CPTs) from a YAML string.
    pub fn load_heuristics_str(&self, yaml: &str) -> Result<usize> {
        let classes: Vec<ClassHeuristic> =
            serde_yaml_ng::from_str(yaml).context("parsing heuristics YAML")?;
        let count = classes.len();
        let mut state = self
            .inner
            .lock()
            .map_err(|e| anyhow::anyhow!("lock: {e}"))?;
        for class in classes {
            state.heuristics.insert(class.class.clone(), class);
        }
        Ok(count)
    }

    /// Reload heuristics (CPTs) from a YAML file or manifest.
    pub fn reload_heuristics(&self, path: &str) -> Result<usize> {
        let mut state = self
            .inner
            .lock()
            .map_err(|e| anyhow::anyhow!("lock: {e}"))?;
        state.heuristics.clear();
        load_heuristics_from_path(&mut state.heuristics, path)?;
        let count = state.heuristics.len();
        tracing::info!(classes = count, "Heuristics reloaded");
        Ok(count)
    }

    /// Get the current temporal window in minutes.
    pub fn get_temporal_window(&self) -> Result<i64> {
        let state = self
            .inner
            .lock()
            .map_err(|e| anyhow::anyhow!("lock: {e}"))?;
        Ok(state.temporal_window.num_minutes())
    }

    /// Set the temporal window in minutes.
    pub fn set_temporal_window(&self, minutes: i64) -> Result<()> {
        let mut state = self
            .inner
            .lock()
            .map_err(|e| anyhow::anyhow!("lock: {e}"))?;
        state.temporal_window = chrono::Duration::minutes(minutes);
        tracing::info!(minutes, "Temporal window updated");
        Ok(())
    }

    /// Export the full graph as Cytoscape.js JSON elements.
    /// Includes node alert status based on active signals/mutations.
    pub fn export_cytoscape(&self) -> Result<serde_json::Value> {
        let state = self
            .inner
            .lock()
            .map_err(|e| anyhow::anyhow!("lock: {e}"))?;
        Ok(state.export_cytoscape())
    }

    /// Export only the subgraphs involved in active alerts (2-hop neighborhoods).
    /// Returns a compact Cytoscape JSON with just the relevant nodes.
    pub fn alert_subgraphs(&self) -> Result<serde_json::Value> {
        let state = self
            .inner
            .lock()
            .map_err(|e| anyhow::anyhow!("lock: {e}"))?;
        state.alert_subgraphs()
    }

    /// Export a neighborhood subgraph around a node (depth hops in each direction).
    pub fn neighborhood(&self, node_id: &str, depth: usize) -> Result<serde_json::Value> {
        let state = self
            .inner
            .lock()
            .map_err(|e| anyhow::anyhow!("lock: {e}"))?;
        state.neighborhood(node_id, depth)
    }

    /// Get all active alerts (nodes with signals) with diagnosis info.
    pub fn alerts(&self) -> Result<Vec<serde_json::Value>> {
        let state = self
            .inner
            .lock()
            .map_err(|e| anyhow::anyhow!("lock: {e}"))?;
        state.alerts()
    }

    /// Get alerts grouped by root cause. Each group contains:
    /// - root_cause: the shared root cause ID
    /// - confidence: the highest confidence in the group
    /// - members: array of individual alert nodes with their details
    /// - count: number of affected nodes
    /// - signal_types: set of all signal types in the group
    ///
    /// Alerts with no root cause are grouped under "unknown".
    pub fn alert_groups(&self) -> Result<Vec<AlertGroup>> {
        let state = self.inner.lock().map_err(|e| anyhow::anyhow!("lock: {e}"))?;
        let alerts = state.alerts()?;

        // Group by root_cause
        let mut groups: HashMap<String, Vec<serde_json::Value>> = HashMap::new();
        for alert in alerts {
            let rc = alert["root_cause"]
                .as_str()
                .unwrap_or("unknown")
                .to_string();
            groups.entry(rc).or_default().push(alert);
        }

        // Build typed AlertGroup objects
        let mut result: Vec<AlertGroup> = groups
            .into_iter()
            .map(|(root_cause, members)| {
                let count = members.len();
                let max_confidence = members
                    .iter()
                    .map(|m| m["confidence"].as_f64().unwrap_or(0.0))
                    .fold(0.0_f64, f64::max);

                let signal_types: Vec<String> = {
                    let mut set = std::collections::HashSet::new();
                    for m in &members {
                        if let Some(arr) = m["signal_types"].as_array() {
                            for v in arr {
                                if let Some(s) = v.as_str() { set.insert(s.to_string()); }
                            }
                        }
                    }
                    let mut v: Vec<String> = set.into_iter().collect();
                    v.sort();
                    v
                };

                let latest_signal = members
                    .iter()
                    .filter_map(|m| m["latest_signal"].as_str())
                    .max()
                    .unwrap_or("")
                    .to_string();

                let causal_path: Vec<String> = members
                    .first()
                    .and_then(|m| m["causal_path"].as_array())
                    .map(|arr| arr.iter().filter_map(|v| v.as_str().map(|s| s.to_string())).collect())
                    .unwrap_or_default();

                let affected_nodes: Vec<String> = members
                    .iter()
                    .filter_map(|m| m["node_id"].as_str().map(|s| s.to_string()))
                    .collect();

                let typed_members: Vec<AlertMember> = members
                    .iter()
                    .map(|m| AlertMember {
                        node_id: m["node_id"].as_str().unwrap_or("").to_string(),
                        confidence: m["confidence"].as_f64().unwrap_or(0.0),
                        signal_types: m["signal_types"]
                            .as_array()
                            .map(|a| a.iter().filter_map(|v| v.as_str().map(|s| s.to_string())).collect())
                            .unwrap_or_default(),
                        signal_count: m["signal_count"].as_u64().unwrap_or(0) as usize,
                        latest_signal: m["latest_signal"].as_str().unwrap_or("").to_string(),
                    })
                    .collect();

                AlertGroup {
                    root_cause,
                    confidence: max_confidence,
                    count,
                    signal_types,
                    causal_path,
                    latest_signal,
                    affected_nodes,
                    members: typed_members,
                }
            })
            .collect();

        // Sort by confidence descending
        result.sort_by(|a, b| {
            b.confidence.partial_cmp(&a.confidence).unwrap_or(std::cmp::Ordering::Equal)
        });

        Ok(result)
    }
}

struct SolverState {
    /// The causal DAG
    graph: DiGraph<CausalNode, CausalEdge>,
    /// Node ID → NodeIndex lookup
    node_index: HashMap<String, NodeIndex>,
    /// CPTs keyed by resource class
    heuristics: HashMap<String, ClassHeuristic>,
    /// Active mutations within the temporal window
    active_mutations: Vec<Mutation>,
    /// Active signals within the temporal window
    active_signals: Vec<Signal>,
    /// Temporal window duration (default: 30 minutes)
    temporal_window: chrono::Duration,
    /// Cached latest diagnoses (updated on each inference run)
    cached_diagnoses: HashMap<String, Diagnosis>,
    /// Checkpoint path
    _checkpoint_path: Option<String>,
}

impl SolverState {
    fn new() -> Self {
        Self {
            graph: DiGraph::new(),
            node_index: HashMap::new(),
            heuristics: HashMap::new(),
            active_mutations: Vec::new(),
            active_signals: Vec::new(),
            temporal_window: chrono::Duration::minutes(30),
            cached_diagnoses: HashMap::new(),
            _checkpoint_path: Some("data/checkpoint.bin".to_string()),
        }
    }

    /// Collect the ancestors of a node (all upstream causal parents) via BFS.
    fn collect_ancestors(&self, start: NodeIndex) -> Vec<NodeIndex> {
        let mut visited = vec![false; self.graph.node_count()];
        let mut queue = std::collections::VecDeque::new();
        let mut ancestors = Vec::new();

        // Walk upstream (incoming edges = causal parents)
        queue.push_back(start);
        visited[start.index()] = true;

        while let Some(current) = queue.pop_front() {
            for edge in self.graph.edges_directed(current, Direction::Incoming) {
                let parent = edge.source();
                if !visited[parent.index()] {
                    visited[parent.index()] = true;
                    ancestors.push(parent);
                    queue.push_back(parent);
                }
            }
        }

        ancestors
    }

    // ── Likelihood-ratio Bayesian inference ────────────────────────────────
    //
    // The key insight: when we observe a degradation signal on a node, the
    // question is NOT "what's the background probability this node fails?"
    // (that's the prior — typically 0.5%). The question is:
    //
    //   "Given that we SEE a symptom, how likely is each candidate mutation
    //    to have caused it?"
    //
    // For each (mutation, signal) pair where the CPT matches:
    //   likelihood_ratio = P(signal | mutation_present) / P(signal | mutation_absent)
    //
    // Example: ImageUpdate → CrashLoopBackOff CPT = [0.75, 0.03]
    //   LR = 0.75 / 0.03 = 25×
    //
    // Starting from a causal prior of 0.5 (uninformative: "did this mutation
    // cause it, or did something else?"):
    //   posterior_odds = prior_odds × LR = 1.0 × 25 = 25
    //   posterior = 25 / 26 = 96.2%
    //
    // This is the correct Bayesian answer to "given CrashLoopBackOff appeared
    // right after an ImageUpdate, how likely is the update to blame?"

    /// Compute causal confidence for a single (mutation, signal) pair using
    /// the likelihood ratio from the CPT. Returns P(mutation_caused | signal_observed).
    fn lr_confidence(cpt: &CptEntry, causal_prior: f64) -> f64 {
        let p_signal_given_mutation = cpt.table[0][0]; // P(signal_high | mutation_present)
        let p_signal_given_no_mutation = cpt.table[0][1]; // P(signal_high | mutation_absent)

        if p_signal_given_no_mutation <= 0.0 {
            return 1.0; // infinite LR → certainty
        }

        let lr = p_signal_given_mutation / p_signal_given_no_mutation;
        let prior_odds = causal_prior / (1.0 - causal_prior);
        let posterior_odds = prior_odds * lr;
        posterior_odds / (1.0 + posterior_odds)
    }

    /// Compute a temporal decay factor for a mutation relative to a signal.
    /// Mutations very close in time to the signal get prior ≈ 0.50 (uninformative).
    /// Mutations near the edge of the temporal window get a lower prior,
    /// reflecting that older changes are less likely to be the immediate cause.
    ///
    /// Decay curve: prior = 0.50 × e^(-λt) where t = gap in minutes, λ = decay rate.
    /// At t=0:  prior = 0.50 (just happened — 50/50)
    /// At t=15: prior ≈ 0.30 (15 min ago — still plausible)
    /// At t=28: prior ≈ 0.10 (almost expired — unlikely but possible)
    fn temporal_prior(mutation_ts: DateTime<Utc>, signal_ts: DateTime<Utc>) -> f64 {
        let gap_minutes = (signal_ts - mutation_ts).num_seconds().max(0) as f64 / 60.0;
        let lambda = 0.055; // decay rate: half-life ≈ 12.6 minutes
        let base_prior = 0.50;
        base_prior * (-lambda * gap_minutes).exp()
    }

    /// Score a mutation as a candidate root cause for the signals on a node.
    /// Returns the combined confidence from all matching CPT entries,
    /// with temporal decay applied to the causal prior.
    fn score_mutation_with_time(
        &self,
        mutation: &Mutation,
        signals_on_node: &[&Signal],
        node_class: &str,
    ) -> f64 {
        let heuristic = match self.heuristics.get(node_class) {
            Some(h) => h,
            None => return 0.0,
        };

        // Compute temporal prior: recent mutations get 0.50, stale ones decay
        let latest_signal_ts = signals_on_node
            .iter()
            .map(|s| s.timestamp)
            .max()
            .unwrap_or_else(Utc::now);
        let causal_prior = Self::temporal_prior(mutation.timestamp, latest_signal_ts);

        let mut combined_confidence = causal_prior;
        let mut any_match = false;

        for cpt in &heuristic.cpts {
            if cpt.mutation != mutation.mutation_type {
                continue;
            }
            let signal_matches = signals_on_node.iter().any(|s| s.signal_type == cpt.signal);

            if signal_matches {
                let conf = Self::lr_confidence(cpt, combined_confidence);
                combined_confidence = conf;
                any_match = true;
            }
        }

        if !any_match {
            return 0.05 * (causal_prior / 0.5); // scale weak evidence by temporal decay too
        }

        combined_confidence
    }

    /// Score a mutation on its own node (backward compat wrapper).
    fn score_mutation(
        &self,
        mutation: &Mutation,
        signals_on_node: &[&Signal],
        node_class: &str,
    ) -> f64 {
        self.score_mutation_with_time(mutation, signals_on_node, node_class)
    }

    /// Score a mutation that is an ancestor (upstream) of the target node.
    /// Checks both:
    ///   1. Whether the ancestor's OWN class CPTs match the signal type
    ///      (e.g., Gateway CertificateRotation → TLSError)
    ///    2. Whether the target's class CPTs match the mutation type
    ///       (e.g., ConfigChange on upstream → error_rate on downstream)
    ///
    /// Takes the higher score and applies hop decay.
    fn score_ancestor_mutation(
        &self,
        mutation: &Mutation,
        ancestor_idx: NodeIndex,
        target_idx: NodeIndex,
        target_signals: &[&Signal],
    ) -> f64 {
        let ancestor = &self.graph[ancestor_idx];
        let target = &self.graph[target_idx];

        // Signals on the ancestor itself
        let ancestor_signals: Vec<&Signal> = target_signals
            .iter()
            .filter(|s| s.node_id == ancestor.id)
            .copied()
            .collect();

        // Signals on the target
        let target_sigs: Vec<&Signal> = target_signals
            .iter()
            .filter(|s| s.node_id == target.id)
            .copied()
            .collect();

        // Strategy 1: Score using ancestor's own CPTs + ancestor's signals
        let conf_ancestor_self = if !ancestor_signals.is_empty() {
            self.score_mutation(mutation, &ancestor_signals, &ancestor.class)
        } else {
            0.0
        };

        // Strategy 2: Score using ancestor's CPTs + downstream signals
        // (e.g., Gateway CertificateRotation CPT matched against TLSError
        //  that manifests on the downstream container)
        let conf_ancestor_cpt_downstream = if !target_sigs.is_empty() {
            self.score_mutation(mutation, &target_sigs, &ancestor.class)
        } else {
            0.0
        };

        // Strategy 3: Score using target's CPTs + mutation type
        // (e.g., mutation type appears in the target class's CPT table)
        let conf_target_cpt = if !target_sigs.is_empty() {
            self.score_mutation(mutation, &target_sigs, &target.class)
        } else {
            0.0
        };

        // Take the best match across all strategies
        let mut confidence = conf_ancestor_self
            .max(conf_ancestor_cpt_downstream)
            .max(conf_target_cpt);

        if confidence <= 0.0 {
            return 0.01;
        }

        // Attenuate by path length — mild decay so upstream causes with
        // strong CPT matches still dominate over weak direct mutations.
        let path = self.find_path(&ancestor.id, &target.id);
        let hops = path.len().saturating_sub(1);
        for _ in 0..hops {
            confidence *= 0.92; // 8% decay per hop
        }

        confidence
    }

    /// BFS to find a path from source to target in the DAG.
    fn find_path(&self, from_id: &str, to_id: &str) -> Vec<String> {
        let start = match self.node_index.get(from_id) {
            Some(&idx) => idx,
            None => return vec![],
        };
        let end = match self.node_index.get(to_id) {
            Some(&idx) => idx,
            None => return vec![],
        };

        if start == end {
            return vec![from_id.to_string()];
        }

        let mut visited = HashMap::new();
        let mut queue = std::collections::VecDeque::new();
        queue.push_back(start);
        visited.insert(start, start); // parent map

        while let Some(current) = queue.pop_front() {
            if current == end {
                let mut path = Vec::new();
                let mut node = end;
                loop {
                    path.push(self.graph[node].id.clone());
                    let parent = visited[&node];
                    if parent == node {
                        break;
                    }
                    node = parent;
                }
                path.reverse();
                return path;
            }

            for edge in self.graph.edges_directed(current, Direction::Outgoing) {
                let neighbor = edge.target();
                visited.entry(neighbor).or_insert_with(|| {
                    queue.push_back(neighbor);
                    current
                });
            }
        }

        vec![]
    }

    fn diagnose(&self, node_id: &str) -> Result<Diagnosis> {
        let target_idx = *self
            .node_index
            .get(node_id)
            .context(format!("node not found: {node_id}"))?;

        let now = Utc::now();
        let window_start = now - self.temporal_window;

        // Collect active evidence within the temporal window
        let candidate_mutations: Vec<&Mutation> = self
            .active_mutations
            .iter()
            .filter(|m| m.timestamp >= window_start)
            .collect();

        let active_signals: Vec<&Signal> = self
            .active_signals
            .iter()
            .filter(|s| s.timestamp >= window_start)
            .collect();

        // Signals on the target node
        let target_signals: Vec<&Signal> = active_signals
            .iter()
            .filter(|s| s.node_id == node_id)
            .copied()
            .collect();

        if candidate_mutations.is_empty() && target_signals.is_empty() {
            return Ok(Diagnosis {
                target_node: node_id.to_string(),
                confidence: 0.0,
                root_cause: None,
                causal_path: vec![],
                competing_causes: vec![],
                timestamp: now,
            });
        }

        if target_signals.is_empty() {
            // Mutations exist but no signals on this node → nothing to diagnose
            return Ok(Diagnosis {
                target_node: node_id.to_string(),
                confidence: 0.0,
                root_cause: None,
                causal_path: vec![],
                competing_causes: vec![],
                timestamp: now,
            });
        }

        if candidate_mutations.is_empty() {
            // Signals but no mutations → noise / background failure
            let prior = self
                .heuristics
                .get(&self.graph[target_idx].class)
                .map(|h| h.default_prior.p_failure)
                .unwrap_or(0.005);
            return Ok(Diagnosis {
                target_node: node_id.to_string(),
                confidence: prior,
                root_cause: None,
                causal_path: vec![],
                competing_causes: vec![],
                timestamp: now,
            });
        }

        // Score each candidate mutation
        let mut cause_scores: Vec<(String, f64, Vec<String>)> = Vec::new();
        let ancestors = self.collect_ancestors(target_idx);

        // 1. Direct mutations on the target node
        for mutation in &candidate_mutations {
            if mutation.node_id == node_id {
                let conf =
                    self.score_mutation(mutation, &target_signals, &self.graph[target_idx].class);
                cause_scores.push((
                    format!("{} ({})", mutation.node_id, mutation.mutation_type),
                    conf,
                    vec![node_id.to_string()],
                ));
            }
        }

        // 2. Mutations on ancestor nodes (upstream propagation)
        for &ancestor_idx in &ancestors {
            let ancestor = &self.graph[ancestor_idx];
            for mutation in &candidate_mutations {
                if mutation.node_id == ancestor.id {
                    let conf = self.score_ancestor_mutation(
                        mutation,
                        ancestor_idx,
                        target_idx,
                        &active_signals.to_vec(),
                    );
                    let path = self.find_path(&ancestor.id, node_id);
                    cause_scores.push((
                        format!("{} ({})", mutation.node_id, mutation.mutation_type),
                        conf,
                        path,
                    ));
                }
            }
        }

        // Sort by confidence descending
        cause_scores.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));

        let (root_cause, confidence, causal_path) =
            if let Some((rc, conf, path)) = cause_scores.first() {
                (Some(rc.clone()), *conf, path.clone())
            } else {
                (None, 0.0, vec![])
            };

        let competing: Vec<(String, f64)> = cause_scores
            .iter()
            .map(|(id, conf, _)| (id.clone(), *conf))
            .collect();

        tracing::debug!(
            node = node_id,
            confidence,
            root_cause = ?root_cause,
            candidates = candidate_mutations.len(),
            signals = target_signals.len(),
            "Diagnosis complete"
        );

        Ok(Diagnosis {
            target_node: node_id.to_string(),
            confidence,
            root_cause,
            causal_path,
            competing_causes: competing,
            timestamp: now,
        })
    }

    fn diagnose_all(&self, min_confidence: f64) -> Result<Vec<Diagnosis>> {
        let now = Utc::now();
        let window_start = now - self.temporal_window;

        // Find all nodes that have active signals
        let signaled_nodes: Vec<String> = self
            .active_signals
            .iter()
            .filter(|s| s.timestamp >= window_start)
            .map(|s| s.node_id.clone())
            .collect::<std::collections::HashSet<_>>()
            .into_iter()
            .collect();

        let mut results = Vec::new();
        for node_id in &signaled_nodes {
            if self.node_index.contains_key(node_id) {
                match self.diagnose(node_id) {
                    Ok(d) if d.confidence >= min_confidence => results.push(d),
                    Ok(_) => {} // below threshold
                    Err(e) => tracing::warn!(node = %node_id, error = %e, "diagnose failed"),
                }
            }
        }

        // Sort by confidence descending
        results.sort_by(|a, b| {
            b.confidence
                .partial_cmp(&a.confidence)
                .unwrap_or(std::cmp::Ordering::Equal)
        });

        Ok(results)
    }

    fn ingest_signal(&mut self, signal: Signal) -> Result<()> {
        tracing::info!(
            node = %signal.node_id,
            signal_type = %signal.signal_type,
            "Ingested signal"
        );
        self.active_signals.push(signal.clone());
        self.evict_expired();

        // Auto-diagnose if the node exists in our graph
        if self.node_index.contains_key(&signal.node_id) {
            match self.diagnose(&signal.node_id) {
                Ok(d) => {
                    if d.confidence > 0.1 {
                        tracing::info!(
                            node = %d.target_node,
                            confidence = d.confidence,
                            root_cause = ?d.root_cause,
                            "Auto-diagnosis"
                        );
                    }
                    self.cached_diagnoses.insert(signal.node_id, d);
                }
                Err(e) => tracing::debug!(error = %e, "auto-diagnose skipped"),
            }
        }

        Ok(())
    }

    fn ingest_mutation(&mut self, mutation: Mutation) -> Result<()> {
        tracing::info!(
            node = %mutation.node_id,
            mutation_type = %mutation.mutation_type,
            "Ingested mutation"
        );
        self.active_mutations.push(mutation);
        self.evict_expired();
        Ok(())
    }

    /// Remove mutations and signals outside the temporal window.
    fn evict_expired(&mut self) {
        let cutoff = Utc::now() - self.temporal_window;
        self.active_mutations.retain(|m| m.timestamp >= cutoff);
        self.active_signals.retain(|s| s.timestamp >= cutoff);
    }

    /// Add a node to the graph.
    fn add_node(&mut self, node: CausalNode) {
        let id = node.id.clone();
        let idx = self.graph.add_node(node);
        self.node_index.insert(id, idx);
    }

    /// Add an edge to the graph.
    fn add_edge(&mut self, edge: CausalEdge, source_id: &str, target_id: &str) -> Result<()> {
        let &src = self
            .node_index
            .get(source_id)
            .context(format!("source node not found: {source_id}"))?;
        let &tgt = self
            .node_index
            .get(target_id)
            .context(format!("target node not found: {target_id}"))?;
        self.graph.add_edge(src, tgt, edge);
        Ok(())
    }

    /// Export the graph as a DOT/Graphviz string.
    fn export_dot(&self) -> String {
        let mut dot =
            String::from("digraph Causinator9000 {\n  rankdir=TB;\n  node [shape=box];\n\n");

        for idx in self.graph.node_indices() {
            let node = &self.graph[idx];
            let color = match node.class.as_str() {
                "ToRSwitch" | "AvailabilityZone" | "PowerDomain" => "lightcoral",
                "Container" | "VirtualMachine" => "lightblue",
                "ManagedIdentity" | "KeyVault" => "lightyellow",
                _ => "white",
            };
            dot.push_str(&format!(
                "  \"{}\" [label=\"{}\\n[{}]\" style=filled fillcolor=\"{}\"];\n",
                node.id, node.label, node.class, color
            ));
        }

        dot.push('\n');

        for edge_ref in self.graph.edge_references() {
            let src = &self.graph[edge_ref.source()];
            let tgt = &self.graph[edge_ref.target()];
            let style = match edge_ref.weight().edge_type {
                EdgeType::Containment => "solid",
                EdgeType::Dependency => "dashed",
                EdgeType::Connection => "dotted",
            };
            dot.push_str(&format!(
                "  \"{}\" -> \"{}\" [style={} label=\"{:?}\"];\n",
                src.id,
                tgt.id,
                style,
                edge_ref.weight().edge_type
            ));
        }

        dot.push_str("}\n");
        dot
    }

    /// Export graph as Cytoscape.js JSON elements array.
    fn export_cytoscape(&self) -> serde_json::Value {
        let now = Utc::now();
        let window_start = now - self.temporal_window;

        // Build sets of nodes with active signals/mutations for status coloring
        let signaled_nodes: std::collections::HashSet<&str> = self
            .active_signals
            .iter()
            .filter(|s| s.timestamp >= window_start)
            .map(|s| s.node_id.as_str())
            .collect();
        let mutated_nodes: std::collections::HashSet<&str> = self
            .active_mutations
            .iter()
            .filter(|m| m.timestamp >= window_start)
            .map(|m| m.node_id.as_str())
            .collect();

        let mut elements = Vec::new();

        // Nodes
        for idx in self.graph.node_indices() {
            let node = &self.graph[idx];
            let has_signal = signaled_nodes.contains(node.id.as_str());
            let has_mutation = mutated_nodes.contains(node.id.as_str());

            let status = if has_signal && has_mutation {
                "alert" // red
            } else if has_signal {
                "signal" // orange
            } else if has_mutation {
                "mutation" // yellow
            } else {
                "normal" // gray
            };

            elements.push(serde_json::json!({
                "group": "nodes",
                "data": {
                    "id": node.id,
                    "label": node.label,
                    "class": node.class,
                    "region": node.region,
                    "rack_id": node.rack_id,
                    "status": status,
                }
            }));
        }

        // Edges
        for edge_ref in self.graph.edge_references() {
            let src = &self.graph[edge_ref.source()];
            let tgt = &self.graph[edge_ref.target()];
            let w = edge_ref.weight();
            elements.push(serde_json::json!({
                "group": "edges",
                "data": {
                    "id": w.id,
                    "source": src.id,
                    "target": tgt.id,
                    "edge_type": format!("{:?}", w.edge_type).to_lowercase(),
                }
            }));
        }

        serde_json::json!(elements)
    }

    /// Export a neighborhood around a node.
    fn neighborhood(&self, node_id: &str, depth: usize) -> Result<serde_json::Value> {
        let start_idx = *self
            .node_index
            .get(node_id)
            .context(format!("node not found: {node_id}"))?;

        let mut visited = std::collections::HashSet::new();
        let mut queue = std::collections::VecDeque::new();
        queue.push_back((start_idx, 0usize));
        visited.insert(start_idx);

        let max_neighbors_per_node = 8; // Cap fan-out to keep the graph readable
        let max_total_nodes = 40; // Hard cap on total neighborhood size

        // BFS in both directions up to depth, with fan-out limits
        while let Some((current, d)) = queue.pop_front() {
            if d >= depth || visited.len() >= max_total_nodes {
                continue;
            }

            // Outgoing (downstream) — capped
            let mut child_count = 0;
            for edge in self.graph.edges_directed(current, Direction::Outgoing) {
                if child_count >= max_neighbors_per_node {
                    break;
                }
                let neighbor = edge.target();
                if visited.insert(neighbor) {
                    queue.push_back((neighbor, d + 1));
                    child_count += 1;
                }
            }

            // Incoming (upstream) — all parents (usually few)
            for edge in self.graph.edges_directed(current, Direction::Incoming) {
                if visited.len() >= max_total_nodes {
                    break;
                }
                let neighbor = edge.source();
                if visited.insert(neighbor) {
                    queue.push_back((neighbor, d + 1));
                }
            }
        }

        let now = Utc::now();
        let window_start = now - self.temporal_window;
        let signaled: std::collections::HashSet<&str> = self
            .active_signals
            .iter()
            .filter(|s| s.timestamp >= window_start)
            .map(|s| s.node_id.as_str())
            .collect();
        let mutated: std::collections::HashSet<&str> = self
            .active_mutations
            .iter()
            .filter(|m| m.timestamp >= window_start)
            .map(|m| m.node_id.as_str())
            .collect();

        let mut elements = Vec::new();

        for &idx in &visited {
            let node = &self.graph[idx];
            let status =
                if signaled.contains(node.id.as_str()) && mutated.contains(node.id.as_str()) {
                    "alert"
                } else if signaled.contains(node.id.as_str()) {
                    "signal"
                } else if mutated.contains(node.id.as_str()) {
                    "mutation"
                } else {
                    "normal"
                };
            elements.push(serde_json::json!({
                "group": "nodes",
                "data": {
                    "id": node.id,
                    "label": node.label,
                    "class": node.class,
                    "region": node.region,
                    "status": status,
                    "selected": node.id == node_id,
                }
            }));
        }

        // Edges where both endpoints are in the visited set
        for edge_ref in self.graph.edge_references() {
            if visited.contains(&edge_ref.source()) && visited.contains(&edge_ref.target()) {
                let src = &self.graph[edge_ref.source()];
                let tgt = &self.graph[edge_ref.target()];
                let w = edge_ref.weight();
                elements.push(serde_json::json!({
                    "group": "edges",
                    "data": {
                        "id": w.id,
                        "source": src.id,
                        "target": tgt.id,
                        "edge_type": format!("{:?}", w.edge_type).to_lowercase(),
                    }
                }));
            }
        }

        Ok(serde_json::json!(elements))
    }

    /// Get all active alerts with diagnosis info.
    fn alerts(&self) -> Result<Vec<serde_json::Value>> {
        let now = Utc::now();
        let window_start = now - self.temporal_window;

        // Group signals by node
        let mut signal_nodes: std::collections::HashMap<&str, Vec<&Signal>> =
            std::collections::HashMap::new();
        for sig in &self.active_signals {
            if sig.timestamp >= window_start {
                signal_nodes.entry(&sig.node_id).or_default().push(sig);
            }
        }

        let mut alerts = Vec::new();
        for (node_id, signals) in &signal_nodes {
            if !self.node_index.contains_key(*node_id) {
                continue;
            }
            let diag = self.diagnose(node_id)?;
            let latest_signal = signals.iter().map(|s| s.timestamp).max().unwrap_or(now);

            alerts.push(serde_json::json!({
                "node_id": node_id,
                "confidence": diag.confidence,
                "root_cause": diag.root_cause,
                "causal_path": diag.causal_path,
                "competing_causes": diag.competing_causes,
                "signal_count": signals.len(),
                "signal_types": signals.iter().map(|s| &s.signal_type).collect::<std::collections::HashSet<_>>(),
                "latest_signal": latest_signal.to_rfc3339(),
                "timestamp": diag.timestamp.to_rfc3339(),
            }));
        }

        // Sort by confidence descending
        alerts.sort_by(|a, b| {
            let ca = a["confidence"].as_f64().unwrap_or(0.0);
            let cb = b["confidence"].as_f64().unwrap_or(0.0);
            cb.partial_cmp(&ca).unwrap_or(std::cmp::Ordering::Equal)
        });

        Ok(alerts)
    }

    /// Build a merged Cytoscape subgraph containing only nodes involved in
    /// active alerts (nodes with signals) plus their 2-hop neighborhoods.
    /// Each alert cluster is tagged with a parent compound node for grouping.
    fn alert_subgraphs(&self) -> Result<serde_json::Value> {
        let now = Utc::now();
        let window_start = now - self.temporal_window;

        let signaled: std::collections::HashSet<&str> = self
            .active_signals
            .iter()
            .filter(|s| s.timestamp >= window_start)
            .map(|s| s.node_id.as_str())
            .collect();
        let mutated: std::collections::HashSet<&str> = self
            .active_mutations
            .iter()
            .filter(|m| m.timestamp >= window_start)
            .map(|m| m.node_id.as_str())
            .collect();

        if signaled.is_empty() {
            return Ok(serde_json::json!([]));
        }

        // For each alert, collect a focused subgraph that reveals
        // cross-boundary causation:
        //
        // 1. The signaled node
        // 2. Full ancestor chain — walk ALL the way up via incoming edges,
        //    but only include ancestors that either (a) have an active
        //    mutation, or (b) are on the path between a mutated ancestor
        //    and the signaled node. This shows CertAuthority → Gateway →
        //    AKS → Pod chains when the CA has a mutation.
        // 3. Direct children of the signaled node (capped at 5) — for
        //    context on what's downstream of the failure.
        // 4. The causal path from the solver's diagnosis.
        //
        // This keeps clusters tight (~10-30 nodes) while showing the
        // full cross-boundary causal chain.

        let mut visited_global = std::collections::HashSet::new();
        let mut node_cluster: HashMap<NodeIndex, String> = HashMap::new();
        let max_children = 5;

        for sig_node_id in &signaled {
            let start_idx = match self.node_index.get(*sig_node_id) {
                Some(&idx) => idx,
                None => continue,
            };

            let cluster_id = format!("cluster-{sig_node_id}");

            // 1. The signaled node
            visited_global.insert(start_idx);
            node_cluster
                .entry(start_idx)
                .or_insert_with(|| cluster_id.clone());

            // 2. Walk full ancestor chain; collect all ancestors, then
            //    include those with mutations + the path connecting them.
            let ancestors = self.collect_ancestors(start_idx);

            // Find which ancestors have active mutations
            let mut mutated_ancestors: Vec<NodeIndex> = Vec::new();
            for &anc_idx in &ancestors {
                let anc = &self.graph[anc_idx];
                if mutated.contains(anc.id.as_str()) {
                    mutated_ancestors.push(anc_idx);
                }
            }

            // For each mutated ancestor, include the path from it to the signaled node
            for &anc_idx in &mutated_ancestors {
                let anc_id = &self.graph[anc_idx].id;
                let path = self.find_path(anc_id, sig_node_id);
                for path_node_id in &path {
                    if let Some(&idx) = self.node_index.get(path_node_id.as_str()) {
                        visited_global.insert(idx);
                        node_cluster
                            .entry(idx)
                            .or_insert_with(|| cluster_id.clone());
                    }
                }
            }

            // Also include direct parents (even if they don't have mutations —
            // they provide structural context)
            for edge in self.graph.edges_directed(start_idx, Direction::Incoming) {
                let parent = edge.source();
                visited_global.insert(parent);
                node_cluster
                    .entry(parent)
                    .or_insert_with(|| cluster_id.clone());
            }

            // 3. Direct children (capped) — what's downstream of the failure
            for (child_count, edge) in self
                .graph
                .edges_directed(start_idx, Direction::Outgoing)
                .enumerate()
            {
                if child_count >= max_children {
                    break;
                }
                let child = edge.target();
                visited_global.insert(child);
                node_cluster
                    .entry(child)
                    .or_insert_with(|| cluster_id.clone());
            }

            // 4. Causal path from diagnosis
            if let Ok(diag) = self.diagnose(sig_node_id) {
                for path_node_id in &diag.causal_path {
                    if let Some(&idx) = self.node_index.get(path_node_id.as_str()) {
                        visited_global.insert(idx);
                        node_cluster
                            .entry(idx)
                            .or_insert_with(|| cluster_id.clone());
                    }
                }
            }
        }

        // Also include nodes that have mutations (even if no signal directly on them)
        for mut_node_id in &mutated {
            if let Some(&idx) = self.node_index.get(*mut_node_id)
                && visited_global.insert(idx)
            {
                node_cluster
                    .entry(idx)
                    .or_insert_with(|| format!("cluster-{mut_node_id}"));
            }
        }

        // Collect unique cluster IDs
        let clusters: std::collections::HashSet<&str> =
            node_cluster.values().map(|s| s.as_str()).collect();

        let mut elements: Vec<serde_json::Value> = Vec::new();

        // Add compound parent nodes for each cluster
        for cluster_id in &clusters {
            // Derive a label from cluster ID
            let label = cluster_id.strip_prefix("cluster-").unwrap_or(cluster_id);
            elements.push(serde_json::json!({
                "group": "nodes",
                "data": {
                    "id": cluster_id,
                    "label": label,
                    "class": "cluster",
                    "status": "cluster",
                }
            }));
        }

        // Add nodes
        for &idx in &visited_global {
            let node = &self.graph[idx];
            let has_signal = signaled.contains(node.id.as_str());
            let has_mutation = mutated.contains(node.id.as_str());
            let status = if has_signal && has_mutation {
                "alert"
            } else if has_signal {
                "signal"
            } else if has_mutation {
                "mutation"
            } else {
                "normal"
            };

            let parent = node_cluster.get(&idx).cloned();
            let mut data = serde_json::json!({
                "id": node.id,
                "label": node.label,
                "class": node.class,
                "region": node.region,
                "status": status,
            });
            if let Some(p) = parent {
                data.as_object_mut()
                    .unwrap()
                    .insert("parent".to_string(), serde_json::json!(p));
            }
            elements.push(serde_json::json!({ "group": "nodes", "data": data }));
        }

        // Add edges where both endpoints are in the set
        for edge_ref in self.graph.edge_references() {
            if visited_global.contains(&edge_ref.source())
                && visited_global.contains(&edge_ref.target())
            {
                let src = &self.graph[edge_ref.source()];
                let tgt = &self.graph[edge_ref.target()];
                let w = edge_ref.weight();
                elements.push(serde_json::json!({
                    "group": "edges",
                    "data": {
                        "id": w.id,
                        "source": src.id,
                        "target": tgt.id,
                        "edge_type": format!("{:?}", w.edge_type).to_lowercase(),
                    }
                }));
            }
        }

        Ok(serde_json::json!(elements))
    }

    /// Save solver state to a checkpoint file.
    fn write_checkpoint(&self, path: &str) -> Result<()> {
        let snapshot = self.to_snapshot();
        crate::checkpoint::write_checkpoint(&snapshot, path)
    }

    /// Build a snapshot of the current state for serialization.
    fn to_snapshot(&self) -> SolverSnapshot {
        let nodes: Vec<CausalNode> = self
            .graph
            .node_indices()
            .map(|idx| self.graph[idx].clone())
            .collect();

        let edges: Vec<BlueprintEdge> = self
            .graph
            .edge_references()
            .map(|e| {
                let weight = e.weight();
                BlueprintEdge {
                    id: weight.id.clone(),
                    source_id: self.graph[e.source()].id.clone(),
                    target_id: self.graph[e.target()].id.clone(),
                    edge_type: format!("{:?}", weight.edge_type).to_lowercase(),
                    properties: weight.properties.clone(),
                }
            })
            .collect();

        SolverSnapshot {
            nodes,
            edges,
            active_mutations: self.active_mutations.clone(),
            active_signals: self.active_signals.clone(),
        }
    }

    /// Restore state from a snapshot.
    #[allow(clippy::wrong_self_convention)]
    fn from_snapshot(&mut self, snapshot: SolverSnapshot) -> Result<()> {
        self.graph.clear();
        self.node_index.clear();

        for node in snapshot.nodes {
            self.add_node(node);
        }

        for edge in snapshot.edges {
            let edge_type = match edge.edge_type.as_str() {
                "containment" => EdgeType::Containment,
                "dependency" => EdgeType::Dependency,
                "connection" => EdgeType::Connection,
                _ => EdgeType::Dependency,
            };
            self.add_edge(
                CausalEdge {
                    id: edge.id,
                    edge_type,
                    properties: edge.properties,
                },
                &edge.source_id,
                &edge.target_id,
            )?;
        }

        self.active_mutations = snapshot.active_mutations;
        self.active_signals = snapshot.active_signals;

        Ok(())
    }
}

// ── BayesianSolver (public API) ──────────────────────────────────────────

pub struct BayesianSolver {
    state: Arc<Mutex<SolverState>>,
}

impl BayesianSolver {
    pub fn new() -> Result<Self> {
        Ok(Self {
            state: Arc::new(Mutex::new(SolverState::new())),
        })
    }

    /// Return a thread-safe handle for use from API/reaction handlers.
    pub fn handle(&self) -> SolverHandle {
        SolverHandle {
            inner: Arc::clone(&self.state),
        }
    }

    /// Load heuristics (CPTs) from a YAML file or manifest.
    pub fn load_heuristics(&mut self, path: &str) -> Result<()> {
        let mut state = self
            .state
            .lock()
            .map_err(|e| anyhow::anyhow!("lock: {e}"))?;
        load_heuristics_from_path(&mut state.heuristics, path)
    }

    /// Load a checkpoint from disk and restore solver state.
    pub fn load_checkpoint(&mut self, path: &str) -> Result<()> {
        let snapshot: SolverSnapshot = crate::checkpoint::read_checkpoint(path)?;
        let mut state = self
            .state
            .lock()
            .map_err(|e| anyhow::anyhow!("lock: {e}"))?;
        state.from_snapshot(snapshot)?;
        tracing::info!(
            nodes = state.graph.node_count(),
            edges = state.graph.edge_count(),
            mutations = state.active_mutations.len(),
            "Restored from checkpoint"
        );
        Ok(())
    }

    /// Load the blueprint graph from the transpiler's binary format.
    ///
    /// The blueprint.bin format (written by scripts/transpile.py):
    /// [4 bytes: node_count][4 bytes: edge_count]
    /// For each node: [4 bytes: id_len][id_bytes][4 bytes: json_len][json_bytes]
    /// For each edge: [4 bytes: id_len][id_bytes][4 bytes: src_len][src_bytes]
    ///                [4 bytes: tgt_len][tgt_bytes][4 bytes: json_len][json_bytes]
    pub fn load_blueprint(&mut self, path: &str) -> Result<()> {
        let data = std::fs::read(path).context(format!("reading blueprint: {path}"))?;
        let mut cursor = 0;

        let read_u32 = |data: &[u8], pos: &mut usize| -> Result<u32> {
            if *pos + 4 > data.len() {
                anyhow::bail!("unexpected EOF in blueprint at offset {pos}");
            }
            let val = u32::from_le_bytes(data[*pos..*pos + 4].try_into()?);
            *pos += 4;
            Ok(val)
        };

        let read_bytes = |data: &[u8], pos: &mut usize, len: usize| -> Result<Vec<u8>> {
            if *pos + len > data.len() {
                anyhow::bail!("unexpected EOF in blueprint at offset {pos}");
            }
            let bytes = data[*pos..*pos + len].to_vec();
            *pos += len;
            Ok(bytes)
        };

        let node_count = read_u32(&data, &mut cursor)? as usize;
        let edge_count = read_u32(&data, &mut cursor)? as usize;

        let mut state = self
            .state
            .lock()
            .map_err(|e| anyhow::anyhow!("lock: {e}"))?;

        // Read nodes
        for _ in 0..node_count {
            let id_len = read_u32(&data, &mut cursor)? as usize;
            let _id_bytes = read_bytes(&data, &mut cursor, id_len)?;

            let json_len = read_u32(&data, &mut cursor)? as usize;
            let json_bytes = read_bytes(&data, &mut cursor, json_len)?;
            let json_str = String::from_utf8(json_bytes).context("invalid UTF-8 in node JSON")?;
            let node: CausalNode = serde_json::from_str(&json_str).context("parsing node JSON")?;
            state.add_node(node);
        }

        // Read edges
        for _ in 0..edge_count {
            let id_len = read_u32(&data, &mut cursor)? as usize;
            let id_bytes = read_bytes(&data, &mut cursor, id_len)?;
            let edge_id = String::from_utf8(id_bytes).context("invalid UTF-8 in edge id")?;

            let src_len = read_u32(&data, &mut cursor)? as usize;
            let src_bytes = read_bytes(&data, &mut cursor, src_len)?;
            let source_id = String::from_utf8(src_bytes).context("invalid UTF-8 in source id")?;

            let tgt_len = read_u32(&data, &mut cursor)? as usize;
            let tgt_bytes = read_bytes(&data, &mut cursor, tgt_len)?;
            let target_id = String::from_utf8(tgt_bytes).context("invalid UTF-8 in target id")?;

            let json_len = read_u32(&data, &mut cursor)? as usize;
            let json_bytes = read_bytes(&data, &mut cursor, json_len)?;
            let json_str = String::from_utf8(json_bytes).context("invalid UTF-8 in edge JSON")?;

            #[derive(Deserialize)]
            struct EdgeJson {
                #[allow(dead_code)]
                id: String,
                edge_type: String,
                #[serde(default)]
                properties: serde_json::Value,
            }

            let ej: EdgeJson = serde_json::from_str(&json_str).context("parsing edge JSON")?;

            let edge_type = match ej.edge_type.as_str() {
                "containment" => EdgeType::Containment,
                "dependency" => EdgeType::Dependency,
                "connection" => EdgeType::Connection,
                _ => EdgeType::Dependency,
            };

            state.add_edge(
                CausalEdge {
                    id: edge_id,
                    edge_type,
                    properties: ej.properties,
                },
                &source_id,
                &target_id,
            )?;
        }

        tracing::info!(
            nodes = state.graph.node_count(),
            edges = state.graph.edge_count(),
            "Blueprint loaded into petgraph"
        );

        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_test_graph() -> SolverState {
        let mut state = SolverState::new();

        // Build a simple graph: ToR → VM → Container
        state.add_node(CausalNode {
            id: "tor-1".into(),
            label: "ToR Switch 1".into(),
            class: "ToRSwitch".into(),
            region: Some("eastus".into()),
            rack_id: Some("rack-01".into()),
            properties: serde_json::json!({}),
        });
        state.add_node(CausalNode {
            id: "vm-1".into(),
            label: "VM 1".into(),
            class: "VirtualMachine".into(),
            region: Some("eastus".into()),
            rack_id: Some("rack-01".into()),
            properties: serde_json::json!({}),
        });
        state.add_node(CausalNode {
            id: "ctr-1".into(),
            label: "Container 1".into(),
            class: "Container".into(),
            region: Some("eastus".into()),
            rack_id: Some("rack-01".into()),
            properties: serde_json::json!({}),
        });

        state
            .add_edge(
                CausalEdge {
                    id: "edge-tor-vm".into(),
                    edge_type: EdgeType::Containment,
                    properties: serde_json::json!({}),
                },
                "tor-1",
                "vm-1",
            )
            .unwrap();
        state
            .add_edge(
                CausalEdge {
                    id: "edge-vm-ctr".into(),
                    edge_type: EdgeType::Containment,
                    properties: serde_json::json!({}),
                },
                "vm-1",
                "ctr-1",
            )
            .unwrap();

        // Load heuristics
        state.heuristics.insert(
            "ToRSwitch".into(),
            ClassHeuristic {
                class: "ToRSwitch".into(),
                default_prior: PriorConfig { p_failure: 0.0008 },
                cpts: vec![CptEntry {
                    mutation: "FirmwareUpdate".into(),
                    signal: "heartbeat".into(),
                    table: vec![vec![0.90, 0.001], vec![0.10, 0.999]],
                }],
            },
        );
        state.heuristics.insert(
            "VirtualMachine".into(),
            ClassHeuristic {
                class: "VirtualMachine".into(),
                default_prior: PriorConfig { p_failure: 0.002 },
                cpts: vec![CptEntry {
                    mutation: "MaintenanceReboot".into(),
                    signal: "heartbeat".into(),
                    table: vec![vec![0.95, 0.001], vec![0.05, 0.999]],
                }],
            },
        );
        state.heuristics.insert(
            "Container".into(),
            ClassHeuristic {
                class: "Container".into(),
                default_prior: PriorConfig { p_failure: 0.005 },
                cpts: vec![CptEntry {
                    mutation: "ImageUpdate".into(),
                    signal: "CrashLoopBackOff".into(),
                    table: vec![vec![0.75, 0.03], vec![0.25, 0.97]],
                }],
            },
        );

        state
    }

    #[test]
    fn test_diagnose_no_evidence() {
        let state = make_test_graph();
        let diag = state.diagnose("ctr-1").unwrap();
        assert_eq!(diag.confidence, 0.0);
        assert!(diag.root_cause.is_none());
    }

    #[test]
    fn test_diagnose_with_mutation_and_signal() {
        let mut state = make_test_graph();

        // Inject mutation on container
        state.active_mutations.push(Mutation {
            id: "m1".into(),
            node_id: "ctr-1".into(),
            mutation_type: "ImageUpdate".into(),
            source: "radius".into(),
            timestamp: Utc::now(),
            properties: serde_json::json!({}),
        });

        // Inject signal on container
        state.active_signals.push(Signal {
            id: "s1".into(),
            node_id: "ctr-1".into(),
            signal_type: "CrashLoopBackOff".into(),
            value: Some(1.0),
            severity: Some("critical".into()),
            timestamp: Utc::now(),
            properties: serde_json::json!({}),
        });

        let diag = state.diagnose("ctr-1").unwrap();
        // With LR-based inference, ImageUpdate + CrashLoopBackOff should
        // produce high confidence: LR = 0.75/0.03 = 25, posterior ≈ 96%
        assert!(
            diag.confidence > 0.8,
            "confidence should be high: {}",
            diag.confidence
        );
        assert!(diag.root_cause.is_some());
    }

    #[test]
    fn test_diagnose_all() {
        let mut state = make_test_graph();

        state.active_mutations.push(Mutation {
            id: "m1".into(),
            node_id: "ctr-1".into(),
            mutation_type: "ImageUpdate".into(),
            source: "radius".into(),
            timestamp: Utc::now(),
            properties: serde_json::json!({}),
        });
        state.active_signals.push(Signal {
            id: "s1".into(),
            node_id: "ctr-1".into(),
            signal_type: "CrashLoopBackOff".into(),
            value: Some(1.0),
            severity: Some("critical".into()),
            timestamp: Utc::now(),
            properties: serde_json::json!({}),
        });

        let results = state.diagnose_all(0.5).unwrap();
        assert!(!results.is_empty());
    }

    #[test]
    fn test_export_dot() {
        let state = make_test_graph();
        let dot = state.export_dot();
        assert!(dot.contains("digraph Causinator9000"));
        assert!(dot.contains("tor-1"));
        assert!(dot.contains("vm-1"));
        assert!(dot.contains("ctr-1"));
    }

    #[test]
    fn test_lr_confidence() {
        // LR = 0.75/0.03 = 25, prior = 0.5 → posterior = 25/26 ≈ 0.962
        let cpt = CptEntry {
            mutation: "ImageUpdate".into(),
            signal: "CrashLoopBackOff".into(),
            table: vec![vec![0.75, 0.03], vec![0.25, 0.97]],
        };
        let conf = SolverState::lr_confidence(&cpt, 0.5);
        assert!(
            (conf - 0.9615).abs() < 0.01,
            "LR confidence should be ~96.2%, got {conf}"
        );

        // With lower prior (0.1), posterior should be lower
        let conf_low = SolverState::lr_confidence(&cpt, 0.1);
        assert!(conf_low < conf, "lower prior should give lower posterior");
        assert!(conf_low > 0.5, "but still above 50% with LR=25");
    }

    #[test]
    fn test_temporal_prior_decay() {
        // At t=0, prior should be 0.50
        let now = Utc::now();
        let prior_0 = SolverState::temporal_prior(now, now);
        assert!(
            (prior_0 - 0.50).abs() < 0.01,
            "at t=0 prior should be 0.50, got {prior_0}"
        );

        // At t=15min, prior should be ~0.22
        let prior_15 = SolverState::temporal_prior(now - chrono::Duration::minutes(15), now);
        assert!(
            prior_15 < 0.30,
            "at 15min prior should be < 0.30, got {prior_15}"
        );
        assert!(
            prior_15 > 0.15,
            "at 15min prior should be > 0.15, got {prior_15}"
        );

        // At t=30min, prior should be very low
        let prior_30 = SolverState::temporal_prior(now - chrono::Duration::minutes(30), now);
        assert!(
            prior_30 < 0.15,
            "at 30min prior should be < 0.15, got {prior_30}"
        );

        // Decay is monotonic
        assert!(prior_0 > prior_15, "prior should decay over time");
        assert!(prior_15 > prior_30, "prior should decay over time");
    }

    #[test]
    fn test_temporal_decay_affects_confidence() {
        let mut state = make_test_graph();
        let now = Utc::now();

        // Fresh mutation (just now)
        state.active_mutations.push(Mutation {
            id: "m-fresh".into(),
            node_id: "ctr-1".into(),
            mutation_type: "ImageUpdate".into(),
            source: "test".into(),
            timestamp: now,
            properties: serde_json::json!({}),
        });
        state.active_signals.push(Signal {
            id: "s1".into(),
            node_id: "ctr-1".into(),
            signal_type: "CrashLoopBackOff".into(),
            value: Some(1.0),
            severity: Some("critical".into()),
            timestamp: now,
            properties: serde_json::json!({}),
        });
        let diag_fresh = state.diagnose("ctr-1").unwrap();

        // Reset and use stale mutation (25 min ago)
        state.active_mutations.clear();
        state.active_mutations.push(Mutation {
            id: "m-stale".into(),
            node_id: "ctr-1".into(),
            mutation_type: "ImageUpdate".into(),
            source: "test".into(),
            timestamp: now - chrono::Duration::minutes(25),
            properties: serde_json::json!({}),
        });
        let diag_stale = state.diagnose("ctr-1").unwrap();

        assert!(
            diag_fresh.confidence > diag_stale.confidence,
            "fresh mutation ({}) should score higher than stale ({})",
            diag_fresh.confidence,
            diag_stale.confidence
        );
    }

    #[test]
    fn test_upstream_propagation() {
        let mut state = make_test_graph();

        // Mutation on the ToR switch (upstream of VM and container)
        state.active_mutations.push(Mutation {
            id: "m1".into(),
            node_id: "tor-1".into(),
            mutation_type: "FirmwareUpdate".into(),
            source: "test".into(),
            timestamp: Utc::now(),
            properties: serde_json::json!({}),
        });

        // Signal on the container (downstream)
        state.active_signals.push(Signal {
            id: "s1".into(),
            node_id: "ctr-1".into(),
            signal_type: "heartbeat".into(),
            value: Some(0.0),
            severity: Some("critical".into()),
            timestamp: Utc::now(),
            properties: serde_json::json!({}),
        });

        let diag = state.diagnose("ctr-1").unwrap();
        // Should find the upstream mutation as root cause
        assert!(
            diag.root_cause.is_some(),
            "should identify upstream root cause"
        );
        let rc = diag.root_cause.unwrap();
        assert!(
            rc.contains("tor-1"),
            "root cause should be tor-1, got: {rc}"
        );
        assert!(diag.confidence > 0.0, "confidence should be above zero");
    }

    #[test]
    fn test_hop_attenuation() {
        let mut state = make_test_graph();
        let now = Utc::now();

        // Direct mutation on container
        state.active_mutations.push(Mutation {
            id: "m-direct".into(),
            node_id: "ctr-1".into(),
            mutation_type: "ImageUpdate".into(),
            source: "test".into(),
            timestamp: now,
            properties: serde_json::json!({}),
        });
        state.active_signals.push(Signal {
            id: "s1".into(),
            node_id: "ctr-1".into(),
            signal_type: "CrashLoopBackOff".into(),
            value: Some(1.0),
            severity: Some("critical".into()),
            timestamp: now,
            properties: serde_json::json!({}),
        });
        let _diag_direct = state.diagnose("ctr-1").unwrap();

        // Now also add an upstream mutation — it should score lower
        state.active_mutations.push(Mutation {
            id: "m-upstream".into(),
            node_id: "tor-1".into(),
            mutation_type: "FirmwareUpdate".into(),
            source: "test".into(),
            timestamp: now,
            properties: serde_json::json!({}),
        });
        let diag_both = state.diagnose("ctr-1").unwrap();
        let cc = &diag_both.competing_causes;

        // Should have multiple candidates
        assert!(!cc.is_empty(), "should have at least 1 competing cause");

        // Direct mutation (0 hops) should score >= upstream (2 hops)
        if cc.len() >= 2 {
            let direct_score = cc
                .iter()
                .find(|(id, _)| id.contains("ctr-1"))
                .map(|(_, c)| *c)
                .unwrap_or(0.0);
            let upstream_score = cc
                .iter()
                .find(|(id, _)| id.contains("tor-1"))
                .map(|(_, c)| *c)
                .unwrap_or(0.0);
            assert!(
                direct_score >= upstream_score,
                "direct ({direct_score}) should score >= upstream ({upstream_score}) due to hop attenuation"
            );
        }
    }

    #[test]
    fn test_no_mutation_no_root_cause() {
        let mut state = make_test_graph();

        // Signal but no mutation — should be noise
        state.active_signals.push(Signal {
            id: "s1".into(),
            node_id: "ctr-1".into(),
            signal_type: "error_rate".into(),
            value: Some(0.5),
            severity: Some("warning".into()),
            timestamp: Utc::now(),
            properties: serde_json::json!({}),
        });

        let diag = state.diagnose("ctr-1").unwrap();
        assert!(diag.root_cause.is_none(), "no mutation → no root cause");
        assert!(
            diag.confidence < 0.01,
            "confidence should be near zero without mutations"
        );
    }

    #[test]
    fn test_cytoscape_export() {
        let state = make_test_graph();
        let cy = state.export_cytoscape();
        let elements = cy.as_array().unwrap();
        let nodes: Vec<_> = elements.iter().filter(|e| e["group"] == "nodes").collect();
        let edges: Vec<_> = elements.iter().filter(|e| e["group"] == "edges").collect();
        assert_eq!(nodes.len(), 3, "should have 3 nodes");
        assert_eq!(edges.len(), 2, "should have 2 edges");
        // Check node data
        let tor = nodes.iter().find(|n| n["data"]["id"] == "tor-1").unwrap();
        assert_eq!(tor["data"]["class"], "ToRSwitch");
        assert_eq!(tor["data"]["status"], "normal");
    }

    #[test]
    fn test_alert_subgraphs_empty() {
        let state = make_test_graph();
        let result = state.alert_subgraphs().unwrap();
        let elements = result.as_array().unwrap();
        assert!(elements.is_empty(), "no alerts → empty subgraph");
    }

    #[test]
    fn test_alert_subgraphs_with_signal() {
        let mut state = make_test_graph();
        state.active_mutations.push(Mutation {
            id: "m1".into(),
            node_id: "ctr-1".into(),
            mutation_type: "ImageUpdate".into(),
            source: "test".into(),
            timestamp: Utc::now(),
            properties: serde_json::json!({}),
        });
        state.active_signals.push(Signal {
            id: "s1".into(),
            node_id: "ctr-1".into(),
            signal_type: "CrashLoopBackOff".into(),
            value: Some(1.0),
            severity: Some("critical".into()),
            timestamp: Utc::now(),
            properties: serde_json::json!({}),
        });

        let result = state.alert_subgraphs().unwrap();
        let elements = result.as_array().unwrap();
        assert!(!elements.is_empty(), "should have alert subgraph elements");
        // Should include at least the signaled node
        let nodes: Vec<_> = elements
            .iter()
            .filter(|e| e["group"] == "nodes" && e["data"]["class"] != "cluster")
            .collect();
        assert!(
            nodes.iter().any(|n| n["data"]["id"] == "ctr-1"),
            "should include signaled node"
        );
    }

    // ── Modular heuristics tests ─────────────────────────────────────────

    #[test]
    fn test_merge_layer_new_class() {
        let mut heuristics = HashMap::new();
        let entries = vec![LayerClassEntry {
            class: "Container".into(),
            default_prior: Some(PriorConfig { p_failure: 0.005 }),
            cpts: vec![CptEntry {
                mutation: "ImageUpdate".into(),
                signal: "CrashLoopBackOff".into(),
                table: vec![vec![0.75, 0.03], vec![0.25, 0.97]],
            }],
        }];
        merge_layer(&mut heuristics, entries);
        assert_eq!(heuristics.len(), 1);
        let ctr = &heuristics["Container"];
        assert!((ctr.default_prior.p_failure - 0.005).abs() < 1e-9);
        assert_eq!(ctr.cpts.len(), 1);
        assert_eq!(ctr.cpts[0].table[0][0], 0.75);
    }

    #[test]
    fn test_merge_layer_override_cpt() {
        let mut heuristics = HashMap::new();
        // Base layer
        merge_layer(
            &mut heuristics,
            vec![LayerClassEntry {
                class: "Container".into(),
                default_prior: Some(PriorConfig { p_failure: 0.005 }),
                cpts: vec![
                    CptEntry {
                        mutation: "ImageUpdate".into(),
                        signal: "CrashLoopBackOff".into(),
                        table: vec![vec![0.75, 0.03], vec![0.25, 0.97]],
                    },
                    CptEntry {
                        mutation: "ConfigChange".into(),
                        signal: "error_rate".into(),
                        table: vec![vec![0.65, 0.04], vec![0.35, 0.96]],
                    },
                ],
            }],
        );

        // Override layer — only override one CPT
        merge_layer(
            &mut heuristics,
            vec![LayerClassEntry {
                class: "Container".into(),
                default_prior: None, // keep existing prior
                cpts: vec![CptEntry {
                    mutation: "ImageUpdate".into(),
                    signal: "CrashLoopBackOff".into(),
                    table: vec![vec![0.85, 0.03], vec![0.15, 0.97]],
                }],
            }],
        );

        let ctr = &heuristics["Container"];
        // Prior should be unchanged
        assert!((ctr.default_prior.p_failure - 0.005).abs() < 1e-9);
        // Should still have 2 CPTs
        assert_eq!(ctr.cpts.len(), 2);
        // ImageUpdate→CrashLoopBackOff should be overridden
        let updated = ctr
            .cpts
            .iter()
            .find(|c| c.mutation == "ImageUpdate" && c.signal == "CrashLoopBackOff")
            .unwrap();
        assert_eq!(updated.table[0][0], 0.85);
        // ConfigChange→error_rate should be unchanged
        let unchanged = ctr
            .cpts
            .iter()
            .find(|c| c.mutation == "ConfigChange")
            .unwrap();
        assert_eq!(unchanged.table[0][0], 0.65);
    }

    #[test]
    fn test_merge_layer_override_prior() {
        let mut heuristics = HashMap::new();
        merge_layer(
            &mut heuristics,
            vec![LayerClassEntry {
                class: "Redis".into(),
                default_prior: Some(PriorConfig { p_failure: 0.002 }),
                cpts: vec![],
            }],
        );
        // Override just the prior
        merge_layer(
            &mut heuristics,
            vec![LayerClassEntry {
                class: "Redis".into(),
                default_prior: Some(PriorConfig { p_failure: 0.001 }),
                cpts: vec![],
            }],
        );
        assert!((heuristics["Redis"].default_prior.p_failure - 0.001).abs() < 1e-9);
    }

    #[test]
    fn test_merge_layer_append_new_cpt() {
        let mut heuristics = HashMap::new();
        merge_layer(
            &mut heuristics,
            vec![LayerClassEntry {
                class: "Container".into(),
                default_prior: Some(PriorConfig { p_failure: 0.005 }),
                cpts: vec![CptEntry {
                    mutation: "ImageUpdate".into(),
                    signal: "CrashLoopBackOff".into(),
                    table: vec![vec![0.75, 0.03], vec![0.25, 0.97]],
                }],
            }],
        );
        // Add a new CPT to an existing class
        merge_layer(
            &mut heuristics,
            vec![LayerClassEntry {
                class: "Container".into(),
                default_prior: None,
                cpts: vec![CptEntry {
                    mutation: "ConfigChange".into(),
                    signal: "TLSError".into(),
                    table: vec![vec![0.30, 0.02], vec![0.70, 0.98]],
                }],
            }],
        );
        assert_eq!(heuristics["Container"].cpts.len(), 2);
    }

    #[test]
    fn test_merge_layer_new_class_without_prior_gets_default() {
        let mut heuristics = HashMap::new();
        merge_layer(
            &mut heuristics,
            vec![LayerClassEntry {
                class: "Custom".into(),
                default_prior: None,
                cpts: vec![CptEntry {
                    mutation: "Deploy".into(),
                    signal: "error_rate".into(),
                    table: vec![vec![0.60, 0.05], vec![0.40, 0.95]],
                }],
            }],
        );
        // Should get the default prior
        assert!(
            (heuristics["Custom"].default_prior.p_failure - DEFAULT_PRIOR_P_FAILURE).abs() < 1e-9
        );
    }

    #[test]
    fn test_load_heuristics_flat_file() {
        let dir = std::env::temp_dir().join("c9k_test_flat");
        std::fs::create_dir_all(&dir).unwrap();
        let flat_path = dir.join("flat.yaml");
        std::fs::write(
            &flat_path,
            r#"
- class: TestNode
  default_prior:
    P_failure: 0.01
  cpts:
    - mutation: Deploy
      signal: error_rate
      table:
        - [0.70, 0.05]
        - [0.30, 0.95]
"#,
        )
        .unwrap();

        let mut heuristics = HashMap::new();
        load_heuristics_from_path(&mut heuristics, flat_path.to_str().unwrap()).unwrap();
        assert_eq!(heuristics.len(), 1);
        assert!((heuristics["TestNode"].default_prior.p_failure - 0.01).abs() < 1e-9);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_load_heuristics_manifest() {
        let dir = std::env::temp_dir().join("c9k_test_manifest");
        std::fs::create_dir_all(&dir).unwrap();

        // Base layer
        std::fs::write(
            dir.join("base.yaml"),
            r#"
- class: Container
  default_prior:
    P_failure: 0.005
  cpts:
    - mutation: ImageUpdate
      signal: CrashLoopBackOff
      table:
        - [0.75, 0.03]
        - [0.25, 0.97]
"#,
        )
        .unwrap();

        // Override layer
        std::fs::write(
            dir.join("override.yaml"),
            r#"
- class: Container
  cpts:
    - mutation: ImageUpdate
      signal: CrashLoopBackOff
      table:
        - [0.90, 0.03]
        - [0.10, 0.97]
- class: NewService
  default_prior:
    P_failure: 0.004
  cpts: []
"#,
        )
        .unwrap();

        // Manifest
        std::fs::write(
            dir.join("manifest.yaml"),
            r#"
layers:
  - path: base.yaml
  - path: override.yaml
"#,
        )
        .unwrap();

        let mut heuristics = HashMap::new();
        load_heuristics_from_path(&mut heuristics, dir.join("manifest.yaml").to_str().unwrap())
            .unwrap();

        assert_eq!(heuristics.len(), 2);
        // Container's CPT should be overridden
        assert_eq!(heuristics["Container"].cpts[0].table[0][0], 0.90);
        // Prior should be unchanged
        assert!((heuristics["Container"].default_prior.p_failure - 0.005).abs() < 1e-9);
        // New class should exist
        assert!(heuristics.contains_key("NewService"));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_load_heuristics_manifest_optional_layer() {
        let dir = std::env::temp_dir().join("c9k_test_optional");
        std::fs::create_dir_all(&dir).unwrap();

        std::fs::write(
            dir.join("base.yaml"),
            r#"
- class: VM
  default_prior:
    P_failure: 0.002
  cpts: []
"#,
        )
        .unwrap();

        // Manifest with optional missing file
        std::fs::write(
            dir.join("manifest.yaml"),
            r#"
layers:
  - path: base.yaml
  - path: nonexistent.yaml
    optional: true
"#,
        )
        .unwrap();

        let mut heuristics = HashMap::new();
        load_heuristics_from_path(&mut heuristics, dir.join("manifest.yaml").to_str().unwrap())
            .unwrap();
        assert_eq!(heuristics.len(), 1);
        assert!(heuristics.contains_key("VM"));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_load_heuristics_manifest_required_missing_layer_fails() {
        let dir = std::env::temp_dir().join("c9k_test_required_missing");
        std::fs::create_dir_all(&dir).unwrap();

        std::fs::write(
            dir.join("manifest.yaml"),
            r#"
layers:
  - path: nonexistent.yaml
"#,
        )
        .unwrap();

        let mut heuristics = HashMap::new();
        let result =
            load_heuristics_from_path(&mut heuristics, dir.join("manifest.yaml").to_str().unwrap());
        assert!(result.is_err(), "should fail on missing required layer");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_load_real_manifest() {
        // Test loading the actual shipped manifest file
        let manifest_path = "config/heuristics.manifest.yaml";
        if std::path::Path::new(manifest_path).exists() {
            let mut heuristics = HashMap::new();
            load_heuristics_from_path(&mut heuristics, manifest_path).unwrap();
            // Should have all 22 classes from the three layer files
            assert!(
                heuristics.len() >= 22,
                "manifest should load all classes, got {}",
                heuristics.len()
            );
            assert!(heuristics.contains_key("Container"));
            assert!(heuristics.contains_key("ToRSwitch"));
            assert!(heuristics.contains_key("CertAuthority"));
        }
    }

    #[test]
    fn test_load_real_flat_file_backward_compat() {
        // Test backward compatibility with the original flat file
        let flat_path = "config/heuristics.yaml";
        if std::path::Path::new(flat_path).exists() {
            let mut heuristics = HashMap::new();
            load_heuristics_from_path(&mut heuristics, flat_path).unwrap();
            assert!(
                heuristics.len() >= 22,
                "flat file should load all classes, got {}",
                heuristics.len()
            );
        }
    }
}
