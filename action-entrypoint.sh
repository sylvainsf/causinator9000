#!/bin/bash
set -e

# ── GitHub Action entrypoint for Causinator 9000 ─────────────────────────
# Starts the engine, ingests failures, and outputs diagnosis.

REPO="${INPUT_REPO:-$GITHUB_REPOSITORY}"
HOURS="${INPUT_HOURS:-48}"
MIN_CONFIDENCE="${INPUT_MIN_CONFIDENCE:-50}"
POST_COMMENT="${INPUT_POST_COMMENT:-true}"

echo "🔍 Causinator 9000 — Analyzing CI failures for ${REPO}"
echo "   Lookback: ${HOURS}h | Min confidence: ${MIN_CONFIDENCE}%"

# ── Authenticate gh CLI ──────────────────────────────────────────────────
if [ -n "$GITHUB_TOKEN" ]; then
    echo "$GITHUB_TOKEN" | gh auth login --with-token 2>/dev/null
fi

# ── Start the engine ─────────────────────────────────────────────────────
c9k-engine &
ENGINE_PID=$!

for i in $(seq 1 30); do
    if curl -sf http://127.0.0.1:8080/api/health >/dev/null 2>&1; then
        break
    fi
    sleep 0.5
done

if ! curl -sf http://127.0.0.1:8080/api/health >/dev/null 2>&1; then
    echo "::error::C9K engine failed to start"
    exit 1
fi

# ── Ingest ───────────────────────────────────────────────────────────────
echo "📥 Ingesting GitHub Actions failures..."
python3 /app/sources/gh_actions_source.py \
    --repo "$REPO" \
    --hours "$HOURS" \
    --engine http://127.0.0.1:8080 2>&1 | tee /tmp/ingest.log

# ── Generate diagnosis ──────────────────────────────────────────────────
python3 /app/mcp-server/action_report.py \
    --min-confidence "$MIN_CONFIDENCE" \
    --repo "$REPO" \
    > /tmp/diagnosis.md

DIAGNOSIS=$(cat /tmp/diagnosis.md)

# ── Output to job summary ───────────────────────────────────────────────
if [ -n "$GITHUB_STEP_SUMMARY" ]; then
    cat /tmp/diagnosis.md >> "$GITHUB_STEP_SUMMARY"
fi

# ── Post as PR comment (if enabled and on a PR) ─────────────────────────
if [ "$POST_COMMENT" = "true" ] && [ -n "$GITHUB_EVENT_PATH" ]; then
    PR_NUMBER=$(python3 -c "
import json, os
try:
    event = json.load(open(os.environ.get('GITHUB_EVENT_PATH', '')))
    pr = event.get('pull_request', event.get('number'))
    if isinstance(pr, dict):
        print(pr.get('number', ''))
    elif pr:
        print(pr)
except:
    pass
" 2>/dev/null)

    if [ -n "$PR_NUMBER" ]; then
        echo "💬 Posting diagnosis to PR #${PR_NUMBER}"
        gh pr comment "$PR_NUMBER" --repo "$REPO" --body-file /tmp/diagnosis.md 2>/dev/null || true
    fi
fi

# ── Set outputs ──────────────────────────────────────────────────────────
ALERT_COUNT=$(curl -sf http://127.0.0.1:8080/api/alert-groups | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "0")
echo "alert-count=${ALERT_COUNT}" >> "$GITHUB_OUTPUT"

echo "✅ Analysis complete — ${ALERT_COUNT} alert groups found"

kill $ENGINE_PID 2>/dev/null || true
