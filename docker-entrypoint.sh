#!/bin/bash
set -e

MODE="${1:-mcp-server}"

# ── GitHub auth: accept GH_TOKEN, GITHUB_TOKEN, or mounted gh config ────
if [ -n "$GH_TOKEN" ] && [ -z "$GITHUB_TOKEN" ]; then
    export GITHUB_TOKEN="$GH_TOKEN"
fi
if [ -n "$GITHUB_TOKEN" ]; then
    echo "$GITHUB_TOKEN" | gh auth login --with-token 2>/dev/null || true
fi

# Start the engine in the background
c9k-engine &
ENGINE_PID=$!

# Wait for engine to be ready
for i in $(seq 1 30); do
    if curl -sf http://127.0.0.1:8080/api/health >/dev/null 2>&1; then
        break
    fi
    sleep 0.5
done

case "$MODE" in
    mcp-server)
        exec python3 /app/mcp-server/server.py
        ;;
    copilot-extension)
        exec python3 /app/copilot-extension/server.py --port "${PORT:-8090}"
        ;;
    engine)
        # Just run the engine in foreground
        wait $ENGINE_PID
        ;;
    shell)
        exec /bin/bash
        ;;
    *)
        echo "Usage: entrypoint.sh {mcp-server|copilot-extension|engine|shell}"
        exit 1
        ;;
esac
