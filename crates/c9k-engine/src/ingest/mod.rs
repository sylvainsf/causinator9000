// Copyright (c) 2026 Sylvain Niles. MIT License.

//! GitHub Actions ingestion — pure Rust, no Python.
//!
//! Uses `gh` CLI for GitHub API access (inherits host auth).
//! Fast mode: classifies from job/step names only, no log downloads.

use std::collections::HashSet;
use std::process::Command;

use anyhow::{Context, Result};
use chrono::Utc;
use regex::Regex;
use serde::Deserialize;

use crate::solver::{GraphPayload, Mutation, Signal, SolverHandle};

// ── Error classification patterns ───────────────────────────────────────

struct ClassifierPattern {
    regex: Regex,
    signal: &'static str,
}

macro_rules! patterns {
    ($( ($re:expr, $sig:expr) ),* $(,)?) => {
        vec![ $( ClassifierPattern { regex: Regex::new($re).unwrap(), signal: $sig } ),* ]
    };
}

fn error_patterns() -> Vec<ClassifierPattern> {
    patterns![
        (
            r"(?i)AADSTS\d+|federated identity|Login failed.*az.*exit code|auth-type",
            "AzureAuthFailure"
        ),
        (
            r"(?i)ErrImagePull|ImagePullBackOff|image.*pull.*fail",
            "ImagePullError"
        ),
        (r"(?i)docker.*push.*fail|oras.*push.*fail", "ImagePushError"),
        (r"(?i)command not found|exit code 127", "CommandNotFound"),
        (
            r"(?i)requires a different Python|not in .>=\d",
            "PythonVersionMismatch"
        ),
        (
            r"(?i)invalid array length|tokeninternal\.go|cannot use .* as type",
            "GoToolchainError"
        ),
        (
            r"(?i)go\.mod was committed|go\.sum is out of sync|go mod tidy",
            "GoModCheckFailure"
        ),
        (
            r"(?i)error forwarding port|wincat\.exe.*exit code",
            "PortForwardError"
        ),
        (
            r"(?i)Fail to read Virtual Memory|sys_metric_stat\.go",
            "VirtualMemoryError"
        ),
        (
            r"(?i)connection refused.*dial tcp 127\.0\.0\.1|UNAVAILABLE:.*connection error.*connection refused",
            "GrpcConnectionRefused"
        ),
        (
            r"(?i)failed to apply.*Helm chart.*deadline exceeded|failed to run Helm install.*deadline exceeded|Install Radius.*context deadline",
            "HelmInstallTimeout"
        ),
        (
            r"(?i)timed out|TimeoutException|deadline exceeded|HTTP request timed out",
            "Timeout"
        ),
        (
            r"(?i)actions must be pinned to a full.length commit SHA|not allowed.*must be pinned",
            "ActionPinningViolation"
        ),
        (
            r"(?i)No task list was present|requireChecklist",
            "ChecklistMissing"
        ),
        (
            r"(?i)helm.*fail|chart.*validation.*fail|no such file.*Chart",
            "HelmChartError"
        ),
        (
            r"(?i)bicep.*fail|bicep build.*exit status",
            "BicepBuildError"
        ),
        (r"(?i)Remote workflow failed", "RemoteWorkflowFailure"),
        (
            r"(?i)Dependabot encountered an error|tool_feature_not_supported",
            "DependabotUpdateFailure"
        ),
        (
            r"(?i)No files were found with the provided path.*No artifacts|Create Artifact Container failed|artifact name.*is not valid",
            "ArtifactUploadFailure"
        ),
        (
            r"(?i)Delete artifacts.*fail|Cleanup artifacts.*fail",
            "ArtifactCleanupFailure"
        ),
        (
            r"(?i)Scorecard|scorecard|supply.chain.security",
            "ScorecardFailure"
        ),
        (r"(?i)automerge|auto.merge", "AutomergeFailure"),
        (r"(?i)lint|golangci|clippy|eslint", "LintFailure"),
        (
            r"(?i)Run make test|Run Unit Tests|unit tests",
            "UnitTestFailure"
        ),
        (
            r"(?i)Generating tests for.*devcontainer|devcontainers",
            "DevContainerTestFailure"
        ),
        (
            r"(?i)resource type.*not found|resource provider.*not registered",
            "RadiusStartupFailure"
        ),
        (r"(?i)Process completed with exit code", "TestFailure"),
    ]
}

fn step_name_patterns() -> Vec<ClassifierPattern> {
    patterns![
        (
            r"(?i)disallowed changes in go\.mod|go\.mod.*check|validate go\.mod",
            "GoModCheckFailure"
        ),
        (r"(?i)Check Python|Python.*Examples", "TestFailure"),
        (
            r"(?i)Spin local environment|Setup.*environment|docker-compose",
            "GrpcConnectionRefused"
        ),
        (
            r"(?i)Build.*dev.container|devcontainer",
            "DevContainerTestFailure"
        ),
        (r"(?i)Run make test$|Run Unit Test", "UnitTestFailure"),
        (r"(?i)Run.*integration|test-integration", "TestFailure"),
        (r"(?i)Run E2E|e2e test", "TestFailure"),
        (r"(?i)Run lint|golangci|clippy|eslint", "LintFailure"),
        (
            r"(?i)Preparing.*cluster|Setup.*AKS|Deploy.*infra",
            "Timeout"
        ),
        (r"(?i)Install Radius|Install radius", "HelmInstallTimeout"),
        (r"(?i)Set up job", "ActionPinningViolation"),
        (
            r"(?i)Delete artifacts|Cleanup artifacts",
            "ArtifactCleanupFailure"
        ),
    ]
}

const INFRA_SIGNALS: &[&str] = &[
    "AzureAuthFailure",
    "ImagePullError",
    "Timeout",
    "ImagePushError",
    "RemoteWorkflowFailure",
    "DependabotUpdateFailure",
    "ArtifactUploadFailure",
    "CommandNotFound",
    "PythonVersionMismatch",
    "GoToolchainError",
    "PortForwardError",
    "VirtualMemoryError",
    "GrpcConnectionRefused",
    "ScorecardFailure",
    "AutomergeFailure",
    "HelmInstallTimeout",
    "ActionPinningViolation",
    "ArtifactCleanupFailure",
    "RadiusStartupFailure",
];

fn is_infra_signal(signal: &str) -> bool {
    INFRA_SIGNALS.contains(&signal)
}

/// Returns true if the workflow or job name is a PR policy/validation check
/// that should be excluded from CI failure analysis. These are deterministic
/// author-caused issues (missing labels, checklists, CLA) where root-cause
/// diagnosis adds no value.
fn is_policy_workflow(name: &str) -> bool {
    let n = name.to_lowercase();
    n.contains("checklist")
        || n.contains("required label")
        || n.contains("pr label")
        || n.contains("title check")
        || n.contains("cla")
        || n.contains("dco")
        || n.contains("copilot code review")
        || n.contains("copilot review")
        || n.contains("scorecard")
        || n.contains("automerge")
        || n.contains("auto-merge")
        || n.contains("stale")
        || n.contains("lock thread")
}

fn classify(failed_steps: &[String], workflow_name: &str) -> &'static str {
    let text = format!("{} {}", failed_steps.join(" "), workflow_name);
    let err_pats = error_patterns();
    for p in &err_pats {
        if p.regex.is_match(&text) {
            return p.signal;
        }
    }
    let step_pats = step_name_patterns();
    let step_text = failed_steps.join(" ");
    for p in &step_pats {
        if p.regex.is_match(&step_text) {
            return p.signal;
        }
    }
    "TestFailure"
}

fn detect_runner_os(job_name: &str) -> &'static str {
    let name = job_name.to_lowercase();
    if name.contains("windows") || name.contains("win") || name.contains("ltsc") {
        "windows"
    } else if name.contains("macos") || name.contains("darwin") {
        "macos"
    } else {
        "linux"
    }
}

fn signal_to_latent(signal: &str, job_name: &str) -> &'static str {
    match signal {
        "AzureAuthFailure" => "latent://azure-oidc",
        "ImagePullError" | "ImagePushError" => "latent://ghcr.io",
        "Timeout" | "RemoteWorkflowFailure" | "HelmInstallTimeout" | "RadiusStartupFailure" => {
            "latent://github-actions-infra"
        }
        "ScorecardFailure" => "latent://github-scorecard",
        "AutomergeFailure" => "latent://github-automerge",
        "ActionPinningViolation" | "ArtifactCleanupFailure" => "latent://github-actions-infra",
        _ => match detect_runner_os(job_name) {
            "windows" => "latent://runner-env/windows",
            "macos" => "latent://runner-env/macos",
            _ => "latent://runner-env/linux",
        },
    }
}

fn detect_mutation_type(
    message: &str,
    author: &str,
    event: &str,
    workflow_name: &str,
    head_branch: &str,
    files: &[String],
) -> &'static str {
    let msg = message.to_lowercase();
    let auth = author.to_lowercase();
    let wf = workflow_name.to_lowercase();
    let branch = head_branch.to_lowercase();

    // Dependabot: detect by author, event type, workflow name, or branch
    let is_dependabot = auth.contains("dependabot")
        || event == "dynamic"
            && (wf.contains("npm_and_yarn")
                || wf.contains("dependabot")
                || wf.contains("pip")
                || wf.contains("cargo")
                || wf.contains("gomod"))
        || branch.starts_with("dependabot/");

    if is_dependabot {
        if wf.contains("github-actions") || msg.contains("github-actions") {
            return "DepActionsBump";
        }
        if msg.contains("from") && msg.contains("to") {
            return "DepMajorBump";
        }
        return "DependencyUpdate";
    }
    if msg.contains("release") || event == "release" {
        return "Release";
    }
    if msg.contains("revert") {
        return "Revert";
    }

    // File-based classification: what did the commit actually touch?
    if !files.is_empty() {
        let has_workflow = files
            .iter()
            .any(|f| f.starts_with(".github/workflows/") || f.starts_with(".github/actions/"));
        let _has_ci_config = files.iter().any(|f| {
            f.contains("Makefile")
                || f.contains("Dockerfile")
                || f.contains("docker-compose")
                || f.ends_with(".yml")
                    && (f.contains("ci") || f.contains("lint") || f.contains("test"))
        });
        let has_deps = files.iter().any(|f| {
            f.ends_with("go.mod")
                || f.ends_with("go.sum")
                || f.ends_with("package.json")
                || f.ends_with("package-lock.json")
                || f.ends_with("pnpm-lock.yaml")
                || f.ends_with("Cargo.toml")
                || f.ends_with("Cargo.lock")
                || f.ends_with("requirements.txt")
                || f.ends_with("pyproject.toml")
        });
        let has_source = files.iter().any(|f| {
            f.ends_with(".go")
                || f.ends_with(".rs")
                || f.ends_with(".ts")
                || f.ends_with(".js")
                || f.ends_with(".py")
                || f.ends_with(".java")
                || f.ends_with(".cs")
        });
        let has_only_docs = files.iter().all(|f| {
            f.ends_with(".md")
                || f.ends_with(".txt")
                || f.ends_with(".rst")
                || f.starts_with("docs/")
                || f.starts_with("doc/")
        });

        if has_only_docs {
            return "DocsOnly";
        }
        if has_workflow && !has_source {
            return "WorkflowChange";
        }
        if has_workflow && has_source {
            return "CodeAndWorkflowChange";
        }
        if has_deps && !has_source {
            return "DependencyFileChange";
        }
    }

    "CodeChange"
}

// ── GitHub API types ────────────────────────────────────────────────────

#[derive(Deserialize)]
struct GhRun {
    #[serde(rename = "databaseId")]
    id: u64,
    #[serde(rename = "headSha")]
    head_sha: String,
    #[serde(rename = "workflowName")]
    workflow_name: String,
    #[serde(default)]
    _conclusion: String,
    #[serde(rename = "createdAt")]
    created_at: String,
    #[serde(rename = "updatedAt")]
    updated_at: String,
    #[serde(default, rename = "headBranch")]
    head_branch: String,
    #[serde(default)]
    event: String,
}

#[derive(Deserialize)]
struct GhJob {
    name: String,
    #[serde(default)]
    _id: u64,
    #[serde(default)]
    failed_steps: Vec<String>,
}

#[derive(Deserialize)]
struct GhCommit {
    commit: GhCommitInner,
    #[serde(default)]
    files: Vec<GhCommitFile>,
}

#[derive(Deserialize)]
struct GhCommitInner {
    message: String,
    author: GhAuthor,
}

#[derive(Deserialize)]
struct GhCommitFile {
    filename: String,
}

#[derive(Deserialize)]
struct GhAuthor {
    name: String,
}

// ── gh CLI helpers ──────────────────────────────────────────────────────

fn gh_command(args: &[&str]) -> Result<String> {
    let output = Command::new("gh")
        .args(args)
        .env("GH_PAGER", "cat")
        .output()
        .context("gh CLI not found — install from https://cli.github.com")?;
    if !output.status.success() {
        let err = String::from_utf8_lossy(&output.stderr);
        anyhow::bail!("gh failed: {err}");
    }
    Ok(String::from_utf8_lossy(&output.stdout).to_string())
}

fn get_runs(repo: &str, hours: u32) -> Result<Vec<GhRun>> {
    let cutoff = Utc::now() - chrono::Duration::hours(hours as i64);
    let mut all_runs = Vec::new();

    // Paginate through the REST API, fetching only failures.
    for page in 1..=50 {
        let url = format!("repos/{repo}/actions/runs?status=failure&per_page=100&page={page}");
        let output = gh_command(&[
            "api",
            &url,
            "--jq",
            r#"[.workflow_runs[] | {databaseId: .id, headSha: .head_sha, workflowName: .name, conclusion, createdAt: .created_at, updatedAt: .updated_at, headBranch: .head_branch, event}]"#,
        ])?;

        let page_runs: Vec<GhRun> = serde_json::from_str(output.trim()).unwrap_or_default();

        if page_runs.is_empty() {
            break;
        }

        // Results are newest-first; once we see a run older than cutoff, stop.
        let mut reached_cutoff = false;
        for run in page_runs {
            let in_window = chrono::DateTime::parse_from_rfc3339(&run.created_at)
                .map(|t| t >= cutoff)
                .unwrap_or(false);
            if in_window {
                all_runs.push(run);
            } else {
                reached_cutoff = true;
            }
        }

        if reached_cutoff {
            break;
        }
    }

    Ok(all_runs)
}

fn get_failed_jobs(repo: &str, run_id: u64) -> Result<Vec<GhJob>> {
    let output = gh_command(&[
        "api",
        &format!("repos/{repo}/actions/runs/{run_id}/jobs"),
        "--jq",
        r#".jobs[] | select(.conclusion == "failure") | {name, id, failed_steps: [.steps[] | select(.conclusion == "failure") | .name]}"#,
    ])?;
    let mut jobs = Vec::new();
    for line in output.lines() {
        if let Ok(job) = serde_json::from_str::<GhJob>(line) {
            jobs.push(job);
        }
    }
    Ok(jobs)
}

/// Commit info: (message, author, changed_files)
fn get_commit_info(repo: &str, sha: &str) -> Result<(String, String, Vec<String>)> {
    let output = gh_command(&["api", &format!("repos/{repo}/commits/{sha}")])?;
    let commit: GhCommit = serde_json::from_str(&output)?;
    let msg = commit
        .commit
        .message
        .lines()
        .next()
        .unwrap_or("")
        .to_string();
    let files: Vec<String> = commit.files.iter().map(|f| f.filename.clone()).collect();
    Ok((msg, commit.commit.author.name, files))
}

// ── Main ingestion ──────────────────────────────────────────────────────

/// Result from GitHub Actions ingestion, including metadata for report formatting.
pub struct IngestResult {
    /// Human-readable ingestion summary (for stderr/logging).
    pub report: String,
    /// SHA (8-char) → head branch name.
    pub commit_branches: std::collections::HashMap<String, String>,
    /// SHA (8-char) → (commit message first line, author name, changed files).
    pub commit_info: std::collections::HashMap<String, (String, String, Vec<String>)>,
    /// Number of policy/validation workflows skipped.
    pub skipped_policy: usize,
}

/// Ingest GitHub Actions failures into the solver.
pub fn ingest_github(solver: &SolverHandle, repo: &str, hours: u32) -> Result<IngestResult> {
    // Auto-expand the temporal window if the ingestion window exceeds it
    let hours_mins = (hours as i64) * 60;
    if solver.get_temporal_window().is_ok_and(|w| hours_mins > w) {
        let _ = solver.set_temporal_window(hours_mins);
    }

    let runs = get_runs(repo, hours)?;
    if runs.is_empty() {
        return Ok(IngestResult {
            report: format!("No failures found for {repo} in the last {hours}h."),
            commit_branches: std::collections::HashMap::new(),
            commit_info: std::collections::HashMap::new(),
            skipped_policy: 0,
        });
    }

    let mut report = format!(
        "Fetching from {repo} (last {hours}h)...\n{} failure runs to process\n",
        runs.len()
    );

    let mut skipped_policy = 0usize;

    // Latent nodes
    let latent_ids = [
        ("latent://azure-oidc", "Azure OIDC", "IdentityProvider"),
        ("latent://ghcr.io", "GHCR", "ContainerRegistry"),
        (
            "latent://github-actions-infra",
            "GitHub Actions Infra",
            "CIPlatform",
        ),
        ("latent://flaky-tests", "Flaky Tests", "FlakyTest"),
        (
            "latent://runner-env/linux",
            "Runner (Linux)",
            "RunnerEnvironment",
        ),
        (
            "latent://runner-env/windows",
            "Runner (Windows)",
            "RunnerEnvironment",
        ),
        (
            "latent://runner-env/macos",
            "Runner (macOS)",
            "RunnerEnvironment",
        ),
        ("latent://github-scorecard", "Scorecard", "CIPlatform"),
        ("latent://github-automerge", "Automerge", "CIPlatform"),
    ];

    let mut nodes = Vec::new();
    let mut edges = Vec::new();

    for (id, label, class) in &latent_ids {
        nodes.push(serde_json::json!({
            "id": id, "label": label, "class": class,
            "region": "github", "rack_id": null,
            "properties": {"source": "gh-actions", "latent": true},
        }));
    }

    let mut mutations = Vec::new();
    let mut signals = Vec::new();
    let mut seen_commits: HashSet<String> = HashSet::new();
    let mut seen_jobs: HashSet<(String, String, String)> = HashSet::new();
    let mut commit_cache: std::collections::HashMap<String, (String, String, Vec<String>)> =
        std::collections::HashMap::new();
    // Track (job_slug, signal_type) → set of commits for cross-commit flaky detection
    let mut job_commit_map: std::collections::HashMap<(String, String), HashSet<String>> =
        std::collections::HashMap::new();
    // Track SHA → branch for PR grouping in reports
    let mut commit_branches: std::collections::HashMap<String, String> =
        std::collections::HashMap::new();

    for run in &runs {
        let sha8 = &run.head_sha[..8.min(run.head_sha.len())];
        let wf = &run.workflow_name;

        // Skip policy/validation workflows — these are author errors, not diagnosable
        if is_policy_workflow(wf) {
            skipped_policy += 1;
            continue;
        }

        // Get commit info (cached)
        if !commit_cache.contains_key(sha8) {
            let info = get_commit_info(repo, &run.head_sha)
                .unwrap_or_else(|_| ("unknown".into(), "unknown".into(), vec![]));
            commit_cache.insert(sha8.to_string(), info);
        }
        // Track branch (first seen wins, since a SHA usually appears on one branch)
        commit_branches
            .entry(sha8.to_string())
            .or_insert_with(|| run.head_branch.clone());
        let (msg, author, files) = commit_cache.get(sha8).unwrap();
        let mut_type = detect_mutation_type(
            msg,
            author,
            &run.event,
            &run.workflow_name,
            &run.head_branch,
            files,
        );

        // Get failed jobs
        let jobs = get_failed_jobs(repo, run.id).unwrap_or_default();
        let jobs = if jobs.is_empty() {
            vec![GhJob {
                name: wf.clone(),
                _id: 0,
                failed_steps: vec![],
            }]
        } else {
            jobs
        };

        for job in &jobs {
            // Skip policy/validation jobs within otherwise valid workflows
            if is_policy_workflow(&job.name) {
                continue;
            }

            let signal_type = classify(&job.failed_steps, wf);
            let is_infra = is_infra_signal(signal_type);

            // Dedup
            let job_slug = job
                .name
                .to_lowercase()
                .replace(|c: char| !c.is_alphanumeric(), "-");
            let dedup_key = (sha8.to_string(), job_slug.clone(), signal_type.to_string());
            if seen_jobs.contains(&dedup_key) {
                continue;
            }
            seen_jobs.insert(dedup_key);

            // Track for cross-commit flaky detection
            job_commit_map
                .entry((job_slug.clone(), signal_type.to_string()))
                .or_default()
                .insert(sha8.to_string());

            // Job node
            let jid = format!("job://{repo}/{}/{job_slug}", run.id);
            nodes.push(serde_json::json!({
                "id": &jid, "label": format!("{wf}: {}", job.name), "class": "CIJob",
                "region": "github", "rack_id": null,
                "properties": {"source": "gh-actions", "run_id": run.id, "commit": sha8},
            }));

            if is_infra {
                let latent = signal_to_latent(signal_type, &job.name);
                edges.push(serde_json::json!({
                    "id": format!("edge-{}-{}", &latent[9..], &jid[jid.len().saturating_sub(30)..]),
                    "source_id": latent, "target_id": &jid,
                    "edge_type": "dependency", "properties": {},
                }));
                signals.push(Signal {
                    id: uuid::Uuid::new_v4().to_string(),
                    node_id: jid.clone(),
                    signal_type: signal_type.to_string(),
                    value: None,
                    severity: Some("critical".to_string()),
                    timestamp: chrono::DateTime::parse_from_rfc3339(&run.updated_at)
                        .map(|t| t.with_timezone(&Utc))
                        .unwrap_or_else(|_| Utc::now()),
                    properties: serde_json::json!({}),
                });
                // Runner-env gets a mutation
                if latent.starts_with("latent://runner-env/") {
                    mutations.push(Mutation {
                        id: uuid::Uuid::new_v4().to_string(),
                        node_id: latent.to_string(),
                        mutation_type: "RunnerImageUpdate".to_string(),
                        source: format!("gh-actions/{repo}"),
                        timestamp: chrono::DateTime::parse_from_rfc3339(&run.created_at)
                            .map(|t| t.with_timezone(&Utc))
                            .unwrap_or_else(|_| Utc::now()),
                        properties: serde_json::json!({}),
                    });
                }
            } else {
                // Code failure
                let cid = format!("commit://{repo}/{sha8}");
                if !seen_commits.contains(&cid) {
                    seen_commits.insert(cid.clone());
                    nodes.push(serde_json::json!({
                        "id": &cid, "label": format!("{sha8}: {}", &msg[..60.min(msg.len())]),
                        "class": "Commit", "region": "github", "rack_id": null,
                        "properties": {"source": "gh-actions", "sha": sha8, "author": author},
                    }));
                    mutations.push(Mutation {
                        id: uuid::Uuid::new_v4().to_string(),
                        node_id: cid.clone(),
                        mutation_type: mut_type.to_string(),
                        source: format!("gh-actions/{repo}"),
                        timestamp: chrono::DateTime::parse_from_rfc3339(&run.created_at)
                            .map(|t| t.with_timezone(&Utc))
                            .unwrap_or_else(|_| Utc::now()),
                        properties: serde_json::json!({}),
                    });
                }
                edges.push(serde_json::json!({
                    "id": format!("edge-{}-{}", &cid[cid.len().saturating_sub(20)..], &jid[jid.len().saturating_sub(30)..]),
                    "source_id": &cid, "target_id": &jid,
                    "edge_type": "dependency", "properties": {},
                }));
                signals.push(Signal {
                    id: uuid::Uuid::new_v4().to_string(),
                    node_id: jid.clone(),
                    signal_type: signal_type.to_string(),
                    value: None,
                    severity: Some("critical".to_string()),
                    timestamp: chrono::DateTime::parse_from_rfc3339(&run.updated_at)
                        .map(|t| t.with_timezone(&Utc))
                        .unwrap_or_else(|_| Utc::now()),
                    properties: serde_json::json!({}),
                });

                // Flaky test competing cause — for test-like failures
                if signal_type == "TestFailure"
                    || signal_type == "UnitTestFailure"
                    || signal_type == "HelmInstallTimeout"
                    || signal_type == "RadiusStartupFailure"
                {
                    edges.push(serde_json::json!({
                        "id": format!("edge-flaky-{}", &jid[jid.len().saturating_sub(30)..]),
                        "source_id": "latent://flaky-tests", "target_id": &jid,
                        "edge_type": "dependency", "properties": {},
                    }));
                    mutations.push(Mutation {
                        id: uuid::Uuid::new_v4().to_string(),
                        node_id: "latent://flaky-tests".to_string(),
                        mutation_type: "FlakyTestRun".to_string(),
                        source: format!("gh-actions/{repo}"),
                        timestamp: chrono::DateTime::parse_from_rfc3339(&run.created_at)
                            .map(|t| t.with_timezone(&Utc))
                            .unwrap_or_else(|_| Utc::now()),
                        properties: serde_json::json!({}),
                    });
                }
            }
        }
    }

    // Cross-commit flaky detection: if the same job+signal appears on 3+
    // different commits, add extra FlakyTestRun mutations to boost the
    // flaky-test prior for those jobs.
    for ((job_slug, _signal_type), commits) in &job_commit_map {
        if commits.len() >= 3 {
            // This pattern is strongly indicative of flakiness: same job
            // failing the same way on multiple independent commits.
            for _ in 0..commits.len() {
                mutations.push(Mutation {
                    id: uuid::Uuid::new_v4().to_string(),
                    node_id: "latent://flaky-tests".to_string(),
                    mutation_type: "FlakyTestRun".to_string(),
                    source: format!("gh-actions/{repo}/cross-commit/{job_slug}"),
                    timestamp: Utc::now(),
                    properties: serde_json::json!({}),
                });
            }
        }
    }

    // Merge topology
    let payload: GraphPayload = serde_json::from_value(serde_json::json!({
        "nodes": nodes, "edges": edges,
    }))?;
    let (_, _, new_nodes, new_edges) = solver.merge_graph(payload)?;
    report.push_str(&format!(
        "Topology: {new_nodes} new nodes, {new_edges} new edges\n"
    ));

    // Ingest mutations
    let mut mut_count = 0;
    for m in mutations {
        if solver.ingest_mutation(m).is_ok() {
            mut_count += 1;
        }
    }

    // Ingest signals
    let mut sig_count = 0;
    for s in signals {
        if solver.ingest_signal(s).is_ok() {
            sig_count += 1;
        }
    }

    report.push_str(&format!(
        "Ingested: {mut_count} mutations, {sig_count} signals\n"
    ));
    if skipped_policy > 0 {
        report.push_str(&format!(
            "Skipped: {skipped_policy} policy/validation workflows (checklists, labels, CLA, etc.)\n"
        ));
    }
    Ok(IngestResult {
        report,
        commit_branches,
        commit_info: commit_cache,
        skipped_policy,
    })
}
