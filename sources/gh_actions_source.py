#!/usr/bin/env python3
"""
GitHub Actions → Causinator 9000 mutations & signals adapter.

Polls recent workflow runs from a GitHub repository and converts them to:
- Mutations: completed workflow runs (deployments, builds, tests)
- Signals: failed jobs/steps within those runs

Uses the `gh` CLI (authenticated via `gh auth login`).

Usage:
  # Ingest last 24 hours of runs from the radius repo
  python3 sources/gh_actions_source.py --repo project-radius/radius

  # Custom time window
  python3 sources/gh_actions_source.py --repo project-radius/radius --hours 48

  # Dry run — show what would be ingested without sending to engine
  python3 sources/gh_actions_source.py --repo project-radius/radius --dry-run

  # Custom engine URL
  python3 sources/gh_actions_source.py --repo project-radius/radius --engine http://localhost:8080

Mapping strategy:
  - Each workflow run completion → Mutation on the target resource
    - Deploy workflows → mutation on the deployed service/resource
    - Test workflows → mutation on the tested component
    - Build workflows → mutation on the build artifact
  - Each failed job → Signal (HTTP_500-like) on the affected resource
  - Each failed step within a failed job → additional Signal with step detail

Node targeting:
  The adapter maps workflow names to infrastructure node IDs using a
  configurable mapping. For workflows that interact with Azure resources
  (functional tests, deployments), it maps to the relevant resource group
  or subscription-level resources already loaded from ARG.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any


ENGINE = os.environ.get("C9K_ENGINE_URL", "http://localhost:8080")

# ── Workflow → node/mutation mapping ────────────────────────────────────

# Map workflow names to (node_id_pattern, mutation_type, description)
# node_id_pattern can use {repo}, {branch}, {rg} placeholders
# azure_deps: list of ARM resource ID suffixes to create dependency edges to
WORKFLOW_MAP = {
    "Build and Test": {
        "mutation_type": "Build",
        "node_pattern": "gh://{repo}/build",
        "node_class": "CIPipeline",
        "signal_type": "BuildFailure",
        "azure_deps": [
            # Pushes images to GHCR and radius ACR
            "providers/microsoft.containerregistry/registries/radius",
            "providers/microsoft.containerregistry/registries/radiusdev",
        ],
    },
    "Functional Tests (with Cloud Resources)": {
        "mutation_type": "FunctionalTest",
        "node_pattern": "gh://{repo}/functional-tests-cloud",
        "node_class": "CIPipeline",
        "signal_type": "TestFailure",
        "azure_deps": [
            # Uses functional test KV, ACR, creates ephemeral RGs
            "resourcegroups/radiusfunctionaltest",
            "providers/microsoft.keyvault/vaults/radiuskvvoltest",
            "providers/microsoft.containerregistry/registries/radius",
        ],
    },
    "Functional Tests (with Non-Cloud Resources)": {
        "mutation_type": "FunctionalTest",
        "node_pattern": "gh://{repo}/functional-tests-noncloud",
        "node_class": "CIPipeline",
        "signal_type": "TestFailure",
        "azure_deps": [
            # Builds and pushes container images to GHCR
            "providers/microsoft.containerregistry/registries/radius",
        ],
    },
    "Long-running test on Azure": {
        "mutation_type": "LongRunningTest",
        "node_pattern": "gh://{repo}/long-running-test",
        "node_class": "CIPipeline",
        "signal_type": "TestFailure",
        "azure_deps": [
            # Uses long-running test AKS cluster + monitoring
            "resourcegroups/radlrtest00",
            "providers/microsoft.containerservice/managedclusters/radlrtest00-aks",
            "providers/microsoft.containerregistry/registries/radius",
            "providers/microsoft.keyvault/vaults/radiuskvvoltest",
        ],
    },
    "Unit Tests": {
        "mutation_type": "UnitTest",
        "node_pattern": "gh://{repo}/unit-tests",
        "node_class": "CIPipeline",
        "signal_type": "TestFailure",
        "azure_deps": [],
    },
    "Release Radius": {
        "mutation_type": "Release",
        "node_pattern": "gh://{repo}/release",
        "node_class": "CIPipeline",
        "signal_type": "ReleaseFailure",
        "azure_deps": [
            # Publishes to ACR, uses release credentials
            "providers/microsoft.containerregistry/registries/radius",
            "providers/microsoft.keyvault/vaults/radius-accounts",
            "providers/microsoft.keyvault/vaults/radius-credentials",
        ],
    },
    "Nightly rad CLI tests": {
        "mutation_type": "CLITest",
        "node_pattern": "gh://{repo}/cli-tests",
        "node_class": "CIPipeline",
        "signal_type": "TestFailure",
        "azure_deps": [
            "providers/microsoft.containerregistry/registries/radius",
        ],
    },
    "CodeQL": {
        "mutation_type": "SecurityScan",
        "node_pattern": "gh://{repo}/codeql",
        "node_class": "CIPipeline",
        "signal_type": "SecurityFinding",
        "azure_deps": [],
    },
    "CodeQL Advanced": {
        "mutation_type": "SecurityScan",
        "node_pattern": "gh://{repo}/codeql-advanced",
        "node_class": "CIPipeline",
        "signal_type": "SecurityFinding",
        "azure_deps": [],
    },
    "Purge Azure test resources": {
        "mutation_type": "ResourcePurge",
        "node_pattern": "gh://{repo}/purge-azure",
        "node_class": "CIPipeline",
        "signal_type": "PurgeFailure",
        "azure_deps": [
            "resourcegroups/radiusfunctionaltest",
        ],
    },
    "Purge AWS test resources": {
        "mutation_type": "ResourcePurge",
        "node_pattern": "gh://{repo}/purge-aws",
        "node_class": "CIPipeline",
        "signal_type": "PurgeFailure",
        "azure_deps": [],
    },
    "Purge test container images": {
        "mutation_type": "ResourcePurge",
        "node_pattern": "gh://{repo}/purge-images",
        "node_class": "CIPipeline",
        "signal_type": "PurgeFailure",
        "azure_deps": [
            "providers/microsoft.containerregistry/registries/radius",
            "providers/microsoft.containerregistry/registries/radiusdev",
        ],
    },
    "Sync issue to Azure DevOps work item": {
        "mutation_type": "WorkflowRun",
        "node_pattern": "gh://{repo}/sync-issue-to-azure-devops-work-item",
        "node_class": "CIPipeline",
        "signal_type": "AzureAuthFailure",
        "azure_deps": [],
    },
}

# ── Latent infrastructure nodes ─────────────────────────────────────────
# These represent transient/shared failure modes that affect multiple CI jobs.
# They're upstream of CI pipeline nodes so failures propagate correctly.

LATENT_NODES = [
    {
        "id": "latent://ghcr.io",
        "label": "GitHub Container Registry (GHCR)",
        "class": "ContainerRegistry",
        "region": "github",
    },
    {
        "id": "latent://github-actions-runner",
        "label": "GitHub Actions Runner Infrastructure",
        "class": "CIPlatform",
        "region": "github",
    },
]

# Edges from latent nodes to CI pipelines that depend on them
LATENT_EDGES = {
    # Every workflow that builds/pushes images depends on GHCR
    "latent://ghcr.io": [
        "gh://{repo}/build",
        "gh://{repo}/functional-tests-cloud",
        "gh://{repo}/functional-tests-noncloud",
        "gh://{repo}/long-running-test",
        "gh://{repo}/cli-tests",
        "gh://{repo}/release",
        "gh://{repo}/purge-images",
    ],
    # Every workflow depends on GH Actions runner infra
    "latent://github-actions-runner": [
        "gh://{repo}/build",
        "gh://{repo}/functional-tests-cloud",
        "gh://{repo}/functional-tests-noncloud",
        "gh://{repo}/long-running-test",
        "gh://{repo}/unit-tests",
        "gh://{repo}/cli-tests",
        "gh://{repo}/release",
        "gh://{repo}/codeql",
        "gh://{repo}/purge-azure",
        "gh://{repo}/purge-aws",
        "gh://{repo}/purge-images",
    ],
}

# ── Error pattern → signal type classification ──────────────────────────
# When we fetch failed job logs, classify the signal type by the actual error.

ERROR_PATTERNS = [
    # (regex_pattern, signal_type, description)
    (r"ErrImagePull|ImagePullBackOff|image.*pull.*fail|pull.*image.*error",
     "ImagePullError", "Container image pull failure"),
    (r"timed out|TimeoutException|deadline exceeded|context deadline",
     "Timeout", "HTTP or operation timeout"),
    (r"unauthorized|denied|403|Login failed|auth.*fail|credential",
     "AzureAuthFailure", "Authentication/authorization failure"),
    (r"docker.*push.*fail|oras.*push.*fail|push.*error",
     "ImagePushError", "Container image push failure"),
    (r"helm.*fail|chart.*validation.*fail|no such file or directory.*Chart",
     "HelmChartError", "Helm chart validation failure"),
    (r"bicep.*fail|bicep build.*exit status",
     "BicepBuildError", "Bicep template build failure"),
    (r"terraform.*fail|terraform.*error",
     "TerraformError", "Terraform operation failure"),
    (r"Process completed with exit code",
     "ProcessExitError", "Process exited with non-zero code"),
]

# Default mapping for workflows not in the map
DEFAULT_MAPPING = {
    "mutation_type": "WorkflowRun",
    "node_class": "CIPipeline",
    "signal_type": "WorkflowFailure",
}


def gh_api(endpoint: str, repo: str) -> Any:
    """Call the GitHub API via gh CLI."""
    cmd = ["gh", "api", endpoint, "--paginate"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        print(f"ERROR: gh api {endpoint} failed:\n{result.stderr}", file=sys.stderr)
        return []
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        # paginate might return multiple JSON objects
        lines = result.stdout.strip().split("\n")
        items = []
        for line in lines:
            try:
                parsed = json.loads(line)
                if isinstance(parsed, list):
                    items.extend(parsed)
                else:
                    items.append(parsed)
            except json.JSONDecodeError:
                pass
        return items


def get_workflow_runs(repo: str, hours: int = 24, limit: int = 100) -> list[dict]:
    """Fetch recent workflow runs from the last N hours."""
    cmd = [
        "gh", "run", "list",
        "--repo", repo,
        "--limit", str(limit),
        "--json", "databaseId,name,status,conclusion,createdAt,updatedAt,headBranch,headSha,workflowName,event,url",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        print(f"ERROR: gh run list failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    runs = json.loads(result.stdout)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    filtered = []
    for r in runs:
        created = datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00"))
        if created >= cutoff:
            filtered.append(r)

    return filtered


def get_failed_jobs(repo: str, run_id: int) -> list[dict]:
    """Get jobs for a specific run, return only failed ones with their steps."""
    cmd = [
        "gh", "run", "view", str(run_id),
        "--repo", repo,
        "--json", "jobs",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return []

    data = json.loads(result.stdout)
    failed = []
    for job in data.get("jobs", []):
        if job.get("conclusion") == "failure":
            failed_steps = [
                s["name"] for s in job.get("steps", [])
                if s.get("conclusion") == "failure"
            ]
            failed.append({
                "name": job["name"],
                "conclusion": job["conclusion"],
                "failed_steps": failed_steps,
            })
    return failed


def post_engine(path: str, payload: dict, engine: str) -> dict | None:
    """POST JSON to the engine API."""
    import urllib.request
    url = f"{engine}/api/{path}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  ERROR posting to {url}: {e}", file=sys.stderr)
        return None


def ensure_node(node_id: str, label: str, node_class: str, engine: str, region: str = "github") -> None:
    """Ensure a CI pipeline node exists in the graph (merge is additive)."""
    payload = {
        "nodes": [{
            "id": node_id,
            "label": label,
            "class": node_class,
            "region": region,
            "rack_id": None,
            "properties": {"source": "gh-actions"},
        }],
        "edges": [],
    }
    post_engine("graph/merge", payload, engine)


def ensure_topology(repo: str, engine: str, sub_id: str | None = None) -> None:
    """
    Create all CI pipeline nodes, latent infrastructure nodes, and edges.
    This builds the full CI topology in one merge call.
    """
    nodes = []
    edges = []

    # Create latent nodes
    for ln in LATENT_NODES:
        nodes.append({
            "id": ln["id"],
            "label": ln["label"],
            "class": ln["class"],
            "region": ln.get("region", "github"),
            "rack_id": None,
            "properties": {"source": "gh-actions", "latent": True},
        })

    # Create all CI pipeline nodes and their edges
    seen_nodes = set()
    for wf_name, mapping in WORKFLOW_MAP.items():
        node_id = mapping["node_pattern"].format(repo=repo, branch="main")
        if node_id in seen_nodes:
            continue
        seen_nodes.add(node_id)

        label = f"{wf_name} ({repo.split('/')[-1]})"
        nodes.append({
            "id": node_id,
            "label": label,
            "class": mapping["node_class"],
            "region": "github",
            "rack_id": None,
            "properties": {"source": "gh-actions", "workflow": wf_name},
        })

        # Edges to Azure resources (only for cloud-touching workflows)
        for dep_suffix in mapping.get("azure_deps", []):
            if sub_id:
                target_id = f"/subscriptions/{sub_id}/{dep_suffix}".lower()
                edges.append({
                    "id": f"edge-{node_id[-40:]}-{dep_suffix[-40:]}",
                    "source_id": node_id,
                    "target_id": target_id,
                    "edge_type": "dependency",
                    "properties": {"source": "gh-actions"},
                })

    # Edges from latent nodes to CI pipelines
    for latent_id, targets in LATENT_EDGES.items():
        for target_pattern in targets:
            target_id = target_pattern.format(repo=repo)
            edges.append({
                "id": f"edge-{latent_id[-30:]}-{target_id[-30:]}",
                "source_id": latent_id,
                "target_id": target_id,
                "edge_type": "dependency",
                "properties": {"source": "gh-actions"},
            })

    payload = {"nodes": nodes, "edges": edges}
    result = post_engine("graph/merge", payload, engine)
    if result:
        print(f"  Topology: {result.get('new_nodes', 0)} new nodes, "
              f"{result.get('new_edges', 0)} new edges", file=sys.stderr)


def classify_error(failed_steps: list[str], job_name: str) -> str:
    """Classify a failure into a specific signal type based on error patterns."""
    import re
    text = " ".join(failed_steps) + " " + job_name
    for pattern, signal_type, _desc in ERROR_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return signal_type
    return "ProcessExitError"


def get_run_error_lines(repo: str, run_id: int) -> list[str]:
    """Fetch error lines from a failed run's logs for classification."""
    cmd = [
        "gh", "run", "view", str(run_id),
        "--repo", repo,
        "--log-failed",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        return []
    # Extract only error annotations and key failure lines
    import re
    errors = []
    for line in result.stdout.split("\n"):
        if re.search(r"##\[error\]|timed out|image.*pull|ErrImage|unauthorized|denied|Login failed|helm.*fail|bicep.*fail|Process completed with exit code", line, re.IGNORECASE):
            # Strip the GH Actions prefix
            clean = re.sub(r"^.*?\d{4}-\d{2}-\d{2}T[\d:.]+Z\s*", "", line).strip()
            if clean:
                errors.append(clean)
    return errors[:10]  # Cap at 10 lines


def process_runs(repo: str, runs: list[dict], engine: str, dry_run: bool = False) -> tuple[int, int]:
    """
    Process workflow runs into mutations and signals.

    Returns (mutations_count, signals_count).
    """
    mutations = 0
    signals = 0

    for run in runs:
        workflow = run["workflowName"]
        run_id = run["databaseId"]
        conclusion = run["conclusion"]
        created = run["createdAt"]
        branch = run.get("headBranch", "unknown")
        sha = run.get("headSha", "")[:8]
        event = run.get("event", "unknown")

        # Map workflow to node
        mapping = WORKFLOW_MAP.get(workflow, DEFAULT_MAPPING)
        node_id = mapping.get("node_pattern", f"gh://{repo}/{workflow.lower().replace(' ', '-')}").format(
            repo=repo, branch=branch,
        )
        mutation_type = mapping["mutation_type"]
        node_class = mapping["node_class"]
        label = f"{workflow} ({repo.split('/')[-1]})"

        if dry_run:
            status_icon = "✗" if conclusion == "failure" else "✓"
            print(f"  {status_icon} {workflow[:50]:50s} → mutation:{mutation_type} on {node_id}")
        else:
            # Every completed run is a mutation
            mutation = {
                "node_id": node_id,
                "mutation_type": mutation_type,
                "source": f"gh-actions/{repo}",
                "properties": {
                    "run_id": run_id,
                    "conclusion": conclusion,
                    "branch": branch,
                    "sha": sha,
                    "event": event,
                    "workflow": workflow,
                    "url": run.get("url", ""),
                },
            }
            result = post_engine("mutations", mutation, engine)
            if result:
                mutations += 1

        # Failed runs → signals with classified error type
        if conclusion == "failure":
            failed_jobs = get_failed_jobs(repo, run_id)

            # Get actual error lines for classification
            error_lines = get_run_error_lines(repo, run_id) if not dry_run else []

            if failed_jobs:
                for job in failed_jobs:
                    # Classify the actual error from step names + error lines
                    all_context = job["failed_steps"] + error_lines
                    signal_type = classify_error(all_context, job["name"])

                    if dry_run:
                        print(f"    SIGNAL: {signal_type} — {job['name']}")
                        for step in job["failed_steps"]:
                            print(f"      step: {step}")
                    else:
                        signal = {
                            "node_id": node_id,
                            "signal_type": signal_type,
                            "severity": "critical",
                            "properties": {
                                "run_id": run_id,
                                "job": job["name"],
                                "failed_steps": job["failed_steps"],
                                "error_lines": error_lines[:5],
                                "branch": branch,
                                "workflow": workflow,
                            },
                        }
                        result = post_engine("signals", signal, engine)
                        if result:
                            signals += 1
            else:
                signal_type = classify_error(error_lines, workflow)
                if dry_run:
                    print(f"    SIGNAL: {signal_type} — {workflow}")
                else:
                    signal = {
                        "node_id": node_id,
                        "signal_type": signal_type,
                        "severity": "critical",
                        "properties": {
                            "run_id": run_id,
                            "branch": branch,
                            "workflow": workflow,
                            "error_lines": error_lines[:5],
                        },
                    }
                    result = post_engine("signals", signal, engine)
                    if result:
                        signals += 1

    return mutations, signals


def main():
    parser = argparse.ArgumentParser(
        description="Ingest GitHub Actions workflow runs as mutations and signals.",
    )
    parser.add_argument(
        "--repo", "-r", required=True,
        help="GitHub repository (owner/name), e.g., project-radius/radius",
    )
    parser.add_argument(
        "--hours", type=int, default=24,
        help="Look back N hours for workflow runs (default: 24).",
    )
    parser.add_argument(
        "--limit", type=int, default=100,
        help="Max number of runs to fetch (default: 100).",
    )
    parser.add_argument(
        "--subscription", "-s",
        help="Azure subscription ID for linking cloud-test workflows to ARG resources.",
    )
    parser.add_argument(
        "--engine", default=ENGINE,
        help=f"Engine URL (default: {ENGINE}).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be ingested without sending to engine.",
    )

    args = parser.parse_args()

    # Verify gh CLI
    result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        print("ERROR: Not authenticated. Run `gh auth login` first.", file=sys.stderr)
        sys.exit(1)

    # Build CI topology (nodes + edges) before ingesting runs
    if not args.dry_run:
        print(f"Building CI topology for {args.repo}...", file=sys.stderr)
        ensure_topology(args.repo, args.engine, sub_id=args.subscription)

    print(f"Fetching workflow runs from {args.repo} (last {args.hours}h)...", file=sys.stderr)
    runs = get_workflow_runs(args.repo, hours=args.hours, limit=args.limit)
    print(f"  → {len(runs)} runs in window", file=sys.stderr)

    if not runs:
        print("No runs found in the time window.", file=sys.stderr)
        return

    # Summarize
    from collections import Counter
    conclusions = Counter(r["conclusion"] for r in runs)
    print(f"  → {conclusions}", file=sys.stderr)

    mutations, signals = process_runs(args.repo, runs, args.engine, dry_run=args.dry_run)

    if args.dry_run:
        print(f"\nDry run: would ingest {len(runs)} mutations, "
              f"{sum(1 for r in runs if r['conclusion'] == 'failure')} failure signals", file=sys.stderr)
    else:
        print(f"\nIngested: {mutations} mutations, {signals} signals", file=sys.stderr)


if __name__ == "__main__":
    main()
