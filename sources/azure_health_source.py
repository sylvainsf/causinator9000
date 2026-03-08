#!/usr/bin/env python3
"""
Azure Resource Health + Resource Changes → Causinator 9000 signals & mutations.

Two data sources in one script:

1. HealthResources (signals): queries Azure Resource Health for degraded/unavailable
   resources and emits signals on the corresponding graph nodes.

2. ResourceChanges (mutations): queries Azure Resource Graph's ResourceChanges table
   for recent ARM-level changes and emits mutations on the changed resources.

Together these close the loop: ResourceChanges tells us what *changed* (mutations),
HealthResources tells us what's *broken* (signals). The engine connects them.

Uses `az` CLI — requires `az login`.

Usage:
  # Ingest both health signals and resource changes (last 24h)
  python3 sources/azure_health_source.py

  # Health signals only
  python3 sources/azure_health_source.py --health-only

  # Resource changes only
  python3 sources/azure_health_source.py --changes-only

  # Custom time window
  python3 sources/azure_health_source.py --hours 4

  # Specific subscription
  python3 sources/azure_health_source.py --subscription $SUB_ID

  # Dry run
  python3 sources/azure_health_source.py --dry-run
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

ENGINE = os.environ.get("C9K_ENGINE_URL", "http://localhost:8080")


def az_graph_query(query: str, subscriptions: list[str] | None = None) -> list[dict]:
    """Run an ARG query via az CLI with pagination."""
    cmd = ["az", "graph", "query", "-q", query, "--output", "json", "--first", "1000"]
    if subscriptions:
        cmd.extend(["--subscriptions"] + subscriptions)

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        print(f"ERROR: az graph query failed:\n{result.stderr}", file=sys.stderr)
        return []

    data = json.loads(result.stdout)
    rows = data.get("data", data) if isinstance(data, dict) else data
    if isinstance(rows, dict):
        rows = rows.get("data", [])
    return rows if isinstance(rows, list) else []


def post_engine(path: str, payload: dict, engine: str) -> dict | None:
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


# ── Health Resources → Signals ───────────────────────────────────────────

HEALTH_STATE_SIGNAL = {
    "Unavailable": ("Unavailable", "critical"),
    "Degraded": ("Degraded", "warning"),
    "Unknown": ("HealthUnknown", "info"),
}


def ingest_health(subscriptions: list[str] | None, engine: str,
                  dry_run: bool) -> int:
    """Query HealthResources for non-Available resources and emit signals."""
    query = """
    HealthResources
    | where type == 'microsoft.resourcehealth/availabilitystatuses'
    | where properties.availabilityState != 'Available'
    | project
        resourceId = tolower(properties.targetResourceId),
        state = tostring(properties.availabilityState),
        reason = tostring(properties.reasonType),
        summary = tostring(properties.summary),
        occurredTime = tostring(properties.occurredTime),
        reportedTime = tostring(properties.reportedTime)
    """

    print("Querying HealthResources...", file=sys.stderr)
    rows = az_graph_query(query, subscriptions)
    print(f"  → {len(rows)} non-Available resources", file=sys.stderr)

    count = 0
    for row in rows:
        resource_id = row.get("resourceId", "").lower()
        state = row.get("state", "Unknown")
        reason = row.get("reason", "")
        summary = row.get("summary", "")

        signal_type, severity = HEALTH_STATE_SIGNAL.get(
            state, ("HealthUnknown", "info"))

        if dry_run:
            print(f"  SIGNAL: {signal_type} ({severity}) on {resource_id[-60:]}")
            print(f"    reason: {reason}")
            if summary:
                print(f"    summary: {summary[:100]}")
        else:
            result = post_engine("signals", {
                "node_id": resource_id,
                "signal_type": signal_type,
                "severity": severity,
                "timestamp": row.get("occurredTime") or row.get("reportedTime"),
                "properties": {
                    "state": state,
                    "reason": reason,
                    "summary": summary[:500],
                    "source": "azure-resource-health",
                },
            }, engine)
            if result:
                count += 1

    return count


# ── Resource Changes → Mutations ─────────────────────────────────────────

# Map ARM change types to mutation types
CHANGE_TYPE_MAP = {
    "Create": "ResourceCreate",
    "Update": "ConfigChange",
    "Delete": "ResourceDelete",
}

# Map common property paths to more specific mutation types
PROPERTY_MUTATION_MAP = {
    "properties.storageProfile": "DiskChange",
    "properties.hardwareProfile": "SKUChange",
    "properties.networkProfile": "NetworkChange",
    "properties.osProfile": "OSConfigChange",
    "sku": "SKUChange",
    "tags": "TagChange",
    "properties.siteConfig": "AppConfigChange",
    "properties.httpsOnly": "SecurityConfigChange",
    "properties.minimumTlsVersion": "SecurityConfigChange",
    "properties.publicNetworkAccess": "SecurityConfigChange",
    "properties.networkAcls": "NetworkACLChange",
    "properties.accessPolicies": "AccessPolicyChange",
    "properties.enableSoftDelete": "SecurityConfigChange",
    "properties.enablePurgeProtection": "SecurityConfigChange",
    "identity": "IdentityChange",
    "properties.kubernetesVersion": "KubernetesUpgrade",
    "properties.agentPoolProfiles": "NodePoolChange",
    "properties.addonProfiles": "AKSAddonChange",
}


def classify_change(change_type: str, changed_properties: list[str]) -> str:
    """Classify a resource change into a specific mutation type."""
    # Check specific property paths first
    for prop in changed_properties:
        for path, mut_type in PROPERTY_MUTATION_MAP.items():
            if prop.startswith(path):
                return mut_type

    return CHANGE_TYPE_MAP.get(change_type, "ConfigChange")


def ingest_changes(subscriptions: list[str] | None, hours: int,
                   engine: str, dry_run: bool) -> int:
    """Query ResourceChanges for recent ARM changes and emit mutations."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")

    query = f"""
    ResourceChanges
    | where properties.changeAttributes.timestamp > datetime('{cutoff}')
    | project
        resourceId = tolower(tostring(properties.targetResourceId)),
        changeType = tostring(properties.changeType),
        timestamp = tostring(properties.changeAttributes.timestamp),
        changedBy = tostring(properties.changeAttributes.changedBy),
        clientType = tostring(properties.changeAttributes.clientType),
        operation = tostring(properties.changeAttributes.operation),
        changes = properties.changes
    | order by timestamp desc
    """

    print(f"Querying ResourceChanges (last {hours}h)...", file=sys.stderr)
    rows = az_graph_query(query, subscriptions)
    print(f"  → {len(rows)} resource changes", file=sys.stderr)

    count = 0
    for row in rows:
        resource_id = row.get("resourceId", "").lower()
        change_type = row.get("changeType", "Update")
        timestamp = row.get("timestamp", "")
        changed_by = row.get("changedBy", "")
        client_type = row.get("clientType", "")
        operation = row.get("operation", "")

        # Get changed property paths
        changes = row.get("changes", {})
        changed_props = list(changes.keys()) if isinstance(changes, dict) else []

        mutation_type = classify_change(change_type, changed_props)

        # Skip tag-only changes (noisy)
        if mutation_type == "TagChange":
            continue

        if dry_run:
            props_str = ", ".join(changed_props[:3])
            print(f"  MUTATION: {mutation_type} on {resource_id[-60:]}")
            print(f"    by: {changed_by[:40]} via {client_type}")
            if props_str:
                print(f"    changed: {props_str}")
        else:
            result = post_engine("mutations", {
                "node_id": resource_id,
                "mutation_type": mutation_type,
                "source": "azure-resource-changes",
                "timestamp": timestamp,
                "properties": {
                    "change_type": change_type,
                    "changed_by": changed_by[:100],
                    "client_type": client_type,
                    "operation": operation[:100],
                    "changed_properties": changed_props[:10],
                },
            }, engine)
            if result:
                count += 1

    return count


def main():
    parser = argparse.ArgumentParser(
        description="Ingest Azure Resource Health (signals) and Resource Changes (mutations).",
    )
    parser.add_argument("--subscription", "-s", action="append", dest="subscriptions",
                        help="Subscription ID (can repeat). Default: current az CLI subscription.")
    parser.add_argument("--hours", type=int, default=24,
                        help="Look back N hours for changes (default: 24)")
    parser.add_argument("--engine", default=ENGINE,
                        help=f"Engine URL (default: {ENGINE})")
    parser.add_argument("--health-only", action="store_true",
                        help="Only ingest health signals, skip resource changes")
    parser.add_argument("--changes-only", action="store_true",
                        help="Only ingest resource changes, skip health signals")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be ingested without sending to engine")
    args = parser.parse_args()

    # Verify az CLI
    result = subprocess.run(["az", "account", "show", "--output", "json"],
                            capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        print("ERROR: Run `az login` first.", file=sys.stderr)
        sys.exit(1)
    account = json.loads(result.stdout)
    print(f"Account: {account.get('user', {}).get('name', 'unknown')}", file=sys.stderr)

    sig_count = 0
    mut_count = 0

    if not args.changes_only:
        sig_count = ingest_health(args.subscriptions, args.engine, args.dry_run)

    if not args.health_only:
        mut_count = ingest_changes(args.subscriptions, args.hours,
                                   args.engine, args.dry_run)

    if args.dry_run:
        print(f"\nDry run complete.", file=sys.stderr)
    else:
        print(f"\nIngested: {mut_count} mutations (changes), {sig_count} signals (health)",
              file=sys.stderr)


if __name__ == "__main__":
    main()
