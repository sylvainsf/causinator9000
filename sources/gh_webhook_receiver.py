#!/usr/bin/env python3
"""
GitHub Webhook Receiver → Causinator 9000 real-time ingestion.

Receives GitHub webhook events and converts them to mutations/signals
in real-time, replacing the polling-based gh_actions_source.py.

Register this webhook on your repository or organization:
  Settings → Webhooks → Add webhook
  Payload URL: https://<your-host>:8090/webhook/github
  Content type: application/json
  Secret: <your-secret>
  Events: workflow_run, workflow_job, check_run, push, pull_request

Run:
  python3 sources/gh_webhook_receiver.py
  python3 sources/gh_webhook_receiver.py --port 8090 --secret $WEBHOOK_SECRET

The receiver translates events into the same causal model as
gh_actions_source.py: failed jobs as nodes, classified errors as signals,
commits as upstream mutations.
"""

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any
from urllib.request import Request, urlopen

ENGINE = os.environ.get("C9K_ENGINE_URL", "http://localhost:8080")
WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")

# ── Reuse classification from gh_actions_source.py ──────────────────────

INFRA_SIGNALS = {"AzureAuthFailure", "ImagePullError", "Timeout", "ImagePushError",
                 "RemoteWorkflowFailure", "DependabotUpdateFailure", "ArtifactUploadFailure"}
CODE_SIGNALS = {"TestFailure", "HelmChartError", "BicepBuildError", "ChecklistMissing",
                "UnitTestFailure", "DevContainerTestFailure"}

ERROR_PATTERNS = [
    (r"AADSTS\d+|federated identity|Login failed.*az.*exit code|auth-type",
     "AzureAuthFailure"),
    (r"ErrImagePull|ImagePullBackOff|image.*pull.*fail", "ImagePullError"),
    (r"timed out|TimeoutException|deadline exceeded", "Timeout"),
    (r"docker.*push.*fail|oras.*push.*fail", "ImagePushError"),
    (r"No task list was present|requireChecklist", "ChecklistMissing"),
    (r"helm.*fail|chart.*validation.*fail|no such file.*Chart", "HelmChartError"),
    (r"bicep.*fail|bicep build.*exit status", "BicepBuildError"),
    (r"Remote workflow failed", "RemoteWorkflowFailure"),
    (r"Dependabot encountered an error", "DependabotUpdateFailure"),
    (r"No files were found with the provided path.*No artifacts", "ArtifactUploadFailure"),
    (r"Run make test|Run Unit Tests|unit tests", "UnitTestFailure"),
    (r"Generating tests for.*devcontainer|devcontainers", "DevContainerTestFailure"),
    (r"Process completed with exit code", "TestFailure"),
]

SIGNAL_TO_LATENT = {
    "AzureAuthFailure": "latent://azure-oidc",
    "ImagePullError": "latent://ghcr.io",
    "ImagePushError": "latent://ghcr.io",
    "Timeout": "latent://github-actions-infra",
    "RemoteWorkflowFailure": "latent://github-actions-infra",
}


def classify_error(context: list[str]) -> str:
    text = " ".join(context)
    for pattern, signal_type in ERROR_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return signal_type
    return "TestFailure"


def detect_mutation_type(msg: str, author: str) -> str:
    msg_lower = msg.lower()
    author_lower = author.lower()
    if "dependabot" in author_lower:
        if "github-actions" in msg_lower:
            return "DepActionsBump"
        count_match = re.search(r'with\s+(\d+)\s+update', msg_lower)
        if count_match and int(count_match.group(1)) >= 5:
            return "DepGroupUpdate"
        version_match = re.search(r'from\s+v?(\d+)\.\d+\S*\s+to\s+v?(\d+)\.\d+', msg)
        if version_match and int(version_match.group(2)) > int(version_match.group(1)):
            return "DepMajorBump"
        if version_match:
            return "DepMinorBump"
        return "DependencyUpdate"
    if "release" in msg_lower:
        return "Release"
    if "revert" in msg_lower:
        return "Revert"
    return "CodeChange"


def job_node_id(repo: str, run_id: int, job_name: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '-', job_name.lower()).strip('-')
    return f"job://{repo}/{run_id}/{slug}"


def commit_node_id(repo: str, sha: str) -> str:
    return f"commit://{repo}/{sha[:8]}"


def post_engine(path: str, payload: dict) -> dict | None:
    url = f"{ENGINE}/api/{path}"
    data = json.dumps(payload).encode()
    req = Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  ERROR posting to {url}: {e}", file=sys.stderr)
        return None


# ── Event handlers ──────────────────────────────────────────────────────

def handle_workflow_run(payload: dict) -> None:
    """Handle workflow_run event — the main event for CI failures."""
    action = payload.get("action", "")
    run = payload.get("workflow_run", {})
    repo_full = payload.get("repository", {}).get("full_name", "")

    # Only care about completed runs
    if action != "completed":
        return

    conclusion = run.get("conclusion", "")
    if conclusion != "failure":
        print(f"  workflow_run: {run.get('name','')} → {conclusion} (skipping)", file=sys.stderr)
        return

    run_id = run.get("id", 0)
    wf_name = run.get("name", "unknown")
    sha = run.get("head_sha", "")[:8]
    branch = run.get("head_branch", "")
    created_at = run.get("created_at", "")
    updated_at = run.get("updated_at", created_at)
    commit_msg = run.get("head_commit", {}).get("message", "").split("\n")[0]
    author = run.get("head_commit", {}).get("author", {}).get("name", "unknown")
    event = run.get("event", "unknown")

    print(f"  ✗ workflow_run: {wf_name} #{run_id} sha={sha} → FAILURE", file=sys.stderr)

    mut_type = detect_mutation_type(commit_msg, author)
    # We don't get individual job/step failure details from workflow_run.
    # Use the workflow name for classification.
    signal_type = classify_error([wf_name])
    is_infra = signal_type in INFRA_SIGNALS

    # Create job node
    jid = job_node_id(repo_full, run_id, wf_name)
    nodes = [{
        "id": jid, "label": f"{wf_name} (#{run_id})", "class": "CIJob",
        "region": "github", "rack_id": None,
        "properties": {
            "source": "gh-webhook", "run_id": run_id,
            "workflow": wf_name, "commit": sha,
            "branch": branch, "author": author,
            "commit_message": commit_msg[:120],
        },
    }]
    edges = []

    if is_infra:
        latent = SIGNAL_TO_LATENT.get(signal_type, "latent://github-actions-infra")
        edges.append({
            "id": f"edge-{latent[-20:]}-{jid[-30:]}",
            "source_id": latent, "target_id": jid,
            "edge_type": "dependency", "properties": {},
        })
    else:
        cid = commit_node_id(repo_full, sha)
        nodes.append({
            "id": cid, "label": f"{sha}: {commit_msg[:50]}",
            "class": "Commit", "region": "github", "rack_id": None,
            "properties": {"source": "gh-webhook", "sha": sha, "author": author},
        })
        edges.append({
            "id": f"edge-{cid[-20:]}-{jid[-30:]}",
            "source_id": cid, "target_id": jid,
            "edge_type": "dependency", "properties": {},
        })
        # Mutation on commit
        post_engine("mutations", {
            "node_id": cid, "mutation_type": mut_type,
            "source": f"gh-webhook/{repo_full}",
            "timestamp": created_at,
            "properties": {"sha": sha, "author": author, "message": commit_msg[:200]},
        })

        # Flaky test competing cause
        if signal_type in ("TestFailure", "UnitTestFailure"):
            edges.append({
                "id": f"edge-flaky-{jid[-30:]}",
                "source_id": "latent://flaky-tests",
                "target_id": jid,
                "edge_type": "dependency", "properties": {},
            })
            post_engine("mutations", {
                "node_id": "latent://flaky-tests",
                "mutation_type": "FlakyTestRun",
                "source": f"gh-webhook/{repo_full}",
                "timestamp": created_at,
                "properties": {},
            })

    # Merge topology
    post_engine("graph/merge", {"nodes": nodes, "edges": edges})

    # Signal on job node
    post_engine("signals", {
        "node_id": jid, "signal_type": signal_type,
        "severity": "critical", "timestamp": updated_at,
        "properties": {
            "run_id": run_id, "workflow": wf_name,
            "trigger_sha": sha, "conclusion": conclusion,
        },
    })

    print(f"  → Ingested: {signal_type} on {jid}", file=sys.stderr)


def handle_workflow_job(payload: dict) -> None:
    """Handle workflow_job event — gives us individual job + step details."""
    action = payload.get("action", "")
    job = payload.get("workflow_job", {})

    if action != "completed" or job.get("conclusion") != "failure":
        return

    repo_full = payload.get("repository", {}).get("full_name", "")
    run_id = job.get("run_id", 0)
    job_name = job.get("name", "unknown")
    sha = job.get("head_sha", "")[:8]
    completed_at = job.get("completed_at", "")

    # Get failed steps
    failed_steps = [s["name"] for s in job.get("steps", [])
                    if s.get("conclusion") == "failure"]

    signal_type = classify_error(failed_steps + [job_name])

    jid = job_node_id(repo_full, run_id, job_name)

    print(f"  ✗ workflow_job: {job_name} → {signal_type} (steps: {failed_steps[:3]})",
          file=sys.stderr)

    # The job node should already exist from workflow_run.
    # Just update the signal with better classification from step details.
    post_engine("signals", {
        "node_id": jid, "signal_type": signal_type,
        "severity": "critical", "timestamp": completed_at,
        "properties": {
            "run_id": run_id, "job": job_name,
            "failed_steps": failed_steps,
        },
    })


# ── HTTP Server ─────────────────────────────────────────────────────────

class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        # Verify signature if secret is configured
        if WEBHOOK_SECRET:
            sig = self.headers.get("X-Hub-Signature-256", "")
            expected = "sha256=" + hmac.new(
                WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, expected):
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"Invalid signature")
                return

        event_type = self.headers.get("X-GitHub-Event", "")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

        # Process in background thread to not block the response
        threading.Thread(
            target=self._process_event,
            args=(event_type, payload),
            daemon=True,
        ).start()

    def _process_event(self, event_type: str, payload: dict):
        repo = payload.get("repository", {}).get("full_name", "?")
        print(f"[{event_type}] {repo}", file=sys.stderr)

        if event_type == "workflow_run":
            handle_workflow_run(payload)
        elif event_type == "workflow_job":
            handle_workflow_job(payload)
        elif event_type == "ping":
            print("  Webhook ping received ✓", file=sys.stderr)
        else:
            print(f"  Ignoring event type: {event_type}", file=sys.stderr)

    def log_message(self, format, *args):
        pass  # Suppress default HTTP logging


def main():
    parser = argparse.ArgumentParser(
        description="GitHub webhook receiver for real-time CI failure ingestion.",
    )
    parser.add_argument("--port", type=int, default=8090,
                        help="Port to listen on (default: 8090)")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Host to bind to (default: 0.0.0.0)")
    parser.add_argument("--engine", default=ENGINE,
                        help=f"Engine URL (default: {ENGINE})")
    parser.add_argument("--secret",
                        help="GitHub webhook secret for signature verification")
    args = parser.parse_args()

    global ENGINE, WEBHOOK_SECRET
    ENGINE = args.engine
    if args.secret:
        WEBHOOK_SECRET = args.secret

    server = HTTPServer((args.host, args.port), WebhookHandler)
    print(f"GitHub webhook receiver listening on {args.host}:{args.port}",
          file=sys.stderr)
    print(f"  Engine: {ENGINE}", file=sys.stderr)
    print(f"  Secret: {'configured' if WEBHOOK_SECRET else 'NOT SET (no signature verification)'!}",
          file=sys.stderr)
    print(f"  Webhook URL: http://<your-host>:{args.port}/webhook/github",
          file=sys.stderr)
    print(f"  Events: workflow_run, workflow_job", file=sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.", file=sys.stderr)
        server.shutdown()


if __name__ == "__main__":
    main()
