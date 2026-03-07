#!/usr/bin/env python3
"""
RCIE Graph Transpiler — ARM JSON → LLM → PostgreSQL + blueprint.bin

Usage:
  python scripts/transpile.py --input arm-template.json
  python scripts/transpile.py --synthetic   # Generate 10k-node test topology

Requires:
  pip install openai        (only for --input mode)
  psql must be on PATH      (uses psql subprocess — no psycopg needed)
"""

import argparse
import collections
import json
import os
import re
import struct
import subprocess
import sys
from pathlib import Path


# ── Configuration ─────────────────────────────────────────────────────────

PG_PORT = os.environ.get("RCIE_PG_PORT", "5433")
PG_DB = os.environ.get("RCIE_PG_DB", "rcie_poc")
PSQL_CMD = ["psql", "-p", PG_PORT, PG_DB, "-v", "ON_ERROR_STOP=1"]

PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "transpiler.md"
BLUEPRINT_PATH = Path(__file__).parent.parent / "data" / "blueprint.bin"

AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_KEY = os.environ.get("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
AZURE_OPENAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")


# ── psql helper ───────────────────────────────────────────────────────────

def run_sql(sql: str) -> str:
    """Pipe SQL to psql and return stdout. Exits on error."""
    result = subprocess.run(
        PSQL_CMD, input=sql, capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"psql error:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    return result.stdout


def query_rows(sql: str) -> list[list[str]]:
    """Run a query via psql and return rows as lists of strings.
    Uses -t (tuples only) and -A (unaligned) for easy parsing."""
    result = subprocess.run(
        PSQL_CMD + ["-t", "-A", "-c", sql],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"psql query error:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    rows = []
    for line in result.stdout.strip().split("\n"):
        if line:
            rows.append(line.split("|"))
    return rows


# ── LLM Transpilation ────────────────────────────────────────────────────

def load_prompt() -> str:
    """Load the transpiler system prompt from prompts/transpiler.md."""
    if not PROMPT_PATH.exists():
        print(f"ERROR: Prompt file not found: {PROMPT_PATH}", file=sys.stderr)
        sys.exit(1)
    return PROMPT_PATH.read_text()


def call_llm(system_prompt: str, arm_json: str) -> str:
    """Send the ARM JSON to Azure OpenAI and return the SQL output."""
    try:
        from openai import AzureOpenAI
    except ImportError:
        print("Install openai: pip install openai", file=sys.stderr)
        sys.exit(1)

    if not AZURE_OPENAI_ENDPOINT or not AZURE_OPENAI_KEY:
        print(
            "ERROR: Set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY env vars",
            file=sys.stderr,
        )
        sys.exit(1)

    client = AzureOpenAI(
        azure_endpoint=AZURE_OPENAI_ENDPOINT,
        api_key=AZURE_OPENAI_KEY,
        api_version=AZURE_OPENAI_API_VERSION,
    )

    response = client.chat.completions.create(
        model=AZURE_OPENAI_DEPLOYMENT,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": arm_json},
        ],
        temperature=0.0,
        max_tokens=16384,
    )

    content = response.choices[0].message.content
    if not content:
        print("ERROR: LLM returned empty response", file=sys.stderr)
        sys.exit(1)

    return content


def extract_sql(llm_output: str) -> str:
    """Extract SQL from the LLM response, stripping markdown fences if present."""
    blocks = re.findall(r"```sql\s*\n(.*?)```", llm_output, re.DOTALL)
    if blocks:
        return "\n".join(blocks)
    return llm_output


# ── Database Operations ──────────────────────────────────────────────────

def execute_sql_file(sql: str) -> None:
    """Execute the transpiled SQL against PostgreSQL via psql."""
    # Clear existing topology first, then run the generated SQL
    full_sql = "DELETE FROM edges;\nDELETE FROM nodes;\n" + sql
    run_sql(full_sql)

    # Report counts
    rows = query_rows("SELECT count(*) FROM nodes")
    node_count = rows[0][0] if rows else "?"
    rows = query_rows("SELECT count(*) FROM edges")
    edge_count = rows[0][0] if rows else "?"
    print(f"Inserted {node_count} nodes, {edge_count} edges")


def validate_dag() -> list[tuple[str, str]]:
    """
    Run a topological sort on the graph. If cycles exist, identify and
    drop the offending edges ("first wins" strategy).
    """
    # Load graph from PG via psql
    edge_rows = query_rows("SELECT id, source_id, target_id FROM edges")
    node_rows = query_rows("SELECT id FROM nodes")

    all_nodes = {r[0] for r in node_rows}
    adj: dict[str, list[str]] = collections.defaultdict(list)
    in_degree: dict[str, int] = {n: 0 for n in all_nodes}

    for edge_id, src, tgt in edge_rows:
        adj[src].append(tgt)
        in_degree[tgt] = in_degree.get(tgt, 0) + 1

    # Kahn's algorithm
    queue = collections.deque([n for n in all_nodes if in_degree.get(n, 0) == 0])
    visited: set[str] = set()

    while queue:
        node = queue.popleft()
        visited.add(node)
        for neighbor in adj.get(node, []):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    cycle_nodes = all_nodes - visited
    dropped: list[tuple[str, str]] = []
    if cycle_nodes:
        print(f"WARNING: Detected cycle involving {len(cycle_nodes)} nodes")
        drop_stmts = []
        for edge_id, src, tgt in edge_rows:
            if src in cycle_nodes and tgt in cycle_nodes:
                print(f"  Dropping edge: {src} → {tgt} (id: {edge_id})")
                drop_stmts.append(f"DELETE FROM edges WHERE id = '{edge_id}';")
                dropped.append((src, tgt))
        if drop_stmts:
            run_sql("\n".join(drop_stmts))

    return dropped


def export_blueprint() -> None:
    """Export the graph to blueprint.bin for the Rust solver's petgraph."""
    BLUEPRINT_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Fetch nodes and edges via psql JSON output
    node_rows = query_rows(
        "SELECT id, label, class, region, rack_id, properties FROM nodes ORDER BY id"
    )
    edge_rows = query_rows(
        "SELECT id, source_id, target_id, edge_type, properties FROM edges ORDER BY id"
    )

    with open(BLUEPRINT_PATH, "wb") as f:
        f.write(struct.pack("<II", len(node_rows), len(edge_rows)))

        for row in node_rows:
            node_id, label, cls, region, rack_id = row[0], row[1], row[2], row[3], row[4]
            props_str = row[5] if len(row) > 5 else "{}"
            region = None if region == "" else region
            rack_id = None if rack_id == "" else rack_id
            try:
                props = json.loads(props_str)
            except (json.JSONDecodeError, TypeError):
                props = {}

            node_json = json.dumps({
                "id": node_id, "label": label, "class": cls,
                "region": region, "rack_id": rack_id, "properties": props,
            }).encode()

            f.write(struct.pack("<I", len(node_id.encode())))
            f.write(node_id.encode())
            f.write(struct.pack("<I", len(node_json)))
            f.write(node_json)

        for row in edge_rows:
            edge_id, src_id, tgt_id, edge_type = row[0], row[1], row[2], row[3]
            props_str = row[4] if len(row) > 4 else "{}"
            try:
                props = json.loads(props_str)
            except (json.JSONDecodeError, TypeError):
                props = {}

            edge_json = json.dumps({
                "id": edge_id, "edge_type": edge_type, "properties": props,
            }).encode()

            f.write(struct.pack("<I", len(edge_id.encode())))
            f.write(edge_id.encode())
            f.write(struct.pack("<I", len(src_id.encode())))
            f.write(src_id.encode())
            f.write(struct.pack("<I", len(tgt_id.encode())))
            f.write(tgt_id.encode())
            f.write(struct.pack("<I", len(edge_json)))
            f.write(edge_json)

    print(f"Blueprint written to {BLUEPRINT_PATH} ({BLUEPRINT_PATH.stat().st_size} bytes)")


# ── Synthetic Topology Generator ──────────────────────────────────────────

def generate_synthetic() -> None:
    """
    Generate a 10k-node synthetic Azure topology for stress testing.
    Bypasses LLM — procedurally generates SQL and pipes to psql.

    Structure:
    - 10 regions
    - 10 racks per region (100 total), each with a latent ToR switch
    - 10 VMs per rack (1,000 total)
    - ~9 containers/identities per VM (8,900 total, bringing total to ~10k)
    - Latent AZ + PowerDomain nodes per region
    """
    stmts = ["DELETE FROM edges;", "DELETE FROM nodes;"]

    regions = [
        "eastus", "eastus2", "westus2", "westus3", "centralus",
        "northeurope", "westeurope", "southeastasia", "japaneast", "australiaeast",
    ]

    node_count = 0
    edge_count = 0

    for region in regions:
        pd_id = f"latent-power-{region}-1"
        stmts.append(
            f"INSERT INTO nodes (id, label, class, region, rack_id, properties) "
            f"VALUES ('{pd_id}', 'Power Domain {region}', 'PowerDomain', '{region}', NULL, '{{}}');"
        )
        node_count += 1

        az_id = f"latent-az-{region}-1"
        stmts.append(
            f"INSERT INTO nodes (id, label, class, region, rack_id, properties) "
            f"VALUES ('{az_id}', 'AZ {region}-1', 'AvailabilityZone', '{region}', NULL, '{{}}');"
        )
        node_count += 1
        stmts.append(
            f"INSERT INTO edges (id, source_id, target_id, edge_type, properties) "
            f"VALUES ('edge-{pd_id}-{az_id}', '{pd_id}', '{az_id}', 'containment', '{{}}');"
        )
        edge_count += 1

        # ── Per-region shared infrastructure ──────────────────────────
        # Gateway (application gateway / ingress) — upstream of containers
        gw_id = f"gw-{region}-01"
        stmts.append(
            f"INSERT INTO nodes (id, label, class, region, rack_id, properties) "
            f"VALUES ('{gw_id}', 'Gateway {region}', 'Gateway', '{region}', NULL, '{{}}');"
        )
        node_count += 1

        # KeyVault — stores secrets consumed by containers
        kv_id = f"kv-{region}-01"
        stmts.append(
            f"INSERT INTO nodes (id, label, class, region, rack_id, properties) "
            f"VALUES ('{kv_id}', 'KeyVault {region}', 'KeyVault', '{region}', NULL, '{{}}');"
        )
        node_count += 1

        for rack in range(10):
            rack_id = f"rack-{region}-{rack:02d}"
            tor_id = f"latent-tor-{region}-{rack:02d}"
            stmts.append(
                f"INSERT INTO nodes (id, label, class, region, rack_id, properties) "
                f"VALUES ('{tor_id}', 'ToR {region} rack {rack:02d}', 'ToRSwitch', "
                f"'{region}', '{rack_id}', '{{}}');"
            )
            node_count += 1
            stmts.append(
                f"INSERT INTO edges (id, source_id, target_id, edge_type, properties) "
                f"VALUES ('edge-{az_id}-{tor_id}', '{az_id}', '{tor_id}', 'containment', '{{}}');"
            )
            edge_count += 1

            for vm in range(10):
                vm_id = f"vm-{region}-{rack:02d}-{vm:02d}"
                stmts.append(
                    f"INSERT INTO nodes (id, label, class, region, rack_id, properties) "
                    f"VALUES ('{vm_id}', 'VM {vm_id}', 'VirtualMachine', "
                    f"'{region}', '{rack_id}', '{{}}');"
                )
                node_count += 1
                stmts.append(
                    f"INSERT INTO edges (id, source_id, target_id, edge_type, properties) "
                    f"VALUES ('edge-{tor_id}-{vm_id}', '{tor_id}', '{vm_id}', "
                    f"'containment', '{{}}');"
                )
                edge_count += 1

                for c in range(7):
                    ctr_id = f"ctr-{region}-{rack:02d}-{vm:02d}-{c:02d}"
                    stmts.append(
                        f"INSERT INTO nodes (id, label, class, region, rack_id, properties) "
                        f"VALUES ('{ctr_id}', 'Container {ctr_id}', 'Container', "
                        f"'{region}', '{rack_id}', '{{}}');"
                    )
                    node_count += 1
                    stmts.append(
                        f"INSERT INTO edges (id, source_id, target_id, edge_type, properties) "
                        f"VALUES ('edge-{vm_id}-{ctr_id}', '{vm_id}', '{ctr_id}', "
                        f"'containment', '{{}}');"
                    )
                    edge_count += 1

                    # First 3 containers per VM depend on the regional Gateway
                    if c < 3:
                        stmts.append(
                            f"INSERT INTO edges (id, source_id, target_id, edge_type, properties) "
                            f"VALUES ('edge-{gw_id}-{ctr_id}', '{gw_id}', '{ctr_id}', "
                            f"'dependency', '{{}}');"
                        )
                        edge_count += 1

                    # First 2 containers per VM depend on the regional KeyVault
                    if c < 2:
                        stmts.append(
                            f"INSERT INTO edges (id, source_id, target_id, edge_type, properties) "
                            f"VALUES ('edge-{kv_id}-{ctr_id}', '{kv_id}', '{ctr_id}', "
                            f"'dependency', '{{}}');"
                        )
                        edge_count += 1

                for mi in range(2):
                    mi_id = f"mi-{region}-{rack:02d}-{vm:02d}-{mi:02d}"
                    stmts.append(
                        f"INSERT INTO nodes (id, label, class, region, rack_id, properties) "
                        f"VALUES ('{mi_id}', 'MI {mi_id}', 'ManagedIdentity', "
                        f"'{region}', '{rack_id}', '{{}}');"
                    )
                    node_count += 1
                    stmts.append(
                        f"INSERT INTO edges (id, source_id, target_id, edge_type, properties) "
                        f"VALUES ('edge-{vm_id}-{mi_id}', '{vm_id}', '{mi_id}', "
                        f"'dependency', '{{}}');"
                    )
                    edge_count += 1

    # ── Per-region shared platform services ───────────────────────────
    # These are upstream dependencies shared by all application stacks
    # in the region: DNS, container registry, identity provider.
    for region in regions:
        gw_id = f"gw-{region}-01"  # already created above
        kv_id = f"kv-{region}-01"  # already created above

        dns_id = f"dns-{region}"
        stmts.append(
            f"INSERT INTO nodes (id, label, class, region, rack_id, properties) "
            f"VALUES ('{dns_id}', 'DNS {region}', 'DNS', '{region}', NULL, '{{}}');"
        )
        node_count += 1

        acr_id = f"acr-{region}"
        stmts.append(
            f"INSERT INTO nodes (id, label, class, region, rack_id, properties) "
            f"VALUES ('{acr_id}', 'ContainerRegistry {region}', 'ContainerRegistry', '{region}', NULL, '{{}}');"
        )
        node_count += 1

        idp_id = f"idp-{region}"
        stmts.append(
            f"INSERT INTO nodes (id, label, class, region, rack_id, properties) "
            f"VALUES ('{idp_id}', 'IdentityProvider {region}', 'IdentityProvider', '{region}', NULL, '{{}}');"
        )
        node_count += 1

        ca_id = f"ca-{region}"
        stmts.append(
            f"INSERT INTO nodes (id, label, class, region, rack_id, properties) "
            f"VALUES ('{ca_id}', 'CertAuthority {region}', 'CertAuthority', '{region}', NULL, '{{}}');"
        )
        node_count += 1

    # ── 1000 Application Stacks (100 per region) ─────────────────────
    #
    # Each application stack models a realistic Azure/Radius deployment:
    #
    #   Application
    #   ├── VirtualNetwork
    #   │   ├── Subnet (frontend)
    #   │   │   └── NetworkSecurityGroup
    #   │   └── Subnet (backend)
    #   │       └── NetworkSecurityGroup
    #   ├── AKSCluster
    #   │   ├── Container × 4 (pods)
    #   │   └── HttpRoute (ingress route)
    #   ├── Gateway (app gateway / ingress controller)
    #   ├── LoadBalancer
    #   ├── ManagedIdentity
    #   └── DataStore (SqlDatabase or RedisCache, alternating)
    #
    # Edges (cause → effect direction):
    #   VNet → Subnet (containment)
    #   Subnet → NSG (dependency)
    #   AKSCluster → Container (containment)
    #   AKSCluster → HttpRoute (containment)
    #   Application → AKSCluster (containment)
    #   Application → Gateway (containment)
    #   Application → LoadBalancer (containment)
    #   Gateway → AKSCluster (dependency: gateway routes to AKS)
    #   LoadBalancer → AKSCluster (dependency: LB fronts the cluster)
    #   Subnet → AKSCluster (containment: cluster lives in subnet)
    #   ManagedIdentity → Container (dependency: pods use the MI)
    #   KeyVault → Container (dependency: pods pull secrets)
    #   ContainerRegistry → Container (dependency: pods pull images)
    #   DNS → Gateway (dependency: DNS resolves to gateway)
    #   CertAuthority → Gateway (dependency: certs for TLS)
    #   DataStore → Container (connection: pods connect to data)
    #   IdentityProvider → ManagedIdentity (dependency: AAD backs the MI)

    for region in regions:
        dns_id = f"dns-{region}"
        acr_id = f"acr-{region}"
        idp_id = f"idp-{region}"
        ca_id = f"ca-{region}"
        kv_id = f"kv-{region}-01"

        for app_idx in range(100):
            prefix = f"{region}-app{app_idx:03d}"

            # ── Application (logical grouping) ───────────────────────
            app_id = f"app-{prefix}"
            stmts.append(
                f"INSERT INTO nodes (id, label, class, region, rack_id, properties) "
                f"VALUES ('{app_id}', 'App {prefix}', 'Application', '{region}', NULL, "
                f"'{{\"app_index\": {app_idx}}}');"
            )
            node_count += 1

            # ── VirtualNetwork ───────────────────────────────────────
            vnet_id = f"vnet-{prefix}"
            stmts.append(
                f"INSERT INTO nodes (id, label, class, region, rack_id, properties) "
                f"VALUES ('{vnet_id}', 'VNet {prefix}', 'VirtualNetwork', '{region}', NULL, '{{}}');"
            )
            node_count += 1

            # ── Subnets (frontend + backend) ─────────────────────────
            for sn_name in ("frontend", "backend"):
                sn_id = f"subnet-{prefix}-{sn_name}"
                stmts.append(
                    f"INSERT INTO nodes (id, label, class, region, rack_id, properties) "
                    f"VALUES ('{sn_id}', 'Subnet {sn_name} {prefix}', 'SubnetGateway', '{region}', NULL, '{{}}');"
                )
                node_count += 1
                # VNet → Subnet (containment)
                stmts.append(
                    f"INSERT INTO edges (id, source_id, target_id, edge_type, properties) "
                    f"VALUES ('edge-{vnet_id}-{sn_id}', '{vnet_id}', '{sn_id}', 'containment', '{{}}');"
                )
                edge_count += 1

                # NSG per subnet
                nsg_id = f"nsg-{prefix}-{sn_name}"
                stmts.append(
                    f"INSERT INTO nodes (id, label, class, region, rack_id, properties) "
                    f"VALUES ('{nsg_id}', 'NSG {sn_name} {prefix}', 'NetworkInterface', '{region}', NULL, '{{}}');"
                )
                node_count += 1
                # Subnet → NSG (dependency)
                stmts.append(
                    f"INSERT INTO edges (id, source_id, target_id, edge_type, properties) "
                    f"VALUES ('edge-{sn_id}-{nsg_id}', '{sn_id}', '{nsg_id}', 'dependency', '{{}}');"
                )
                edge_count += 1

            # ── AKS Cluster ──────────────────────────────────────────
            aks_id = f"aks-{prefix}"
            stmts.append(
                f"INSERT INTO nodes (id, label, class, region, rack_id, properties) "
                f"VALUES ('{aks_id}', 'AKS {prefix}', 'AKSCluster', '{region}', NULL, '{{}}');"
            )
            node_count += 1
            # Application → AKS (containment)
            stmts.append(
                f"INSERT INTO edges (id, source_id, target_id, edge_type, properties) "
                f"VALUES ('edge-{app_id}-{aks_id}', '{app_id}', '{aks_id}', 'containment', '{{}}');"
            )
            edge_count += 1
            # Backend subnet → AKS (containment: cluster lives in backend subnet)
            be_subnet = f"subnet-{prefix}-backend"
            stmts.append(
                f"INSERT INTO edges (id, source_id, target_id, edge_type, properties) "
                f"VALUES ('edge-{be_subnet}-{aks_id}', '{be_subnet}', '{aks_id}', 'containment', '{{}}');"
            )
            edge_count += 1

            # ── Gateway (ingress controller) ─────────────────────────
            appgw_id = f"appgw-{prefix}"
            stmts.append(
                f"INSERT INTO nodes (id, label, class, region, rack_id, properties) "
                f"VALUES ('{appgw_id}', 'Gateway {prefix}', 'Gateway', '{region}', NULL, '{{}}');"
            )
            node_count += 1
            # Application → Gateway (containment)
            stmts.append(
                f"INSERT INTO edges (id, source_id, target_id, edge_type, properties) "
                f"VALUES ('edge-{app_id}-{appgw_id}', '{app_id}', '{appgw_id}', 'containment', '{{}}');"
            )
            edge_count += 1
            # Gateway → AKS (dependency: gateway routes traffic to cluster)
            stmts.append(
                f"INSERT INTO edges (id, source_id, target_id, edge_type, properties) "
                f"VALUES ('edge-{appgw_id}-{aks_id}', '{appgw_id}', '{aks_id}', 'dependency', '{{}}');"
            )
            edge_count += 1
            # Frontend subnet → Gateway (containment)
            fe_subnet = f"subnet-{prefix}-frontend"
            stmts.append(
                f"INSERT INTO edges (id, source_id, target_id, edge_type, properties) "
                f"VALUES ('edge-{fe_subnet}-{appgw_id}', '{fe_subnet}', '{appgw_id}', 'containment', '{{}}');"
            )
            edge_count += 1
            # DNS → Gateway (dependency: DNS resolves to this gateway)
            stmts.append(
                f"INSERT INTO edges (id, source_id, target_id, edge_type, properties) "
                f"VALUES ('edge-{dns_id}-{appgw_id}', '{dns_id}', '{appgw_id}', 'dependency', '{{}}');"
            )
            edge_count += 1
            # CertAuthority → Gateway (dependency: TLS certs)
            stmts.append(
                f"INSERT INTO edges (id, source_id, target_id, edge_type, properties) "
                f"VALUES ('edge-{ca_id}-{appgw_id}', '{ca_id}', '{appgw_id}', 'dependency', '{{}}');"
            )
            edge_count += 1

            # ── LoadBalancer ─────────────────────────────────────────
            lb_id = f"lb-{prefix}"
            stmts.append(
                f"INSERT INTO nodes (id, label, class, region, rack_id, properties) "
                f"VALUES ('{lb_id}', 'LB {prefix}', 'LoadBalancer', '{region}', NULL, '{{}}');"
            )
            node_count += 1
            # Application → LB (containment)
            stmts.append(
                f"INSERT INTO edges (id, source_id, target_id, edge_type, properties) "
                f"VALUES ('edge-{app_id}-{lb_id}', '{app_id}', '{lb_id}', 'containment', '{{}}');"
            )
            edge_count += 1
            # LB → AKS (dependency: load balancer fronts the cluster)
            stmts.append(
                f"INSERT INTO edges (id, source_id, target_id, edge_type, properties) "
                f"VALUES ('edge-{lb_id}-{aks_id}', '{lb_id}', '{aks_id}', 'dependency', '{{}}');"
            )
            edge_count += 1

            # ── HttpRoute (ingress route) ────────────────────────────
            route_id = f"route-{prefix}"
            stmts.append(
                f"INSERT INTO nodes (id, label, class, region, rack_id, properties) "
                f"VALUES ('{route_id}', 'Route {prefix}', 'HttpRoute', '{region}', NULL, '{{}}');"
            )
            node_count += 1
            # AKS → Route (containment)
            stmts.append(
                f"INSERT INTO edges (id, source_id, target_id, edge_type, properties) "
                f"VALUES ('edge-{aks_id}-{route_id}', '{aks_id}', '{route_id}', 'containment', '{{}}');"
            )
            edge_count += 1
            # Gateway → Route (dependency: gateway uses route)
            stmts.append(
                f"INSERT INTO edges (id, source_id, target_id, edge_type, properties) "
                f"VALUES ('edge-{appgw_id}-{route_id}', '{appgw_id}', '{route_id}', 'dependency', '{{}}');"
            )
            edge_count += 1

            # ── ManagedIdentity ──────────────────────────────────────
            mi_id = f"mi-{prefix}"
            stmts.append(
                f"INSERT INTO nodes (id, label, class, region, rack_id, properties) "
                f"VALUES ('{mi_id}', 'MI {prefix}', 'ManagedIdentity', '{region}', NULL, '{{}}');"
            )
            node_count += 1
            # IdentityProvider → MI (dependency: AAD backs the identity)
            stmts.append(
                f"INSERT INTO edges (id, source_id, target_id, edge_type, properties) "
                f"VALUES ('edge-{idp_id}-{mi_id}', '{idp_id}', '{mi_id}', 'dependency', '{{}}');"
            )
            edge_count += 1

            # ── Data store (alternating SQL / Redis / Mongo) ─────────
            ds_type_idx = app_idx % 3
            if ds_type_idx == 0:
                ds_class, ds_label = "SqlDatabase", "SQL"
            elif ds_type_idx == 1:
                ds_class, ds_label = "RedisCache", "Redis"
            else:
                ds_class, ds_label = "MongoDatabase", "Mongo"
            ds_id = f"ds-{prefix}"
            stmts.append(
                f"INSERT INTO nodes (id, label, class, region, rack_id, properties) "
                f"VALUES ('{ds_id}', '{ds_label} {prefix}', '{ds_class}', '{region}', NULL, '{{}}');"
            )
            node_count += 1
            # Application → DataStore (containment)
            stmts.append(
                f"INSERT INTO edges (id, source_id, target_id, edge_type, properties) "
                f"VALUES ('edge-{app_id}-{ds_id}', '{app_id}', '{ds_id}', 'containment', '{{}}');"
            )
            edge_count += 1

            # ── Containers (pods in the AKS cluster) ─────────────────
            for pod in range(4):
                pod_id = f"pod-{prefix}-{pod:02d}"
                stmts.append(
                    f"INSERT INTO nodes (id, label, class, region, rack_id, properties) "
                    f"VALUES ('{pod_id}', 'Pod {prefix}/{pod}', 'Container', '{region}', NULL, '{{}}');"
                )
                node_count += 1
                # AKS → Pod (containment)
                stmts.append(
                    f"INSERT INTO edges (id, source_id, target_id, edge_type, properties) "
                    f"VALUES ('edge-{aks_id}-{pod_id}', '{aks_id}', '{pod_id}', 'containment', '{{}}');"
                )
                edge_count += 1
                # MI → Pod (dependency: pod uses managed identity)
                stmts.append(
                    f"INSERT INTO edges (id, source_id, target_id, edge_type, properties) "
                    f"VALUES ('edge-{mi_id}-{pod_id}', '{mi_id}', '{pod_id}', 'dependency', '{{}}');"
                )
                edge_count += 1
                # KeyVault → Pod (dependency: pod pulls secrets)
                stmts.append(
                    f"INSERT INTO edges (id, source_id, target_id, edge_type, properties) "
                    f"VALUES ('edge-{kv_id}-{pod_id}', '{kv_id}', '{pod_id}', 'dependency', '{{}}');"
                )
                edge_count += 1
                # ContainerRegistry → Pod (dependency: pod pulls images)
                stmts.append(
                    f"INSERT INTO edges (id, source_id, target_id, edge_type, properties) "
                    f"VALUES ('edge-{acr_id}-{pod_id}', '{acr_id}', '{pod_id}', 'dependency', '{{}}');"
                )
                edge_count += 1
                # DataStore → Pod (connection: pod connects to data store)
                stmts.append(
                    f"INSERT INTO edges (id, source_id, target_id, edge_type, properties) "
                    f"VALUES ('edge-{ds_id}-{pod_id}', '{ds_id}', '{pod_id}', 'connection', '{{}}');"
                )
                edge_count += 1

    print(f"Generating synthetic topology: {node_count} nodes, {edge_count} edges")
    # Wrap in a transaction and batch for speed
    batch_size = 5000
    all_stmts = stmts
    print(f"Writing {len(all_stmts)} SQL statements in batches of {batch_size}...")
    for i in range(0, len(all_stmts), batch_size):
        batch = all_stmts[i:i+batch_size]
        sql = "BEGIN;\n" + "\n".join(batch) + "\nCOMMIT;\n"
        run_sql(sql)
        done = min(i + batch_size, len(all_stmts))
        print(f"  {done}/{len(all_stmts)} statements executed")
    print("Synthetic topology written to PostgreSQL")
    export_blueprint()


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="RCIE Graph Transpiler")
    parser.add_argument("--input", type=str, help="Path to Radius ARM JSON file")
    parser.add_argument(
        "--synthetic", action="store_true", help="Generate 10k-node synthetic topology"
    )
    args = parser.parse_args()

    if args.synthetic:
        generate_synthetic()
        return

    if not args.input:
        parser.error("Either --input or --synthetic is required")

    arm_path = Path(args.input)
    if not arm_path.exists():
        print(f"ERROR: File not found: {arm_path}", file=sys.stderr)
        sys.exit(1)

    arm_json = arm_path.read_text()
    try:
        json.loads(arm_json)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in {arm_path}: {e}", file=sys.stderr)
        sys.exit(1)

    prompt = load_prompt()
    print(f"Calling LLM with {len(arm_json)} chars of ARM JSON...")
    llm_output = call_llm(prompt, arm_json)

    sql = extract_sql(llm_output)
    print(f"LLM returned {len(sql)} chars of SQL")

    execute_sql_file(sql)

    dropped = validate_dag()
    if dropped:
        print(f"Dropped {len(dropped)} edges to break cycles")
    else:
        print("DAG validation passed — no cycles detected")

    export_blueprint()


if __name__ == "__main__":
    main()
