#!/usr/bin/env python3
"""
Seed data for the alert-groups screenshot.

Creates two simultaneous incidents that produce 9 total alerts,
collapsing into 3 incident groups — the perfect demo of the grouping feature.

Run this, then take the screenshot:
  python3 scripts/screenshot_data.py
  open http://localhost:8080/

Screenshot to take:
  → docs/screenshots/alert-groups.png
  Shows 3 collapsed incident groups in the left panel:
    [5 nodes] kv-eastus-01 (SecretRotation) — 89.8% — all AccessDenied_403
    [3 nodes] ca-westeurope (CertificateRotation) — 77.0% — all TLSError
    [1 node]  pod-centralus-app020-01 (ImageUpdate) — 96.2% — CrashLoopBackOff

  Click the KV group to expand it → shows 5 individual pod cards underneath.
  This highlights: "9 alerts, but really 3 incidents to investigate."
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

    # ─── Incident 1: KeyVault SecretRotation → 5 pods get 403 ───
    print("\nIncident 1: kv-eastus-01 SecretRotation → 5 pods")
    post("mutations", {"node_id": "kv-eastus-01", "mutation_type": "SecretRotation"})
    for i in range(5):
        post("signals", {"node_id": f"pod-eastus-app00{i}-00", "signal_type": "AccessDenied_403", "severity": "critical"})

    # ─── Incident 2: CertAuthority rotation → 3 pods get TLS errors ───
    print("Incident 2: ca-westeurope CertificateRotation → 3 pods")
    post("mutations", {"node_id": "ca-westeurope", "mutation_type": "CertificateRotation"})
    for i in range(3):
        post("signals", {"node_id": f"pod-westeurope-app01{i}-01", "signal_type": "TLSError", "severity": "critical"})

    # ─── Incident 3: Direct deploy crash ───
    print("Incident 3: pod-centralus-app020-01 ImageUpdate → CrashLoopBackOff")
    post("mutations", {"node_id": "pod-centralus-app020-01", "mutation_type": "ImageUpdate"})
    post("signals", {"node_id": "pod-centralus-app020-01", "signal_type": "CrashLoopBackOff", "severity": "critical"})

    # Verify grouping
    groups = requests.get(f"{E}/api/alert-groups", timeout=5).json()
    total_alerts = sum(g["count"] for g in groups)
    print(f"\n{total_alerts} alerts → {len(groups)} incident groups:")
    for g in groups:
        pct = g["confidence"] * 100
        sigs = ", ".join(g["signal_types"])
        nodes = ", ".join(g["affected_nodes"][:3])
        more = f" +{g['count']-3} more" if g["count"] > 3 else ""
        print(f"  [{g['count']} nodes] {g['root_cause']}: {pct:.1f}%")
        print(f"    signals: {sigs}")
        print(f"    nodes: {nodes}{more}")

    print(f"""
Screenshot instructions:
  1. Open http://localhost:8080/
  2. You should see 3 collapsed incident groups in the left panel
  3. Click the "kv-eastus-01" group to expand it — shows 5 pod cards
  4. Take screenshot → docs/screenshots/alert-groups.png
""")

if __name__ == "__main__":
    main()
