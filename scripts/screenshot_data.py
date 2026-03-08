#!/usr/bin/env python3
"""
Seed data for the alert-groups screenshot.

Creates 4 simultaneous incidents that produce 12 total alerts,
collapsing into 4 incident groups — the perfect demo of the grouping feature.

The key demo: incidents 1 and 4 both produce AccessDenied_403 signals, but
the engine correctly separates them into two groups by root cause (kv-eastus-01
vs kv-centralus-01). Naive signal-type grouping would merge them into one.

Run this, then take the screenshot:
  python3 scripts/screenshot_data.py
  open http://localhost:8080/

Screenshot to take:
  → docs/screenshots/alert-groups.png
  Shows 4 collapsed incident groups in the left panel:
    [5 nodes] kv-eastus-01 (SecretRotation) — 89.8% — AccessDenied_403
    [3 nodes] kv-centralus-01 (SecretRotation) — 89.8% — AccessDenied_403
    [3 nodes] ca-westeurope (CertificateRotation) — 77.0% — TLSError
    [1 node]  pod-centralus-app020-01 (ImageUpdate) — 96.2% — CrashLoopBackOff

  Two groups show the SAME signal type (AccessDenied_403) from DIFFERENT root causes.
  This highlights: "12 alerts, but really 4 incidents — including 2 that look identical."
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

    # ─── Incident 3: SECOND KeyVault rotation → ALSO AccessDenied_403 ───
    # This is the key demo: same signal type as Incident 1, different root cause.
    # Naive grouping-by-signal-type would merge these; root-cause grouping separates them.
    print("Incident 3: kv-centralus-01 SecretRotation → 3 pods (ALSO AccessDenied_403)")
    post("mutations", {"node_id": "kv-centralus-01", "mutation_type": "SecretRotation"})
    for i in range(3):
        post("signals", {"node_id": f"pod-centralus-app02{i}-00", "signal_type": "AccessDenied_403", "severity": "critical"})

    # ─── Incident 4: Direct deploy crash ───
    print("Incident 4: pod-centralus-app020-01 ImageUpdate → CrashLoopBackOff")
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
  3. Note the two separate AccessDenied_403 groups (kv-eastus-01 vs kv-centralus-01)
  4. Click the "kv-eastus-01" group to expand it — shows 5 pod cards
  5. Take screenshot → docs/screenshots/alert-groups.png
""")

if __name__ == "__main__":
    main()
