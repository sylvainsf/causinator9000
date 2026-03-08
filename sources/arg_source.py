#!/usr/bin/env python3
"""
Azure Resource Graph → Causinator 9000 GraphPayload source adapter.

Extracts infrastructure topology from an Azure subscription (or management group)
using Azure Resource Graph queries via the `az` CLI. Requires a logged-in
`az login` session — no SDK dependencies, no service principals needed for dev.

Usage:
  # All resources in current subscription
  python3 sources/arg_source.py

  # Specific subscription
  python3 sources/arg_source.py --subscription 00000000-0000-0000-0000-000000000000

  # Multiple subscriptions
  python3 sources/arg_source.py --subscription sub1 --subscription sub2

  # Management group scope
  python3 sources/arg_source.py --management-group myMG

  # Filter to specific resource groups
  python3 sources/arg_source.py --resource-group rg-prod --resource-group rg-staging

  # Filter to specific resource types
  python3 sources/arg_source.py --type microsoft.compute/virtualmachines --type microsoft.network/virtualnetworks

  # Save to file instead of stdout
  python3 sources/arg_source.py --output graph.json

  # Pipe directly to engine
  python3 sources/arg_source.py | curl -X POST http://localhost:8080/api/graph/merge -H 'Content-Type: application/json' -d @-
"""

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from typing import Any


# ── Azure type → Causinator node class mapping ────────────────────────────

TYPE_MAP = {
    # Compute
    "microsoft.compute/virtualmachines": "VirtualMachine",
    "microsoft.compute/virtualmachinescalesets": "VirtualMachineScaleSet",
    "microsoft.compute/disks": "Disk",
    "microsoft.compute/availabilitysets": "AvailabilitySet",
    "microsoft.compute/images": "Image",
    "microsoft.compute/snapshots": "Snapshot",
    # Containers & Kubernetes
    "microsoft.containerservice/managedclusters": "AKSCluster",
    "microsoft.containerregistry/registries": "ContainerRegistry",
    "microsoft.containerinstance/containergroups": "ContainerGroup",
    "microsoft.app/containerapps": "ContainerApp",
    "microsoft.app/managedenvironments": "ContainerAppEnvironment",
    # Networking
    "microsoft.network/virtualnetworks": "VirtualNetwork",
    "microsoft.network/networkinterfaces": "NetworkInterface",
    "microsoft.network/networksecuritygroups": "NetworkSecurityGroup",
    "microsoft.network/publicipaddresses": "PublicIP",
    "microsoft.network/loadbalancers": "LoadBalancer",
    "microsoft.network/applicationgateways": "Gateway",
    "microsoft.network/frontdoors": "FrontDoor",
    "microsoft.network/privatednszones": "PrivateDNS",
    "microsoft.network/dnszones": "DNS",
    "microsoft.network/routetables": "RouteTable",
    "microsoft.network/privateendpoints": "PrivateEndpoint",
    "microsoft.network/bastionhosts": "BastionHost",
    "microsoft.network/azurefirewalls": "Firewall",
    "microsoft.network/connections": "VPNConnection",
    "microsoft.network/virtualnetworkgateways": "VNetGateway",
    "microsoft.network/localnetworkgateways": "LocalNetGateway",
    "microsoft.network/natgateways": "NATGateway",
    # Identity
    "microsoft.managedidentity/userassignedidentities": "ManagedIdentity",
    "microsoft.aad/domainservices": "AADDomainService",
    # Key Vault
    "microsoft.keyvault/vaults": "KeyVault",
    "microsoft.keyvault/managedhsms": "ManagedHSM",
    # Databases
    "microsoft.sql/servers": "SqlServer",
    "microsoft.sql/servers/databases": "SqlDatabase",
    "microsoft.sql/servers/elasticpools": "SqlElasticPool",
    "microsoft.dbformysql/flexibleservers": "MySqlServer",
    "microsoft.dbforpostgresql/flexibleservers": "PostgresServer",
    "microsoft.documentdb/databaseaccounts": "CosmosDB",
    "microsoft.cache/redis": "RedisCache",
    "microsoft.cache/redisenterprise": "RedisEnterprise",
    # Storage
    "microsoft.storage/storageaccounts": "StorageAccount",
    "microsoft.netapp/netappaccounts": "NetAppAccount",
    "microsoft.netapp/netappaccounts/capacitypools/volumes": "NetAppVolume",
    # Messaging
    "microsoft.eventhub/namespaces": "EventHub",
    "microsoft.servicebus/namespaces": "ServiceBus",
    "microsoft.eventgrid/topics": "EventGridTopic",
    "microsoft.eventgrid/systemtopics": "EventGridSystemTopic",
    "microsoft.signalrservice/signalr": "SignalR",
    # Web / App Service
    "microsoft.web/serverfarms": "AppServicePlan",
    "microsoft.web/sites": "AppService",
    "microsoft.web/staticsites": "StaticWebApp",
    "microsoft.cdn/profiles": "FrontDoorCDN",
    # API Management
    "microsoft.apimanagement/service": "APIManagement",
    # Monitoring
    "microsoft.insights/components": "AppInsights",
    "microsoft.operationalinsights/workspaces": "LogAnalytics",
    "microsoft.insights/actiongroups": "ActionGroup",
    "microsoft.insights/metricalerts": "MetricAlert",
    "microsoft.insights/scheduledqueryrules": "AlertRule",
    "microsoft.insights/datacollectionrules": "DCR",
    # Security
    "microsoft.network/firewallpolicies": "FirewallPolicy",
    # Recovery
    "microsoft.recoveryservices/vaults": "RecoveryVault",
    "microsoft.dataprotection/backupvaults": "BackupVault",
    # Other
    "microsoft.logic/workflows": "LogicApp",
    "microsoft.automation/automationaccounts": "AutomationAccount",
    "microsoft.datafactory/factories": "DataFactory",
    "microsoft.machinelearningservices/workspaces": "MLWorkspace",
    "microsoft.search/searchservices": "CognitiveSearch",
    "microsoft.cognitiveservices/accounts": "CognitiveService",
}


# ── Helpers ───────────────────────────────────────────────────────────────

def az_graph_query(query: str, subscriptions: list[str] | None = None,
                   management_group: str | None = None) -> list[dict]:
    """Run an ARG query via `az graph query` and return all results (handling pagination)."""
    cmd = ["az", "graph", "query", "-q", query, "--output", "json", "--first", "1000"]
    if subscriptions:
        cmd.extend(["--subscriptions"] + subscriptions)
    if management_group:
        cmd.extend(["--management-groups", management_group])

    all_results = []
    skip = 0

    while True:
        run_cmd = cmd + (["--skip", str(skip)] if skip > 0 else [])
        result = subprocess.run(run_cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            print(f"ERROR: az graph query failed:\n{result.stderr}", file=sys.stderr)
            sys.exit(1)

        data = json.loads(result.stdout)
        rows = data.get("data", data) if isinstance(data, dict) else data
        if isinstance(rows, dict):
            rows = rows.get("data", [])
        if not rows:
            break

        all_results.extend(rows)
        # Check if there's more data
        total = data.get("total_records", data.get("totalRecords", len(rows)))
        if len(all_results) >= total:
            break
        skip = len(all_results)

    return all_results


def arm_id(s: str) -> str:
    """Normalize an ARM resource ID to lowercase for consistent node IDs."""
    return s.lower().rstrip("/") if s else ""


def node(id: str, name: str, cls: str, location: str, rg: str = "",
         sub: str = "", props: dict | None = None) -> dict:
    """Create a CausalNode dict."""
    p = props or {}
    p["resource_group"] = rg
    p["subscription_id"] = sub
    return {
        "id": id,
        "label": name,
        "class": cls,
        "region": location,
        "rack_id": None,
        "properties": p,
    }


def edge(source: str, target: str, edge_type: str = "dependency") -> dict:
    """Create a BlueprintEdge dict."""
    return {
        "id": f"edge-{source[-60:]}-{target[-60:]}",
        "source_id": source,
        "target_id": target,
        "edge_type": edge_type,
        "properties": {},
    }


# ── Resource extractors ──────────────────────────────────────────────────
# Each returns (nodes, edges) tuples extracted from ARG query results.

def extract_resources(resources: list[dict]) -> tuple[list[dict], list[dict]]:
    """Convert raw ARG resources to CausalNode dicts."""
    nodes = []
    for r in resources:
        rid = arm_id(r.get("id", ""))
        rtype = r.get("type", "").lower()
        name = r.get("name", "")
        location = r.get("location", "")
        rg = r.get("resourceGroup", "")
        sub = r.get("subscriptionId", "")

        cls = TYPE_MAP.get(rtype, rtype.split("/")[-1].title())
        nodes.append(node(rid, name, cls, location, rg, sub))

    return nodes, []


def extract_vm_edges(resources: list[dict]) -> list[dict]:
    """Extract VM → NIC, VM → Disk edges from VM properties."""
    edges = []
    for r in resources:
        rtype = r.get("type", "").lower()
        if rtype != "microsoft.compute/virtualmachines":
            continue

        vm_id = arm_id(r["id"])
        props = r.get("properties", {})

        # VM → NICs
        nics = props.get("networkProfile", {}).get("networkInterfaces", [])
        for nic in nics:
            nic_id = arm_id(nic.get("id", ""))
            if nic_id:
                edges.append(edge(vm_id, nic_id, "dependency"))

        # VM → OS disk
        os_disk = props.get("storageProfile", {}).get("osDisk", {}).get("managedDisk", {})
        disk_id = arm_id(os_disk.get("id", ""))
        if disk_id:
            edges.append(edge(vm_id, disk_id, "dependency"))

        # VM → Data disks
        for dd in props.get("storageProfile", {}).get("dataDisks", []):
            dd_id = arm_id(dd.get("managedDisk", {}).get("id", ""))
            if dd_id:
                edges.append(edge(vm_id, dd_id, "dependency"))

        # VM → Availability Set
        avset = arm_id(props.get("availabilitySet", {}).get("id", ""))
        if avset:
            edges.append(edge(avset, vm_id, "containment"))

    return edges


def extract_nic_edges(resources: list[dict]) -> list[dict]:
    """Extract NIC → Subnet, NIC → NSG, NIC → Public IP edges."""
    edges = []
    for r in resources:
        if r.get("type", "").lower() != "microsoft.network/networkinterfaces":
            continue

        nic_id = arm_id(r["id"])
        props = r.get("properties", {})

        # NIC → NSG
        nsg_id = arm_id(props.get("networkSecurityGroup", {}).get("id", ""))
        if nsg_id:
            edges.append(edge(nsg_id, nic_id, "dependency"))

        # NIC → Subnet, NIC → Public IP
        for ipconf in props.get("ipConfigurations", []):
            ip_props = ipconf.get("properties", {})
            subnet_id = arm_id(ip_props.get("subnet", {}).get("id", ""))
            if subnet_id:
                edges.append(edge(subnet_id, nic_id, "containment"))

            pip_id = arm_id(ip_props.get("publicIPAddress", {}).get("id", ""))
            if pip_id:
                edges.append(edge(nic_id, pip_id, "dependency"))

    return edges


def extract_subnet_nodes_and_edges(resources: list[dict]) -> tuple[list[dict], list[dict]]:
    """Extract subnets (embedded in VNet properties) as separate nodes with edges."""
    nodes = []
    edges = []

    for r in resources:
        if r.get("type", "").lower() != "microsoft.network/virtualnetworks":
            continue

        vnet_id = arm_id(r["id"])
        location = r.get("location", "")
        rg = r.get("resourceGroup", "")
        sub = r.get("subscriptionId", "")

        for sn in r.get("properties", {}).get("subnets", []):
            sn_id = arm_id(sn.get("id", ""))
            sn_name = sn.get("name", "")
            if not sn_id:
                continue

            sn_props = sn.get("properties", {})
            nodes.append(node(sn_id, sn_name, "Subnet", location, rg, sub))
            edges.append(edge(vnet_id, sn_id, "containment"))

            # Subnet → NSG
            nsg_id = arm_id(sn_props.get("networkSecurityGroup", {}).get("id", ""))
            if nsg_id:
                edges.append(edge(nsg_id, sn_id, "dependency"))

            # Subnet → Route Table
            rt_id = arm_id(sn_props.get("routeTable", {}).get("id", ""))
            if rt_id:
                edges.append(edge(rt_id, sn_id, "dependency"))

            # Subnet → NAT Gateway
            nat_id = arm_id(sn_props.get("natGateway", {}).get("id", ""))
            if nat_id:
                edges.append(edge(nat_id, sn_id, "dependency"))

    return nodes, edges


def extract_aks_edges(resources: list[dict]) -> list[dict]:
    """Extract AKS → Subnet, AKS → Identity edges."""
    edges = []
    for r in resources:
        if r.get("type", "").lower() != "microsoft.containerservice/managedclusters":
            continue

        aks_id = arm_id(r["id"])
        props = r.get("properties", {})

        # AKS → Subnets (from agent pools)
        for pool in props.get("agentPoolProfiles", []):
            subnet_id = arm_id(pool.get("vnetSubnetID", ""))
            if subnet_id:
                edges.append(edge(subnet_id, aks_id, "containment"))

        # AKS → User-assigned identities
        identity = r.get("identity", {})
        for mi_id in (identity.get("userAssignedIdentities") or {}).keys():
            edges.append(edge(arm_id(mi_id), aks_id, "dependency"))

        # AKS → Network profile NSG / route table references
        net = props.get("networkProfile", {})
        # These are sometimes set as well
        for key in ("podCidr", "serviceCidr", "dnsServiceIP"):
            pass  # CIDR-level info, not node refs

    return edges


def extract_lb_edges(resources: list[dict]) -> list[dict]:
    """Extract LoadBalancer → Backend Pool → NIC edges."""
    edges = []
    for r in resources:
        if r.get("type", "").lower() != "microsoft.network/loadbalancers":
            continue

        lb_id = arm_id(r["id"])
        props = r.get("properties", {})

        # LB → Frontend subnet
        for fip in props.get("frontendIPConfigurations", []):
            fip_props = fip.get("properties", {})
            subnet_id = arm_id(fip_props.get("subnet", {}).get("id", ""))
            if subnet_id:
                edges.append(edge(subnet_id, lb_id, "containment"))
            pip_id = arm_id(fip_props.get("publicIPAddress", {}).get("id", ""))
            if pip_id:
                edges.append(edge(lb_id, pip_id, "dependency"))

        # LB → Backend NICs
        for pool in props.get("backendAddressPools", []):
            for bic in pool.get("properties", {}).get("backendIPConfigurations", []):
                nic_ref = arm_id(bic.get("id", ""))
                if nic_ref:
                    # Strip the /ipConfigurations/... suffix to get NIC ID
                    nic_id = nic_ref.split("/ipconfigurations/")[0] if "/ipconfigurations/" in nic_ref else nic_ref
                    edges.append(edge(lb_id, nic_id, "dependency"))

    return edges


def extract_appgw_edges(resources: list[dict]) -> list[dict]:
    """Extract Application Gateway → Subnet edges."""
    edges = []
    for r in resources:
        if r.get("type", "").lower() != "microsoft.network/applicationgateways":
            continue

        gw_id = arm_id(r["id"])
        props = r.get("properties", {})

        for gw_ip in props.get("gatewayIPConfigurations", []):
            subnet_id = arm_id(gw_ip.get("properties", {}).get("subnet", {}).get("id", ""))
            if subnet_id:
                edges.append(edge(subnet_id, gw_id, "containment"))

    return edges


def extract_private_endpoint_edges(resources: list[dict]) -> list[dict]:
    """Extract Private Endpoint → target resource and PE → subnet edges."""
    edges = []
    for r in resources:
        if r.get("type", "").lower() != "microsoft.network/privateendpoints":
            continue

        pe_id = arm_id(r["id"])
        props = r.get("properties", {})

        # PE → Subnet
        subnet_id = arm_id(props.get("subnet", {}).get("id", ""))
        if subnet_id:
            edges.append(edge(subnet_id, pe_id, "containment"))

        # PE → Target resource
        for plsc in props.get("privateLinkServiceConnections", []) + \
                     props.get("manualPrivateLinkServiceConnections", []):
            target = arm_id(plsc.get("properties", {}).get("privateLinkServiceId", ""))
            if target:
                edges.append(edge(pe_id, target, "dependency"))

    return edges


def extract_sql_edges(resources: list[dict]) -> list[dict]:
    """Extract SQL Server → Database containment edges."""
    edges = []
    for r in resources:
        rtype = r.get("type", "").lower()
        if rtype == "microsoft.sql/servers/databases":
            db_id = arm_id(r["id"])
            # Parent server is everything before /databases/
            parts = db_id.rsplit("/databases/", 1)
            if len(parts) == 2:
                server_id = parts[0]
                edges.append(edge(server_id, db_id, "containment"))
    return edges


def extract_resource_group_edges(resources: list[dict]) -> tuple[list[dict], list[dict]]:
    """Create resource group nodes and containment edges."""
    rgs: dict[str, dict] = {}
    edges = []

    for r in resources:
        rid = arm_id(r.get("id", ""))
        rg_name = r.get("resourceGroup", "")
        sub = r.get("subscriptionId", "")
        location = r.get("location", "")

        if not rg_name or not sub:
            continue

        rg_id = arm_id(f"/subscriptions/{sub}/resourcegroups/{rg_name}")
        if rg_id not in rgs:
            rgs[rg_id] = node(rg_id, rg_name, "ResourceGroup", location, rg_name, sub)

        edges.append(edge(rg_id, rid, "containment"))

    return list(rgs.values()), edges


# ── Main pipeline ─────────────────────────────────────────────────────────

def build_graph(subscriptions: list[str] | None = None,
                management_group: str | None = None,
                resource_groups: list[str] | None = None,
                resource_types: list[str] | None = None,
                include_rg_nodes: bool = True) -> dict:
    """
    Query ARG and build a GraphPayload dict.

    Args:
        subscriptions: List of subscription IDs to scope the query.
        management_group: Management group ID to scope the query.
        resource_groups: Optional list of resource group names to filter.
        resource_types: Optional list of resource types to filter.
        include_rg_nodes: Whether to create ResourceGroup nodes with containment edges.
    """

    # Build the KQL query with optional filters
    filters = []
    if resource_groups:
        rg_list = ", ".join(f"'{rg}'" for rg in resource_groups)
        filters.append(f"resourceGroup in~ ({rg_list})")
    if resource_types:
        type_list = ", ".join(f"'{t.lower()}'" for t in resource_types)
        filters.append(f"type in~ ({type_list})")

    where_clause = " | where " + " and ".join(filters) if filters else ""
    query = f"Resources{where_clause} | project id, name, type, location, resourceGroup, subscriptionId, properties, identity, tags"

    print(f"Querying ARG: {query[:120]}...", file=sys.stderr)
    resources = az_graph_query(query, subscriptions, management_group)
    print(f"  → {len(resources)} resources", file=sys.stderr)

    if not resources:
        print("WARNING: No resources found. Check scope and filters.", file=sys.stderr)
        return {"nodes": [], "edges": []}

    # Extract nodes
    all_nodes, _ = extract_resources(resources)

    # Extract embedded subnets as separate nodes
    subnet_nodes, subnet_edges = extract_subnet_nodes_and_edges(resources)
    all_nodes.extend(subnet_nodes)

    # Extract all edge types
    all_edges = []
    all_edges.extend(subnet_edges)
    all_edges.extend(extract_vm_edges(resources))
    all_edges.extend(extract_nic_edges(resources))
    all_edges.extend(extract_aks_edges(resources))
    all_edges.extend(extract_lb_edges(resources))
    all_edges.extend(extract_appgw_edges(resources))
    all_edges.extend(extract_private_endpoint_edges(resources))
    all_edges.extend(extract_sql_edges(resources))

    # Resource group containment
    if include_rg_nodes:
        rg_nodes, rg_edges = extract_resource_group_edges(resources)
        all_nodes.extend(rg_nodes)
        all_edges.extend(rg_edges)

    # Deduplicate nodes by ID
    seen_nodes: dict[str, dict] = {}
    for n in all_nodes:
        seen_nodes[n["id"]] = n
    dedup_nodes = list(seen_nodes.values())

    # Deduplicate edges by (source, target, type)
    seen_edges: set[tuple[str, str, str]] = set()
    dedup_edges = []
    for e in all_edges:
        key = (e["source_id"], e["target_id"], e["edge_type"])
        if key not in seen_edges:
            seen_edges.add(key)
            dedup_edges.append(e)

    # Drop edges that reference nodes not in the graph
    node_ids = set(n["id"] for n in dedup_nodes)
    valid_edges = [e for e in dedup_edges
                   if e["source_id"] in node_ids and e["target_id"] in node_ids]
    dangling = len(dedup_edges) - len(valid_edges)

    print(f"  → {len(dedup_nodes)} nodes, {len(valid_edges)} edges"
          f" ({dangling} dangling edges dropped)", file=sys.stderr)

    return {"nodes": dedup_nodes, "edges": valid_edges}


def main():
    parser = argparse.ArgumentParser(
        description="Extract Azure infrastructure topology into Causinator 9000 GraphPayload format.",
        epilog="Requires `az login` — uses your current CLI session credentials.",
    )
    parser.add_argument(
        "--subscription", "-s", action="append", dest="subscriptions",
        help="Subscription ID to query. Can be repeated. Default: current az CLI subscription.",
    )
    parser.add_argument(
        "--management-group", "-m",
        help="Management group ID — queries all subscriptions in the hierarchy.",
    )
    parser.add_argument(
        "--resource-group", "-g", action="append", dest="resource_groups",
        help="Filter to specific resource groups. Can be repeated.",
    )
    parser.add_argument(
        "--type", "-t", action="append", dest="resource_types",
        help="Filter to specific Azure resource types (e.g., microsoft.compute/virtualmachines). Can be repeated.",
    )
    parser.add_argument(
        "--no-resource-groups", action="store_true",
        help="Don't create ResourceGroup nodes and containment edges.",
    )
    parser.add_argument(
        "--output", "-o",
        help="Write output to a file instead of stdout.",
    )
    parser.add_argument(
        "--pretty", action="store_true",
        help="Pretty-print the JSON output.",
    )

    args = parser.parse_args()

    # Verify az CLI is available and logged in
    try:
        result = subprocess.run(
            ["az", "account", "show", "--output", "json"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            print("ERROR: Not logged in. Run `az login` first.", file=sys.stderr)
            sys.exit(1)
        account = json.loads(result.stdout)
        print(f"Using account: {account.get('user', {}).get('name', 'unknown')}"
              f" (tenant: {account.get('tenantId', 'unknown')[:8]}...)", file=sys.stderr)
        if not args.subscriptions and not args.management_group:
            print(f"  Subscription: {account.get('name', '')} ({account.get('id', '')})", file=sys.stderr)
    except FileNotFoundError:
        print("ERROR: `az` CLI not found. Install Azure CLI: https://aka.ms/installazurecli", file=sys.stderr)
        sys.exit(1)

    # Verify az graph extension is installed
    result = subprocess.run(
        ["az", "extension", "show", "--name", "resource-graph", "--output", "json"],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        print("Installing Azure Resource Graph CLI extension...", file=sys.stderr)
        subprocess.run(["az", "extension", "add", "--name", "resource-graph"], check=True, timeout=60)

    graph = build_graph(
        subscriptions=args.subscriptions,
        management_group=args.management_group,
        resource_groups=args.resource_groups,
        resource_types=args.resource_types,
        include_rg_nodes=not args.no_resource_groups,
    )

    output = json.dumps(graph, indent=2 if args.pretty else None)

    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
