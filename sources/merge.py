#!/usr/bin/env python3
"""
Merge multiple GraphPayload JSON files into one.

Nodes are deduplicated by ID (last writer wins for properties).
Edges are deduplicated by (source_id, target_id, edge_type).
Dangling edges (referencing nodes not in any source) are dropped.

Usage:
  # Merge two files
  python3 sources/merge.py graph1.json graph2.json --output merged.json

  # Merge from stdin (process substitution)
  python3 sources/merge.py \
    <(python3 sources/arg_source.py -s $SUB) \
    <(python3 sources/terraform_source.py --state terraform.tfstate)

  # Pipe to engine
  python3 sources/merge.py graph1.json graph2.json | \
    curl -X POST http://localhost:8080/api/graph/load -H 'Content-Type: application/json' -d @-
"""

import argparse
import json
import sys


def merge_graphs(*graphs: dict) -> dict:
    """
    Merge multiple GraphPayload dicts into one.

    - Nodes: deduplicated by id. Later sources override earlier ones,
      but `properties` dicts are shallow-merged so both sources contribute.
    - Edges: deduplicated by (source_id, target_id, edge_type).
    - Dangling edges referencing absent nodes are dropped.
    """
    merged_nodes: dict[str, dict] = {}
    all_edges: list[dict] = []

    for i, g in enumerate(graphs):
        nodes = g.get("nodes", [])
        edges = g.get("edges", [])

        for n in nodes:
            nid = n["id"]
            if nid in merged_nodes:
                # Shallow-merge properties
                existing_props = merged_nodes[nid].get("properties", {})
                new_props = n.get("properties", {})
                merged_props = {**existing_props, **new_props}
                merged_nodes[nid] = {**merged_nodes[nid], **n}
                merged_nodes[nid]["properties"] = merged_props
            else:
                merged_nodes[nid] = n

        all_edges.extend(edges)

    # Deduplicate edges
    seen: set[tuple[str, str, str]] = set()
    dedup_edges = []
    for e in all_edges:
        key = (e["source_id"], e["target_id"], e["edge_type"])
        if key not in seen:
            seen.add(key)
            dedup_edges.append(e)

    # Drop dangling
    node_ids = set(merged_nodes.keys())
    valid_edges = [e for e in dedup_edges
                   if e["source_id"] in node_ids and e["target_id"] in node_ids]
    dangling = len(dedup_edges) - len(valid_edges)

    result_nodes = list(merged_nodes.values())
    print(f"Merged: {len(result_nodes)} nodes, {len(valid_edges)} edges"
          f" ({dangling} dangling edges dropped)", file=sys.stderr)

    return {"nodes": result_nodes, "edges": valid_edges}


def main():
    parser = argparse.ArgumentParser(
        description="Merge multiple Causinator 9000 GraphPayload JSON files.",
    )
    parser.add_argument(
        "files", nargs="+",
        help="GraphPayload JSON files to merge (or use process substitution).",
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

    graphs = []
    for path in args.files:
        try:
            if path == "-":
                graphs.append(json.load(sys.stdin))
            else:
                with open(path) as f:
                    graphs.append(json.load(f))
                print(f"Loaded {path}: {len(graphs[-1].get('nodes', []))} nodes, "
                      f"{len(graphs[-1].get('edges', []))} edges", file=sys.stderr)
        except Exception as e:
            print(f"ERROR reading {path}: {e}", file=sys.stderr)
            sys.exit(1)

    result = merge_graphs(*graphs)

    output = json.dumps(result, indent=2 if args.pretty else None)

    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
