#!/usr/bin/env python3
"""
RCIE Radius Webhook Receiver

Receives Radius deployment webhooks and writes mutation rows to PostgreSQL.
This is a bridge script — not part of the engine.

Usage:
  pip install fastapi uvicorn psycopg[binary]
  python scripts/radius_receiver.py

Environment:
  RCIE_DATABASE_URL  PostgreSQL connection string (default: postgresql://localhost:5433/rcie_poc)
  RCIE_RECEIVER_PORT Port to listen on (default: 8081)
"""

import json
import os
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

try:
    import psycopg
except ImportError:
    print("Install psycopg: pip install 'psycopg[binary]'")
    raise SystemExit(1)

DB_URL = os.environ.get("RCIE_DATABASE_URL", "postgresql://localhost:5433/rcie_poc")
PORT = int(os.environ.get("RCIE_RECEIVER_PORT", "8081"))

app = FastAPI(title="RCIE Radius Webhook Receiver")


class RadiusDeploymentEvent(BaseModel):
    """Payload from a Radius deployment webhook."""
    target_node_id: str
    mutation_type: str
    completed_at: Optional[datetime] = None
    metadata: Optional[dict] = None


@app.post("/webhook/radius")
async def radius_webhook(payload: RadiusDeploymentEvent):
    """Receive a Radius deployment event and INSERT into mutations table."""
    timestamp = payload.completed_at or datetime.now(timezone.utc)
    mutation_id = str(uuid4())

    try:
        with psycopg.connect(DB_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO mutations (id, node_id, mutation_type, source, timestamp, properties) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        mutation_id,
                        payload.target_node_id,
                        payload.mutation_type,
                        "radius",
                        timestamp,
                        json.dumps(payload.metadata or {}),
                    ),
                )
                conn.commit()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"status": "accepted", "id": mutation_id}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "radius-receiver"}


if __name__ == "__main__":
    print(f"Starting Radius webhook receiver on port {PORT}")
    print(f"Database: {DB_URL}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
