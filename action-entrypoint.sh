#!/bin/bash
set -e

# ── GitHub Action entrypoint for Causinator 9000 ─────────────────────────
# Starts the engine, ingests failures, and outputs diagnosis.

REPO="${INPUT_REPO:-$GITHUB_REPOSITORY}"
HOURS="${INPUT_HOURS:-48}"
MIN_CONFIDENCE="${INPUT_MIN_CONFIDENCE:-50}"
POST_COMMENT="${INPUT_POST_COMMENT:-true}"
CREATE_ISSUE="${INPUT_CREATE_ISSUE:-false}"
ISSUE_LABEL="${INPUT_ISSUE_LABEL:-c9k-digest}"
RUN_ID="${INPUT_RUN_ID:-}"
FAIL_ON_FINDINGS="${INPUT_FAIL_ON_FINDINGS:-false}"

echo "🔍 Causinator 9000: Analyzing CI failures for ${REPO}"
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

# ── Resolve PR number ───────────────────────────────────────────────────
# Works for pull_request, workflow_run, and issue_comment triggers
PR_NUMBER=""
if [ -n "$GITHUB_EVENT_PATH" ]; then
    PR_NUMBER=$(python3 -c "
import json, os, subprocess, sys
try:
    event = json.load(open(os.environ.get('GITHUB_EVENT_PATH', '')))
    # Direct pull_request trigger
    pr = event.get('pull_request', {})
    if isinstance(pr, dict) and pr.get('number'):
        print(pr['number'])
        sys.exit(0)
    # workflow_run trigger, find the PR associated with the head branch
    wr = event.get('workflow_run', {})
    if wr:
        head_branch = wr.get('head_branch', '')
        head_repo = wr.get('head_repository', {}).get('full_name', '')
        if head_branch:
            # Use gh CLI to find open PRs for this branch
            r = subprocess.run(
                ['gh', 'pr', 'list', '--repo', '${REPO}',
                 '--head', head_branch, '--state', 'open',
                 '--json', 'number', '--limit', '1'],
                capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                prs = json.loads(r.stdout)
                if prs:
                    print(prs[0]['number'])
                    sys.exit(0)
    # issue_comment or number directly in event
    num = event.get('number')
    if num:
        print(num)
except Exception:
    pass
" 2>/dev/null)
fi

# ── Post as PR comment (if enabled and on a PR) ─────────────────────────
if [ "$POST_COMMENT" = "true" ] && [ -n "$PR_NUMBER" ]; then
    echo "💬 Posting diagnosis to PR #${PR_NUMBER}"
    gh pr comment "$PR_NUMBER" --repo "$REPO" --body-file /tmp/diagnosis.md 2>/dev/null || true
fi

# ── Create digest issue and prune old ones (if enabled) ─────────────────
if [ "$CREATE_ISSUE" = "true" ]; then
    RETENTION_DAYS="${INPUT_DIGEST_RETENTION_DAYS:-14}"
    echo "📝 Creating digest issue with label '${ISSUE_LABEL}' (auto-close after ${RETENTION_DAYS}d)..."

    # Ensure the label exists
    gh label create "$ISSUE_LABEL" --repo "$REPO" \
        --description "Causinator 9000 CI failure digest" \
        --color "D93F0B" 2>/dev/null || true

    # Always open a fresh dated issue for this run. Each nightly/weekly
    # report is its own issue; we never append to a previous one.
    echo "   Creating new digest issue"
    gh issue create --repo "$REPO" \
        --title "🔍 C9K CI Failure Digest: $(date -u +%Y-%m-%d)" \
        --body-file /tmp/diagnosis.md \
        --label "$ISSUE_LABEL" 2>/dev/null || true

    # Prune: auto-close digest issues older than the retention window so
    # nobody has to clean them up by hand. Issues a human has engaged with
    # (comments or assignees) are left untouched. jq's `length` on a number
    # returns that number, so the comment check works whether gh reports
    # `comments` as a count or as an array.
    STALE=$(RETENTION_DAYS="$RETENTION_DAYS" gh issue list --repo "$REPO" \
        --label "$ISSUE_LABEL" --state open \
        --json number,createdAt,comments,assignees --limit 200 \
        --jq '(env.RETENTION_DAYS|tonumber) as $d | .[] | select((.comments|length)==0 and (.assignees|length)==0 and ((.createdAt|fromdateiso8601) < (now - ($d*86400)))) | .number' 2>/dev/null || echo "")

    for n in $STALE; do
        echo "   Auto-closing stale digest issue #${n}"
        gh issue close "$n" --repo "$REPO" \
            --comment "Auto-closed by Causinator 9000: this digest is more than ${RETENTION_DAYS} days old and has been superseded by newer reports. Reopen it if you still need it." 2>/dev/null || true
    done
fi

# ── Set outputs ──────────────────────────────────────────────────────────
ALERT_COUNT=$(curl -sf http://127.0.0.1:8080/api/alert-groups | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "0")
DIAG_COUNT=$(curl -sf http://127.0.0.1:8080/api/diagnosis/all | python3 -c "import sys,json; print(len([d for d in json.load(sys.stdin) if d.get('confidence',0) >= ${MIN_CONFIDENCE}/100.0]))" 2>/dev/null || echo "0")
echo "alert-count=${ALERT_COUNT}" >> "$GITHUB_OUTPUT"
echo "diagnosis-count=${DIAG_COUNT}" >> "$GITHUB_OUTPUT"
echo "report=/tmp/diagnosis.md" >> "$GITHUB_OUTPUT"

# ── Fail on findings (if enabled) ───────────────────────────────────────
if [ "$FAIL_ON_FINDINGS" = "true" ] && [ "$DIAG_COUNT" -gt 0 ] 2>/dev/null; then
    echo "::error::C9K found ${DIAG_COUNT} diagnoses above ${MIN_CONFIDENCE}% confidence"
    exit 1
fi

echo "✅ Analysis complete, ${ALERT_COUNT} alert groups found"

kill $ENGINE_PID 2>/dev/null || true
