#!/usr/bin/env python3
"""
Kubernetes → Causinator 9000 topology + signals source.

Extracts K8s cluster state and events as graph nodes, edges, mutations,
and signals. Works with any cluster accessible via kubectl.

Two modes:
  Snapshot (polling): Extracts current state of pods, deployments, services,
    events. Run periodically via make ingest-k8s.
  Watch (real-time): Streams K8s events in real-time via kubectl --watch.
    Run as a background process via make watch-k8s.

Usage:
  # Snapshot mode — ingest current cluster state
  python3 sources/k8s_source.py --context radlrtest00-aks

  # Watch mode — stream events in real-time
  python3 sources/k8s_source.py --context radlrtest00-aks --watch

  # Specific namespaces
  python3 sources/k8s_source.py --context radlrtest00-aks -n radius-system -n dapr-system

  # Dry run
  python3 sources/k8s_source.py --context radlrtest00-aks --dry-run
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone

ENGINE = os.environ.get("C9K_ENGINE_URL", "http://localhost:8080")

# ── Pod status → signal mapping ─────────────────────────────────────────

POD_SIGNAL_MAP = {
    "CrashLoopBackOff": ("CrashLoopBackOff", "critical"),
    "ImagePullBackOff": ("ImagePullError", "critical"),
    "ErrImagePull": ("ImagePullError", "critical"),
    "OOMKilled": ("OOMKilled", "critical"),
    "Error": ("PodError", "critical"),
    "CreateContainerConfigError": ("ConfigError", "critical"),
    "Pending": ("PodPending", "warning"),
    "Unknown": ("PodUnknown", "warning"),
}

# ── Event reason → signal/mutation mapping ──────────────────────────────

EVENT_SIGNAL_MAP = {
    # Signals (something is wrong)
    "Failed": ("PodFailed", "critical"),
    "FailedCreate": ("FailedCreate", "critical"),
    "FailedScheduling": ("SchedulingFailure", "warning"),
    "Unhealthy": ("HealthCheckFailed", "warning"),
    "BackOff": ("CrashLoopBackOff", "critical"),
    "FailedMount": ("VolumeMountFailure", "critical"),
    "FailedAttachVolume": ("VolumeMountFailure", "critical"),
    "OOMKilling": ("OOMKilled", "critical"),
    "Evicted": ("PodEviction", "warning"),
    "FailedGetContainerResourceMetric": ("MetricsUnavailable", "info"),
}

EVENT_MUTATION_MAP = {
    # Mutations (something changed)
    "Pulled": "ImagePull",
    "Created": "ContainerCreated",
    "Started": "ContainerStarted",
    "Killing": "ContainerStopping",
    "ScalingReplicaSet": "ScaleEvent",
    "SuccessfulCreate": "PodCreated",
    "SuccessfulDelete": "PodDeleted",
    "LeaderElection": "LeaderElection",
    "Scheduled": "PodScheduled",
}


def post_engine(path, payload):
    import urllib.request
    url = f"{ENGINE}/api/{path}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  ERROR: {url}: {e}", file=sys.stderr)
        return None


def kubectl(*args, context=None):
    cmd = ["kubectl"]
    if context:
        cmd.extend(["--context", context])
    cmd.extend(args)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        print(f"  kubectl error: {r.stderr[:200]}", file=sys.stderr)
        return None
    return r.stdout


def k8s_node_id(cluster: str, namespace: str, kind: str, name: str) -> str:
    return f"k8s://{cluster}/{namespace}/{kind}/{name}"


def cluster_node_id(cluster: str) -> str:
    return f"k8s://{cluster}"


# ── Snapshot mode ────────────────────────────────────────────────────────

def ingest_snapshot(context: str, namespaces: list[str], aks_resource_id: str | None,
                    engine: str, dry_run: bool) -> tuple[int, int, int]:
    """Snapshot current cluster state into the graph."""
    cluster = context
    nodes = []
    edges = []
    signals_to_send = []

    # Use the ARG resource ID as the cluster node if provided (merges with ARG node)
    # Otherwise create a standalone K8s cluster node
    if aks_resource_id:
        cid = aks_resource_id.lower()
    else:
        cid = cluster_node_id(cluster)
        nodes.append({
            "id": cid, "label": cluster, "class": "AKSCluster",
            "region": "kubernetes", "rack_id": None,
            "properties": {"source": "k8s", "context": context},
        })

    ns_list = namespaces
    if not ns_list:
        raw = kubectl("get", "namespaces", "-o", "jsonpath={.items[*].metadata.name}",
                       context=context)
        ns_list = raw.split() if raw else ["default"]

    for ns in ns_list:
        # Namespace node
        nsid = k8s_node_id(cluster, ns, "namespace", ns)
        nodes.append({
            "id": nsid, "label": ns, "class": "KubernetesNamespace",
            "region": "kubernetes", "rack_id": None,
            "properties": {"source": "k8s"},
        })
        edges.append({
            "id": f"edge-{cid[-15:]}-{ns}",
            "source_id": cid, "target_id": nsid,
            "edge_type": "containment", "properties": {},
        })

        # Pods
        raw = kubectl("get", "pods", "-n", ns, "-o", "json", context=context)
        if not raw:
            continue
        pod_list = json.loads(raw).get("items", [])

        for pod in pod_list:
            name = pod["metadata"]["name"]
            phase = pod["status"].get("phase", "Unknown")
            pid = k8s_node_id(cluster, ns, "pod", name)

            # Get container status for signal detection
            container_statuses = pod["status"].get("containerStatuses", [])
            waiting_reasons = []
            for cs in container_statuses:
                waiting = cs.get("state", {}).get("waiting", {})
                if waiting.get("reason"):
                    waiting_reasons.append(waiting["reason"])
                terminated = cs.get("state", {}).get("terminated", {})
                if terminated.get("reason"):
                    waiting_reasons.append(terminated["reason"])

            restart_count = sum(cs.get("restartCount", 0) for cs in container_statuses)
            owner_refs = pod["metadata"].get("ownerReferences", [])
            owner = owner_refs[0]["name"] if owner_refs else ""

            nodes.append({
                "id": pid, "label": name, "class": "Container",
                "region": "kubernetes", "rack_id": None,
                "properties": {
                    "source": "k8s", "phase": phase,
                    "restart_count": restart_count,
                    "namespace": ns, "owner": owner,
                },
            })
            edges.append({
                "id": f"edge-{nsid[-15:]}-{name[:15]}",
                "source_id": nsid, "target_id": pid,
                "edge_type": "containment", "properties": {},
            })

            # Detect signals from pod status
            for reason in waiting_reasons:
                signal_info = POD_SIGNAL_MAP.get(reason)
                if signal_info:
                    sig_type, severity = signal_info
                    if dry_run:
                        print(f"  SIGNAL: {sig_type} on {pid}", file=sys.stderr)
                    else:
                        signals_to_send.append({
                            "node_id": pid,
                            "signal_type": sig_type,
                            "severity": severity,
                            "properties": {
                                "reason": reason, "phase": phase,
                                "restart_count": restart_count,
                                "namespace": ns,
                            },
                        })

            # Pending pods with no node → scheduling failure
            if phase == "Pending" and not pod["status"].get("conditions"):
                if dry_run:
                    print(f"  SIGNAL: PodPending on {pid}", file=sys.stderr)
                else:
                    signals_to_send.append({
                        "node_id": pid,
                        "signal_type": "PodPending",
                        "severity": "warning",
                        "properties": {"phase": phase, "namespace": ns},
                    })

    if dry_run:
        print(f"\n  {len(nodes)} nodes, {len(edges)} edges, "
              f"{len(signals_to_send)} signals", file=sys.stderr)
        return 0, 0, 0

    # Merge topology
    result = post_engine("graph/merge", {"nodes": nodes, "edges": edges})
    new_nodes = result.get("new_nodes", 0) if result else 0
    new_edges = result.get("new_edges", 0) if result else 0
    print(f"  Topology: {new_nodes} new nodes, {new_edges} new edges", file=sys.stderr)

    # Send signals
    sig_count = 0
    for s in signals_to_send:
        if post_engine("signals", s):
            sig_count += 1

    return new_nodes, 0, sig_count


# ── Watch mode ───────────────────────────────────────────────────────────

def watch_events(context: str, namespaces: list[str]):
    """Stream K8s events in real-time and convert to mutations/signals."""
    cluster = context

    cmd = ["kubectl", "--context", context, "get", "events",
           "--all-namespaces" if not namespaces else f"-n", 
           namespaces[0] if namespaces else "",
           "--watch-only", "-o", "json"]
    if not namespaces:
        cmd = ["kubectl", "--context", context, "get", "events",
               "--all-namespaces", "--watch-only", "-o", "json"]

    print(f"Watching K8s events on {context}...", file=sys.stderr)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    buffer = ""
    try:
        for line in proc.stdout:
            buffer += line
            # Try to parse accumulated JSON
            try:
                event = json.loads(buffer)
                buffer = ""
                process_k8s_event(cluster, event)
            except json.JSONDecodeError:
                continue
    except KeyboardInterrupt:
        print("\nStopping watch.", file=sys.stderr)
    finally:
        proc.terminate()


def process_k8s_event(cluster: str, event: dict):
    """Process a single K8s event into a mutation or signal."""
    reason = event.get("reason", "")
    message = event.get("message", "")
    ns = event.get("metadata", {}).get("namespace", "default")
    involved = event.get("involvedObject", {})
    obj_kind = involved.get("kind", "unknown").lower()
    obj_name = involved.get("name", "unknown")
    event_type = event.get("type", "Normal")  # Normal or Warning
    timestamp = event.get("lastTimestamp") or event.get("metadata", {}).get("creationTimestamp", "")

    node_id = k8s_node_id(cluster, ns, obj_kind, obj_name)

    # Check if this is a signal (Warning event or known failure)
    if reason in EVENT_SIGNAL_MAP:
        sig_type, severity = EVENT_SIGNAL_MAP[reason]
        print(f"  SIGNAL: {sig_type} on {node_id} — {message[:80]}", file=sys.stderr)

        # Ensure node exists
        post_engine("graph/merge", {"nodes": [{
            "id": node_id, "label": obj_name,
            "class": "Container" if obj_kind == "pod" else obj_kind.title(),
            "region": "kubernetes", "rack_id": None,
            "properties": {"source": "k8s-watch", "namespace": ns},
        }], "edges": []})

        post_engine("signals", {
            "node_id": node_id,
            "signal_type": sig_type,
            "severity": severity,
            "timestamp": timestamp,
            "properties": {
                "reason": reason,
                "message": message[:500],
                "namespace": ns,
                "kind": obj_kind,
            },
        })

    elif reason in EVENT_MUTATION_MAP:
        mut_type = EVENT_MUTATION_MAP[reason]
        # Only log interesting mutations, skip routine scheduling
        if mut_type not in ("PodScheduled", "LeaderElection"):
            print(f"  MUTATION: {mut_type} on {node_id}", file=sys.stderr)

        post_engine("mutations", {
            "node_id": node_id,
            "mutation_type": mut_type,
            "source": "k8s-watch",
            "timestamp": timestamp,
            "properties": {
                "reason": reason,
                "message": message[:200],
                "namespace": ns,
            },
        })


def main():
    parser = argparse.ArgumentParser(
        description="Kubernetes cluster → Causinator 9000 topology + signals.",
    )
    parser.add_argument("--context", "-c",
                        default=os.environ.get("DRASI_K8S_CONTEXT", ""),
                        help="kubectl context (cluster name)")
    parser.add_argument("--namespace", "-n", action="append", dest="namespaces",
                        help="Namespace to monitor (repeatable). Default: all.")
    parser.add_argument("--aks-resource-id",
                        help="ARM resource ID of the AKS cluster for cross-linking to ARG")
    parser.add_argument("--watch", "-w", action="store_true",
                        help="Watch mode: stream events in real-time")
    parser.add_argument("--engine", default=ENGINE)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    global ENGINE
    ENGINE = args.engine

    if not args.context:
        # Try to detect current context
        r = subprocess.run(["kubectl", "config", "current-context"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            args.context = r.stdout.strip()
        else:
            print("ERROR: No kubectl context. Use --context or set DRASI_K8S_CONTEXT",
                  file=sys.stderr)
            sys.exit(1)

    print(f"Cluster: {args.context}", file=sys.stderr)

    if args.watch:
        watch_events(args.context, args.namespaces or [])
    else:
        nodes, muts, sigs = ingest_snapshot(
            args.context, args.namespaces or [], args.aks_resource_id,
            args.engine, args.dry_run)
        if args.dry_run:
            print("Dry run complete.", file=sys.stderr)
        else:
            print(f"\nIngested: {nodes} nodes, {muts} mutations, {sigs} signals",
                  file=sys.stderr)


if __name__ == "__main__":
    main()
