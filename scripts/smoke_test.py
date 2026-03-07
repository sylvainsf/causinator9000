#!/usr/bin/env python3
"""
Quick smoke test — verifies the engine is running, accepts events, and
produces diagnoses.

Usage:
  python3 scripts/smoke_test.py

Prerequisites:
  Engine running with topology loaded.
"""
import os
import sys
import requests

ENGINE = os.environ.get("RCIE_ENGINE_URL", "http://localhost:8080")

def main():
    # 1. Health check
    try:
        h = requests.get(f"{ENGINE}/api/health", timeout=5).json()
    except Exception as e:
        print(f"FAIL: Engine not responding at {ENGINE} ({e})")
        sys.exit(1)

    print(f"Engine: v{h['version']} — {h['nodes']:,} nodes, {h['edges']:,} edges")
    print(f"Active: {h['active_mutations']} mutations, {h['active_signals']} signals")

    if h["nodes"] == 0:
        print("WARN: No topology loaded. Run: python3 scripts/transpile.py --synthetic")

    # 2. Inject a mutation via POST API
    r = requests.post(f"{ENGINE}/api/mutations", json={
        "node_id": "ctr-eastus-00-00-00",
        "mutation_type": "ImageUpdate",
    }, timeout=5)
    r.raise_for_status()
    print(f"\nPOST /api/mutations: {r.json()['status']}")

    # 3. Inject a signal via POST API
    r = requests.post(f"{ENGINE}/api/signals", json={
        "node_id": "ctr-eastus-00-00-00",
        "signal_type": "CrashLoopBackOff",
        "value": 1.0,
        "severity": "critical",
    }, timeout=5)
    r.raise_for_status()
    print(f"POST /api/signals: {r.json()['status']}")

    # 4. Check health again
    h2 = requests.get(f"{ENGINE}/api/health", timeout=5).json()
    print(f"\nAfter injection: {h2['active_mutations']} mutations, {h2['active_signals']} signals")

    # 5. Diagnose
    d = requests.get(f"{ENGINE}/api/diagnosis", params={"target": "ctr-eastus-00-00-00"}, timeout=5).json()
    conf = d["confidence"] * 100
    rc = d.get("root_cause") or "none"
    print(f"Diagnosis: {conf:.1f}% confidence, root_cause={rc}")

    if d["confidence"] > 0.5:
        print("\nSUCCESS: Engine accepted events and produced a high-confidence diagnosis.")
    elif d["confidence"] > 0.0:
        print(f"\nPARTIAL: Diagnosis produced but confidence is low ({conf:.1f}%).")
    else:
        print("\nWARN: Confidence is 0% — check that the topology includes ctr-eastus-00-00-00.")

    # 6. Clear
    requests.post(f"{ENGINE}/api/clear", timeout=5)
    print("Events cleared.")

if __name__ == "__main__":
    main()
