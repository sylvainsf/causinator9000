#!/usr/bin/env python3
"""Seed alerts including cross-boundary scenarios and verify the alert-graph."""
import os
import requests

E = os.environ.get("C9K_ENGINE_URL", "http://localhost:8080")
requests.post(f"{E}/api/clear")

# Scenario A: KeyVault secret rotation → 3 pods get 403
# Path: kv-eastus-01 → pod (1 hop, dependency edge)
requests.post(f"{E}/api/mutations", json={"node_id": "kv-eastus-01", "mutation_type": "SecretRotation"})
for i in range(3):
    requests.post(f"{E}/api/signals", json={"node_id": f"pod-eastus-app00{i}-00", "signal_type": "AccessDenied_403", "severity": "critical"})

# Scenario B: CertAuthority cert rotation → Gateway → AKS → Pod gets TLS error
# Path: ca-westeurope → appgw-westeurope-app010 → aks-westeurope-app010 → pod (3 hops!)
requests.post(f"{E}/api/mutations", json={"node_id": "ca-westeurope", "mutation_type": "CertificateRotation"})
requests.post(f"{E}/api/signals", json={"node_id": "pod-westeurope-app010-01", "signal_type": "TLSError", "severity": "critical"})
requests.post(f"{E}/api/signals", json={"node_id": "pod-westeurope-app010-02", "signal_type": "TLSError", "severity": "critical"})

# Scenario C: IdentityProvider policy change → ManagedIdentity → Pod gets 403
# Path: idp-japaneast → mi-japaneast-app050 → pod (2 hops!)
requests.post(f"{E}/api/mutations", json={"node_id": "idp-japaneast", "mutation_type": "PolicyChange"})
requests.post(f"{E}/api/signals", json={"node_id": "pod-japaneast-app050-00", "signal_type": "AccessDenied_403", "severity": "critical"})

# Scenario D: Direct deploy crash (1 hop, same node)
requests.post(f"{E}/api/mutations", json={"node_id": "pod-centralus-app020-01", "mutation_type": "ImageUpdate"})
requests.post(f"{E}/api/signals", json={"node_id": "pod-centralus-app020-01", "signal_type": "CrashLoopBackOff", "severity": "critical"})

g = requests.get(f"{E}/api/alert-graph").json()
nodes = [e for e in g if e["group"] == "nodes" and e["data"].get("class") != "cluster"]
clusters = [e for e in g if e["group"] == "nodes" and e["data"].get("class") == "cluster"]
edges = [e for e in g if e["group"] == "edges"]
print(f"Alert graph: {len(nodes)} nodes, {len(edges)} edges, {len(clusters)} clusters")

a = requests.get(f"{E}/api/alerts").json()
print(f"\n{len(a)} alerts:")
for x in a:
    pct = x["confidence"] * 100
    rc = x.get("root_cause") or "none"
    path = x.get("causal_path", [])
    path_str = " → ".join(path) if len(path) > 1 else ""
    print(f"  {x['node_id']}: {pct:.1f}%  root={rc}")
    if path_str:
        print(f"    path: {path_str}")
