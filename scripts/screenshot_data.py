#!/usr/bin/env python3
"""
Seed data for the alert-groups screenshot.

Creates 2 simultaneous incidents that produce 8 HTTP_500 alerts, collapsing
into 2 incident groups — demonstrating root-cause grouping vs signal-type grouping.

The key scenario: both incidents produce the SAME signal (HTTP_500) within a
5-minute window on pods in the same region. Naive monitoring groups them as one
outage: "elevated 500s across the cluster." Causinator traces each group to its
own root cause:

  Incident A: A block device backing app015's SQL database goes read-only.
              The 4 pods that depend on that store start throwing 500s.
              Root cause: ds-centralus-app015 (BlockDeviceReadOnly)

  Incident B: A deployment pushes a new container image with a bug in the code
              to app016's AKS cluster. All 4 pods restart with the bad image and
              start throwing 500s.
              Root cause: aks-centralus-app016 (Deployment)

Same symptom. Different causes. Different response teams (storage vs. dev rollback).

Run this, then take the screenshot:
  python3 scripts/screenshot_data.py
  open http://localhost:8080/

Screenshot to take:
  → docs/screenshots/alert-groups.png
  Shows 2 collapsed incident groups in the left panel, BOTH showing HTTP_500:
    [4 pods] ds-centralus-app015 (BlockDeviceReadOnly) — all HTTP_500
    [4 pods] aks-centralus-app016 (Deployment)         — all HTTP_500

  This highlights: "8 alerts, same signal type, but 2 independent incidents."
"""
import os
import requests
import sys

E = os.environ.get("RCIE_ENGINE_URL", os.environ.get("C9K_ENGINE_URL", "http://localhost:8080"))

def post(path, json):
    r = requests.post(f"{E}/api/{path}", json=json, timeout=5)
    r.raise_for_status()
    return r.json()

def main():
    # Check engine
    try:
        h = requests.get(f"{E}/api/health", timeout=5).json()
        print(f"Engine: {h['nodes']:,} nodes")
    except:
        print(f"ERROR: Engine not responding at {E}")
        sys.exit(1)

    # Clear everything
    post("clear", {})

    # ─── Incident A: Block device read-only on SQL database ───
    # app015 uses SqlDatabase (15 % 3 == 0). Its managed disk goes RO.
    # The 4 pods that query this store start returning HTTP 500.
    print("\nIncident A: ds-centralus-app015 BlockDeviceReadOnly → 4 pods HTTP_500")
    post("mutations", {"node_id": "ds-centralus-app015", "mutation_type": "BlockDeviceReadOnly"})
    for pod in range(4):
        post("signals", {
            "node_id": f"pod-centralus-app015-{pod:02}",
            "signal_type": "HTTP_500",
            "severity": "critical",
        })

    # ─── Incident B: Bad container image deployment ───
    # A new image is deployed to app016's AKS cluster. The code has a bug.
    # All 4 pods restart with the bad image and start 500ing.
    print("Incident B: aks-centralus-app016 Deployment → 4 pods HTTP_500")
    post("mutations", {"node_id": "aks-centralus-app016", "mutation_type": "Deployment"})
    for pod in range(4):
        post("signals", {
            "node_id": f"pod-centralus-app016-{pod:02}",
            "signal_type": "HTTP_500",
            "severity": "critical",
        })

    # Verify grouping
    groups = requests.get(f"{E}/api/alert-groups", timeout=5).json()
    total_alerts = sum(g["count"] for g in groups)
    print(f"\n{total_alerts} alerts → {len(groups)} incident groups:")
    for g in groups:
        pct = g["confidence"] * 100
        sigs = ", ".join(g["signal_types"])
        nodes = ", ".join(g["affected_nodes"][:3])
        more = f" +{g['count']-3} more" if g["count"] > 3 else ""
        print(f"  [{g['count']} pods] {g['root_cause']}: {pct:.1f}%")
        print(f"    signals: {sigs}")
        print(f"    nodes: {nodes}{more}")

    if len(groups) == 2:
        print("\n✓ Both groups show HTTP_500, but different root causes.")
        print("  Naive grouping: 1 big incident (all 500s).")
        print("  Causal grouping: 2 separate incidents (disk RO vs bad deploy).")
    elif len(groups) == 1:
        print("\n✗ Only 1 group — the engine merged them. Check CPTs or edges.")
    else:
        print(f"\n? Expected 2 groups, got {len(groups)}. Check topology.")

    print(f"""
Screenshot instructions:
  1. Open http://localhost:8080/
  2. You should see 2 incident groups in the left panel — both HTTP_500
  3. Note they have different root causes despite identical signal types
  4. Take screenshot → docs/screenshots/alert-groups.png
""")

if __name__ == "__main__":
    main()
