#!/usr/bin/env python3
"""
GitHub Actions → Causinator 9000 causal graph adapter (v3).

Correct causal model:
  - NODES are failed jobs (the unit of observation, "this specific thing broke")
  - SIGNALS are the classified failure type on the job node
  - MUTATIONS go on the upstream cause:
      Code failures → commit node (the code change caused it)
      Infra failures → latent node (OIDC, GHCR, runner infra)
      Flaky tests → latent FlakyTest node (competing cause)
  - EDGES connect causes to job nodes

Only failed jobs become nodes. Successful runs don't pollute the graph.

Graph example:
  commit://repo/9f403647 ──(CodeChange)──→ job://repo/22797031763/run-functional-tests
                                            signal: TestFailure
  latent://azure-oidc    ──(competing)──→ job://repo/22797031763/run-functional-tests
  latent://flaky-tests   ──(competing)──→ job://repo/22797031763/run-functional-tests

  latent://azure-oidc    ──(?)──→ job://repo/22798093791/ado
                                   signal: AzureAuthFailure
                                   (no known mutation → low confidence)

Usage:
  python3 sources/gh_actions_source.py --repo project-radius/radius --hours 48
  python3 sources/gh_actions_source.py --repo project-radius/radius -s $AZURE_SUB_ID
  python3 sources/gh_actions_source.py --repo project-radius/radius --dry-run
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

ENGINE = os.environ.get("C9K_ENGINE_URL", "http://localhost:8080")

# ── Error classification: what signal type does this failure produce? ────

ERROR_PATTERNS = [
    # (regex on error lines + step names, signal_type)
    # Order matters, first match wins. More specific patterns first.
    (r"AADSTS\d+|federated identity|Login failed.*az.*exit code|auth-type|Login to Azure.*fail|azure.login.*fail|AZURE_.*not set|azure.*credentials.*error",
     "AzureAuthFailure"),
    (r"ErrImagePull|ImagePullBackOff|image.*pull.*fail",
     "ImagePullError"),
    (r"docker.*push.*fail|oras.*push.*fail",
     "ImagePushError"),
    # Runner environment / provisioning issues (before generic patterns)
    (r"Current runner version.*Runner Image Provisioner|Hosted Compute Agent.*exit|runner provisioning|Runner\.Worker.*fail",
     "RunnerFailure"),
    (r"command not found|exit code 127",
     "CommandNotFound"),
    (r"requires a different Python|not in .>=\d",
     "PythonVersionMismatch"),
    (r"invalid array length|tokeninternal\.go|cannot use .* as type",
     "GoToolchainError"),
    (r"go\.mod was committed|go\.sum is out of sync|go mod tidy",
     "GoModCheckFailure"),
    (r"error forwarding port|wincat\.exe.*exit code",
     "PortForwardError"),
    (r"Fail to read Virtual Memory|sys_metric_stat\.go",
     "VirtualMemoryError"),
    (r"connection refused.*dial tcp 127\.0\.0\.1|UNAVAILABLE:.*connection error.*connection refused",
     "GrpcConnectionRefused"),
    (r"timed out|TimeoutException|deadline exceeded|HTTP request timed out",
     "Timeout"),
    (r"No task list was present|requireChecklist",
     "ChecklistMissing"),
    (r"helm.*fail|chart.*validation.*fail|no such file.*Chart",
     "HelmChartError"),
    (r"bicep.*fail|bicep build.*exit status",
     "BicepBuildError"),
    (r"Remote workflow failed",
     "RemoteWorkflowFailure"),
    (r"Dependabot encountered an error",
     "DependabotUpdateFailure"),
    (r"No files were found with the provided path.*No artifacts|Create Artifact Container failed|artifact name.*is not valid",
     "ArtifactUploadFailure"),
    (r"Scorecard|scorecard|supply.chain.security",
     "ScorecardFailure"),
    (r"automerge|auto.merge",
     "AutomergeFailure"),
    (r"lint|golangci|clippy|eslint",
     "LintFailure"),
    (r"Run make test|Run Unit Tests|unit tests",
     "UnitTestFailure"),
    (r"Generating tests for.*devcontainer|devcontainers",
     "DevContainerTestFailure"),
    (r"Process completed with exit code",
     "TestFailure"),  # generic, tests are the most common non-specific failure
]

# Workflow-level patterns: when the error doesn't match anything specific,
# these are checked against the workflow name to produce a better-than-generic
# signal type.
WORKFLOW_FALLBACK_SIGNALS = {
    # Workflows whose failures are typically workflow-config issues,
    # not code or test failures.
    "release": "WorkflowConfigFailure",
    "deploy": "WorkflowConfigFailure",
    "publish": "WorkflowConfigFailure",
}

# Step-name patterns: used when step names are the primary classification signal
# (fast mode or when error lines are sparse). Checked against failed step names.
STEP_NAME_PATTERNS = [
    (r"disallowed changes in go\.mod|go\.mod.*check|validate go\.mod",
     "GoModCheckFailure"),
    (r"Check Python|Python.*Examples",
     "TestFailure"),   # Could be PythonVersionMismatch but need logs to confirm
    (r"Spin local environment|Setup.*environment|docker-compose",
     "GrpcConnectionRefused"),  # Environment spin-up failures
    (r"Build.*dev.container|devcontainer",
     "DevContainerTestFailure"),
    (r"Run make test$|Run Unit Test",
     "UnitTestFailure"),
    (r"Run.*integration|test-integration",
     "TestFailure"),
    (r"Run E2E|e2e test",
     "TestFailure"),
    (r"Run lint|golangci|clippy|eslint",
     "LintFailure"),
    (r"Login to Azure|azure.login|__azure_login",
     "AzureAuthFailure"),
    (r"Preparing.*cluster|Setup.*AKS|Deploy.*infra",
     "Timeout"),
]

# ── Failure attribution: is this a code problem or an infra problem? ─────

INFRA_SIGNALS = {"AzureAuthFailure", "ImagePullError", "Timeout", "ImagePushError",
                 "RemoteWorkflowFailure", "DependabotUpdateFailure", "ArtifactUploadFailure",
                 "CommandNotFound", "PythonVersionMismatch", "GoToolchainError",
                 "PortForwardError", "VirtualMemoryError", "GrpcConnectionRefused",
                 "ScorecardFailure", "AutomergeFailure", "WorkflowConfigFailure",
                 "RunnerFailure"}
CODE_SIGNALS = {"TestFailure", "HelmChartError", "BicepBuildError",
                "UnitTestFailure", "DevContainerTestFailure", "GoModCheckFailure",
                "LintFailure"}
# TestFailure also gets a FlakyTest competing cause

# ── Latent infrastructure nodes ──────────────────────────────────────────

LATENT_NODES = {
    "latent://azure-oidc": {
        "label": "Azure OIDC / Federated Credentials",
        "class": "IdentityProvider",
    },
    "latent://ghcr.io": {
        "label": "GitHub Container Registry (GHCR)",
        "class": "ContainerRegistry",
    },
    "latent://github-actions-infra": {
        "label": "GitHub Actions Infrastructure",
        "class": "CIPlatform",
    },
    "latent://flaky-tests": {
        "label": "Flaky / Non-deterministic Tests",
        "class": "FlakyTest",
    },
    "latent://runner-env/linux": {
        "label": "GitHub Runner Environment (Linux)",
        "class": "RunnerEnvironment",
    },
    "latent://runner-env/windows": {
        "label": "GitHub Runner Environment (Windows)",
        "class": "RunnerEnvironment",
    },
    "latent://runner-env/macos": {
        "label": "GitHub Runner Environment (macOS)",
        "class": "RunnerEnvironment",
    },
    "latent://github-scorecard": {
        "label": "GitHub Scorecard / Supply Chain Security",
        "class": "CIPlatform",
    },
    "latent://github-automerge": {
        "label": "GitHub Automerge Infrastructure",
        "class": "CIPlatform",
    },
    "latent://workflow-config": {
        "label": "Workflow Configuration Issue",
        "class": "CIPlatform",
    },
    "latent://runner-failure": {
        "label": "GitHub Actions Runner Failure",
        "class": "RunnerEnvironment",
    },
}

# Map infra signal types to which latent node is the likely cause.
# Signals mapped to None use OS-specific runner-env nodes (resolved at runtime).
SIGNAL_TO_LATENT = {
    "AzureAuthFailure": "latent://azure-oidc",
    "ImagePullError": "latent://ghcr.io",
    "ImagePushError": "latent://ghcr.io",
    "Timeout": "latent://github-actions-infra",
    "RemoteWorkflowFailure": "latent://github-actions-infra",
    "CommandNotFound": None,         # runner-env (OS-specific)
    "PythonVersionMismatch": None,   # runner-env (OS-specific)
    "GoToolchainError": None,        # runner-env (OS-specific)
    "PortForwardError": None,        # runner-env (OS-specific)
    "VirtualMemoryError": None,      # runner-env (OS-specific)
    "GrpcConnectionRefused": None,   # runner-env (OS-specific)
    "ScorecardFailure": "latent://github-scorecard",
    "AutomergeFailure": "latent://github-automerge",
    "WorkflowConfigFailure": "latent://workflow-config",
    "RunnerFailure": "latent://runner-failure",
}

# ── Workflow → Azure resource dependencies ───────────────────────────────

WORKFLOW_AZURE_DEPS = {
    "Functional Tests (with Cloud Resources)": [
        "resourcegroups/radiusfunctionaltest",
        "providers/microsoft.keyvault/vaults/radiuskvvoltest",
    ],
    "Long-running test on Azure": [
        "resourcegroups/radlrtest00",
        "providers/microsoft.containerservice/managedclusters/radlrtest00-aks",
    ],
    "Purge Azure test resources": [
        "resourcegroups/radiusfunctionaltest",
    ],
    "Release Radius": [
        "providers/microsoft.keyvault/vaults/radius-accounts",
    ],
}


# ── Helpers ──────────────────────────────────────────────────────────────

def post_engine(path: str, payload: dict, engine: str) -> dict | None:
    import urllib.request
    url = f"{engine}/api/{path}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  ERROR posting to {url}: {e}", file=sys.stderr)
        return None


def get_workflow_runs(repo: str, hours: int, limit: int) -> list[dict]:
    cmd = ["gh", "run", "list", "--repo", repo, "--limit", str(limit),
           "--status", "failure",
           "--json", "databaseId,name,status,conclusion,createdAt,updatedAt,"
                     "headBranch,headSha,workflowName,event,url"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        print(f"ERROR: gh run list failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    runs = json.loads(result.stdout)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return [r for r in runs
            if datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00")) >= cutoff]


def get_failed_jobs(repo: str, run_id: int) -> list[dict]:
    cmd = ["gh", "run", "view", str(run_id), "--repo", repo, "--json", "jobs"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return []
    data = json.loads(result.stdout)
    return [
        {"name": j["name"],
         "failed_steps": [s["name"] for s in j.get("steps", [])
                          if s.get("conclusion") == "failure"]}
        for j in data.get("jobs", []) if j.get("conclusion") == "failure"
    ]


def get_error_lines(repo: str, run_id: int) -> list[str]:
    cmd = ["gh", "run", "view", str(run_id), "--repo", repo, "--log-failed"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0 or not result.stdout.strip():
        # Fallback: use the jobs API to get failed step names as context.
        # This handles pull_request_target and other events where log
        # download permissions differ.
        return _get_error_context_from_jobs_api(repo, run_id)
    errors = []
    for line in result.stdout.split("\n"):
        if re.search(r"##\[error\]", line, re.IGNORECASE):
            clean = re.sub(r"^.*?##\[error\]", "", line).strip()
            if clean:
                errors.append(clean)
    if not errors:
        return _get_error_context_from_jobs_api(repo, run_id)
    return errors[:15]


def _get_error_context_from_jobs_api(repo: str, run_id: int) -> list[str]:
    """Fallback when logs aren't available: extract context from job/step metadata."""
    env = {**os.environ, "GH_PAGER": "cat"}
    cmd = [
        "gh", "api", f"repos/{repo}/actions/runs/{run_id}/jobs",
        "--jq", '[.jobs[] | select(.conclusion == "failure") | '
                '{name, steps: [.steps[] | select(.conclusion == "failure") | .name]}]'
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=env)
        if result.returncode != 0:
            return []
        jobs = json.loads(result.stdout)
        context = []
        for j in jobs:
            for step in j.get("steps", []):
                context.append(f"Failed step: {step}")
            context.append(f"Failed job: {j.get('name', '')}")
        return context
    except Exception:
        return []


def get_failed_jobs_fast(repo: str, run_id: int) -> list[dict]:
    """Fast path: use the REST API directly to get jobs + failed steps.
    
    Returns the same format as get_failed_jobs but uses gh api instead of
    gh run view, and includes the job ID for potential log follow-up.
    """
    env = {**os.environ, "GH_PAGER": "cat"}
    cmd = ["gh", "api", f"repos/{repo}/actions/runs/{run_id}/jobs",
           "--jq", '.jobs[] | select(.conclusion == "failure") | '
                   '{name, id, failed_steps: [.steps[] | select(.conclusion == "failure") | .name]}']
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=env)
    if result.returncode != 0:
        return []
    jobs = []
    for line in result.stdout.strip().split("\n"):
        if line.strip():
            try:
                jobs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return jobs


def get_commit_info(repo: str, sha: str) -> dict:
    env = {**os.environ, "GH_PAGER": "cat"}
    cmd = ["gh", "api", f"repos/{repo}/commits/{sha}"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=env)
    if result.returncode != 0:
        return {"sha": sha[:8], "message": "unknown", "author": "unknown", "date": ""}
    c = json.loads(result.stdout)
    msg = c.get("commit", {}).get("message", "").split("\n")[0][:120]
    author = c.get("commit", {}).get("author", {}).get("name", "unknown")
    date = c.get("commit", {}).get("author", {}).get("date", "")
    return {"sha": sha[:8], "message": msg, "author": author, "date": date}


def classify_error(error_lines: list[str], failed_steps: list[str],
                   workflow_name: str = "") -> str:
    """Classify failure into a signal type from actual error messages."""

    text = " ".join(error_lines) + " " + " ".join(failed_steps) + " " + workflow_name
    for pattern, signal_type in ERROR_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return signal_type
    # Fallback: match step names specifically (fast mode)
    step_text = " ".join(failed_steps)
    if step_text:
        for pattern, signal_type in STEP_NAME_PATTERNS:
            if re.search(pattern, step_text, re.IGNORECASE):
                return signal_type
    # Workflow-level fallback: if the workflow name suggests a non-test
    # purpose (release, deploy, publish), classify as WorkflowConfigFailure
    # rather than TestFailure, the user can investigate the workflow config.
    wf_lower = workflow_name.lower()
    for keyword, signal_type in WORKFLOW_FALLBACK_SIGNALS.items():
        if keyword in wf_lower:
            return signal_type
    return "TestFailure"


def detect_mutation_type(commit_info: dict, event: str) -> str:
    """Classify the code change type from commit message + author."""
    msg = commit_info.get("message", "")
    msg_lower = msg.lower()
    author = commit_info.get("author", "").lower()

    # Empty/retrigger commits, not a real code change
    if re.match(r'^(empty commit|retrigger|re-trigger|retry|re-run|trigger ci|ci retry)\s*$',
                msg_lower.strip()):
        return "CIRetrigger"

    if "dependabot" in author:
        if "github-actions" in msg_lower:
            return "DepActionsBump"
        count_match = re.search(r'with\s+(\d+)\s+update', msg_lower)
        if count_match and int(count_match.group(1)) >= 5:
            return "DepGroupUpdate"
        version_match = re.search(
            r'from\s+v?(\d+)\.\d+\S*\s+to\s+v?(\d+)\.\d+', msg)
        if version_match and int(version_match.group(2)) > int(version_match.group(1)):
            return "DepMajorBump"
        if version_match:
            return "DepMinorBump"
        if "go-dependencies" in msg_lower:
            return "DepGroupUpdate"
        return "DependencyUpdate"

    if "release" in msg_lower or event == "release":
        return "Release"
    if "revert" in msg_lower:
        return "Revert"
    return "CodeChange"


def job_node_id(repo: str, run_id: int, job_name: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '-', job_name.lower()).strip('-')
    return f"job://{repo}/{run_id}/{slug}"


def commit_node_id(repo: str, sha: str) -> str:
    return f"commit://{repo}/{sha[:8]}"


def detect_runner_os(job_name: str) -> str:
    """Infer the runner OS from the job name."""
    name = job_name.lower()
    if "windows" in name or "win" in name or "ltsc" in name:
        return "windows"
    if "macos" in name or "darwin" in name:
        return "macos"
    return "linux"


def runner_env_latent(job_name: str) -> str:
    """Return the OS-specific runner-env latent node for a job."""
    return f"latent://runner-env/{detect_runner_os(job_name)}"


def get_workflow_flaky_rate(repo: str, workflow_name: str, limit: int = 20) -> float:
    """Query recent pass/fail ratio for a workflow to estimate flaky rate."""
    env = {**os.environ, "GH_PAGER": "cat"}
    cmd = ["gh", "run", "list", "--repo", repo,
           "--workflow", workflow_name, "--limit", str(limit),
           "--json", "conclusion"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=env)
        if result.returncode != 0:
            return 0.1  # default
        runs = json.loads(result.stdout)
        if not runs:
            return 0.1
        failures = sum(1 for r in runs if r.get("conclusion") == "failure")
        return max(failures / len(runs), 0.01)  # floor at 1%
    except Exception:
        return 0.1


def get_ancestor_commits(repo: str, sha: str, hours: int,
                         commit_cache: dict) -> list[dict]:
    """Fetch recent commits on the branch ending at sha, bounded by age.

    Returns list of commit info dicts (newest first), stopping when a
    commit's author date is older than `hours` hours from now.
    """
    env = {**os.environ, "GH_PAGER": "cat"}
    cmd = ["gh", "api", f"repos/{repo}/commits",
           "--method", "GET",
           "-f", f"sha={sha}", "-f", "per_page=30",
           "--jq", '[.[] | {sha: .sha, message: (.commit.message | split("\n")[0])[:120], '
                   'author: .commit.author.name, date: .commit.author.date}]']
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=env)
        if result.returncode != 0:
            return []
        commits = json.loads(result.stdout)
    except Exception:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    ancestors = []
    for c in commits[1:]:  # skip the HEAD commit (already processed)
        sha8 = c["sha"][:8]
        date_str = c.get("date", "")
        if date_str:
            try:
                commit_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                if commit_date < cutoff:
                    break  # commit is older than the lookback window
            except (ValueError, TypeError):
                pass
        # Cache the commit info
        commit_cache[sha8] = {
            "sha": sha8,
            "message": c.get("message", ""),
            "author": c.get("author", ""),
            "date": date_str,
        }
        ancestors.append(commit_cache[sha8])
    return ancestors


def get_last_green_sha(repo: str, workflow_name: str) -> str | None:
    """Find the SHA of the most recent successful run of a workflow."""
    env = {**os.environ, "GH_PAGER": "cat"}
    cmd = ["gh", "run", "list", "--repo", repo,
           "--workflow", workflow_name, "--status", "success",
           "--limit", "1", "--json", "headSha"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=env)
        if result.returncode == 0:
            runs = json.loads(result.stdout)
            if runs:
                return runs[0]["headSha"]
    except Exception:
        pass
    return None


# ── Main pipeline ────────────────────────────────────────────────────────

def process_failures(repo: str, runs: list[dict], engine: str,
                     sub_id: str | None, dry_run: bool,
                     fast: bool = False) -> tuple[int, int, int]:
    """
    Process failed runs into the causal graph.

    For each failed job:
    1. Create a job node (the thing that broke)
    2. Classify the error → signal type
    3. Determine attribution:
       - Infra signal → edge from latent node, signal on job, no code mutation
       - Code signal → edge from commit node, mutation on commit, signal on job
       - TestFailure → both commit AND flaky-test as competing causes
    4. Emit mutations + signals to engine

    Returns (nodes_created, mutations, signals).
    """
    nodes = []
    edges = []
    mutations_to_send = []
    signals_to_send = []
    commit_cache = {}  # sha → commit_info
    seen_commit_nodes = set()
    seen_job_signals = set()  # (sha8, job_slug, signal_type, domain) for cross-run dedup
    flaky_rate_cache = {}  # workflow_name → float
    last_green_cache = {}  # workflow_name → sha or None
    ancestor_cache = {}  # sha → list[commit_info]

    # Compute a fixed "beginning of lookback window" timestamp for flaky-test
    # mutations.  Using a fixed timestamp prevents the flaky node from
    # resetting its decay clock on every failure.
    hours_back = max((
        (datetime.now(timezone.utc) -
         datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00"))
        ).total_seconds() / 3600
        for r in runs if r.get("createdAt")
    ), default=48)
    flaky_base_timestamp = (
        datetime.now(timezone.utc) - timedelta(hours=hours_back)
    ).isoformat()

    # Detect default branch for domain classification
    default_branch = "main"
    try:
        r = subprocess.run(
            ["gh", "repo", "view", repo, "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"],
            capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            default_branch = r.stdout.strip()
    except Exception:
        pass

    # Ensure latent nodes exist
    for lid, linfo in LATENT_NODES.items():
        nodes.append({
            "id": lid, "label": linfo["label"], "class": linfo["class"],
            "region": "github", "rack_id": None,
            "properties": {"source": "gh-actions", "latent": True},
        })

    failed_runs = [r for r in runs if r["conclusion"] == "failure"]

    def event_domain(event: str, branch: str) -> str:
        """Map GitHub event type + branch to causal domain.

        For schedule/dispatch events, we further distinguish whether the
        run targets the default branch (regression testing) or a release/
        feature branch (release validation).  Runs on non-default branches
        get domain 'release' so their failures route to a latent node
        rather than blaming the HEAD commit.
        """
        if event in ("pull_request", "pull_request_target"):
            return "pr"
        if event == "schedule":
            if branch == default_branch:
                return "schedule"
            return "release"
        if event in ("workflow_dispatch", "repository_dispatch"):
            if branch == default_branch:
                return "dispatch"
            return "release"
        return "pr"  # push, dynamic, merge_group, etc. behave like PR domain

    for run in failed_runs:
        run_id = run["databaseId"]
        sha = run["headSha"]
        sha8 = sha[:8]
        wf = run["workflowName"]
        branch = run.get("headBranch", "")
        event = run.get("event", "")
        domain = event_domain(event, branch)
        # Use actual event timestamps
        run_created = run.get("createdAt", "")
        run_updated = run.get("updatedAt", run_created)  # completion time

        # Get commit info (cached)
        if sha8 not in commit_cache:
            commit_cache[sha8] = get_commit_info(repo, sha) if not dry_run else {
                "sha": sha8, "message": "...", "author": "...", "date": ""}
        commit_info = commit_cache[sha8]
        mut_type = detect_mutation_type(commit_info, event)

        # For schedule/dispatch domains, use the commit author date as the
        # mutation timestamp so temporal decay reflects how old the code
        # change actually is, not when the scheduler happened to run.
        if domain in ("schedule", "dispatch") and commit_info.get("date"):
            mut_timestamp = commit_info["date"]
        else:
            mut_timestamp = run_created

        # Get failed jobs and errors
        if fast:
            failed_jobs = get_failed_jobs_fast(repo, run_id) if not dry_run else []
            error_lines = []
        else:
            failed_jobs = get_failed_jobs(repo, run_id) if not dry_run else []
            error_lines = get_error_lines(repo, run_id) if not dry_run else []

        if not failed_jobs and not dry_run:
            # Fallback: create a single job node for the whole run
            failed_jobs = [{"name": wf, "failed_steps": []}]

        if dry_run:
            # For dry run, just show what we'd do
            signal_type = classify_error(error_lines, [], wf)
            is_infra = signal_type in INFRA_SIGNALS
            print(f"\n  Run #{run_id} [{wf}] sha={sha8} event={event} domain={domain}")
            print(f"    Signal: {signal_type} ({'INFRA' if is_infra else 'CODE'})")
            if is_infra:
                latent = SIGNAL_TO_LATENT.get(signal_type)
                if latent is None:
                    latent = "latent://runner-env/linux"
                print(f"    → latent cause: {latent}")
            elif domain == "release":
                print(f"    → release-validation cause: latent://release-validation/{branch}")
            else:
                print(f"    → code cause: commit://{repo}/{sha8} ({mut_type})")
                if signal_type == "TestFailure":
                    print(f"    → competing: latent://flaky-tests")
            continue

        for job in failed_jobs:
            job_name = job["name"]
            all_context = error_lines + job["failed_steps"]
            signal_type = classify_error(all_context, job["failed_steps"], wf)
            is_infra = signal_type in INFRA_SIGNALS

            # Cross-run dedup: if the same commit + job slug + signal + domain
            # was already processed from an earlier run, skip to avoid duplicate
            # alerts for retried workflows. Domain is included so that a PR test
            # and a scheduled test on the same SHA are tracked independently.
            job_slug = re.sub(r'[^a-z0-9]+', '-', job_name.lower()).strip('-')
            dedup_key = (sha8, job_slug, signal_type, domain)
            if dedup_key in seen_job_signals:
                continue
            seen_job_signals.add(dedup_key)

            # Create job node
            jid = job_node_id(repo, run_id, job_name)
            job_label = f"{wf}: {job_name}"
            nodes.append({
                "id": jid, "label": job_label, "class": "CIJob",
                "region": "github", "rack_id": None,
                "properties": {
                    "source": "gh-actions",
                    "run_id": run_id,
                    "workflow": wf,
                    "job": job_name,
                    "failed_steps": job["failed_steps"],
                    "commit": sha8,
                    "branch": branch,
                    "event": event,
                    "domain": domain,
                    "author": commit_info.get("author", ""),
                    "commit_message": commit_info.get("message", "")[:120],
                },
            })

            # Azure resource edges for cloud-interacting workflows
            if sub_id:
                for dep in WORKFLOW_AZURE_DEPS.get(wf, []):
                    target = f"/subscriptions/{sub_id}/{dep}".lower()
                    edges.append({
                        "id": f"edge-{jid[-25:]}-{dep[-25:]}",
                        "source_id": jid, "target_id": target,
                        "edge_type": "dependency", "properties": {},
                    })

            if is_infra:
                # Infra failure: edge from latent node → job
                latent = SIGNAL_TO_LATENT.get(signal_type)
                if latent is None:
                    # OS-specific runner environment issue
                    latent = runner_env_latent(job_name)
                edges.append({
                    "id": f"edge-{latent[-20:]}-{jid[-30:]}",
                    "source_id": latent, "target_id": jid,
                    "edge_type": "dependency", "properties": {},
                })
                # Signal on the job node (timestamp = run completion time)
                signals_to_send.append({
                    "node_id": jid,
                    "signal_type": signal_type,
                    "severity": "critical",
                    "timestamp": run_updated,
                    "properties": {
                        "run_id": run_id, "job": job_name,
                        "failed_steps": job["failed_steps"],
                        "error_lines": error_lines[:5],
                    },
                })
                # Runner-env latent nodes get a RunnerImageUpdate mutation
                if latent.startswith("latent://runner-env/"):
                    mutations_to_send.append({
                        "node_id": latent,
                        "mutation_type": "RunnerImageUpdate",
                        "source": f"gh-actions/{repo}",
                        "timestamp": run_created,
                        "properties": {"note": "Runner environment change (inferred)"},
                    })
                # GrpcConnectionRefused also gets flaky-tests as competing cause
                if signal_type == "GrpcConnectionRefused":
                    edges.append({
                        "id": f"edge-flaky-{jid[-30:]}",
                        "source_id": "latent://flaky-tests",
                        "target_id": jid,
                        "edge_type": "dependency", "properties": {},
                    })
                    mutations_to_send.append({
                        "node_id": "latent://flaky-tests",
                        "mutation_type": "FlakyTestRun",
                        "source": f"gh-actions/{repo}",
                        "timestamp": flaky_base_timestamp,
                        "properties": {"note": "Competing cause for gRPC flakes"},
                    })
            else:
                # Code failure attribution depends on domain
                if domain == "release":
                    # Release-validation: run targets a non-default branch
                    # (release tag, old branch).  The HEAD commit is a known
                    # release, not a recent change, route to a latent node
                    # so failures are attributed to environment drift / latent
                    # bugs rather than blaming the pinned SHA.
                    release_latent = f"latent://release-validation/{branch}"
                    nodes.append({
                        "id": release_latent,
                        "label": f"Release validation ({branch})",
                        "class": "CIInfra",
                        "region": "github", "rack_id": None,
                        "properties": {"source": "gh-actions", "latent": True,
                                       "branch": branch},
                    })
                    edges.append({
                        "id": f"edge-{release_latent[-20:]}-{jid[-30:]}",
                        "source_id": release_latent, "target_id": jid,
                        "edge_type": "dependency", "properties": {},
                    })
                    mutations_to_send.append({
                        "node_id": release_latent,
                        "mutation_type": "ReleaseValidation",
                        "source": f"gh-actions/{repo}",
                        "timestamp": run_created,
                        "properties": {"branch": branch, "sha": sha8},
                    })
                    signals_to_send.append({
                        "node_id": jid,
                        "signal_type": signal_type,
                        "severity": "critical",
                        "timestamp": run_updated,
                        "properties": {
                            "run_id": run_id, "job": job_name,
                            "failed_steps": job["failed_steps"],
                            "error_lines": error_lines[:5],
                            "trigger_sha": sha8,
                            "domain": domain,
                        },
                    })
                else:
                    # PR / schedule / dispatch on default branch:
                    # commit node → job, mutation on commit
                    cid = commit_node_id(repo, sha)
                    if cid not in seen_commit_nodes:
                        seen_commit_nodes.add(cid)
                        commit_label = f"{sha8}: {commit_info['message'][:60]}"
                        nodes.append({
                            "id": cid, "label": commit_label, "class": "Commit",
                            "region": "github", "rack_id": None,
                            "properties": {
                                "source": "gh-actions",
                                "sha": sha8, "branch": branch,
                                "author": commit_info.get("author", ""),
                                "event": event,
                            },
                        })
                        # Mutation: the code change
                        mutations_to_send.append({
                            "node_id": cid,
                            "mutation_type": mut_type,
                            "source": f"gh-actions/{repo}",
                            "timestamp": mut_timestamp,
                            "properties": {
                                "sha": sha8, "branch": branch,
                                "author": commit_info.get("author", ""),
                                "message": commit_info.get("message", "")[:200],
                            },
                        })

                    # Edge: commit → job
                    edges.append({
                        "id": f"edge-{cid[-20:]}-{jid[-30:]}",
                        "source_id": cid, "target_id": jid,
                        "edge_type": "dependency", "properties": {},
                    })

                    # For schedule/dispatch domains, add ancestor commits
                    # as competing causes.  Walk the commit history back
                    # from HEAD, stopping when a commit is older than the
                    # lookback window or we hit the last-green SHA.
                    if domain in ("schedule", "dispatch") and not dry_run:
                        # Get last-green SHA for this workflow (cached)
                        if wf not in last_green_cache:
                            last_green_cache[wf] = get_last_green_sha(repo, wf)
                        last_green = last_green_cache[wf]

                        # Get ancestor commits (cached by HEAD sha)
                        if sha not in ancestor_cache:
                            ancestor_cache[sha] = get_ancestor_commits(
                                repo, sha, hours_back, commit_cache)
                        ancestors = ancestor_cache[sha]

                        for anc in ancestors:
                            anc_sha8 = anc["sha"][:8]
                            # Stop if we've reached the last successful run
                            if last_green and last_green.startswith(anc_sha8):
                                break

                            anc_cid = commit_node_id(repo, anc["sha"])
                            anc_mut_type = detect_mutation_type(anc, event)
                            anc_date = anc.get("date", "")

                            if anc_cid not in seen_commit_nodes:
                                seen_commit_nodes.add(anc_cid)
                                nodes.append({
                                    "id": anc_cid,
                                    "label": f"{anc_sha8}: {anc['message'][:60]}",
                                    "class": "Commit",
                                    "region": "github", "rack_id": None,
                                    "properties": {
                                        "source": "gh-actions",
                                        "sha": anc_sha8, "branch": branch,
                                        "author": anc.get("author", ""),
                                        "event": event,
                                    },
                                })
                                mutations_to_send.append({
                                    "node_id": anc_cid,
                                    "mutation_type": anc_mut_type,
                                    "source": f"gh-actions/{repo}",
                                    "timestamp": anc_date or mut_timestamp,
                                    "properties": {
                                        "sha": anc_sha8, "branch": branch,
                                        "author": anc.get("author", ""),
                                        "message": anc.get("message", "")[:200],
                                    },
                                })

                            # Edge: ancestor commit → job (competing cause)
                            edges.append({
                                "id": f"edge-{anc_cid[-20:]}-{jid[-30:]}",
                                "source_id": anc_cid, "target_id": jid,
                                "edge_type": "dependency", "properties": {},
                            })

                    # Signal on the job node (timestamp = run completion time)
                    signals_to_send.append({
                        "node_id": jid,
                        "signal_type": signal_type,
                        "severity": "critical",
                        "timestamp": run_updated,
                        "properties": {
                            "run_id": run_id, "job": job_name,
                            "failed_steps": job["failed_steps"],
                            "error_lines": error_lines[:5],
                            "trigger_sha": sha8,
                            "domain": domain,
                        },
                    })

                    # Competing cause: flaky tests (for TestFailure-like signals)
                    if signal_type in ("TestFailure", "UnitTestFailure",
                                       "DevContainerTestFailure"):
                        # Look up historical flaky rate for this workflow
                        if wf not in flaky_rate_cache and not dry_run:
                            flaky_rate_cache[wf] = get_workflow_flaky_rate(repo, wf)
                        flaky_rate = flaky_rate_cache.get(wf, 0.1)

                        edges.append({
                            "id": f"edge-flaky-{jid[-30:]}",
                            "source_id": "latent://flaky-tests",
                            "target_id": jid,
                            "edge_type": "dependency", "properties": {},
                        })
                        # Mutation on flaky-tests with fixed base timestamp
                        # and the historical flaky rate encoded in properties
                        mutations_to_send.append({
                            "node_id": "latent://flaky-tests",
                            "mutation_type": "FlakyTestRun",
                            "source": f"gh-actions/{repo}",
                            "timestamp": flaky_base_timestamp,
                            "properties": {
                                "note": "Competing cause for test failures",
                                "workflow": wf,
                                "historical_flaky_rate": flaky_rate,
                            },
                        })

    if dry_run:
        return 0, 0, 0

    # Merge topology
    result = post_engine("graph/merge", {"nodes": nodes, "edges": edges}, engine)
    new_nodes = result.get("new_nodes", 0) if result else 0
    new_edges = result.get("new_edges", 0) if result else 0
    print(f"  Topology: {new_nodes} new nodes, {new_edges} new edges", file=sys.stderr)

    # Send mutations
    mut_count = 0
    for m in mutations_to_send:
        if post_engine("mutations", m, engine):
            mut_count += 1

    # Send signals
    sig_count = 0
    for s in signals_to_send:
        if post_engine("signals", s, engine):
            sig_count += 1

    return new_nodes, mut_count, sig_count


def main():
    parser = argparse.ArgumentParser(
        description="Ingest GitHub Actions failures as causal graph: "
                    "failed jobs as nodes, classified errors as signals, "
                    "commits or infra as upstream mutations.",
    )
    parser.add_argument("--repo", "-r", required=True,
                        help="GitHub repository (owner/name)")
    parser.add_argument("--hours", type=int, default=24,
                        help="Look back N hours (default: 24)")
    parser.add_argument("--limit", type=int, default=200,
                        help="Max runs to fetch (default: 200)")
    parser.add_argument("--subscription", "-s",
                        help="Azure subscription ID for linking to ARG resources")
    parser.add_argument("--engine", default=ENGINE,
                        help=f"Engine URL (default: {ENGINE})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show classification without ingesting")
    parser.add_argument("--fast", action="store_true",
                        help="Skip log downloads, classify from step names only (~20x faster)")
    parser.add_argument("--exclude-workflow", action="append", default=[],
                        help="Workflow name to exclude (repeatable)")
    args = parser.parse_args()

    result = subprocess.run(["gh", "auth", "status"],
                            capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        print("ERROR: Run `gh auth login` first.", file=sys.stderr)
        sys.exit(1)

    print(f"Fetching runs from {args.repo} (last {args.hours}h)...", file=sys.stderr)
    runs = get_workflow_runs(args.repo, hours=args.hours, limit=args.limit)

    # Exclude workflows by name
    if args.exclude_workflow:
        exclude_set = {w.lower() for w in args.exclude_workflow}
        runs = [r for r in runs if r.get("workflowName", "").lower() not in exclude_set]

    from collections import Counter
    conclusions = Counter(r["conclusion"] for r in runs)
    failed = [r for r in runs if r["conclusion"] == "failure"]
    print(f"  {len(runs)} runs: {dict(conclusions)}", file=sys.stderr)
    print(f"  {len(failed)} failures to process", file=sys.stderr)

    if not failed:
        print("No failures found.", file=sys.stderr)
        return

    nodes, muts, sigs = process_failures(
        args.repo, runs, args.engine, args.subscription, args.dry_run,
        fast=args.fast)

    if args.dry_run:
        print(f"\nDry run complete.", file=sys.stderr)
    else:
        print(f"\nIngested: {nodes} nodes, {muts} mutations, {sigs} signals",
              file=sys.stderr)


if __name__ == "__main__":
    main()
