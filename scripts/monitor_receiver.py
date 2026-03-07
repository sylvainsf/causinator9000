#!/usr/bin/env python3
"""
RCIE Azure Monitor Webhook Receiver

Receives Azure Monitor Action Group webhooks and writes signal rows to PostgreSQL.
This is a bridge script — not part of the engine.

Usage:
  pip install fastapi uvicorn psycopg[binary]
  python scripts/monitor_receiver.py

Environment:
  RCIE_DATABASE_URL  PostgreSQL connection string (default: postgresql://localhost:5433/rcie_poc)
  RCIE_RECEIVER_PORT Port to listen on (default: 8082)
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
PORT = int(os.environ.get("RCIE_RECEIVER_PORT", "8082"))

app = FastAPI(title="RCIE Azure Monitor Webhook Receiver")


class AzureMonitorAlert(BaseModel):
    """Simplified Azure Monitor Action Group webhook payload."""
    target_resource_id: str
    metric_name: str
    metric_value: Optional[float] = None
    severity: Optional[str] = "warning"
    fired_at: Optional[datetime] = None
    context: Optional[dict] = None


@app.post("/webhook/azure-monitor")
async def monitor_webhook(payload: AzureMonitorAlert):
    """Receive an Azure Monitor alert and INSERT into signals table."""
    timestamp = payload.fired_at or datetime.now(timezone.utc)
    signal_id = str(uuid4())

    try:
        with psycopg.connect(DB_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO signals (id, node_id, signal_type, value, severity, timestamp, properties) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        signal_id,
                        payload.target_resource_id,
                        payload.metric_name,
                        payload.metric_value,
                        payload.severity,
                        timestamp,
                        json.dumps(payload.context or {}),
                    ),
                )
                conn.commit()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"status": "accepted", "id": signal_id}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "monitor-receiver"}


if __name__ == "__main__":
    print(f"Starting Azure Monitor webhook receiver on port {PORT}")
    print(f"Database: {DB_URL}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
