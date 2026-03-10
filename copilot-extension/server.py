#!/usr/bin/env python3
"""
Causinator 9000 — GitHub Copilot Extension Server.

Receives chat messages from GitHub Copilot (via webhook) and responds
with CI failure diagnoses. The engine runs continuously and is kept
warm via workflow_run webhooks.

Endpoints:
  POST /agent    — Copilot Extension chat handler
  POST /webhook  — GitHub workflow_run webhook (keeps engine warm)
  GET  /health   — Health check

Usage:
  python3 copilot-extension/server.py --port 8090
  docker run -p 8090:8090 ghcr.io/sylvainsf/causinator9000 copilot-extension

Environment:
  GITHUB_TOKEN          — GitHub token for API access
  C9K_ENGINE_URL        — Engine URL (default: http://127.0.0.1:8080)
  GITHUB_WEBHOOK_SECRET — Webhook secret for signature verification
"""

import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any
from urllib.request import Request, urlopen

ENGINE_URL = os.environ.get("C9K_ENGINE_URL", "http://127.0.0.1:8080")
WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
APP_DIR = os.environ.get("C9K_APP_DIR", os.path.dirname(os.path.dirname(__file__)))


# ── Engine helpers ───────────────────────────────────────────────────────

def engine_get(path: str) -> Any:
    url = f"{ENGINE_URL}/api/{path}"
    with urlopen(Request(url), timeout=15) as resp:
        return json.loads(resp.read())


def engine_post(path: str, payload: dict | None = None) -> Any:
    url = f"{ENGINE_URL}/api/{path}"
    data = json.dumps(payload or {}).encode()
    req = Request(url, data=data, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def engine_healthy() -> bool:
    try:
        engine_get("health")
        return True
    except Exception:
        return False


# ── Fast ingestion ───────────────────────────────────────────────────────

def ingest_repo(repo: str, hours: int = 48) -> str:
    """Run fast ingestion for a repo and return summary."""
    cmd = [
        sys.executable,
        os.path.join(APP_DIR, "sources", "gh_actions_source.py"),
        "--repo", repo, "--hours", str(hours),
        "--engine", ENGINE_URL, "--fast",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return result.stderr.strip()


# ── Chat message parsing ─────────────────────────────────────────────────

def parse_intent(message: str) -> dict:
    """Parse a chat message into an intent + parameters."""
    msg = message.lower().strip()

    # Extract repo if mentioned
    repo_match = re.search(r'([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)', message)
    repo = repo_match.group(1) if repo_match else None

    # Extract hours
    hours_match = re.search(r'(\d+)\s*(?:hours?|h\b)', msg)
    hours = int(hours_match.group(1)) if hours_match else 48

    # Extract run ID
    run_match = re.search(r'(?:run|#)\s*(\d{10,})', msg)
    run_id = int(run_match.group(1)) if run_match else None

    if any(w in msg for w in ["diagnose", "analyze", "what's breaking", "failures", "why"]):
        return {"intent": "diagnose", "repo": repo, "hours": hours}
    if any(w in msg for w in ["verify", "check run", "logs"]):
        return {"intent": "verify", "repo": repo, "run_id": run_id}
    if any(w in msg for w in ["status", "health", "how many"]):
        return {"intent": "status"}
    if any(w in msg for w in ["clear", "reset"]):
        return {"intent": "clear"}

    # Default: diagnose if repo given, status otherwise
    if repo:
        return {"intent": "diagnose", "repo": repo, "hours": hours}
    return {"intent": "status"}


# ── Response generation ──────────────────────────────────────────────────

def handle_chat(message: str, refs: dict | None = None) -> str:
    """Handle a chat message and return a markdown response."""
    intent = parse_intent(message)

    # If the chat context includes a repo ref, use it
    if refs and refs.get("repository") and not intent.get("repo"):
        intent["repo"] = refs["repository"]

    match intent["intent"]:
        case "diagnose":
            return _handle_diagnose(intent)
        case "verify":
            return _handle_verify(intent)
        case "status":
            return _handle_status()
        case "clear":
            return _handle_clear()
        case _:
            return "I can help you diagnose CI failures. Try:\n- *diagnose owner/repo*\n- *what's breaking in owner/repo?*\n- *verify run 12345*"


def _handle_diagnose(intent: dict) -> str:
    repo = intent.get("repo")
    if not repo:
        return "Please specify a repository, e.g. *diagnose dapr/dapr*"

    hours = intent.get("hours", 48)

    # Clear and ingest
    engine_post("clear")
    ingest_output = ingest_repo(repo, hours)

    # Get results
    try:
        health = engine_get("health")
        groups = engine_get("alert-groups")
        diagnoses = engine_get("diagnosis/all")
    except Exception as e:
        return f"Engine error: {e}\n\nIngestion output:\n```\n{ingest_output}\n```"

    if not diagnoses:
        return f"No CI failures found for **{repo}** in the last {hours}h."

    high = [d for d in diagnoses if d.get("confidence", 0) > 0.5]

    lines = [f"## 🔍 CI Failure Analysis: {repo}\n"]
    lines.append(f"**{health.get('active_signals', 0)} failures** analyzed "
                 f"| {len(groups)} alert groups | last {hours}h\n")

    if groups:
        lines.append("### Alert Groups\n")
        lines.append("| Root Cause | Confidence | Jobs | Type |")
        lines.append("|---|---|---|---|")
        for g in sorted(groups, key=lambda x: x.get("confidence", 0), reverse=True):
            rc = g.get("root_cause", "?")
            conf = g.get("confidence", 0)
            members = len(g.get("members", []))
            cause_type = _classify_cause_type(rc)
            rc_display = _format_root_cause(rc)
            lines.append(f"| {rc_display} | {conf:.0%} | {members} | {cause_type} |")
        lines.append("")

    if high:
        lines.append("<details>")
        lines.append(f"<summary>📋 {len(high)} detailed diagnoses</summary>\n")
        lines.append("| Confidence | Job | Root Cause |")
        lines.append("|---|---|---|")
        for d in sorted(high, key=lambda x: x.get("confidence", 0), reverse=True)[:15]:
            target = d.get("target_node", "?")
            rc = d.get("root_cause", "?")
            conf = d.get("confidence", 0)
            target_short = "/".join(target.split("/")[-2:]) if "/" in target else target
            rc_short = _format_root_cause(rc)
            lines.append(f"| {conf:.0%} | {target_short} | {rc_short} |")
        lines.append("\n</details>")

    return "\n".join(lines)


def _handle_verify(intent: dict) -> str:
    repo = intent.get("repo")
    run_id = intent.get("run_id")
    if not repo or not run_id:
        return "Please specify a repo and run ID, e.g. *verify run 22891725877 in dapr/dapr*"

    env = {**os.environ, "GH_PAGER": "cat"}
    cmd = ["gh", "api", f"repos/{repo}/actions/runs/{run_id}/jobs",
           "--jq", '.jobs[] | select(.conclusion == "failure") | '
                   '{name, id, steps: [.steps[] | select(.conclusion == "failure") | .name]}']
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
    if result.returncode != 0:
        return f"Could not fetch run {run_id}: {result.stderr}"

    lines = [f"## Run {run_id} — Failed Jobs\n"]
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        try:
            job = json.loads(line)
        except json.JSONDecodeError:
            continue
        lines.append(f"**{job['name']}**")
        steps = job.get("steps", [])
        if steps:
            lines.append(f"  Failed steps: {', '.join(steps)}")
        lines.append("")

    return "\n".join(lines)


def _handle_status() -> str:
    if not engine_healthy():
        return "Engine is not running."
    h = engine_get("health")
    return (
        f"**Engine running** (v{h.get('version', '?')})\n"
        f"- Nodes: {h.get('nodes', 0):,}\n"
        f"- Edges: {h.get('edges', 0):,}\n"
        f"- Mutations: {h.get('active_mutations', 0)}\n"
        f"- Signals: {h.get('active_signals', 0)}"
    )


def _handle_clear() -> str:
    engine_post("clear")
    return "Graph cleared."


def _classify_cause_type(rc: str) -> str:
    if "runner-env" in rc: return "🖥️ Runner"
    if "flaky" in rc: return "🎲 Flaky"
    if "scorecard" in rc: return "🔒 Security"
    if "automerge" in rc: return "🔄 Automerge"
    if "latent://" in rc: return "🏗️ Infra"
    if "commit://" in rc: return "💻 Code"
    return "❓"


def _format_root_cause(rc: str) -> str:
    if "commit://" in rc:
        sha = rc.split("/")[-1].split()[0]
        mut = rc.split("(")[-1].rstrip(")") if "(" in rc else ""
        return f"`{sha}` {mut}"
    if "latent://" in rc:
        return rc.split("//")[1].split()[0]
    return rc[:40]


# ── HTTP Handler ─────────────────────────────────────────────────────────

class CopilotExtensionHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        if self.path == "/agent":
            self._handle_agent(body)
        elif self.path == "/webhook":
            self._handle_webhook(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            healthy = engine_healthy()
            self.send_response(200 if healthy else 503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": healthy}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_agent(self, body: bytes):
        """Handle Copilot Extension chat messages.
        
        The request body contains a messages array in OpenAI format.
        We extract the last user message and respond with markdown.
        """
        try:
            data = json.loads(body)
            messages = data.get("messages", [])

            # Get the last user message
            user_msg = ""
            for m in reversed(messages):
                if m.get("role") == "user":
                    user_msg = m.get("content", "")
                    break

            if not user_msg:
                user_msg = "status"

            # Extract repo context from copilot references
            refs = {}
            copilot_refs = data.get("copilot_references", [])
            for ref in copilot_refs:
                if ref.get("type") == "repository":
                    refs["repository"] = ref.get("id", "")

            # Generate response
            response_text = handle_chat(user_msg, refs)

            # Stream SSE response (Copilot Extensions protocol)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            # Send as a single completion chunk
            chunk = {
                "choices": [{
                    "index": 0,
                    "delta": {"role": "assistant", "content": response_text},
                    "finish_reason": "stop",
                }]
            }
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def _handle_webhook(self, body: bytes):
        """Handle GitHub workflow_run webhooks for continuous ingestion."""
        # Verify signature if secret is set
        if WEBHOOK_SECRET:
            sig = self.headers.get("X-Hub-Signature-256", "")
            expected = "sha256=" + hmac.new(
                WEBHOOK_SECRET.encode(), body, hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(sig, expected):
                self.send_response(403)
                self.end_headers()
                return

        event = self.headers.get("X-GitHub-Event", "")
        if event != "workflow_run":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ignored"}')
            return

        try:
            data = json.loads(body)
            action = data.get("action", "")
            conclusion = data.get("workflow_run", {}).get("conclusion", "")
            repo = data.get("repository", {}).get("full_name", "")

            if action == "completed" and conclusion == "failure" and repo:
                # Ingest this single failed run in background
                threading.Thread(
                    target=ingest_repo, args=(repo, 1), daemon=True
                ).start()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())

        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def log_message(self, format, *args):
        """Suppress default access logging."""
        pass


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="C9K Copilot Extension Server")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    print(f"C9K Copilot Extension listening on {args.host}:{args.port}")
    print(f"  Engine: {ENGINE_URL}")
    print(f"  Agent endpoint: POST /agent")
    print(f"  Webhook endpoint: POST /webhook")

    server = HTTPServer((args.host, args.port), CopilotExtensionHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
