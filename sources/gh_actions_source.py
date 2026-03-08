#!/usr/bin/env python3
"""
GitHub Actions → Causinator 9000 causal graph adapter (v3).

Correct causal model:
  - NODES are failed jobs (the unit of observation — "this specific thing broke")
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
    # Order matters — first match wins. More specific patterns first.
    (r"AADSTS\d+|federated identity|Login failed.*az.*exit code|auth-type",
     "AzureAuthFailure"),
    (r"ErrImagePull|ImagePullBackOff|image.*pull.*fail",
     "ImagePullError"),
    (r"timed out|TimeoutException|deadline exceeded|HTTP request timed out",
     "Timeout"),
    (r"docker.*push.*fail|oras.*push.*fail",
     "ImagePushError"),
    (r"No task list was present|requireChecklist",
     "ChecklistMissing"),
    (r"helm.*fail|chart.*validation.*fail|no such file.*Chart",
     "HelmChartError"),
    (r"bicep.*fail|bicep build.*exit status",
     "BicepBuildError"),
    (r"Remote workflow failed",
     "RemoteWorkflowFailure"),
    (r"Process completed with exit code",
     "TestFailure"),  # generic — tests are the most common non-specific failure
]

# ── Failure attribution: is this a code problem or an infra problem? ─────

INFRA_SIGNALS = {"AzureAuthFailure", "ImagePullError", "Timeout", "ImagePushError",
                 "RemoteWorkflowFailure"}
CODE_SIGNALS = {"TestFailure", "HelmChartError", "BicepBuildError", "ChecklistMissing"}
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
}

# Map infra signal types to which latent node is the likely cause
SIGNAL_TO_LATENT = {
    "AzureAuthFailure": "latent://azure-oidc",
    "ImagePullError": "latent://ghcr.io",
    "ImagePushError": "latent://ghcr.io",
    "Timeout": "latent://github-actions-infra",
    "RemoteWorkflowFailure": "latent://github-actions-infra",
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
    if result.returncode != 0:
        return []
    errors = []
    for line in result.stdout.split("\n"):
        if re.search(r"##\[error\]", line, re.IGNORECASE):
            clean = re.sub(r"^.*?##\[error\]", "", line).strip()
            if clean:
                errors.append(clean)
    return errors[:15]


def get_commit_info(repo: str, sha: str) -> dict:
    env = {**os.environ, "GH_PAGER": "cat"}
    cmd = ["gh", "api", f"repos/{repo}/commits/{sha}"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=env)
    if result.returncode != 0:
        return {"sha": sha[:8], "message": "unknown", "author": "unknown"}
    c = json.loads(result.stdout)
    msg = c.get("commit", {}).get("message", "").split("\n")[0][:120]
    author = c.get("commit", {}).get("author", {}).get("name", "unknown")
    return {"sha": sha[:8], "message": msg, "author": author}


def classify_error(error_lines: list[str], failed_steps: list[str]) -> str:
    """Classify failure into a signal type from actual error messages."""
    text = " ".join(error_lines) + " " + " ".join(failed_steps)
    for pattern, signal_type in ERROR_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return signal_type
    return "TestFailure"


def detect_mutation_type(commit_info: dict, event: str) -> str:
    """Classify the code change type from commit message + author."""
    msg = commit_info.get("message", "")
    msg_lower = msg.lower()
    author = commit_info.get("author", "").lower()

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


# ── Main pipeline ────────────────────────────────────────────────────────

def process_failures(repo: str, runs: list[dict], engine: str,
                     sub_id: str | None, dry_run: bool) -> tuple[int, int, int]:
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

    # Ensure latent nodes exist
    for lid, linfo in LATENT_NODES.items():
        nodes.append({
            "id": lid, "label": linfo["label"], "class": linfo["class"],
            "region": "github", "rack_id": None,
            "properties": {"source": "gh-actions", "latent": True},
        })

    failed_runs = [r for r in runs if r["conclusion"] == "failure"]

    for run in failed_runs:
        run_id = run["databaseId"]
        sha = run["headSha"]
        sha8 = sha[:8]
        wf = run["workflowName"]
        branch = run.get("headBranch", "")
        event = run.get("event", "")
        # Use actual event timestamps
        run_created = run.get("createdAt", "")
        run_updated = run.get("updatedAt", run_created)  # completion time

        # Get commit info (cached)
        if sha8 not in commit_cache:
            commit_cache[sha8] = get_commit_info(repo, sha) if not dry_run else {
                "sha": sha8, "message": "...", "author": "..."}
        commit_info = commit_cache[sha8]
        mut_type = detect_mutation_type(commit_info, event)

        # Get failed jobs and errors
        failed_jobs = get_failed_jobs(repo, run_id) if not dry_run else []
        error_lines = get_error_lines(repo, run_id) if not dry_run else []

        if not failed_jobs and not dry_run:
            # Fallback: create a single job node for the whole run
            failed_jobs = [{"name": wf, "failed_steps": []}]

        if dry_run:
            # For dry run, just show what we'd do
            signal_type = classify_error(error_lines, [])
            is_infra = signal_type in INFRA_SIGNALS
            print(f"\n  Run #{run_id} [{wf}] sha={sha8} event={event}")
            print(f"    Signal: {signal_type} ({'INFRA' if is_infra else 'CODE'})")
            if is_infra:
                latent = SIGNAL_TO_LATENT.get(signal_type, "latent://github-actions-infra")
                print(f"    → latent cause: {latent}")
            else:
                print(f"    → code cause: commit://{repo}/{sha8} ({mut_type})")
                if signal_type == "TestFailure":
                    print(f"    → competing: latent://flaky-tests")
            continue

        for job in failed_jobs:
            job_name = job["name"]
            all_context = error_lines + job["failed_steps"]
            signal_type = classify_error(all_context, job["failed_steps"])
            is_infra = signal_type in INFRA_SIGNALS

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
                latent = SIGNAL_TO_LATENT.get(signal_type, "latent://github-actions-infra")
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
            else:
                # Code failure: commit node → job, mutation on commit
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
                    # Mutation: the code change (timestamp = run start time)
                    mutations_to_send.append({
                        "node_id": cid,
                        "mutation_type": mut_type,
                        "source": f"gh-actions/{repo}",
                        "timestamp": run_created,
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
                    },
                })

                # Competing cause: flaky tests (for TestFailure only)
                if signal_type == "TestFailure":
                    edges.append({
                        "id": f"edge-flaky-{jid[-30:]}",
                        "source_id": "latent://flaky-tests",
                        "target_id": jid,
                        "edge_type": "dependency", "properties": {},
                    })
                    # Add a mutation on flaky-tests so the engine can compete
                    mutations_to_send.append({
                        "node_id": "latent://flaky-tests",
                        "mutation_type": "FlakyTestRun",
                        "source": f"gh-actions/{repo}",
                        "timestamp": run_created,
                        "properties": {"note": "Competing cause for test failures"},
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
    args = parser.parse_args()

    result = subprocess.run(["gh", "auth", "status"],
                            capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        print("ERROR: Run `gh auth login` first.", file=sys.stderr)
        sys.exit(1)

    print(f"Fetching runs from {args.repo} (last {args.hours}h)...", file=sys.stderr)
    runs = get_workflow_runs(args.repo, hours=args.hours, limit=args.limit)

    from collections import Counter
    conclusions = Counter(r["conclusion"] for r in runs)
    failed = [r for r in runs if r["conclusion"] == "failure"]
    print(f"  {len(runs)} runs: {dict(conclusions)}", file=sys.stderr)
    print(f"  {len(failed)} failures to process", file=sys.stderr)

    if not failed:
        print("No failures found.", file=sys.stderr)
        return

    nodes, muts, sigs = process_failures(
        args.repo, runs, args.engine, args.subscription, args.dry_run)

    if args.dry_run:
        print(f"\nDry run complete.", file=sys.stderr)
    else:
        print(f"\nIngested: {nodes} nodes, {muts} mutations, {sigs} signals",
              file=sys.stderr)


if __name__ == "__main__":
    main()
