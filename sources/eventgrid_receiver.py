#!/usr/bin/env python3
"""
Azure Event Grid Webhook Receiver → Causinator 9000 real-time ingestion.

Receives Azure Event Grid events and converts them to mutations/signals
in real-time, replacing the polling-based azure_health_source.py.

Setup:
  1. Start receiver: make webhook-azure
  2. Create Event Grid system topic + subscription:

     # Resource write/delete events
     az eventgrid system-topic create \
       --name c9k-resource-events \
       --resource-group <rg> \
       --source /subscriptions/<sub-id> \
       --topic-type Microsoft.Resources.Subscriptions

     az eventgrid system-topic event-subscription create \
       --name c9k-mutations \
       --system-topic-name c9k-resource-events \
       --resource-group <rg> \
       --endpoint https://<your-host>:8091/webhook/eventgrid \
       --included-event-types \
         Microsoft.Resources.ResourceWriteSuccess \
         Microsoft.Resources.ResourceDeleteSuccess \
         Microsoft.Resources.ResourceActionSuccess

     # Resource Health events
     az eventgrid system-topic create \
       --name c9k-health-events \
       --resource-group <rg> \
       --source /subscriptions/<sub-id> \
       --topic-type Microsoft.ResourceHealth

     az eventgrid system-topic event-subscription create \
       --name c9k-health-signals \
       --system-topic-name c9k-health-events \
       --resource-group <rg> \
       --endpoint https://<your-host>:8091/webhook/eventgrid \
       --included-event-types \
         Microsoft.ResourceHealth.AvailabilityStatusChanged

  3. Events flow in real-time as mutations and signals.

Run:
  python3 sources/eventgrid_receiver.py
  python3 sources/eventgrid_receiver.py --port 8091
"""

import argparse
import json
import os
import re
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen

ENGINE = os.environ.get("C9K_ENGINE_URL", "http://localhost:8080")

# Reuse mutation classification from azure_health_source.py
PROPERTY_MUTATION_MAP = {
    "properties.extended.instanceView.powerState": "PowerStateChange",
    "properties.provisioningState": "ProvisioningStateChange",
    "managedBy": "DiskAttachDetach",
    "properties.diskState": "DiskStateChange",
    "properties.kubernetesVersion": "KubernetesUpgrade",
    "properties.agentPoolProfiles": "NodePoolChange",
    "properties.accessPolicies": "AccessPolicyChange",
    "identity": "IdentityChange",
    "sku": "SKUChange",
    "properties.networkAcls": "NetworkACLChange",
    "properties.publicNetworkAccess": "SecurityConfigChange",
    "properties.siteConfig": "AppConfigChange",
}

HEALTH_STATE_SIGNAL = {
    "Unavailable": ("Unavailable", "critical"),
    "Degraded": ("Degraded", "warning"),
    "Unknown": ("HealthUnknown", "info"),
    "Available": None,  # don't signal on recovery (could clear signals later)
}


def post_engine(path, payload):
    url = f"{ENGINE}/api/{path}"
    data = json.dumps(payload).encode()
    req = Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  ERROR: {url}: {e}", file=sys.stderr)
        return None


def classify_operation(operation: str, resource_type: str) -> str:
    """Classify ARM operation into mutation type."""
    op = operation.lower()
    if "/delete" in op:
        return "ResourceDelete"
    if "/write" in op:
        # Try to be more specific based on resource type
        rt = resource_type.lower()
        if "virtualmachine" in rt:
            return "VMConfigChange"
        if "managedcluster" in rt or "kubernetes" in rt:
            return "AKSConfigChange"
        if "storageaccount" in rt:
            return "StorageConfigChange"
        if "keyvault" in rt:
            return "KeyVaultConfigChange"
        if "database" in rt or "sql" in rt:
            return "DatabaseConfigChange"
        if "networkinterface" in rt or "virtualnetwork" in rt:
            return "NetworkConfigChange"
        return "ConfigChange"
    if "/action" in op:
        return "ResourceAction"
    return "ConfigChange"


def handle_resource_event(event: dict) -> None:
    """Handle Microsoft.Resources.* events → mutations."""
    event_type = event.get("eventType", "")
    data = event.get("data", {})
    resource_id = data.get("resourceUri", "").lower()
    operation = data.get("operationName", "")
    timestamp = event.get("eventTime", "")
    resource_type = data.get("resourceType", "")

    if not resource_id:
        return

    mutation_type = classify_operation(operation, resource_type)

    # Skip noisy power state changes
    if mutation_type == "PowerStateChange":
        return

    print(f"  MUTATION: {mutation_type} on {resource_id[-50:]}", file=sys.stderr)

    post_engine("mutations", {
        "node_id": resource_id,
        "mutation_type": mutation_type,
        "source": "eventgrid",
        "timestamp": timestamp,
        "properties": {
            "operation": operation,
            "resource_type": resource_type,
            "event_type": event_type,
        },
    })


def handle_health_event(event: dict) -> None:
    """Handle Microsoft.ResourceHealth.AvailabilityStatusChanged → signals."""
    data = event.get("data", {})
    resource_id = data.get("resourceUri", "").lower()
    current_state = data.get("availabilityStatus", "")
    previous_state = data.get("previousAvailabilityStatus", "")
    timestamp = event.get("eventTime", "")

    if not resource_id:
        return

    signal_info = HEALTH_STATE_SIGNAL.get(current_state)
    if not signal_info:
        print(f"  Health: {resource_id[-40:]} → {current_state} (no signal)", file=sys.stderr)
        return

    signal_type, severity = signal_info
    print(f"  SIGNAL: {signal_type} on {resource_id[-50:]}", file=sys.stderr)

    post_engine("signals", {
        "node_id": resource_id,
        "signal_type": signal_type,
        "severity": severity,
        "timestamp": timestamp,
        "properties": {
            "current_state": current_state,
            "previous_state": previous_state,
            "source": "eventgrid-health",
        },
    })


class EventGridHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            events = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        # Event Grid sends an array of events
        if not isinstance(events, list):
            events = [events]

        # Handle Event Grid validation handshake
        for event in events:
            if event.get("eventType") == "Microsoft.EventGrid.SubscriptionValidationEvent":
                code = event.get("data", {}).get("validationCode", "")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"validationResponse": code}).encode())
                print(f"  Event Grid validation handshake completed", file=sys.stderr)
                return

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

        # Process events in background
        threading.Thread(target=self._process, args=(events,), daemon=True).start()

    def _process(self, events):
        for event in events:
            event_type = event.get("eventType", "")
            if event_type.startswith("Microsoft.Resources."):
                handle_resource_event(event)
            elif event_type.startswith("Microsoft.ResourceHealth."):
                handle_health_event(event)
            else:
                print(f"  Ignoring: {event_type}", file=sys.stderr)

    def do_OPTIONS(self):
        """Handle CORS preflight for Event Grid."""
        self.send_response(200)
        self.send_header("WebHook-Allowed-Origin", "*")
        self.end_headers()

    def log_message(self, format, *args):
        pass


def main():
    parser = argparse.ArgumentParser(
        description="Azure Event Grid webhook receiver for real-time mutations and signals.",
    )
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("EVENTGRID_WEBHOOK_PORT", "8091")),
                        help="Port (default: 8091)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--engine", default=ENGINE)
    args = parser.parse_args()

    global ENGINE
    ENGINE = args.engine

    server = HTTPServer((args.host, args.port), EventGridHandler)
    print(f"Event Grid receiver listening on {args.host}:{args.port}", file=sys.stderr)
    print(f"  Engine: {ENGINE}", file=sys.stderr)
    print(f"  Endpoint: http://<your-host>:{args.port}/webhook/eventgrid", file=sys.stderr)
    print(f"  Events: ResourceWrite/Delete, AvailabilityStatusChanged", file=sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.", file=sys.stderr)
        server.shutdown()


if __name__ == "__main__":
    main()
