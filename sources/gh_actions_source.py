#!/usr/bin/env python3
"""
GitHub Actions → Causinator 9000 causal graph adapter.

Correct causal model:
  - MUTATIONS are the things that trigger workflows: commits, PR merges,
    dependency updates, scheduled cron ticks.  These are the root causes.
  - SIGNALS are workflow/job failures — the symptoms observed on pipeline nodes.
  - EDGES connect trigger nodes to the pipeline nodes they affect.

This means when a commit causes 3 different pipelines to fail, the engine
traces all 3 failures back to the same commit and groups them.  When a
transient GHCR outage causes image pull errors across unrelated pipelines,
the engine traces those to the latent GHCR node instead of the commit.

Graph structure:
  commit:9f403647 (CodeChange / DependencyUpdate / Release)
    ├─→ gh://repo/build              (CIPipeline)  → Signal: BicepBuildError
    ├─→ gh://repo/functional-tests   (CIPipeline)  → Signal: Timeout
    └─→ gh://repo/long-running-test  (CIPipeline)  → Signal: HelmChartError

  latent://ghcr.io (ContainerRegistry)
    └─→ all pipelines that pull/push images

Usage:
  python3 sources/gh_actions_source.py --repo project-radius/radius
  python3 sources/gh_actions_source.py --repo project-radius/radius --hours 48
  python3 sources/gh_actions_source.py --repo project-radius/radius --dry-run
  python3 sources/gh_actions_source.py --repo project-radius/radius -s $AZURE_SUB_ID
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

# ── Trigger event → mutation type mapping ─────────────────────────────────

EVENT_MUTATION_TYPE = {
    "push": "CodeChange",
    "pull_request": "PullRequest",
    "pull_request_target": "PullRequest",
    "schedule": "ScheduledRun",
    "workflow_dispatch": "ManualTrigger",
    "dynamic": "DynamicTrigger",
    "issue_comment": "IssueComment",
    "issues": "IssueEvent",
    "release": "Release",
    "create": "BranchCreate",
    "delete": "BranchDelete",
}

# ── Workflow → pipeline config ────────────────────────────────────────────

PIPELINE_CONFIG = {
    "Build and Test": {"signal_type": "BuildFailure", "azure_deps": [
        "providers/microsoft.containerregistry/registries/radius",
        "providers/microsoft.containerregistry/registries/radiusdev",
    ]},
    "Functional Tests (with Cloud Resources)": {"signal_type": "TestFailure", "azure_deps": [
        "resourcegroups/radiusfunctionaltest",
        "providers/microsoft.keyvault/vaults/radiuskvvoltest",
        "providers/microsoft.containerregistry/registries/radius",
    ]},
    "Functional Tests (with Non-Cloud Resources)": {"signal_type": "TestFailure", "azure_deps": [
        "providers/microsoft.containerregistry/registries/radius",
    ]},
    "Long-running test on Azure": {"signal_type": "TestFailure", "azure_deps": [
        "resourcegroups/radlrtest00",
        "providers/microsoft.containerservice/managedclusters/radlrtest00-aks",
        "providers/microsoft.containerregistry/registries/radius",
    ]},
    "Unit Tests": {"signal_type": "TestFailure", "azure_deps": []},
    "Release Radius": {"signal_type": "ReleaseFailure", "azure_deps": [
        "providers/microsoft.containerregistry/registries/radius",
        "providers/microsoft.keyvault/vaults/radius-accounts",
    ]},
    "Nightly rad CLI tests": {"signal_type": "TestFailure", "azure_deps": []},
    "CodeQL": {"signal_type": "SecurityFinding", "azure_deps": []},
    "CodeQL Advanced": {"signal_type": "SecurityFinding", "azure_deps": []},
    "Purge Azure test resources": {"signal_type": "PurgeFailure", "azure_deps": [
        "resourcegroups/radiusfunctionaltest",
    ]},
    "Purge test container images": {"signal_type": "PurgeFailure", "azure_deps": [
        "providers/microsoft.containerregistry/registries/radius",
    ]},
}

# ── Error pattern → signal type classification ──────────────────────────

ERROR_PATTERNS = [
    (r"ErrImagePull|ImagePullBackOff|image.*pull.*fail", "ImagePullError"),
    (r"timed out|TimeoutException|deadline exceeded", "Timeout"),
    (r"unauthorized|denied|403|Login failed|auth.*fail", "AzureAuthFailure"),
    (r"docker.*push.*fail|oras.*push.*fail", "ImagePushError"),
    (r"helm.*fail|chart.*validation.*fail|no such file.*Chart", "HelmChartError"),
    (r"bicep.*fail|bicep build.*exit status", "BicepBuildError"),
    (r"terraform.*fail|terraform.*error", "TerraformError"),
    (r"Process completed with exit code", "ProcessExitError"),
]

# ── Latent infrastructure ────────────────────────────────────────────────

LATENT_NODES = [
    {"id": "latent://ghcr.io", "label": "GitHub Container Registry (GHCR)", "class": "ContainerRegistry"},
    {"id": "latent://github-actions-infra", "label": "GitHub Actions Infrastructure", "class": "CIPlatform"},
]


# ── Helpers ──────────────────────────────────────────────────────────────

def post_engine(path: str, payload: dict, engine: str) -> dict | None:
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


def get_workflow_runs(repo: str, hours: int, limit: int) -> list[dict]:
    cmd = ["gh", "run", "list", "--repo", repo, "--limit", str(limit),
           "--json", "databaseId,name,status,conclusion,createdAt,updatedAt,"
                     "headBranch,headSha,workflowName,event,url"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        print(f"ERROR: gh run list failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    runs = json.loads(result.stdout)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return [r for r in runs if datetime.fromisoformat(
        r["createdAt"].replace("Z", "+00:00")) >= cutoff]


def get_failed_jobs(repo: str, run_id: int) -> list[dict]:
    cmd = ["gh", "run", "view", str(run_id), "--repo", repo, "--json", "jobs"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return []
    data = json.loads(result.stdout)
    return [
        {"name": j["name"], "failed_steps": [
            s["name"] for s in j.get("steps", []) if s.get("conclusion") == "failure"
        ]}
        for j in data.get("jobs", []) if j.get("conclusion") == "failure"
    ]


def get_error_lines(repo: str, run_id: int) -> list[str]:
    cmd = ["gh", "run", "view", str(run_id), "--repo", repo, "--log-failed"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        return []
    errors = []
    for line in result.stdout.split("\n"):
        if re.search(r"##\[error\]|timed out|image.*pull|ErrImage|unauthorized|denied|"
                      r"Login failed|helm.*fail|bicep.*fail|Process completed with exit code",
                      line, re.IGNORECASE):
            clean = re.sub(r"^.*?\d{4}-\d{2}-\d{2}T[\d:.]+Z\s*", "", line).strip()
            if clean:
                errors.append(clean)
    return errors[:10]


def get_commit_info(repo: str, sha: str) -> dict:
    cmd = ["gh", "api", f"repos/{repo}/commits/{sha}"]
    env = {**os.environ, "GH_PAGER": "cat"}
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=env)
    if result.returncode != 0:
        return {"sha": sha[:8], "message": "unknown", "author": "unknown"}
    c = json.loads(result.stdout)
    msg = c.get("commit", {}).get("message", "").split("\n")[0][:120]
    author = c.get("commit", {}).get("author", {}).get("name", "unknown")
    return {"sha": sha[:8], "message": msg, "author": author}


def classify_error(context: list[str]) -> str:
    text = " ".join(context)
    for pattern, signal_type in ERROR_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return signal_type
    return "ProcessExitError"


def pipeline_node_id(repo: str, workflow: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '-', workflow.lower()).strip('-')
    return f"gh://{repo}/{slug}"


def trigger_node_id(repo: str, sha: str) -> str:
    return f"commit://{repo}/{sha[:8]}"


def detect_commit_mutation_type(commit_info: dict, event: str) -> str:
    """
    Classify the mutation type from commit message, author, and event.

    Dependabot PRs get specific sub-types because different kinds of
    dependency updates have very different failure probabilities:
      DepMajorBump    - semver major version change (high risk)
      DepMinorBump    - semver minor/patch change (moderate risk)
      DepGroupUpdate  - multi-package group update like "30 updates" (high risk)
      DepActionsBump  - GitHub Actions version bumps (low risk, infra only)
      DependencyUpdate - can't classify further (moderate risk)
    """
    msg = commit_info.get("message", "")
    msg_lower = msg.lower()
    author = commit_info.get("author", "").lower()

    # ── Dependabot / dependency updates ──
    if "dependabot" in author or ("bump" in msg_lower and ("group" in msg_lower or "updates" in msg_lower or "from" in msg_lower)):
        # GitHub Actions version bumps (e.g. "Bump the github-actions group")
        if "github-actions" in msg_lower:
            return "DepActionsBump"

        # Group updates with multiple packages (e.g. "with 30 updates")
        count_match = re.search(r'with\s+(\d+)\s+update', msg_lower)
        if count_match:
            count = int(count_match.group(1))
            if count >= 5:
                return "DepGroupUpdate"  # high risk: many packages at once

        # Try to detect semver bump type from "from X to Y"
        version_match = re.search(r'from\s+v?(\d+)\.(\d+)(?:\.\d+)?\s+to\s+v?(\d+)\.(\d+)', msg)
        if version_match:
            from_major, from_minor = int(version_match.group(1)), int(version_match.group(2))
            to_major, to_minor = int(version_match.group(3)), int(version_match.group(4))
            if to_major > from_major:
                return "DepMajorBump"
            else:
                return "DepMinorBump"

        # Go-dependencies group (often large)
        if "go-dependencies" in msg_lower:
            return "DepGroupUpdate"

        return "DependencyUpdate"

    # ── Release commits ──
    if "release" in msg_lower or event == "release":
        return "Release"

    # ── Reverts ──
    if "revert" in msg_lower:
        return "Revert"

    # ── PR merges ──
    if event in ("push",) and "merge" in msg_lower and ("pull request" in msg_lower or "#" in msg):
        return "PRMerge"

    # ── Scheduled / cron ──
    if event == "schedule":
        return "ScheduledRun"

    return EVENT_MUTATION_TYPE.get(event, "CodeChange")


# ── Main pipeline ────────────────────────────────────────────────────────

def build_topology(repo: str, engine: str, sub_id: str | None, runs: list[dict]) -> None:
    """Build the causal graph: trigger nodes → pipeline nodes → Azure resources."""
    nodes = []
    edges = []
    seen_nodes = set()

    # Latent infrastructure nodes
    for ln in LATENT_NODES:
        nodes.append({"id": ln["id"], "label": ln["label"], "class": ln["class"],
                       "region": "github", "rack_id": None,
                       "properties": {"source": "gh-actions", "latent": True}})

    # Pipeline nodes (one per workflow)
    for wf_name, cfg in PIPELINE_CONFIG.items():
        nid = pipeline_node_id(repo, wf_name)
        if nid in seen_nodes:
            continue
        seen_nodes.add(nid)
        nodes.append({"id": nid, "label": f"{wf_name} ({repo.split('/')[-1]})",
                       "class": "CIPipeline", "region": "github", "rack_id": None,
                       "properties": {"source": "gh-actions", "workflow": wf_name}})

        # Pipeline → Azure resource edges
        if sub_id:
            for dep in cfg.get("azure_deps", []):
                target = f"/subscriptions/{sub_id}/{dep}".lower()
                edges.append({"id": f"edge-{nid[-30:]}-{dep[-30:]}",
                              "source_id": nid, "target_id": target,
                              "edge_type": "dependency", "properties": {}})

        # Latent GHCR → pipelines that use container images
        if any(d for d in cfg.get("azure_deps", []) if "containerregistry" in d.lower()):
            edges.append({"id": f"edge-ghcr-{nid[-30:]}",
                          "source_id": "latent://ghcr.io", "target_id": nid,
                          "edge_type": "dependency", "properties": {}})

        # Latent GH infra → all pipelines
        edges.append({"id": f"edge-ghinfra-{nid[-30:]}",
                      "source_id": "latent://github-actions-infra", "target_id": nid,
                      "edge_type": "dependency", "properties": {}})

    # Trigger nodes — one per distinct (event, headSha) pair
    triggers = {}
    for run in runs:
        sha = run["headSha"]
        event = run["event"]
        key = (event, sha[:8])
        if key not in triggers:
            triggers[key] = run

    for (event, sha8), run in triggers.items():
        nid = trigger_node_id(repo, run["headSha"])
        if nid in seen_nodes:
            continue
        seen_nodes.add(nid)

        commit_info = get_commit_info(repo, run["headSha"])
        mut_type = detect_commit_mutation_type(commit_info, event)
        label = f"{sha8}: {commit_info['message'][:60]}"
        cls = "Commit" if event in ("push", "pull_request", "pull_request_target") else "Trigger"

        nodes.append({"id": nid, "label": label, "class": cls,
                       "region": "github", "rack_id": None,
                       "properties": {"source": "gh-actions", "event": event,
                                      "sha": run["headSha"][:8], "branch": run["headBranch"],
                                      "author": commit_info["author"],
                                      "mutation_type": mut_type}})

    # Trigger → pipeline edges (one per run's trigger → workflow)
    seen_edges = set()
    for run in runs:
        src = trigger_node_id(repo, run["headSha"])
        tgt = pipeline_node_id(repo, run["workflowName"])
        ekey = (src, tgt)
        if ekey in seen_edges:
            continue
        seen_edges.add(ekey)
        edges.append({"id": f"edge-{src[-25:]}-{tgt[-25:]}",
                      "source_id": src, "target_id": tgt,
                      "edge_type": "dependency", "properties": {}})

    # Also handle workflows not in PIPELINE_CONFIG
    for run in runs:
        nid = pipeline_node_id(repo, run["workflowName"])
        if nid not in seen_nodes:
            seen_nodes.add(nid)
            nodes.append({"id": nid, "label": f"{run['workflowName']} ({repo.split('/')[-1]})",
                           "class": "CIPipeline", "region": "github", "rack_id": None,
                           "properties": {"source": "gh-actions"}})
            edges.append({"id": f"edge-ghinfra-{nid[-30:]}",
                          "source_id": "latent://github-actions-infra", "target_id": nid,
                          "edge_type": "dependency", "properties": {}})

    result = post_engine("graph/merge", {"nodes": nodes, "edges": edges}, engine)
    if result:
        print(f"  Topology: {result.get('new_nodes', 0)} new nodes, "
              f"{result.get('new_edges', 0)} new edges", file=sys.stderr)


def ingest_events(repo: str, runs: list[dict], engine: str, dry_run: bool) -> tuple[int, int]:
    """
    Ingest mutations (on trigger/commit nodes) and signals (on pipeline nodes).

    Returns (mutations_count, signals_count).
    """
    mutations = 0
    signals = 0

    # Group runs by trigger
    trigger_groups = defaultdict(list)
    for run in runs:
        key = (run["event"], run["headSha"][:8])
        trigger_groups[key].append(run)

    for (event, sha8), group_runs in trigger_groups.items():
        # One mutation per trigger
        trigger_nid = trigger_node_id(repo, group_runs[0]["headSha"])
        commit_info = get_commit_info(repo, group_runs[0]["headSha"])
        mut_type = detect_commit_mutation_type(commit_info, event)

        if dry_run:
            fails = [r for r in group_runs if r["conclusion"] == "failure"]
            icon = "✗" if fails else "✓"
            print(f"  {icon} [{event}] {sha8} {commit_info['message'][:60]}")
            print(f"    mutation: {mut_type} on {trigger_nid}")
            print(f"    triggered {len(group_runs)} workflows, {len(fails)} failed")
        else:
            result = post_engine("mutations", {
                "node_id": trigger_nid,
                "mutation_type": mut_type,
                "source": f"gh-actions/{repo}",
                "properties": {
                    "sha": group_runs[0]["headSha"][:8],
                    "branch": group_runs[0]["headBranch"],
                    "event": event,
                    "author": commit_info["author"],
                    "message": commit_info["message"][:200],
                    "workflows_triggered": len(group_runs),
                },
            }, engine)
            if result:
                mutations += 1

        # Signals on failed pipeline nodes
        for run in group_runs:
            if run["conclusion"] != "failure":
                continue

            pipeline_nid = pipeline_node_id(repo, run["workflowName"])
            failed_jobs = get_failed_jobs(repo, run["databaseId"])
            error_lines = get_error_lines(repo, run["databaseId"]) if not dry_run else []

            if failed_jobs:
                for job in failed_jobs:
                    all_context = job["failed_steps"] + error_lines
                    signal_type = classify_error(all_context)

                    if dry_run:
                        print(f"      SIGNAL: {signal_type} on {pipeline_nid} — {job['name']}")
                        for step in job["failed_steps"]:
                            print(f"        step: {step}")
                    else:
                        result = post_engine("signals", {
                            "node_id": pipeline_nid,
                            "signal_type": signal_type,
                            "severity": "critical",
                            "properties": {
                                "run_id": run["databaseId"],
                                "job": job["name"],
                                "failed_steps": job["failed_steps"],
                                "error_lines": error_lines[:5],
                                "trigger_sha": sha8,
                                "workflow": run["workflowName"],
                            },
                        }, engine)
                        if result:
                            signals += 1
            else:
                # No job detail — generic signal
                signal_type = classify_error(error_lines) if error_lines else \
                    PIPELINE_CONFIG.get(run["workflowName"], {}).get("signal_type", "WorkflowFailure")
                if dry_run:
                    print(f"      SIGNAL: {signal_type} on {pipeline_nid}")
                else:
                    result = post_engine("signals", {
                        "node_id": pipeline_nid,
                        "signal_type": signal_type,
                        "severity": "critical",
                        "properties": {
                            "run_id": run["databaseId"],
                            "trigger_sha": sha8,
                            "workflow": run["workflowName"],
                        },
                    }, engine)
                    if result:
                        signals += 1

    return mutations, signals


def main():
    parser = argparse.ArgumentParser(
        description="Ingest GitHub Actions as causal graph: commits/triggers as mutations, "
                    "workflow failures as signals.",
    )
    parser.add_argument("--repo", "-r", required=True,
                        help="GitHub repository (owner/name)")
    parser.add_argument("--hours", type=int, default=24,
                        help="Look back N hours (default: 24)")
    parser.add_argument("--limit", type=int, default=100,
                        help="Max runs to fetch (default: 100)")
    parser.add_argument("--subscription", "-s",
                        help="Azure subscription ID for linking cloud-test workflows to ARG resources")
    parser.add_argument("--engine", default=ENGINE,
                        help=f"Engine URL (default: {ENGINE})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be ingested without sending to engine")
    args = parser.parse_args()

    # Verify gh CLI
    result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        print("ERROR: Not authenticated. Run `gh auth login` first.", file=sys.stderr)
        sys.exit(1)

    print(f"Fetching workflow runs from {args.repo} (last {args.hours}h)...", file=sys.stderr)
    runs = get_workflow_runs(args.repo, hours=args.hours, limit=args.limit)
    print(f"  → {len(runs)} runs in window", file=sys.stderr)

    if not runs:
        print("No runs found.", file=sys.stderr)
        return

    from collections import Counter
    conclusions = Counter(r["conclusion"] for r in runs)
    triggers = len(set((r["event"], r["headSha"][:8]) for r in runs))
    print(f"  → {conclusions}", file=sys.stderr)
    print(f"  → {triggers} distinct triggers", file=sys.stderr)

    if not args.dry_run:
        print("Building causal topology...", file=sys.stderr)
        build_topology(args.repo, args.engine, args.subscription, runs)

    mutations, signals = ingest_events(args.repo, runs, args.engine, dry_run=args.dry_run)

    if args.dry_run:
        print(f"\nDry run: {mutations} trigger mutations, {signals} failure signals", file=sys.stderr)
    else:
        print(f"\nIngested: {mutations} mutations (triggers), {signals} signals (failures)", file=sys.stderr)


if __name__ == "__main__":
    main()
