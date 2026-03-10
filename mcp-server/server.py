#!/usr/bin/env python3
"""
Causinator 9000 MCP Server — Model Context Protocol interface.

Exposes C9K engine capabilities as MCP tools so AI agents can:
  - Ingest GitHub Actions failures, Terraform state, K8s clusters
  - Run causal diagnoses and get alert groups
  - Verify predictions against actual failure logs
  - Manage engine lifecycle (clear, reload CPTs)

Runs over stdio transport. The engine must be reachable at C9K_ENGINE_URL.

Usage:
  python3 mcp-server/server.py                    # stdio (default)
  docker run -i ghcr.io/sylvainsf/causinator9000   # via container
"""

import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

ENGINE_URL = os.environ.get("C9K_ENGINE_URL", "http://127.0.0.1:8080")
APP_DIR = os.environ.get("C9K_APP_DIR", os.path.dirname(os.path.dirname(__file__)))

server = Server("causinator9000")


# ── Engine HTTP helpers ──────────────────────────────────────────────────

def engine_get(path: str) -> dict:
    url = f"{ENGINE_URL}/api/{path}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def engine_post(path: str, payload: dict | None = None) -> dict:
    url = f"{ENGINE_URL}/api/{path}"
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(url, data=data,
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def engine_healthy() -> bool:
    try:
        engine_get("health")
        return True
    except Exception:
        return False


# ── Tool definitions ─────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="c9k_health",
            description="Check if the Causinator 9000 engine is running. Returns node/edge/mutation/signal counts.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="c9k_clear",
            description="Clear the entire causal graph. Use before ingesting a fresh dataset.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="c9k_reload_cpts",
            description="Reload heuristic CPT definitions from config files on disk.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="c9k_ingest_github",
            description=(
                "Ingest GitHub Actions CI failures for a repository into the causal graph. "
                "Downloads failed run logs, classifies errors, creates nodes for failed jobs, "
                "mutations for commits, and connects latent infrastructure causes. "
                "Returns a summary of what was ingested and the top diagnoses."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "GitHub repository in owner/name format (e.g. 'dapr/dapr')",
                    },
                    "hours": {
                        "type": "integer",
                        "description": "Hours to look back (default: 48)",
                        "default": 48,
                    },
                    "subscription": {
                        "type": "string",
                        "description": "Azure subscription ID for linking to cloud resources (optional)",
                    },
                },
                "required": ["repo"],
            },
        ),
        Tool(
            name="c9k_diagnose_all",
            description=(
                "Get all active causal diagnoses sorted by confidence. "
                "Each diagnosis shows the target node, predicted root cause, "
                "confidence score, and competing causes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "min_confidence": {
                        "type": "number",
                        "description": "Minimum confidence threshold (0.0-1.0, default: 0.1)",
                        "default": 0.1,
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="c9k_alert_groups",
            description=(
                "Get correlated alert groups — failures grouped by shared root cause. "
                "Shows the root cause, confidence, and number of affected jobs."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="c9k_diagnose",
            description="Get the causal diagnosis for a specific node ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Node ID to diagnose (e.g. 'job://dapr/dapr/22873406988/unit-tests')",
                    },
                },
                "required": ["target"],
            },
        ),
        Tool(
            name="c9k_verify_run",
            description=(
                "Download the actual failure logs for a GitHub Actions run and extract error details. "
                "Use this to verify whether a C9K prediction is accurate."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "GitHub repository (owner/name)",
                    },
                    "run_id": {
                        "type": "integer",
                        "description": "GitHub Actions run ID to inspect",
                    },
                },
                "required": ["repo", "run_id"],
            },
        ),
        Tool(
            name="c9k_commit_info",
            description="Get commit details (message, author, date) for a SHA in a repository.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "GitHub repository (owner/name)",
                    },
                    "sha": {
                        "type": "string",
                        "description": "Commit SHA (full or abbreviated)",
                    },
                },
                "required": ["repo", "sha"],
            },
        ),
        Tool(
            name="c9k_compare_commits",
            description="Check the relationship between two commits (ancestry, ahead/behind counts).",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "GitHub repository (owner/name)",
                    },
                    "base": {
                        "type": "string",
                        "description": "Base commit SHA",
                    },
                    "head": {
                        "type": "string",
                        "description": "Head commit SHA",
                    },
                },
                "required": ["repo", "base", "head"],
            },
        ),
        Tool(
            name="c9k_neighborhood",
            description="Get the causal neighborhood around a node — its upstream causes and downstream effects.",
            inputSchema={
                "type": "object",
                "properties": {
                    "node_id": {
                        "type": "string",
                        "description": "Node ID to explore",
                    },
                },
                "required": ["node_id"],
            },
        ),
    ]


# ── Tool implementations ─────────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        result = _dispatch(name, arguments)
        return [TextContent(type="text", text=result)]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


def _dispatch(name: str, args: dict) -> str:
    match name:
        case "c9k_health":
            return _health()
        case "c9k_clear":
            return _clear()
        case "c9k_reload_cpts":
            return _reload_cpts()
        case "c9k_ingest_github":
            return _ingest_github(args)
        case "c9k_diagnose_all":
            return _diagnose_all(args)
        case "c9k_alert_groups":
            return _alert_groups()
        case "c9k_diagnose":
            return _diagnose(args)
        case "c9k_verify_run":
            return _verify_run(args)
        case "c9k_commit_info":
            return _commit_info(args)
        case "c9k_compare_commits":
            return _compare_commits(args)
        case "c9k_neighborhood":
            return _neighborhood(args)
        case _:
            return f"Unknown tool: {name}"


# ── Tool implementations ─────────────────────────────────────────────────

def _health() -> str:
    if not engine_healthy():
        return "Engine is NOT running or unreachable at " + ENGINE_URL
    h = engine_get("health")
    return (
        f"Engine: **running** ({h.get('version', '?')})\n"
        f"- Nodes: {h.get('nodes', 0):,}\n"
        f"- Edges: {h.get('edges', 0):,}\n"
        f"- Active mutations: {h.get('active_mutations', 0)}\n"
        f"- Active signals: {h.get('active_signals', 0)}"
    )


def _clear() -> str:
    engine_post("clear")
    return "Graph cleared."


def _reload_cpts() -> str:
    r = engine_post("reload-cpts")
    return f"Reloaded {r.get('classes', '?')} heuristic classes from {r.get('path', '?')}."


def _ingest_github(args: dict) -> str:
    repo = args["repo"]
    hours = args.get("hours", 48)
    sub = args.get("subscription")

    cmd = [
        sys.executable, os.path.join(APP_DIR, "sources", "gh_actions_source.py"),
        "--repo", repo, "--hours", str(hours),
        "--engine", ENGINE_URL, "--fast",
    ]
    if sub:
        cmd.extend(["--subscription", sub])

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    output = result.stderr.strip()

    # Now get diagnoses and alert groups for the summary
    lines = [f"## GitHub Actions Ingestion: {repo}\n", output, ""]

    try:
        groups = engine_get("alert-groups")
        if groups:
            lines.append(f"### Alert Groups ({len(groups)} found)\n")
            lines.append("| Root Cause | Confidence | Members |")
            lines.append("|---|---|---|")
            for g in groups:
                rc = g.get("root_cause", "?")
                conf = g.get("confidence", 0)
                members = len(g.get("members", []))
                lines.append(f"| {rc} | {conf:.0%} | {members} |")
            lines.append("")
    except Exception:
        pass

    try:
        diagnoses = engine_get("diagnosis/all")
        high_conf = [d for d in diagnoses if d.get("confidence", 0) > 0.5]
        if high_conf:
            lines.append(f"### Top Diagnoses ({len(high_conf)} above 50%)\n")
            lines.append("| Confidence | Failed Job | Root Cause |")
            lines.append("|---|---|---|")
            for d in sorted(high_conf, key=lambda x: x.get("confidence", 0), reverse=True)[:15]:
                target = d.get("target_node", "?")
                rc = d.get("root_cause", "?")
                conf = d.get("confidence", 0)
                lines.append(f"| {conf:.0%} | {target} | {rc} |")
    except Exception:
        pass

    return "\n".join(lines)


def _diagnose_all(args: dict) -> str:
    min_conf = args.get("min_confidence", 0.1)
    diagnoses = engine_get("diagnosis/all")
    filtered = [d for d in diagnoses if d.get("confidence", 0) >= min_conf]
    filtered.sort(key=lambda x: x.get("confidence", 0), reverse=True)

    if not filtered:
        return "No diagnoses above the confidence threshold."

    lines = [f"## All Diagnoses ({len(filtered)} results)\n"]
    lines.append("| Confidence | Target | Root Cause | Competing Causes |")
    lines.append("|---|---|---|---|")
    for d in filtered[:30]:
        target = d.get("target_node", "?")
        rc = d.get("root_cause", "?")
        conf = d.get("confidence", 0)
        competing = d.get("competing_causes", [])
        comp_str = ", ".join(f"{c} ({p:.0%})" for c, p in competing[:3]) if competing else "—"
        lines.append(f"| {conf:.0%} | {target} | {rc} | {comp_str} |")

    return "\n".join(lines)


def _alert_groups() -> str:
    groups = engine_get("alert-groups")
    if not groups:
        return "No alert groups."

    lines = [f"## Alert Groups ({len(groups)} groups)\n"]
    lines.append("| Root Cause | Confidence | Members |")
    lines.append("|---|---|---|")
    for g in groups:
        rc = g.get("root_cause", "?")
        conf = g.get("confidence", 0)
        members = g.get("members", [])
        lines.append(f"| {rc} | {conf:.0%} | {len(members)} |")

    return "\n".join(lines)


def _diagnose(args: dict) -> str:
    target = args["target"]
    result = engine_get(f"diagnosis?target={urllib.request.quote(target)}")
    if not result:
        return f"No diagnosis found for `{target}`."

    lines = [f"## Diagnosis: {target}\n"]
    lines.append(f"- **Root cause:** {result.get('root_cause', '?')}")
    lines.append(f"- **Confidence:** {result.get('confidence', 0):.0%}")
    path = result.get("causal_path", [])
    if path:
        lines.append(f"- **Causal path:** {' → '.join(path)}")
    competing = result.get("competing_causes", [])
    if competing:
        lines.append("- **Competing causes:**")
        for c, p in competing:
            lines.append(f"  - {c} ({p:.0%})")

    return "\n".join(lines)


def _verify_run(args: dict) -> str:
    repo = args["repo"]
    run_id = args["run_id"]

    env = {**os.environ, "GH_PAGER": "cat"}

    # Get failed jobs
    cmd = [
        "gh", "api",
        f"repos/{repo}/actions/runs/{run_id}/jobs",
        "--jq", '[.jobs[] | select(.conclusion == "failure") | {name, id, '
                'failed_steps: [.steps[] | select(.conclusion == "failure") | .name]}]'
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
    if result.returncode != 0:
        return f"Failed to fetch jobs for run {run_id}: {result.stderr}"

    jobs = json.loads(result.stdout)
    if not jobs:
        return f"No failed jobs found in run {run_id}."

    lines = [f"## Run {run_id} — {len(jobs)} failed jobs\n"]

    for job in jobs[:5]:  # Limit to 5 jobs
        job_name = job["name"]
        job_id = job["id"]
        failed_steps = job.get("failed_steps", [])

        lines.append(f"### {job_name}")
        if failed_steps:
            lines.append(f"Failed steps: {', '.join(failed_steps)}")

        # Download job log and extract errors
        log_cmd = ["gh", "api", f"repos/{repo}/actions/jobs/{job_id}/logs"]
        log_result = subprocess.run(log_cmd, capture_output=True, text=True, timeout=60, env=env)

        if log_result.returncode == 0:
            log_lines = log_result.stdout.splitlines()
            error_patterns = re.compile(
                r'--- FAIL:|DONE.*fail|command not found|exit code \d|'
                r'ERROR:.*Process|invalid|not in .>=|connection refused|'
                r'panic:|BUILD FAILURE|requires a different|'
                r'make:.*Error|FAIL\s|Create Artifact.*failed',
                re.IGNORECASE
            )
            skip = re.compile(r'pipefail|--noprofile', re.IGNORECASE)
            errors = [
                re.sub(r'^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s*', '', l).strip()
                for l in log_lines
                if error_patterns.search(l) and not skip.search(l)
            ]
            if errors:
                lines.append("```")
                for e in errors[-8:]:
                    lines.append(e)
                lines.append("```")
            else:
                # Show last few lines as fallback
                tail = [l.strip() for l in log_lines[-10:] if l.strip()]
                if tail:
                    lines.append("```")
                    for t in tail[-5:]:
                        clean = re.sub(r'^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s*', '', t).strip()
                        lines.append(clean)
                    lines.append("```")
        else:
            lines.append(f"(Could not download logs: {log_result.stderr.strip()})")

        lines.append("")

    return "\n".join(lines)


def _commit_info(args: dict) -> str:
    repo = args["repo"]
    sha = args["sha"]
    env = {**os.environ, "GH_PAGER": "cat"}

    cmd = [
        "gh", "api", f"repos/{repo}/commits/{sha}",
        "--jq", '{sha: .sha[0:8], message: .commit.message, '
                'author: .commit.author.name, date: .commit.author.date}'
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=env)
    if result.returncode != 0:
        return f"Failed to get commit {sha}: {result.stderr}"

    c = json.loads(result.stdout)
    return (
        f"**{c.get('sha', sha[:8])}** by {c.get('author', '?')} "
        f"({c.get('date', '?')})\n\n{c.get('message', '?')}"
    )


def _compare_commits(args: dict) -> str:
    repo = args["repo"]
    base = args["base"]
    head = args["head"]
    env = {**os.environ, "GH_PAGER": "cat"}

    cmd = [
        "gh", "api", f"repos/{repo}/compare/{base}...{head}",
        "--jq", '{status, ahead_by, behind_by, '
                'merge_base: .merge_base_commit.sha[0:8]}'
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=env)
    if result.returncode != 0:
        return f"Failed to compare: {result.stderr}"

    c = json.loads(result.stdout)
    return (
        f"**{base[:8]}** → **{head[:8]}**: {c.get('status', '?')}\n"
        f"- Ahead by: {c.get('ahead_by', '?')}\n"
        f"- Behind by: {c.get('behind_by', '?')}\n"
        f"- Merge base: {c.get('merge_base', '?')}"
    )


def _neighborhood(args: dict) -> str:
    node_id = args["node_id"]
    result = engine_get(f"neighborhood?target={urllib.request.quote(node_id)}")
    if not result:
        return f"No neighborhood data for `{node_id}`."

    nodes = result.get("nodes", [])
    edges = result.get("edges", [])
    return (
        f"## Neighborhood: {node_id}\n\n"
        f"- {len(nodes)} nodes, {len(edges)} edges\n\n"
        f"```json\n{json.dumps(result, indent=2)[:3000]}\n```"
    )


# ── Main ──────────────────────────────────────────────────────────────────

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
