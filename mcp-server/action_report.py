#!/usr/bin/env python3
"""
Generate a markdown diagnosis report for GitHub Actions job summary / PR comments.

Reports are organized by causal domain (PR, Schedule, Dispatch, Release)
so that scheduled/LRT failures are always visible even when PR failures
dominate by volume.

Usage:
  python3 action_report.py --min-confidence 50 --repo owner/repo
"""

import argparse
import json
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

ENGINE_URL = "http://127.0.0.1:8080"

DOMAIN_LABELS = {
    "pr": "🔀 Pull Request CI",
    "schedule": "⏰ Scheduled / Nightly Tests",
    "dispatch": "🔧 Manual Dispatch",
    "release": "📦 Release Validation",
    "unknown": "❓ Other",
}

DOMAIN_ORDER = ["pr", "schedule", "dispatch", "release", "unknown"]

MUTATION_LABELS = {
    "CodeChange": "code change",
    "DepMinorBump": "dependency update (minor)",
    "DepMajorBump": "dependency update (major)",
    "DepGroupUpdate": "dependency group update",
    "DependencyUpdate": "dependency update",
    "DepActionsBump": "actions dependency update",
    "Release": "release",
    "Revert": "revert",
    "CIRetrigger": "CI retrigger (empty commit)",
    "FlakyTestRun": "flaky test",
    "RunnerImageUpdate": "runner image update",
    "ReleaseValidation": "release validation",
}


def engine_get(path: str) -> dict | list:
    url = f"{ENGINE_URL}/api/{path}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read())


def shorten_target(target: str) -> str:
    if "job://" in target:
        parts = target.split("/")
        return "/".join(parts[-2:]) if len(parts) > 2 else target
    return target


def describe_root_cause(rc: str, node_props: dict, node_labels: dict,
                        repo: str = "") -> str:
    """Build a human-readable root cause description using node properties."""
    if "commit://" in rc:
        sha = rc.split("/")[-1].split()[0]
        # Find the commit node
        commit_nid = None
        for nid in node_props:
            if sha in nid and "commit://" in nid:
                commit_nid = nid
                break
        props = node_props.get(commit_nid, {}) if commit_nid else {}
        label = node_labels.get(commit_nid, "") if commit_nid else ""
        # Extract message from label (format: "sha: message")
        msg = ""
        if label and ": " in label:
            msg = label.split(": ", 1)[1][:80]
        # Also check job node properties for commit_message
        if not msg:
            for nid, p in node_props.items():
                if p.get("commit") == sha and p.get("commit_message"):
                    msg = p["commit_message"][:80]
                    break
        author = props.get("author", "")
        mut_raw = rc.split("(")[-1].rstrip(")") if "(" in rc else ""
        mut_label = MUTATION_LABELS.get(mut_raw, mut_raw)

        # Build linked SHA
        if repo:
            sha_str = f"[`{sha}`](https://github.com/{repo}/commit/{sha})"
        else:
            sha_str = f"`{sha}`"

        parts = [sha_str]
        if author:
            parts.append(f"by {author}")
        if msg:
            parts.append(f"- {msg}")
        elif mut_label:
            parts.append(f"({mut_label})")
        return " ".join(parts)
    if "latent://release-validation" in rc:
        branch = rc.split("/")[-1]
        return f"release validation ({branch})"
    if "latent://flaky" in rc:
        return "flaky / non-deterministic tests"
    if "latent://runner-env" in rc:
        os_name = rc.split("/")[-1]
        return f"runner environment ({os_name})"
    if "latent://azure-oidc" in rc:
        return "Azure OIDC / federated credentials"
    if "latent://ghcr.io" in rc:
        return "GitHub Container Registry (GHCR)"
    if "latent://workflow-config" in rc:
        return "workflow configuration issue"
    if "latent://runner-failure" in rc:
        return "GitHub Actions runner failure"
    if "latent://" in rc:
        return rc.split("//")[1]
    return rc


def cause_type_emoji(rc: str) -> str:
    if "latent://runner-env" in rc or "latent://runner-failure" in rc:
        return "🖥️"
    if "latent://workflow-config" in rc:
        return "⚙️"
    if "latent://flaky" in rc:
        return "🎲"
    if "latent://release-validation" in rc:
        return "📦"
    if "latent://" in rc:
        return "🏗️"
    if "commit://" in rc:
        return "💻"
    return "❓"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-confidence", type=int, default=50)
    parser.add_argument("--repo", default="")
    args = parser.parse_args()

    min_conf = args.min_confidence / 100.0

    # Get health stats
    try:
        health = engine_get("health")
    except Exception:
        print("## ⚠️ Causinator 9000: Engine Unavailable")
        print("The C9K engine could not be reached.")
        return

    mutations = health.get("active_mutations", 0)
    signals = health.get("active_signals", 0)

    if signals == 0:
        print("## ✅ Causinator 9000: No Failures Detected")
        print(f"No CI failures found for `{args.repo}` in the lookback window.")
        return

    # Build node lookup for domain classification and rich context
    node_props = {}   # id → properties dict
    node_labels = {}  # id → label string
    default_branch = "main"
    try:
        graph = engine_get("graph/export")
        for n in graph.get("nodes", []):
            nid = n.get("id", "")
            node_props[nid] = n.get("properties", {})
            node_labels[nid] = n.get("label", "")
    except Exception:
        pass

    def infer_domain(target: str) -> str:
        """Infer causal domain from node properties."""
        props = node_props.get(target, {})
        # Prefer explicit domain if set
        if props.get("domain"):
            return props["domain"]
        # Infer from event type + branch
        event = props.get("event", "")
        branch = props.get("branch", "")
        if event in ("pull_request", "pull_request_target"):
            return "pr"
        if event == "schedule":
            return "schedule" if branch == default_branch else "release"
        if event in ("workflow_dispatch", "repository_dispatch"):
            return "dispatch" if branch == default_branch else "release"
        if event:  # push, dynamic, merge_group, etc.
            return "pr"
        return "unknown"

    def run_url(run_id) -> str:
        """Build a GitHub Actions run URL."""
        if run_id and args.repo:
            return f"https://github.com/{args.repo}/actions/runs/{run_id}"
        return ""

    def commit_url(sha: str) -> str:
        """Build a GitHub commit URL."""
        if sha and args.repo:
            return f"https://github.com/{args.repo}/commit/{sha}"
        return ""

    def format_sha_link(sha: str) -> str:
        """Format a SHA as a markdown link."""
        url = commit_url(sha)
        return f"[`{sha}`]({url})" if url else f"`{sha}`"

    def format_run_links(members, node_props) -> str:
        """Format run IDs as numbered markdown links."""
        seen = {}
        for m in members:
            mid = m if isinstance(m, str) else m.get("node_id", "")
            rid = node_props.get(mid, {}).get("run_id")
            if rid and rid not in seen:
                seen[rid] = len(seen) + 1
        if not seen:
            return ""
        return " ".join(
            f"[{i}]({run_url(rid)})" for rid, i in seen.items()
        )

    def get_failure_context(target: str) -> str:
        """Get the actual failure details for a job node."""
        p = node_props.get(target, {})
        steps = p.get("failed_steps", [])
        if steps:
            return ", ".join(steps[:3])
        return ""

    # Get alert groups and diagnoses
    groups = engine_get("alert-groups")
    groups = [g for g in groups if g.get("confidence", 0) >= min_conf]

    diagnoses = engine_get("diagnosis/all")
    high = [d for d in diagnoses if d.get("confidence", 0) >= min_conf]

    # Classify each diagnosis by domain
    by_domain = defaultdict(list)
    for d in high:
        target = d.get("target_node", "")
        domain = infer_domain(target)
        rc = d.get("root_cause", "")
        if "latent://release-validation" in rc:
            domain = "release"
        by_domain[domain].append(d)

    # Header
    total = len(high)
    domain_counts = {d: len(diags) for d, diags in by_domain.items()}
    domain_summary = ", ".join(
        f"{DOMAIN_LABELS.get(d, d).split(' ', 1)[-1]}: {c}"
        for d, c in sorted(domain_counts.items(), key=lambda x: DOMAIN_ORDER.index(x[0]) if x[0] in DOMAIN_ORDER else 99)
    )

    print(f"## 🔍 Causinator 9000: CI Failure Analysis")
    print()
    print(f"**{total} failures** diagnosed above {args.min_confidence}% confidence "
          f"| {mutations} mutations | {signals} signals")
    print()
    if domain_counts:
        print(f"**By domain:** {domain_summary}")
        print()

    # Alert groups summary with rich context
    if groups:
        print("### Alert Groups")
        print()
        print("| Root Cause | Confidence | Domain | Failed Runs | Signal | What Failed |")
        print("|---|---|---|---|---|---|")
        for g in sorted(groups, key=lambda x: x.get("confidence", 0), reverse=True):
            rc = g.get("root_cause", "?")
            conf = g.get("confidence", 0)
            members = g.get("members", [])
            desc = describe_root_cause(rc, node_props, node_labels, args.repo)
            sig_types = g.get("signal_types", [])
            sig_str = ", ".join(sorted(set(sig_types))) if sig_types else ""

            # Determine domain(s) for this group
            domains = set()
            job_names = []
            failed_context = []
            for m in members:
                mid = m if isinstance(m, str) else m.get("node_id", "")
                domains.add(infer_domain(mid))
                jprops = node_props.get(mid, {})
                jname = jprops.get("job", shorten_target(mid))
                if jname not in job_names:
                    job_names.append(jname)
                steps = jprops.get("failed_steps", [])
                for s in steps:
                    if s not in failed_context:
                        failed_context.append(s)
            domain_str = ", ".join(sorted(domains - {"unknown"})) or "-"
            runs_str = format_run_links(members, node_props)
            context_str = ", ".join(failed_context[:3])
            if len(failed_context) > 3:
                context_str += f" (+{len(failed_context) - 3})"

            print(f"| {cause_type_emoji(rc)} {desc} | {conf:.0%} | {domain_str} "
                  f"| {runs_str} | {sig_str} | {context_str} |")
        print()

    # Per-domain sections with rich context
    for domain in DOMAIN_ORDER:
        diags = by_domain.get(domain)
        if not diags:
            continue

        label = DOMAIN_LABELS.get(domain, domain)
        diags_sorted = sorted(diags, key=lambda x: x.get("confidence", 0), reverse=True)

        print(f"### {label} ({len(diags_sorted)} failures)")
        print()
        print("| Confidence | Workflow / Job | Root Cause | What Failed | Run |")
        print("|---|---|---|---|---|")

        for d in diags_sorted:
            target = d.get("target_node", "?")
            rc = d.get("root_cause", "?")
            conf = d.get("confidence", 0)

            # Rich job description from node properties
            jprops = node_props.get(target, {})
            wf = jprops.get("workflow", "")
            job = jprops.get("job", "")
            rid = jprops.get("run_id")
            if wf and job and wf != job:
                job_desc = f"{wf} / {job}"
            elif wf:
                job_desc = wf
            else:
                job_desc = shorten_target(target)

            desc = describe_root_cause(rc, node_props, node_labels, args.repo)
            context = get_failure_context(target)
            run_link = f"[view]({run_url(rid)})" if rid else ""

            print(f"| {conf:.0%} | {job_desc} | {cause_type_emoji(rc)} {desc} "
                  f"| {context} | {run_link} |")

        print()

    print("---")
    print(f"*Generated by [Causinator 9000](https://github.com/sylvainsf/causinator9000) "
          f"at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}*")
    print()
    print("Am I wrong? Please let me know! "
          "[Report a misdiagnosis](https://github.com/sylvainsf/causinator9000/issues/new"
          "?template=cpt-change.yml&title=Misdiagnosis+report"
          f"&labels=cpt-change&body=Repository%3A+{args.repo})")


if __name__ == "__main__":
    main()
