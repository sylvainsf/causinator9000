#!/usr/bin/env python3
"""
RCIE Load Generator

Writes synthetic mutations and signals directly to PostgreSQL for POC testing.
Simulates real-world scenarios including ToR switch failures and red herrings.

Usage:
  pip install psycopg[binary]
  python scripts/load_generator.py --scenario tor-failure
  python scripts/load_generator.py --scenario red-herring
  python scripts/load_generator.py --flood --rate 50000

Environment:
  RCIE_DATABASE_URL  PostgreSQL connection string (default: postgresql://localhost:5433/rcie_poc)
"""

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

try:
    import psycopg
except ImportError:
    print("Install psycopg: pip install 'psycopg[binary]'")
    raise SystemExit(1)

DB_URL = os.environ.get("RCIE_DATABASE_URL", "postgresql://localhost:5433/rcie_poc")


def get_conn():
    return psycopg.connect(DB_URL)


def insert_mutation(cur, node_id: str, mutation_type: str, timestamp=None, source="load_generator"):
    ts = timestamp or datetime.now(timezone.utc)
    mid = str(uuid4())
    cur.execute(
        "INSERT INTO mutations (id, node_id, mutation_type, source, timestamp, properties) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (mid, node_id, mutation_type, source, ts, "{}"),
    )
    return mid


def insert_signal(cur, node_id: str, signal_type: str, value=None, severity="warning", timestamp=None):
    ts = timestamp or datetime.now(timezone.utc)
    sid = str(uuid4())
    cur.execute(
        "INSERT INTO signals (id, node_id, signal_type, value, severity, timestamp, properties) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (sid, node_id, signal_type, value, severity, ts, "{}"),
    )
    return sid


def get_nodes(cur, class_filter=None, rack_filter=None, limit=None):
    """Fetch node IDs from the graph."""
    query = "SELECT id, class, rack_id FROM nodes WHERE 1=1"
    params = []
    if class_filter:
        query += " AND class = %s"
        params.append(class_filter)
    if rack_filter:
        query += " AND rack_id = %s"
        params.append(rack_filter)
    if limit:
        query += f" LIMIT {limit}"
    cur.execute(query, params)
    return cur.fetchall()


# ── Scenarios ─────────────────────────────────────────────────────────────

def scenario_tor_failure():
    """
    Simulate a ToR switch failure: 100 heartbeat-loss signals simultaneously
    from all VMs on a single rack. No mutation — this is a latent cause.
    """
    print("=== Scenario: ToR Switch Failure ===")
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Find a rack with VMs
            racks = cur.execute(
                "SELECT DISTINCT rack_id FROM nodes WHERE rack_id IS NOT NULL LIMIT 1"
            ).fetchall()
            if not racks:
                print("ERROR: No racks found. Run transpile.py --synthetic first.")
                return
            rack_id = racks[0][0]

            # Get all VMs on this rack
            vms = get_nodes(cur, class_filter="VirtualMachine", rack_filter=rack_id)
            print(f"Rack: {rack_id}, VMs: {len(vms)}")

            # Simultaneously kill all heartbeats
            now = datetime.now(timezone.utc)
            for vm_id, _, _ in vms:
                insert_signal(cur, vm_id, "heartbeat", value=0.0, severity="critical", timestamp=now)

            # Also kill containers on those VMs
            containers = get_nodes(cur, class_filter="Container", rack_filter=rack_id)
            for ctr_id, _, _ in containers:
                insert_signal(cur, ctr_id, "heartbeat", value=0.0, severity="critical", timestamp=now)

            conn.commit()
            print(f"Injected {len(vms)} VM + {len(containers)} container heartbeat-loss signals")


def scenario_red_herring():
    """
    Simulate a red herring: an unrelated deployment occurs at the same time
    as a genuine ToR failure. The solver should attribute to ToR, not the deploy.
    """
    print("=== Scenario: Red Herring ===")
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Find two different racks
            racks = cur.execute(
                "SELECT DISTINCT rack_id FROM nodes WHERE rack_id IS NOT NULL LIMIT 2"
            ).fetchall()
            if len(racks) < 2:
                print("ERROR: Need at least 2 racks. Run transpile.py --synthetic first.")
                return

            rack_a, rack_b = racks[0][0], racks[1][0]
            now = datetime.now(timezone.utc)

            # Rack A: genuine ToR failure (heartbeat loss on all VMs)
            vms_a = get_nodes(cur, class_filter="VirtualMachine", rack_filter=rack_a)
            for vm_id, _, _ in vms_a:
                insert_signal(cur, vm_id, "heartbeat", value=0.0, severity="critical", timestamp=now)
            print(f"Rack A ({rack_a}): {len(vms_a)} heartbeat-loss signals (genuine ToR failure)")

            # Rack B: unrelated deployment (mutation + no signal)
            containers_b = get_nodes(cur, class_filter="Container", rack_filter=rack_b, limit=3)
            for ctr_id, _, _ in containers_b:
                insert_mutation(cur, ctr_id, "ImageUpdate", timestamp=now)
            print(f"Rack B ({rack_b}): {len(containers_b)} unrelated deployments (red herring)")

            conn.commit()


def scenario_identity_rotation():
    """
    Simulate an identity rotation causing 403 errors on dependent services.
    """
    print("=== Scenario: Identity Rotation ===")
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Find a ManagedIdentity node
            identities = get_nodes(cur, class_filter="ManagedIdentity", limit=1)
            if not identities:
                print("ERROR: No ManagedIdentity nodes found.")
                return

            mi_id = identities[0][0]
            now = datetime.now(timezone.utc)

            # Mutation: secret rotation
            insert_mutation(cur, mi_id, "SecretRotation", timestamp=now)
            print(f"Mutation: SecretRotation on {mi_id}")

            # Signals: 403 errors on containers in the same rack
            rack_id = identities[0][2]
            containers = get_nodes(cur, class_filter="Container", rack_filter=rack_id, limit=5)
            signal_time = now + timedelta(seconds=30)
            for ctr_id, _, _ in containers:
                insert_signal(cur, ctr_id, "AccessDenied_403", value=1.0, severity="critical", timestamp=signal_time)
            print(f"Signals: 403 errors on {len(containers)} containers")

            conn.commit()


def scenario_slow_poison():
    """
    Simulate a deployment that causes gradual memory pressure,
    manifesting as OOM kill 25 minutes later.
    """
    print("=== Scenario: Slow Poison ===")
    with get_conn() as conn:
        with conn.cursor() as cur:
            containers = get_nodes(cur, class_filter="Container", limit=1)
            if not containers:
                print("ERROR: No Container nodes found.")
                return

            ctr_id = containers[0][0]
            now = datetime.now(timezone.utc)

            # Mutation at t=0
            insert_mutation(cur, ctr_id, "ImageUpdate", timestamp=now)
            print(f"Mutation: ImageUpdate on {ctr_id} at t=0")

            # Gradual memory signals over 25 minutes
            for minutes in range(5, 26, 5):
                ts = now + timedelta(minutes=minutes)
                value = 0.5 + (minutes / 25.0) * 0.5  # ramps from 0.6 to 1.0
                insert_signal(cur, ctr_id, "memory_rss", value=value, severity="warning", timestamp=ts)

            # OOM at t=25min
            oom_time = now + timedelta(minutes=25)
            insert_signal(cur, ctr_id, "CrashLoopBackOff", value=1.0, severity="critical", timestamp=oom_time)
            print(f"Signal: CrashLoopBackOff on {ctr_id} at t=25min")

            conn.commit()


def flood(rate: int, duration: int):
    """
    Flood the signals table at the specified rate (signals/minute) for the given duration (seconds).
    """
    print(f"=== Signal Flood: {rate} signals/min for {duration}s ===")

    with get_conn() as conn:
        with conn.cursor() as cur:
            # Get all node IDs
            cur.execute("SELECT id FROM nodes")
            all_nodes = [row[0] for row in cur.fetchall()]
            if not all_nodes:
                print("ERROR: No nodes found. Run transpile.py --synthetic first.")
                return

            signal_types = ["heartbeat", "error_rate", "memory_rss", "disk_io_latency", "CrashLoopBackOff"]
            signals_per_second = rate / 60.0
            batch_size = max(1, int(signals_per_second))

            start = time.time()
            total = 0
            while time.time() - start < duration:
                batch_start = time.time()
                for _ in range(batch_size):
                    node_id = random.choice(all_nodes)
                    sig_type = random.choice(signal_types)
                    value = random.random()
                    severity = random.choice(["warning", "critical"])
                    insert_signal(cur, node_id, sig_type, value=value, severity=severity)
                    total += 1
                conn.commit()

                # Pace to target rate
                elapsed = time.time() - batch_start
                target = batch_size / signals_per_second
                if elapsed < target:
                    time.sleep(target - elapsed)

            print(f"Injected {total} signals in {time.time() - start:.1f}s")


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="RCIE Load Generator")
    parser.add_argument(
        "--scenario",
        choices=["tor-failure", "red-herring", "identity-rotation", "slow-poison"],
        help="Run a specific test scenario",
    )
    parser.add_argument("--flood", action="store_true", help="Run signal flood")
    parser.add_argument("--rate", type=int, default=50000, help="Signals/minute for flood mode")
    parser.add_argument("--duration", type=int, default=60, help="Flood duration in seconds")
    parser.add_argument("--clear", action="store_true", help="Clear mutations and signals tables first")
    args = parser.parse_args()

    if args.clear:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM signals")
                cur.execute("DELETE FROM mutations")
                conn.commit()
        print("Cleared mutations and signals tables")

    if args.scenario == "tor-failure":
        scenario_tor_failure()
    elif args.scenario == "red-herring":
        scenario_red_herring()
    elif args.scenario == "identity-rotation":
        scenario_identity_rotation()
    elif args.scenario == "slow-poison":
        scenario_slow_poison()
    elif args.flood:
        flood(args.rate, args.duration)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
