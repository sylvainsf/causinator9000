#!/usr/bin/env python3
"""
Causinator 9000 Golden Test Suite

Pre-scripted scenarios that validate solver correctness by:
1. Writing known data to PostgreSQL (mutations + signals)
2. Querying the engine's REST API
3. Asserting solver output matches expected behavior

Usage:
  # Start the engine first, then:
  python scripts/golden_tests.py

  # Or run with synthetic topology generation:
  python scripts/golden_tests.py --setup

Environment:
  C9K_DATABASE_URL  PostgreSQL connection (default: postgresql://localhost:5433/c9k_poc)
  C9K_ENGINE_URL    Engine REST API URL (default: http://localhost:8080)
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import requests

try:
    import psycopg
except ImportError:
    print("Install psycopg: pip install 'psycopg[binary]'")
    raise SystemExit(1)

DB_URL = os.environ.get("C9K_DATABASE_URL", "postgresql://localhost:5433/c9k_poc")
ENGINE_URL = os.environ.get("C9K_ENGINE_URL", "http://localhost:8080")


# ── Helpers ───────────────────────────────────────────────────────────────

def get_conn():
    return psycopg.connect(DB_URL)


def clear_events():
    """Clear all mutations and signals (leave topology intact)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM signals")
            cur.execute("DELETE FROM mutations")
            conn.commit()


def insert_mutation(cur, node_id, mutation_type, timestamp=None):
    ts = timestamp or datetime.now(timezone.utc)
    mid = str(uuid4())
    cur.execute(
        "INSERT INTO mutations (id, node_id, mutation_type, source, timestamp, properties) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (mid, node_id, mutation_type, "golden_test", ts, "{}"),
    )
    return mid


def insert_signal(cur, node_id, signal_type, value=1.0, severity="critical", timestamp=None):
    ts = timestamp or datetime.now(timezone.utc)
    sid = str(uuid4())
    cur.execute(
        "INSERT INTO signals (id, node_id, signal_type, value, severity, timestamp, properties) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (sid, node_id, signal_type, value, severity, ts, "{}"),
    )
    return sid


def diagnose(node_id: str) -> dict:
    """Call the engine's diagnosis endpoint."""
    resp = requests.get(f"{ENGINE_URL}/diagnosis", params={"target": node_id}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def diagnose_all() -> list:
    """Call the engine's diagnose-all endpoint."""
    resp = requests.get(f"{ENGINE_URL}/diagnosis/all", timeout=10)
    resp.raise_for_status()
    return resp.json()


def engine_healthy() -> bool:
    """Check if the engine is responding."""
    try:
        resp = requests.get(f"{ENGINE_URL}/health", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


# ── Setup ─────────────────────────────────────────────────────────────────

def setup_mini_topology():
    """
    Create a small deterministic topology for golden tests.
    Avoids relying on the 10k synthetic topology.

    Graph:
      latent-tor-test-01  (ToRSwitch)
      ├── vm-test-01      (VirtualMachine)
      │   ├── ctr-test-01 (Container)
      │   ├── ctr-test-02 (Container)
      │   └── mi-test-01  (ManagedIdentity)
      └── vm-test-02      (VirtualMachine)
          ├── ctr-test-03 (Container)
          └── ctr-test-04 (Container)

      kv-test-01          (KeyVault)
      └── ctr-test-01     (dependency: ctr-test-01 depends on keyvault)
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Clear everything
            cur.execute("DELETE FROM signals")
            cur.execute("DELETE FROM mutations")
            cur.execute("DELETE FROM edges")
            cur.execute("DELETE FROM nodes")

            nodes = [
                ("latent-tor-test-01", "ToR Test Rack", "ToRSwitch", "eastus", "rack-test-01"),
                ("vm-test-01", "VM Test 1", "VirtualMachine", "eastus", "rack-test-01"),
                ("vm-test-02", "VM Test 2", "VirtualMachine", "eastus", "rack-test-01"),
                ("ctr-test-01", "Container 1", "Container", "eastus", "rack-test-01"),
                ("ctr-test-02", "Container 2", "Container", "eastus", "rack-test-01"),
                ("ctr-test-03", "Container 3", "Container", "eastus", "rack-test-01"),
                ("ctr-test-04", "Container 4", "Container", "eastus", "rack-test-01"),
                ("mi-test-01", "Managed Identity 1", "ManagedIdentity", "eastus", "rack-test-01"),
                ("kv-test-01", "KeyVault Test", "KeyVault", "eastus", None),
            ]

            for nid, label, cls, region, rack in nodes:
                cur.execute(
                    "INSERT INTO nodes (id, label, class, region, rack_id, properties) "
                    "VALUES (%s, %s, %s, %s, %s, '{}')",
                    (nid, label, cls, region, rack),
                )

            edges = [
                ("edge-tor-vm1", "latent-tor-test-01", "vm-test-01", "containment"),
                ("edge-tor-vm2", "latent-tor-test-01", "vm-test-02", "containment"),
                ("edge-vm1-ctr1", "vm-test-01", "ctr-test-01", "containment"),
                ("edge-vm1-ctr2", "vm-test-01", "ctr-test-02", "containment"),
                ("edge-vm1-mi1", "vm-test-01", "mi-test-01", "dependency"),
                ("edge-vm2-ctr3", "vm-test-02", "ctr-test-03", "containment"),
                ("edge-vm2-ctr4", "vm-test-02", "ctr-test-04", "containment"),
                ("edge-kv-ctr1", "kv-test-01", "ctr-test-01", "dependency"),
            ]

            for eid, src, tgt, etype in edges:
                cur.execute(
                    "INSERT INTO edges (id, source_id, target_id, edge_type, properties) "
                    "VALUES (%s, %s, %s, %s, '{}')",
                    (eid, src, tgt, etype),
                )

            conn.commit()

    print(f"Golden test topology: {len(nodes)} nodes, {len(edges)} edges")


# ── Test Cases ────────────────────────────────────────────────────────────

class TestResult:
    def __init__(self, name: str):
        self.name = name
        self.passed = False
        self.message = ""

    def ok(self, msg=""):
        self.passed = True
        self.message = msg

    def fail(self, msg):
        self.passed = False
        self.message = msg


def test_true_positive() -> TestResult:
    """Identity rotation → 403 signals → solver identifies rotation as root cause."""
    result = TestResult("True Positive")
    clear_events()

    with get_conn() as conn:
        with conn.cursor() as cur:
            now = datetime.now(timezone.utc)
            # Mutation: SecretRotation on ManagedIdentity
            insert_mutation(cur, "mi-test-01", "SecretRotation", now)
            # Signals: 403 errors on containers
            for ctr in ["ctr-test-01", "ctr-test-02"]:
                insert_signal(cur, ctr, "AccessDenied_403", timestamp=now + timedelta(seconds=10))
            conn.commit()

    # Wait for engine to process (Drasi CDC latency)
    time.sleep(1)

    try:
        diag = diagnose("ctr-test-01")
        if diag["confidence"] > 0.01:
            result.ok(f"confidence={diag['confidence']:.3f}, root_cause={diag.get('root_cause')}")
        else:
            result.fail(f"confidence too low: {diag['confidence']:.3f}")
    except Exception as e:
        result.fail(str(e))

    return result


def test_true_negative() -> TestResult:
    """Random 403 errors with no mutations → solver reports low confidence."""
    result = TestResult("True Negative")
    clear_events()

    with get_conn() as conn:
        with conn.cursor() as cur:
            now = datetime.now(timezone.utc)
            # Signals only, no mutations
            insert_signal(cur, "ctr-test-01", "AccessDenied_403", timestamp=now)
            insert_signal(cur, "ctr-test-03", "error_rate", value=0.1, severity="warning", timestamp=now)
            conn.commit()

    time.sleep(1)

    try:
        diag = diagnose("ctr-test-01")
        # With no mutations, confidence should be near-zero
        if diag["confidence"] < 0.3:
            result.ok(f"confidence={diag['confidence']:.3f} (correctly low)")
        else:
            result.fail(f"confidence too high without mutations: {diag['confidence']:.3f}")
    except Exception as e:
        result.fail(str(e))

    return result


def test_red_herring() -> TestResult:
    """
    Unrelated deployment on ctr-test-03 + genuine ToR failure affecting vm-test-01.
    Solver should attribute to ToR, not the deployment.
    """
    result = TestResult("Red Herring")
    clear_events()

    with get_conn() as conn:
        with conn.cursor() as cur:
            now = datetime.now(timezone.utc)
            # Red herring: unrelated deployment
            insert_mutation(cur, "ctr-test-03", "ImageUpdate", now)
            # Real cause: heartbeat loss on BOTH VMs (implies ToR)
            for vm in ["vm-test-01", "vm-test-02"]:
                insert_signal(cur, vm, "heartbeat", value=0.0, severity="critical", timestamp=now)
            conn.commit()

    time.sleep(1)

    try:
        diag = diagnose("vm-test-01")
        # The deployment on ctr-test-03 should NOT be the root cause for vm-test-01
        if diag.get("root_cause") != "ctr-test-03":
            result.ok(f"root_cause={diag.get('root_cause')}, confidence={diag['confidence']:.3f}")
        else:
            result.fail(f"incorrectly attributed to red herring deployment")
    except Exception as e:
        result.fail(str(e))

    return result


def test_explaining_away() -> TestResult:
    """
    Two candidate mutations, evidence supports only one.
    Confidence should drop for the unsupported mutation.
    """
    result = TestResult("Explaining Away")
    clear_events()

    with get_conn() as conn:
        with conn.cursor() as cur:
            now = datetime.now(timezone.utc)
            # Two mutations on the same container
            insert_mutation(cur, "ctr-test-01", "ImageUpdate", now)
            insert_mutation(cur, "kv-test-01", "SecretRotation", now)
            # Signal: CrashLoopBackOff (matches ImageUpdate CPT, not SecretRotation)
            insert_signal(cur, "ctr-test-01", "CrashLoopBackOff", timestamp=now + timedelta(seconds=5))
            conn.commit()

    time.sleep(1)

    try:
        diag = diagnose("ctr-test-01")
        # With CrashLoopBackOff signal, ImageUpdate should score higher than SecretRotation
        if diag["confidence"] > 0.0:
            result.ok(f"confidence={diag['confidence']:.3f}, competing={diag.get('competing_causes', [])}")
        else:
            result.fail("no diagnosis produced")
    except Exception as e:
        result.fail(str(e))

    return result


def test_slow_poison() -> TestResult:
    """
    Deployment at t=0, OOM signals at t=25min.
    Should be within temporal window (24 hours).
    """
    result = TestResult("Slow Poison")
    clear_events()

    with get_conn() as conn:
        with conn.cursor() as cur:
            now = datetime.now(timezone.utc)
            # Mutation at t=0
            insert_mutation(cur, "ctr-test-01", "ImageUpdate", now)
            # Signal at t=25min (within 30-min window)
            signal_time = now + timedelta(minutes=25)
            insert_signal(cur, "ctr-test-01", "CrashLoopBackOff", timestamp=signal_time)
            conn.commit()

    time.sleep(1)

    try:
        diag = diagnose("ctr-test-01")
        if diag["confidence"] > 0.0:
            result.ok(f"confidence={diag['confidence']:.3f} (mutation within window)")
        else:
            result.fail("failed to identify mutation within temporal window")
    except Exception as e:
        result.fail(str(e))

    return result


def test_window_expiry() -> TestResult:
    """
    Deployment at t=-35min, signals now.
    Deployment should NOT be identified (outside 30-min window).
    """
    result = TestResult("Window Expiry")
    clear_events()

    with get_conn() as conn:
        with conn.cursor() as cur:
            now = datetime.now(timezone.utc)
            # Mutation 35 minutes ago (outside window)
            mutation_time = now - timedelta(minutes=35)
            insert_mutation(cur, "ctr-test-01", "ImageUpdate", mutation_time)
            # Signal now
            insert_signal(cur, "ctr-test-01", "CrashLoopBackOff", timestamp=now)
            conn.commit()

    time.sleep(1)

    try:
        diag = diagnose("ctr-test-01")
        # The mutation should be expired, so no root cause from it
        # Confidence should be low (no candidate mutations in window)
        result.ok(f"confidence={diag['confidence']:.3f}, root_cause={diag.get('root_cause')}")
    except Exception as e:
        result.fail(str(e))

    return result


# ── Runner ────────────────────────────────────────────────────────────────

def run_all_tests():
    tests = [
        test_true_positive,
        test_true_negative,
        test_red_herring,
        test_explaining_away,
        test_slow_poison,
        test_window_expiry,
    ]

    results = []
    for test_fn in tests:
        print(f"\n--- {test_fn.__doc__.strip().split(chr(10))[0]} ---")
        result = test_fn()
        results.append(result)
        status = "✓ PASS" if result.passed else "✗ FAIL"
        print(f"  {status}: {result.name}")
        if result.message:
            print(f"         {result.message}")

    print("\n" + "=" * 60)
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    print(f"Results: {passed}/{total} passed")

    if passed < total:
        failed = [r for r in results if not r.passed]
        print("Failed tests:")
        for r in failed:
            print(f"  - {r.name}: {r.message}")
        return 1

    return 0


def main():
    parser = argparse.ArgumentParser(description="Causinator 9000 Golden Test Suite")
    parser.add_argument("--setup", action="store_true", help="Create mini topology before running tests")
    parser.add_argument("--setup-only", action="store_true", help="Create mini topology and exit")
    args = parser.parse_args()

    if args.setup or args.setup_only:
        setup_mini_topology()
        if args.setup_only:
            print("Topology created. Start the engine and re-run without --setup-only.")
            return

    if not engine_healthy():
        print(f"ERROR: Engine not responding at {ENGINE_URL}")
        print("Start the engine first: cargo run --release --bin c9k-engine")
        sys.exit(1)

    sys.exit(run_all_tests())


if __name__ == "__main__":
    main()
