#!/usr/bin/env python3
"""
Generate the exact data needed for each README screenshot.

Run this, then take screenshots:
  1. Alert Trees view   → docs/screenshots/alert-trees.png
  2. Alert Cards panel   → docs/screenshots/alert-cards.png
  3. Node Detail panel   → docs/screenshots/node-detail.png  (click pod-westeurope-app010-01)
  4. Neighborhood view   → docs/screenshots/neighborhood.png (click "Neighborhood", select pod-eastus-app001-00)

Usage:
  python3 scripts/screenshot_data.py
  open http://localhost:8080/
"""
import os
import requests
import sys

E = os.environ.get("C9K_ENGINE_URL", "http://localhost:8080")

def post(path, json):
    r = requests.post(f"{E}/api/{path}", json=json, timeout=5)
    r.raise_for_status()
    return r.json()

def check():
    try:
        h = requests.get(f"{E}/api/health", timeout=5).json()
        print(f"Engine: {h['nodes']:,} nodes, {h['edges']:,} edges")
        return True
    except:
        print(f"ERROR: Engine not responding at {E}")
        return False

def main():
    if not check():
        sys.exit(1)

    # Clear everything
    post("clear", {})
    print("Cleared all events\n")

    # ── Screenshot 1 & 2: Alert Trees + Alert Cards ──────────────────
    # Four distinct incidents across different regions and hop depths
    print("=== Scenario A: KeyVault SecretRotation → 3 pods (1 hop) ===")
    post("mutations", {"node_id": "kv-eastus-01", "mutation_type": "SecretRotation"})
    for i in range(3):
        post("signals", {"node_id": f"pod-eastus-app00{i}-00", "signal_type": "AccessDenied_403", "severity": "critical"})
    print("  kv-eastus-01 SecretRotation → 3× AccessDenied_403")

    print("=== Scenario B: CertAuthority rotation → Gateway → AKS → 2 pods (3 hops) ===")
    post("mutations", {"node_id": "ca-westeurope", "mutation_type": "CertificateRotation"})
    post("signals", {"node_id": "pod-westeurope-app010-01", "signal_type": "TLSError", "severity": "critical"})
    post("signals", {"node_id": "pod-westeurope-app010-02", "signal_type": "TLSError", "severity": "critical"})
    print("  ca-westeurope CertificateRotation → 2× TLSError")

    print("=== Scenario C: IdentityProvider PolicyChange → MI → pod (2 hops) ===")
    post("mutations", {"node_id": "idp-japaneast", "mutation_type": "PolicyChange"})
    post("signals", {"node_id": "pod-japaneast-app050-00", "signal_type": "AccessDenied_403", "severity": "critical"})
    print("  idp-japaneast PolicyChange → 1× AccessDenied_403")

    print("=== Scenario D: Direct ImageUpdate crash (0 hops) ===")
    post("mutations", {"node_id": "pod-centralus-app020-01", "mutation_type": "ImageUpdate"})
    post("signals", {"node_id": "pod-centralus-app020-01", "signal_type": "CrashLoopBackOff", "severity": "critical"})
    print("  pod-centralus-app020-01 ImageUpdate → CrashLoopBackOff")

    # Verify
    alerts = requests.get(f"{E}/api/alerts", timeout=5).json()
    print(f"\n{len(alerts)} alerts ready:")
    for a in alerts:
        pct = a["confidence"] * 100
        rc = a.get("root_cause") or "none"
        print(f"  {a['node_id']}: {pct:.1f}% — {rc}")

    graph = requests.get(f"{E}/api/alert-graph", timeout=5).json()
    nodes = [e for e in graph if e["group"] == "nodes" and e["data"].get("class") != "cluster"]
    print(f"\nAlert graph: {len(nodes)} nodes")

    print(f"""
Screenshots to take:

1. ALERT TREES (default view when you open http://localhost:8080/)
   → docs/screenshots/alert-trees.png
   Shows 4 discrete causal tree clusters

2. ALERT CARDS (left panel)
   → docs/screenshots/alert-cards.png
   Shows the confidence-sorted alert list with bars

3. NODE DETAIL (click on pod-westeurope-app010-01 in the graph or cards)
   → docs/screenshots/node-detail.png
   Shows the 3-hop causal path: ca-westeurope → appgw → aks → pod

4. NEIGHBORHOOD (click "Neighborhood" button, then select pod-eastus-app001-00)
   → docs/screenshots/neighborhood.png
   Shows the local dependency subgraph around a pod
""")

if __name__ == "__main__":
    main()
