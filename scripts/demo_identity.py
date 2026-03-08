#!/usr/bin/env python3
"""
Causinator 9000 — Managed Identity Deletion Demo

Interactive 3-act demo with real Azure infrastructure:

  Act 1: Deploy a simple pod using a managed identity to call Key Vault.
         Pod is healthy, dashboard shows green.

  Act 2: Volunteer deletes the managed identity in the Azure Portal.
         Pod starts getting 401 AuthorizationFailed errors.

  Act 3: Re-ingest data. The engine traces the auth failures on the pod
         back to the MI deletion via ARM ResourceChanges. Dashboard shows
         the causal path: MI deleted → pod AuthFailure, 95%+ confidence.

Prerequisites:
  - An AKS cluster with workload identity enabled
  - kubectl context configured for that cluster
  - The engine running (make run-release)
  - Azure topology loaded (make ingest-arg)

Usage:
  python3 scripts/demo_identity.py --context <kube-context>

  The script will prompt for each step and pause for the volunteer action.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid

ENGINE = os.environ.get("C9K_ENGINE_URL", "http://localhost:8080")


def run(cmd, check=True, capture=True, timeout=30):
    r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=capture,
                       text=True, timeout=timeout)
    if check and r.returncode != 0:
        print(f"  ERROR: {r.stderr[:200]}", file=sys.stderr)
    return r


def post(path, payload):
    import urllib.request
    url = f"{ENGINE}/api/{path}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  ERROR: {e}")
        return None


def get(path):
    import urllib.request
    try:
        resp = urllib.request.urlopen(f"{ENGINE}/api/{path}", timeout=5)
        return json.loads(resp.read())
    except:
        return None


def kubectl(*args, context=None):
    cmd = ["kubectl"]
    if context:
        cmd.extend(["--context", context])
    cmd.extend(args)
    return run(cmd, check=False)


def banner(text):
    w = max(len(text) + 4, 50)
    print(f"\n{'═' * w}")
    print(f"  {text}")
    print(f"{'═' * w}\n")


def pause(msg="Press ENTER to continue..."):
    print()
    input(f"  {msg} ")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Managed Identity deletion demo — real Azure infrastructure")
    parser.add_argument("--context", "-c", required=True,
                        help="kubectl context for the AKS cluster")
    parser.add_argument("--namespace", "-n", default="c9k-demo",
                        help="Namespace for demo resources (default: c9k-demo)")
    parser.add_argument("--engine", default=ENGINE)
    args = parser.parse_args()

    global ENGINE
    ENGINE = args.engine
    ctx = args.context
    ns = args.namespace

    # Detect subscription and AKS details from the context
    print("Detecting cluster info...")
    r = run(f"az aks list --query \"[?name=='{ctx}' || name=='{ctx.split('-')[0]}']\" -o json", check=False)
    aks_info = None
    if r.returncode == 0:
        clusters = json.loads(r.stdout)
        if clusters:
            aks_info = clusters[0]

    if not aks_info:
        # Try to find by listing all and matching
        r = run("az aks list -o json", check=False)
        if r.returncode == 0:
            for c in json.loads(r.stdout):
                if ctx in c.get("name", "") or ctx in str(c.get("id", "")):
                    aks_info = c
                    break

    sub_id = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
    aks_rg = ""
    aks_name = ""
    aks_id = ""
    location = "eastus"

    if aks_info:
        aks_id = aks_info["id"].lower()
        aks_rg = aks_info["resourceGroup"]
        aks_name = aks_info["name"]
        location = aks_info["location"]
        sub_id = aks_id.split("/subscriptions/")[1].split("/")[0]
        print(f"  Cluster: {aks_name} in {aks_rg} ({location})")
    else:
        print(f"  Could not auto-detect AKS info for context '{ctx}'")
        print(f"  Demo will work but won't auto-link to ARG topology")
        sub_id = sub_id or run("az account show --query id -o tsv").stdout.strip()

    mi_name = f"c9k-demo-identity"
    mi_rg = aks_rg or "c9k-demo"
    demo_uid = str(uuid.uuid4())[:8]

    # ══════════════════════════════════════════════════════════════════
    banner("Act 1 — Deploy: Create managed identity + pod")
    # ══════════════════════════════════════════════════════════════════

    print("  Step 1: Creating resource group and managed identity...")
    run(f"az group create -n {mi_rg} -l {location} -o none 2>/dev/null", check=False)
    r = run(f"az identity create -n {mi_name} -g {mi_rg} -l {location} -o json")
    if r.returncode != 0:
        print("  Failed to create managed identity. Check permissions.")
        sys.exit(1)
    mi_info = json.loads(r.stdout)
    mi_id = mi_info["id"].lower()
    mi_client_id = mi_info["clientId"]
    mi_principal_id = mi_info["principalId"]
    print(f"  ✓ Created: {mi_name} (client: {mi_client_id[:8]}...)")

    print("\n  Step 2: Creating demo namespace and pod...")
    kubectl("create", "namespace", ns, context=ctx)

    # Create a simple pod that uses the MI to make Azure API calls
    pod_manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "demo-app",
            "namespace": ns,
            "labels": {"app": "c9k-demo", "azure.workload.identity/use": "true"},
        },
        "spec": {
            "serviceAccountName": "default",
            "containers": [{
                "name": "app",
                "image": "mcr.microsoft.com/azure-cli:latest",
                "command": ["sh", "-c",
                    "while true; do "
                    "echo \"[$(date -Iseconds)] Checking identity...\"; "
                    f"az login --identity --username {mi_client_id} 2>&1 | tail -1; "
                    "sleep 15; "
                    "done"
                ],
            }],
        },
    }
    kubectl("apply", "-f", "-", context=ctx,
            ).input if False else None
    # Write manifest to temp file and apply
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(pod_manifest, f)
        manifest_path = f.name
    kubectl("apply", "-f", manifest_path, context=ctx)
    os.unlink(manifest_path)
    print(f"  ✓ Pod 'demo-app' created in namespace '{ns}'")

    print("\n  Step 3: Loading topology into engine...")
    post("clear", {})
    # Load ARG topology
    if os.path.exists("/tmp/c9k-arg-graph.json"):
        with open("/tmp/c9k-arg-graph.json") as f:
            graph = json.load(f)
        post("graph/load", graph)
    else:
        print("  (Run 'make ingest-arg' first for full ARG topology)")

    post("reload-cpts", {})

    # Add the K8s topology
    r = run(f"python3 sources/k8s_source.py --context {ctx} -n {ns}"
            + (f" --aks-resource-id {aks_id}" if aks_id else ""))
    print(f"  ✓ K8s topology ingested")

    print("\n  Step 4: Waiting for pod to stabilize...")
    for i in range(6):
        r = kubectl("get", "pod", "demo-app", "-n", ns, "-o",
                     "jsonpath={.status.phase}", context=ctx)
        phase = r.stdout.strip() if r.returncode == 0 else "Unknown"
        print(f"    Pod phase: {phase}")
        if phase == "Running":
            break
        time.sleep(5)

    health = get("health")
    if health:
        print(f"\n  Engine: {health['nodes']:,} nodes, {health['active_signals']} signals")
    print(f"\n  Dashboard: open {ENGINE}")
    print(f"  → Pod 'demo-app' should be green (no alerts)")

    # ══════════════════════════════════════════════════════════════════
    banner("Act 2 — Break it: Volunteer deletes the managed identity")
    # ══════════════════════════════════════════════════════════════════

    print("━" * 60)
    print("  VOLUNTEER ACTION:")
    print()
    print(f"  Go to the Azure Portal:")
    print(f"    portal.azure.com → Managed Identities")
    print(f"    → Find: {mi_name}")
    print(f"    → Resource group: {mi_rg}")
    print(f"    → Click 'Delete'")
    print()
    print(f"  Or via CLI:")
    print(f"    az identity delete -n {mi_name} -g {mi_rg}")
    print()
    print("  This will cause the pod to lose Azure auth.")
    print("━" * 60)

    pause("Press ENTER after the managed identity is deleted...")

    # ══════════════════════════════════════════════════════════════════
    banner("Act 3 — Detect: Engine traces the failure")
    # ══════════════════════════════════════════════════════════════════

    print("  Step 1: Checking pod status...")
    r = kubectl("logs", "demo-app", "-n", ns, "--tail=5", context=ctx)
    if r.returncode == 0:
        for line in r.stdout.strip().split("\n"):
            print(f"    {line}")

    print("\n  Step 2: Ingesting Azure resource changes...")
    run(f"python3 sources/azure_health_source.py --hours 1")
    print("  ✓ ResourceChanges ingested")

    print("\n  Step 3: Ingesting K8s cluster state...")
    run(f"python3 sources/k8s_source.py --context {ctx} -n {ns}"
        + (f" --aks-resource-id {aks_id}" if aks_id else ""))
    print("  ✓ K8s state ingested")

    print("\n  Step 4: Checking results...")
    time.sleep(2)

    groups = get("alert-groups") or []
    print()
    for g in groups:
        pct = g["confidence"] * 100
        sigs = ", ".join(g["signal_types"])
        path = " → ".join(g["causal_path"][:3])
        print(f"  [{g['count']} nodes] {g['root_cause'][:60]}")
        print(f"    Confidence: {pct:.1f}%")
        print(f"    Signals: {sigs}")
        if path:
            print(f"    Path: {path}")
        print()
    print(f"  {len(groups)} alert group(s)")

    print(f"\n  Dashboard: open {ENGINE}")
    print(f"\n  Click the alert group to see the causal tree:")
    print(f"  Managed Identity deletion → AKS → Namespace → Pod → AuthFailure")

    # ══════════════════════════════════════════════════════════════════
    banner("Cleanup")
    # ══════════════════════════════════════════════════════════════════

    pause("Press ENTER to clean up demo resources...")

    kubectl("delete", "namespace", ns, context=ctx)
    run(f"az identity delete -n {mi_name} -g {mi_rg} 2>/dev/null", check=False)
    print("  ✓ Demo resources cleaned up")


if __name__ == "__main__":
    main()
