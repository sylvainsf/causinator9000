#!/usr/bin/env python3
"""
Azure Policy → Causinator 9000 latent deny-policy nodes + edges.

Queries Azure Policy compliance state via ARG and creates:
- Latent DenyPolicy nodes for each active deny-effect policy assignment
- Edges from deny policy nodes → non-compliant resources
- Signals (PolicyViolation) on resources blocked by deny policies

These are highly specific, highly weighted latent causes: when a deployment
fails because a deny policy blocked it, the engine traces it directly to
the policy (very high confidence) rather than blaming a code change.

Uses `az` CLI — requires `az login`.

Usage:
  python3 sources/azure_policy_source.py
  python3 sources/azure_policy_source.py --subscription $SUB_ID
  python3 sources/azure_policy_source.py --dry-run
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict

ENGINE = os.environ.get("C9K_ENGINE_URL", "http://localhost:8080")


def az_graph_query(query, subscriptions=None):
    cmd = ["az", "graph", "query", "-q", query, "--output", "json", "--first", "1000"]
    if subscriptions:
        cmd.extend(["--subscriptions"] + subscriptions)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        print(f"ERROR: {result.stderr[:200]}", file=sys.stderr)
        return []
    data = json.loads(result.stdout)
    rows = data.get("data", data)
    if isinstance(rows, dict):
        rows = rows.get("data", [])
    return rows if isinstance(rows, list) else []


def post_engine(path, payload, engine):
    import urllib.request
    url = f"{engine}/api/{path}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  ERROR posting to {url}: {e}", file=sys.stderr)
        return None


def policy_node_id(policy_assignment):
    slug = re.sub(r'[^a-z0-9]+', '-', policy_assignment.lower()).strip('-')
    return f"policy://{slug}"


def ingest_policies(subscriptions, engine, dry_run):
    """
    Query deny-effect policy violations and create latent nodes + edges.
    """

    # Get all deny-effect non-compliant policy states
    print("Querying deny-effect policy violations...", file=sys.stderr)
    rows = az_graph_query("""
    PolicyResources
    | where type == 'microsoft.policyinsights/policystates'
    | where properties.complianceState == 'NonCompliant'
    | where properties.policyDefinitionAction == 'deny'
    | project
        resourceId = tolower(tostring(properties.resourceId)),
        resourceType = tostring(properties.resourceType),
        policyAssignment = tostring(properties.policyAssignmentName),
        policySetName = tostring(properties.policySetDefinitionName),
        policyDefName = tostring(properties.policyDefinitionName),
        timestamp = tostring(properties.timestamp)
    """, subscriptions)

    print(f"  → {len(rows)} deny violations", file=sys.stderr)

    if not rows:
        print("No deny policy violations found.", file=sys.stderr)
        return 0, 0

    # Group by policy assignment
    by_policy = defaultdict(list)
    for r in rows:
        by_policy[r.get("policyAssignment", "unknown")].append(r)

    nodes = []
    edges = []
    signals_to_send = []

    for policy_name, violations in by_policy.items():
        pid = policy_node_id(policy_name)
        resource_types = set(v["resourceType"] for v in violations)
        rt_str = ", ".join(sorted(resource_types))

        if dry_run:
            print(f"\n  POLICY: {policy_name} → {len(violations)} violations")
            print(f"    Types: {rt_str}")
            for v in violations[:3]:
                rid = v["resourceId"].split("/")[-1] if "/" in v["resourceId"] else v["resourceId"]
                print(f"    - {rid} ({v['resourceType']})")
            if len(violations) > 3:
                print(f"    ... +{len(violations) - 3} more")
            continue

        # Create latent DenyPolicy node
        nodes.append({
            "id": pid,
            "label": f"Deny: {policy_name}",
            "class": "DenyPolicy",
            "region": "azure-policy",
            "rack_id": None,
            "properties": {
                "source": "azure-policy",
                "policy_assignment": policy_name,
                "resource_types": list(resource_types),
                "violation_count": len(violations),
                "latent": True,
            },
        })

        # Create edges from policy → each non-compliant resource
        seen_edges = set()
        for v in violations:
            rid = v["resourceId"]
            if not rid:
                continue
            ekey = (pid, rid)
            if ekey in seen_edges:
                continue
            seen_edges.add(ekey)

            edges.append({
                "id": f"edge-{pid[-25:]}-{rid[-35:]}",
                "source_id": pid,
                "target_id": rid,
                "edge_type": "dependency",
                "properties": {
                    "policy_assignment": policy_name,
                    "resource_type": v["resourceType"],
                },
            })

            # Signal on the resource: it's non-compliant with a deny policy
            signals_to_send.append({
                "node_id": rid,
                "signal_type": "PolicyViolation",
                "severity": "warning",
                "timestamp": v.get("timestamp"),
                "properties": {
                    "policy": policy_name,
                    "effect": "deny",
                    "resource_type": v["resourceType"],
                    "source": "azure-policy",
                },
            })

    if dry_run:
        return 0, 0

    # Merge topology
    if nodes:
        result = post_engine("graph/merge", {"nodes": nodes, "edges": edges}, engine)
        new_nodes = result.get("new_nodes", 0) if result else 0
        new_edges = result.get("new_edges", 0) if result else 0
        print(f"  Topology: {new_nodes} policy nodes, {new_edges} edges",
              file=sys.stderr)

    # Emit signals
    sig_count = 0
    for s in signals_to_send:
        if post_engine("signals", s, engine):
            sig_count += 1

    # Emit mutations on the policy nodes (the policy IS the cause)
    mut_count = 0
    for policy_name in by_policy:
        pid = policy_node_id(policy_name)
        result = post_engine("mutations", {
            "node_id": pid,
            "mutation_type": "PolicyEnforcement",
            "source": "azure-policy",
            "properties": {
                "policy_assignment": policy_name,
                "violation_count": len(by_policy[policy_name]),
            },
        }, engine)
        if result:
            mut_count += 1

    return mut_count, sig_count


def main():
    parser = argparse.ArgumentParser(
        description="Ingest Azure deny-policy violations as latent causal nodes.",
    )
    parser.add_argument("--subscription", "-s", action="append", dest="subscriptions")
    parser.add_argument("--engine", default=ENGINE)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    result = subprocess.run(["az", "account", "show", "--output", "json"],
                            capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        print("ERROR: Run `az login` first.", file=sys.stderr)
        sys.exit(1)

    muts, sigs = ingest_policies(args.subscriptions, args.engine, args.dry_run)

    if args.dry_run:
        print(f"\nDry run complete.", file=sys.stderr)
    else:
        print(f"\nIngested: {muts} policy mutations, {sigs} violation signals",
              file=sys.stderr)


if __name__ == "__main__":
    main()
