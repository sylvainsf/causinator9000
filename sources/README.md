# Graph Sources

Source adapters that extract infrastructure topology from external systems
and produce `GraphPayload` JSON for the Causinator 9000 engine.

## Architecture

Each source adapter is a standalone script that:
1. Connects to an external system (ARG, Terraform, Radius, etc.)
2. Extracts nodes and edges
3. Outputs a `GraphPayload` JSON to stdout (or a file)

The output format matches the engine's `POST /api/graph/load` and
`POST /api/graph/merge` endpoints:

```json
{
  "nodes": [
    {
      "id": "unique-node-id",
      "label": "Human-readable name",
      "class": "VirtualMachine",
      "region": "eastus",
      "rack_id": null,
      "properties": {}
    }
  ],
  "edges": [
    {
      "id": "edge-source-target",
      "source_id": "source-node-id",
      "target_id": "target-node-id",
      "edge_type": "dependency",
      "properties": {}
    }
  ]
}
```

## Available Sources

| Source | Script | Auth | What it extracts |
|--------|--------|------|------------------|
| Azure Resource Graph | `arg_source.py` | `az login` / DefaultAzureCredential | VMs, NICs, disks, VNets, subnets, NSGs, AKS, KV, SQL, LBs, AppGWs, identities |
| Terraform State | `terraform_source.py` | Local `.tfstate` file or remote backend | All resources + dependency edges from `depends_on` |
| Radius | (planned) | Radius API | Application graph with recipes and connections |

## Usage

### Single source → engine

```bash
# Extract from ARG, load directly
python3 sources/arg_source.py --subscription $SUB_ID | \
  curl -X POST http://localhost:8080/api/graph/load \
    -H 'Content-Type: application/json' -d @-

# Extract from Terraform state, merge into existing graph
python3 sources/terraform_source.py --state ./terraform.tfstate | \
  curl -X POST http://localhost:8080/api/graph/merge \
    -H 'Content-Type: application/json' -d @-
```

### Multiple sources → merged

```bash
# Merge multiple sources client-side, then load
python3 sources/merge.py \
  <(python3 sources/arg_source.py --subscription $SUB_ID) \
  <(python3 sources/terraform_source.py --state ./terraform.tfstate) \
  | curl -X POST http://localhost:8080/api/graph/load \
    -H 'Content-Type: application/json' -d @-
```

### Save to file for inspection

```bash
python3 sources/arg_source.py --subscription $SUB_ID --output graph.json
python3 sources/terraform_source.py --state ./terraform.tfstate --output tf.json
python3 sources/merge.py graph.json tf.json --output merged.json
```

## Merge Semantics

When merging graphs from multiple sources:
- **Nodes**: deduplicated by `id`. If two sources define the same node ID,
  the later source's properties are merged (shallow merge on `properties`).
- **Edges**: deduplicated by `(source_id, target_id, edge_type)`.
  Duplicate edges are dropped.
- **Cross-source edges**: Sources can reference node IDs from other sources.
  E.g., a Terraform resource can reference an ARG-discovered subnet by
  its ARM resource ID.

## Node ID Convention

All sources should use the **ARM resource ID** (lowercased) as the node ID
when possible. This ensures cross-source deduplication works:

```
/subscriptions/{sub}/resourcegroups/{rg}/providers/microsoft.compute/virtualmachines/{name}
```

For resources without ARM IDs (e.g., Kubernetes pods, Radius recipes),
use a hierarchical ID that won't collide:

```
k8s://{cluster}/{namespace}/{kind}/{name}
radius://{app}/{resource}
```
