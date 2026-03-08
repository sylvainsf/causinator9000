#!/usr/bin/env python3
"""
Terraform State → Causinator 9000 GraphPayload source adapter.

Parses a Terraform state file (v4 JSON format) and extracts Azure resources
as nodes with dependency edges. Works with local .tfstate files or remote
backends via `terraform state pull`.

Usage:
  # From a local state file
  python3 sources/terraform_source.py --state terraform.tfstate

  # From remote backend (runs `terraform state pull`)
  python3 sources/terraform_source.py --pull --chdir /path/to/tf/project

  # Filter to specific resource types
  python3 sources/terraform_source.py --state terraform.tfstate --type azurerm_kubernetes_cluster

  # Merge with ARG source
  python3 sources/merge.py \
    <(python3 sources/arg_source.py -s $SUB) \
    <(python3 sources/terraform_source.py --state terraform.tfstate) \
    | curl -X POST http://localhost:8080/api/graph/merge -H 'Content-Type: application/json' -d @-
"""

import argparse
import json
import subprocess
import sys
from typing import Any


# ── Terraform provider type → Causinator node class mapping ───────────────

AZURERM_TYPE_MAP = {
    # Compute
    "azurerm_virtual_machine": "VirtualMachine",
    "azurerm_linux_virtual_machine": "VirtualMachine",
    "azurerm_windows_virtual_machine": "VirtualMachine",
    "azurerm_virtual_machine_scale_set": "VirtualMachineScaleSet",
    "azurerm_linux_virtual_machine_scale_set": "VirtualMachineScaleSet",
    "azurerm_windows_virtual_machine_scale_set": "VirtualMachineScaleSet",
    "azurerm_managed_disk": "Disk",
    "azurerm_availability_set": "AvailabilitySet",
    "azurerm_image": "Image",
    "azurerm_snapshot": "Snapshot",
    # Containers & Kubernetes
    "azurerm_kubernetes_cluster": "AKSCluster",
    "azurerm_kubernetes_cluster_node_pool": "AKSNodePool",
    "azurerm_container_registry": "ContainerRegistry",
    "azurerm_container_group": "ContainerGroup",
    "azurerm_container_app": "ContainerApp",
    "azurerm_container_app_environment": "ContainerAppEnvironment",
    # Networking
    "azurerm_virtual_network": "VirtualNetwork",
    "azurerm_subnet": "Subnet",
    "azurerm_network_interface": "NetworkInterface",
    "azurerm_network_security_group": "NetworkSecurityGroup",
    "azurerm_public_ip": "PublicIP",
    "azurerm_lb": "LoadBalancer",
    "azurerm_application_gateway": "Gateway",
    "azurerm_frontdoor": "FrontDoor",
    "azurerm_private_dns_zone": "PrivateDNS",
    "azurerm_dns_zone": "DNS",
    "azurerm_route_table": "RouteTable",
    "azurerm_private_endpoint": "PrivateEndpoint",
    "azurerm_bastion_host": "BastionHost",
    "azurerm_firewall": "Firewall",
    "azurerm_nat_gateway": "NATGateway",
    "azurerm_virtual_network_gateway": "VNetGateway",
    "azurerm_local_network_gateway": "LocalNetGateway",
    "azurerm_virtual_network_peering": "VNetPeering",
    "azurerm_subnet_network_security_group_association": "_association",
    "azurerm_subnet_route_table_association": "_association",
    "azurerm_subnet_nat_gateway_association": "_association",
    "azurerm_network_interface_security_group_association": "_association",
    # Identity
    "azurerm_user_assigned_identity": "ManagedIdentity",
    # Key Vault
    "azurerm_key_vault": "KeyVault",
    "azurerm_key_vault_secret": "KeyVaultSecret",
    "azurerm_key_vault_certificate": "KeyVaultCert",
    "azurerm_key_vault_key": "KeyVaultKey",
    # Databases
    "azurerm_mssql_server": "SqlServer",
    "azurerm_mssql_database": "SqlDatabase",
    "azurerm_mssql_elasticpool": "SqlElasticPool",
    "azurerm_mysql_flexible_server": "MySqlServer",
    "azurerm_postgresql_flexible_server": "PostgresServer",
    "azurerm_cosmosdb_account": "CosmosDB",
    "azurerm_redis_cache": "RedisCache",
    "azurerm_redis_enterprise_cluster": "RedisEnterprise",
    # Storage
    "azurerm_storage_account": "StorageAccount",
    "azurerm_storage_container": "StorageContainer",
    "azurerm_storage_share": "StorageShare",
    # Messaging
    "azurerm_eventhub_namespace": "EventHub",
    "azurerm_servicebus_namespace": "ServiceBus",
    "azurerm_eventgrid_topic": "EventGridTopic",
    # Web / App
    "azurerm_service_plan": "AppServicePlan",
    "azurerm_linux_web_app": "AppService",
    "azurerm_windows_web_app": "AppService",
    "azurerm_linux_function_app": "FunctionApp",
    "azurerm_windows_function_app": "FunctionApp",
    "azurerm_static_web_app": "StaticWebApp",
    "azurerm_cdn_frontdoor_profile": "FrontDoorCDN",
    # API Management
    "azurerm_api_management": "APIManagement",
    # Monitoring
    "azurerm_application_insights": "AppInsights",
    "azurerm_log_analytics_workspace": "LogAnalytics",
    "azurerm_monitor_action_group": "ActionGroup",
    # Other
    "azurerm_logic_app_workflow": "LogicApp",
    "azurerm_resource_group": "ResourceGroup",
    "azurerm_data_factory": "DataFactory",
    # Non-Azure
    "kubernetes_deployment": "KubernetesDeployment",
    "kubernetes_service": "KubernetesService",
    "kubernetes_namespace": "KubernetesNamespace",
    "helm_release": "HelmRelease",
}

# Attributes that commonly hold ARM resource ID references
REF_ATTRIBUTES = [
    "resource_group_id",
    "virtual_network_id", "subnet_id", "network_interface_id",
    "network_security_group_id", "route_table_id", "nat_gateway_id",
    "public_ip_address_id", "key_vault_id", "storage_account_id",
    "log_analytics_workspace_id", "application_insights_id",
    "managed_disk_id", "availability_set_id",
    "container_registry_id", "dns_zone_id",
    "private_dns_zone_id", "firewall_policy_id",
    "kubernetes_cluster_id", "service_plan_id",
    "eventhub_namespace_id", "servicebus_namespace_id",
    "cosmosdb_account_id", "mssql_server_id",
    "user_assigned_identity_id",
]


def arm_id(s: str) -> str:
    """Normalize an ARM resource ID to lowercase."""
    return s.lower().rstrip("/") if s else ""


def node(id: str, name: str, cls: str, location: str = "",
         rg: str = "", props: dict | None = None) -> dict:
    p = props or {}
    p["resource_group"] = rg
    return {
        "id": id,
        "label": name,
        "class": cls,
        "region": location,
        "rack_id": None,
        "properties": p,
    }


def edge(source: str, target: str, edge_type: str = "dependency") -> dict:
    return {
        "id": f"edge-{source[-60:]}-{target[-60:]}",
        "source_id": source,
        "target_id": target,
        "edge_type": edge_type,
        "properties": {},
    }


def parse_tf_state(state: dict, type_filter: list[str] | None = None) -> dict:
    """
    Parse Terraform v4 state JSON into a GraphPayload.

    Extracts:
    - Resources as nodes (using ARM IDs from attributes.id when available)
    - Explicit dependencies from depends_on
    - Implicit dependencies from ARM ID references in attributes
    - Association resources as edges between their referenced resources
    """
    version = state.get("version", 0)
    if version < 4:
        print(f"WARNING: Terraform state version {version} — expected v4. May not parse correctly.",
              file=sys.stderr)

    all_nodes = []
    all_edges = []
    # Map terraform address → ARM ID for cross-referencing
    addr_to_id: dict[str, str] = {}
    # Track association resources separately
    associations: list[dict] = []

    for resource in state.get("resources", []):
        mode = resource.get("mode", "managed")
        if mode != "managed":
            continue

        tf_type = resource.get("type", "")
        module = resource.get("module", "")
        name = resource.get("name", "")

        if type_filter and tf_type not in type_filter:
            continue

        cls = AZURERM_TYPE_MAP.get(tf_type, tf_type.split("_", 1)[-1].title() if "_" in tf_type else tf_type)

        # Skip association resources — process them as edges later
        if cls == "_association":
            associations.append(resource)
            continue

        depends_on = resource.get("depends_on", [])

        for instance in resource.get("instances", []):
            attrs = instance.get("attributes", {})
            index_key = instance.get("index_key")

            # Use ARM ID from attributes if available, else construct a TF address
            resource_id = arm_id(attrs.get("id", ""))
            tf_addr = f"{module}.{tf_type}.{name}" if module else f"{tf_type}.{name}"
            if index_key is not None:
                tf_addr += f"[{index_key}]"

            if not resource_id:
                resource_id = f"tf://{tf_addr}"

            addr_to_id[tf_addr] = resource_id

            # Build node
            location = attrs.get("location", "")
            rg = attrs.get("resource_group_name", "")
            label = attrs.get("name", name)
            n = node(resource_id, label, cls, location, rg, {
                "tf_address": tf_addr,
                "tf_type": tf_type,
            })
            all_nodes.append(n)

            # Extract implicit dependency edges from ARM ID references in attributes
            for ref_attr in REF_ATTRIBUTES:
                ref_val = attrs.get(ref_attr, "")
                if ref_val and isinstance(ref_val, str) and ref_val.startswith("/"):
                    ref_id = arm_id(ref_val)
                    all_edges.append(edge(ref_id, resource_id, "dependency"))

            # Explicit depends_on from Terraform
            for dep in depends_on:
                # Will resolve after all resources are processed
                dep_clean = dep.replace("module.", "").strip()
                all_edges.append(edge(f"__tf_dep__{dep_clean}", resource_id, "dependency"))

    # Process association resources as edges
    for resource in associations:
        for instance in resource.get("instances", []):
            attrs = instance.get("attributes", {})
            tf_type = resource.get("type", "")

            if "subnet" in tf_type and "network_security_group" in tf_type:
                src = arm_id(attrs.get("network_security_group_id", ""))
                tgt = arm_id(attrs.get("subnet_id", ""))
                if src and tgt:
                    all_edges.append(edge(src, tgt, "dependency"))

            elif "subnet" in tf_type and "route_table" in tf_type:
                src = arm_id(attrs.get("route_table_id", ""))
                tgt = arm_id(attrs.get("subnet_id", ""))
                if src and tgt:
                    all_edges.append(edge(src, tgt, "dependency"))

            elif "subnet" in tf_type and "nat_gateway" in tf_type:
                src = arm_id(attrs.get("nat_gateway_id", ""))
                tgt = arm_id(attrs.get("subnet_id", ""))
                if src and tgt:
                    all_edges.append(edge(src, tgt, "dependency"))

            elif "network_interface" in tf_type and "security_group" in tf_type:
                src = arm_id(attrs.get("network_security_group_id", ""))
                tgt = arm_id(attrs.get("network_interface_id", ""))
                if src and tgt:
                    all_edges.append(edge(src, tgt, "dependency"))

    # Resolve Terraform depends_on references
    resolved_edges = []
    for e in all_edges:
        if e["source_id"].startswith("__tf_dep__"):
            dep_addr = e["source_id"].replace("__tf_dep__", "")
            resolved_id = addr_to_id.get(dep_addr)
            if resolved_id:
                e["source_id"] = resolved_id
                resolved_edges.append(e)
            # else: drop unresolvable dep
        else:
            resolved_edges.append(e)

    # Deduplicate
    seen_nodes: dict[str, dict] = {}
    for n in all_nodes:
        seen_nodes[n["id"]] = n
    dedup_nodes = list(seen_nodes.values())

    seen_edges: set[tuple[str, str, str]] = set()
    dedup_edges = []
    for e in resolved_edges:
        key = (e["source_id"], e["target_id"], e["edge_type"])
        if key not in seen_edges:
            seen_edges.add(key)
            dedup_edges.append(e)

    # Drop dangling edges
    node_ids = set(n["id"] for n in dedup_nodes)
    valid_edges = [e for e in dedup_edges
                   if e["source_id"] in node_ids and e["target_id"] in node_ids]
    dangling = len(dedup_edges) - len(valid_edges)

    print(f"  → {len(dedup_nodes)} nodes, {len(valid_edges)} edges"
          f" ({dangling} dangling edges dropped)", file=sys.stderr)

    return {"nodes": dedup_nodes, "edges": valid_edges}


def main():
    parser = argparse.ArgumentParser(
        description="Extract Terraform state into Causinator 9000 GraphPayload format.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--state", "-f",
        help="Path to a terraform.tfstate file (JSON v4 format).",
    )
    group.add_argument(
        "--pull", action="store_true",
        help="Run `terraform state pull` to get state from remote backend.",
    )
    parser.add_argument(
        "--chdir", "-C",
        help="Directory containing the Terraform project (used with --pull).",
    )
    parser.add_argument(
        "--type", "-t", action="append", dest="resource_types",
        help="Filter to specific Terraform resource types (e.g., azurerm_kubernetes_cluster). Can be repeated.",
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

    if args.pull:
        cmd = ["terraform", "state", "pull"]
        if args.chdir:
            cmd = ["terraform", f"-chdir={args.chdir}", "state", "pull"]
        print("Running: " + " ".join(cmd), file=sys.stderr)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            print(f"ERROR: terraform state pull failed:\n{result.stderr}", file=sys.stderr)
            sys.exit(1)
        state = json.loads(result.stdout)
    else:
        print(f"Reading state from {args.state}", file=sys.stderr)
        with open(args.state) as f:
            state = json.load(f)

    graph = parse_tf_state(state, type_filter=args.resource_types)

    output = json.dumps(graph, indent=2 if args.pretty else None)

    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
