#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────
# Causinator 9000 — Policy Violation Demo
#
# This script sets up a clean demo showing how deny policies are detected
# as root causes. It has two phases:
#
# PHASE 1 (automated): Cleans the engine, loads Azure topology + policies,
#   ingests GH Actions failures. Shows the current state.
#
# PHASE 2 (manual): A volunteer tries to create a storage account with
#   public blob access enabled, which violates the StorHard-DenyPubAcc-V3
#   deny policy. The deployment fails. We re-ingest and the engine traces
#   the failure to the deny policy with ~99% confidence.
#
# Prerequisites:
#   - Engine running: make run-release
#   - az login (to the Radius Test subscription)
#   - gh auth login (for GH Actions data)
#
# Usage:
#   ./scripts/demo_policy.sh          # Run phase 1
#   ./scripts/demo_policy.sh phase2   # Run phase 2 after the volunteer acts
# ─────────────────────────────────────────────────────────────────────────

set -euo pipefail
ENGINE=${C9K_ENGINE_URL:-http://localhost:8080}
REPO=project-radius/radius
SUB=$(az account show --query id -o tsv)
RG_NAME="c9k-demo-policy-$(date +%s | tail -c 5)"
SA_NAME="c9kdemo$(date +%s | tail -c 8)"
LOCATION=eastus

phase1() {
    echo "╔══════════════════════════════════════════════════════════╗"
    echo "║  Causinator 9000 — Policy Violation Demo (Phase 1)     ║"
    echo "╚══════════════════════════════════════════════════════════╝"
    echo ""

    # Step 1: Clear engine
    echo "→ Clearing engine..."
    curl -sf -X POST "$ENGINE/api/clear" -H 'Content-Type: application/json' -d '{}' > /dev/null

    # Step 2: Load Azure topology
    echo "→ Loading Azure topology from ARG..."
    python3 sources/arg_source.py --output /tmp/c9k-demo-graph.json 2>&1 | grep -E '→|Written'
    curl -sf -X POST "$ENGINE/api/graph/load" \
        -H 'Content-Type: application/json' \
        -d @/tmp/c9k-demo-graph.json | python3 -c "import sys,json; r=json.load(sys.stdin); print(f'  Loaded: {r[\"nodes\"]} nodes, {r[\"edges\"]} edges')"

    # Step 3: Reload CPTs
    echo "→ Reloading CPTs..."
    curl -sf -X POST "$ENGINE/api/reload-cpts" | python3 -c "import sys,json; r=json.load(sys.stdin); print(f'  {r[\"classes\"]} classes')"

    # Step 4: Ingest deny policies
    echo "→ Ingesting deny policies..."
    python3 sources/azure_policy_source.py 2>&1 | grep -E '→|Ingested|Topology'

    # Step 5: Ingest Azure resource changes
    echo "→ Ingesting Azure resource changes (48h)..."
    python3 sources/azure_health_source.py --hours 48 2>&1 | grep -E '→|Ingested'

    # Step 6: Ingest GH Actions
    echo "→ Ingesting GH Actions failures..."
    python3 sources/gh_actions_source.py --repo "$REPO" --hours 48 --subscription "$SUB" 2>&1 | grep -E '→|Ingested|Topology'

    echo ""
    echo "═══ Current State ═══"
    curl -sf "$ENGINE/api/health" | python3 -c "import sys,json; h=json.load(sys.stdin); print(f'  Nodes: {h[\"nodes\"]:,}  Edges: {h[\"edges\"]:,}  Mutations: {h[\"active_mutations\"]}  Signals: {h[\"active_signals\"]}')"

    echo ""
    echo "═══ Alert Groups ═══"
    curl -sf "$ENGINE/api/alert-groups" | python3 -c "
import sys,json
groups = json.load(sys.stdin)
for g in groups:
    pct = g['confidence'] * 100
    sigs = ', '.join(g['signal_types'])
    print(f'  [{g[\"count\"]}] {g[\"root_cause\"][:60]}: {pct:.1f}% ({sigs})')
print(f'  {len(groups)} groups total')
"

    echo ""
    echo "═══ Phase 1 Complete ═══"
    echo ""
    echo "Dashboard: open $ENGINE"
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  DEMO INSTRUCTIONS FOR VOLUNTEER:"
    echo ""
    echo "  The policy 'StorHard-DenyPubAcc-V3' denies creating storage"
    echo "  accounts with public blob access enabled."
    echo ""
    echo "  Try this (it will fail):"
    echo ""
    echo "    az group create -n $RG_NAME -l $LOCATION"
    echo ""
    echo "    az storage account create \\"
    echo "      --name $SA_NAME \\"
    echo "      --resource-group $RG_NAME \\"
    echo "      --location $LOCATION \\"
    echo "      --allow-blob-public-access true"
    echo ""
    echo "  Expected error: RequestDisallowedByPolicy"
    echo ""
    echo "  After the failure, run:"
    echo "    ./scripts/demo_policy.sh phase2"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

phase2() {
    echo "╔══════════════════════════════════════════════════════════╗"
    echo "║  Causinator 9000 — Policy Violation Demo (Phase 2)     ║"
    echo "╚══════════════════════════════════════════════════════════╝"
    echo ""

    # Re-ingest to pick up new changes + policy state
    echo "→ Re-ingesting Azure resource changes..."
    python3 sources/azure_health_source.py --hours 1 2>&1 | grep -E '→|Ingested'

    echo "→ Re-ingesting deny policies..."
    python3 sources/azure_policy_source.py 2>&1 | grep -E '→|Ingested|Topology'

    # If there's a new storage account that got created despite the policy,
    # or the failed deployment left a trace in resource changes, inject
    # a manual signal for the demo
    echo ""
    echo "→ Injecting deployment failure signal..."

    # Find any recently created resource groups matching our demo pattern
    DEMO_RG=$(az group list --query "[?starts_with(name, 'c9k-demo-policy')].name" -o tsv 2>/dev/null | head -1)
    if [ -n "$DEMO_RG" ]; then
        DEMO_RG_ID=$(az group show -n "$DEMO_RG" --query id -o tsv 2>/dev/null | tr '[:upper:]' '[:lower:]')
        echo "  Found demo resource group: $DEMO_RG"

        # Merge the RG node into the graph
        curl -sf -X POST "$ENGINE/api/graph/merge" \
            -H 'Content-Type: application/json' \
            -d "{\"nodes\":[{\"id\":\"$DEMO_RG_ID\",\"label\":\"$DEMO_RG\",\"class\":\"ResourceGroup\",\"region\":\"$LOCATION\",\"rack_id\":null,\"properties\":{\"source\":\"demo\"}}],\"edges\":[]}" > /dev/null

        # The deny policy should already have an edge to non-compliant resources.
        # Inject the deployment failure signal
        curl -sf -X POST "$ENGINE/api/signals" \
            -H 'Content-Type: application/json' \
            -d "{\"node_id\":\"$DEMO_RG_ID\",\"signal_type\":\"DeploymentFailed\",\"severity\":\"critical\",\"properties\":{\"error\":\"RequestDisallowedByPolicy\",\"policy\":\"StorHard-DenyPubAcc-V3\",\"resource_type\":\"Microsoft.Storage/storageAccounts\",\"message\":\"Resource was disallowed by policy\"}}" > /dev/null

        # Also inject as a mutation (the deployment attempt)
        curl -sf -X POST "$ENGINE/api/mutations" \
            -H 'Content-Type: application/json' \
            -d "{\"node_id\":\"$DEMO_RG_ID\",\"mutation_type\":\"ResourceCreate\",\"source\":\"demo\",\"properties\":{\"resource_type\":\"Microsoft.Storage/storageAccounts\",\"requested_config\":\"allow-blob-public-access=true\"}}" > /dev/null

        echo "  ✓ Injected DeploymentFailed signal on $DEMO_RG"
    else
        echo "  No demo resource group found. Creating a simulated failure..."

        # Simulate: use an existing non-compliant storage account
        SA_ID="/subscriptions/$SUB/resourcegroups/radiusoidc/providers/microsoft.storage/storageaccounts/radiusoidc"

        curl -sf -X POST "$ENGINE/api/signals" \
            -H 'Content-Type: application/json' \
            -d "{\"node_id\":\"$SA_ID\",\"signal_type\":\"PolicyViolation\",\"severity\":\"critical\",\"properties\":{\"error\":\"RequestDisallowedByPolicy\",\"policy\":\"StorHard-DenyPubAcc-V3\",\"message\":\"Storage account has public blob access enabled\"}}" > /dev/null

        echo "  ✓ Injected PolicyViolation signal on radiusoidc storage account"
    fi

    echo ""
    echo "═══ Updated Alert Groups ═══"
    curl -sf "$ENGINE/api/alert-groups" | python3 -c "
import sys,json
groups = json.load(sys.stdin)
for g in groups:
    pct = g['confidence'] * 100
    sigs = ', '.join(g['signal_types'])
    is_policy = 'Policy' in sigs or 'Deployment' in sigs
    marker = ' ← NEW' if is_policy else ''
    print(f'  [{g[\"count\"]}] {g[\"root_cause\"][:60]}: {pct:.1f}% ({sigs}){marker}')
print(f'  {len(groups)} groups total')
"

    echo ""
    echo "═══ Phase 2 Complete ═══"
    echo "  Dashboard: open $ENGINE"
    echo ""
    echo "  Look for the PolicyViolation alert card — it should trace"
    echo "  to the 'StorHard-DenyPubAcc-V3' deny policy with high confidence."
    echo ""
    echo "  The causal path: DenyPolicy → StorageAccount → DeploymentFailed"
    echo ""

    # Cleanup hint
    if [ -n "${DEMO_RG:-}" ]; then
        echo "  Cleanup: az group delete -n $DEMO_RG --yes --no-wait"
    fi
}

case "${1:-phase1}" in
    phase1) phase1 ;;
    phase2) phase2 ;;
    *) echo "Usage: $0 [phase1|phase2]"; exit 1 ;;
esac
