#!/bin/bash
set -e

MODE="${1:-mcp-server}"

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
    engine)
        # Just run the engine in foreground
        wait $ENGINE_PID
        ;;
    shell)
        exec /bin/bash
        ;;
    *)
        echo "Usage: entrypoint.sh {mcp-server|engine|shell}"
        exit 1
        ;;
esac
