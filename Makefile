# Causinator 9000 — Makefile
#
# Prerequisites:
#   - Rust 1.75+ (rustup.rs)
#   - Python 3.11+ (for source adapters)
#   - az CLI (azure.microsoft.com/cli) + `az login`
#   - gh CLI (cli.github.com) + `gh auth login`
#
# Configuration:
#   cp .env.example .env    # then edit .env with your values
#
# Quick start:
#   make build && make run-release
#   make ingest-all
#   make open

SHELL := /bin/bash
.DEFAULT_GOAL := help

# Load .env if it exists (gitignored — local config)
-include .env
export

# ── Configuration (override via .env or command line) ────────────────────

ENGINE_URL           ?= $(or $(C9K_ENGINE_URL),http://localhost:8080)
AZURE_SUB            ?= $(or $(AZURE_SUBSCRIPTION_ID),$(shell az account show --query id -o tsv 2>/dev/null))
REPO                 ?= $(or $(firstword $(subst $(comma), ,$(GH_REPOS))),project-radius/radius)
GH_HOURS             ?= $(or $(GH_HOURS),48)
AZURE_CHANGES_HOURS  ?= $(or $(AZURE_CHANGES_HOURS),48)
GH_WEBHOOK_PORT      ?= $(or $(GH_WEBHOOK_PORT),8090)
ARG_OUTPUT           ?= /tmp/c9k-arg-graph.json
comma := ,

# ── Build ────────────────────────────────────────────────────────────────

.PHONY: build build-release test clean

build:  ## Build debug binaries
	cargo build

build-release:  ## Build optimized release binaries
	cargo build --release

test:  ## Run all tests (Rust + Python)
	cargo test
	python3 -m pytest tests/ -v

test-rust:  ## Run Rust tests only
	cargo test

test-python:  ## Run Python source adapter tests only
	python3 -m pytest tests/ -v

test-integration:  ## Run integration tests (requires running engine)
	C9K_INTEGRATION=1 python3 -m pytest tests/ -v -m "not skipif"

clean:  ## Remove build artifacts
	cargo clean
	rm -f $(ARG_OUTPUT)

# ── Run ──────────────────────────────────────────────────────────────────

.PHONY: run run-release stop restart

run:  ## Start the engine (debug build, foreground)
	cargo run --bin c9k-engine

run-release:  ## Start the engine (release build, background)
	@mkdir -p data
	nohup ./target/release/c9k-engine > data/engine.log 2>&1 &
	@sleep 2
	@curl -sf $(ENGINE_URL)/api/health | python3 -c "import sys,json; h=json.load(sys.stdin); print(f'Engine started: {h[\"nodes\"]:,} nodes')" || echo "Engine failed to start — check data/engine.log"

stop:  ## Stop the engine
	pkill -f c9k-engine 2>/dev/null || true

restart: stop build-release run-release  ## Rebuild and restart the engine

# ── Data Ingestion ───────────────────────────────────────────────────────

.PHONY: ingest-arg ingest-gh ingest-all clear

ingest-arg:  ## Extract Azure topology from ARG and load into engine (replaces graph)
	@echo "Extracting Azure resources from subscription $(AZURE_SUB)..."
	python3 sources/arg_source.py --output $(ARG_OUTPUT)
	@echo "Loading into engine..."
	curl -s -X POST $(ENGINE_URL)/api/graph/load \
		-H 'Content-Type: application/json' \
		-d @$(ARG_OUTPUT) | python3 -c "import sys,json; r=json.load(sys.stdin); print(f'Loaded: {r[\"nodes\"]} nodes, {r[\"edges\"]} edges')"

ingest-arg-merge:  ## Extract Azure topology and merge into existing graph (additive)
	@echo "Extracting Azure resources from subscription $(AZURE_SUB)..."
	python3 sources/arg_source.py --output $(ARG_OUTPUT)
	@echo "Merging into engine..."
	curl -s -X POST $(ENGINE_URL)/api/graph/merge \
		-H 'Content-Type: application/json' \
		-d @$(ARG_OUTPUT) | python3 -c "import sys,json; r=json.load(sys.stdin); print(f'Merged: +{r[\"new_nodes\"]} nodes, +{r[\"new_edges\"]} edges (total: {r[\"total_nodes\"]})')"

ingest-gh:  ## Ingest GitHub Actions failures as causal graph (REPO= GH_HOURS=)
	@echo "Ingesting failures from $(REPO) (last $(GH_HOURS)h)..."
	python3 sources/gh_actions_source.py \
		--repo $(REPO) \
		--hours $(GH_HOURS) \
		--subscription $(AZURE_SUB)

ingest-gh-dry:  ## Dry run — show what would be ingested without sending to engine
	python3 sources/gh_actions_source.py \
		--repo $(REPO) \
		--hours $(GH_HOURS) \
		--dry-run

ingest-all: ingest-arg ingest-azure-health ingest-azure-policy ingest-gh  ## Full ingestion: ARG + health/changes + policy + GH Actions

ingest-azure-health:  ## Ingest Azure Resource Health signals + Resource Changes mutations
	python3 sources/azure_health_source.py --hours $(AZURE_CHANGES_HOURS)

ingest-azure-health-dry:  ## Dry run — show Azure health signals and resource changes
	python3 sources/azure_health_source.py --hours $(AZURE_CHANGES_HOURS) --dry-run

ingest-azure-policy:  ## Ingest Azure deny-policy violations as latent causal nodes
	python3 sources/azure_policy_source.py

ingest-azure-policy-dry:  ## Dry run — show deny policy violations
	python3 sources/azure_policy_source.py --dry-run

ingest-k8s:  ## Ingest Kubernetes cluster state (pods, events, signals)
	python3 sources/k8s_source.py

ingest-k8s-dry:  ## Dry run — show K8s cluster state
	python3 sources/k8s_source.py --dry-run

clear:  ## Clear all mutations and signals (keeps graph)
	curl -s -X POST $(ENGINE_URL)/api/clear -H 'Content-Type: application/json' -d '{}' | python3 -m json.tool

reload-cpts:  ## Reload CPT heuristics from config/heuristics.manifest.yaml
	curl -s -X POST $(ENGINE_URL)/api/reload-cpts | python3 -c "import sys,json; r=json.load(sys.stdin); print(f'Reloaded: {r[\"classes\"]} classes')"

# ── Terraform ────────────────────────────────────────────────────────────

.PHONY: ingest-tf

ingest-tf:  ## Ingest Terraform state and merge into engine (STATE= or TFDIR=)
ifdef STATE
	python3 sources/terraform_source.py --state $(STATE) | \
		curl -s -X POST $(ENGINE_URL)/api/graph/merge \
			-H 'Content-Type: application/json' -d @- | python3 -m json.tool
else ifdef TFDIR
	python3 sources/terraform_source.py --pull --chdir $(TFDIR) | \
		curl -s -X POST $(ENGINE_URL)/api/graph/merge \
			-H 'Content-Type: application/json' -d @- | python3 -m json.tool
else
	@echo "Usage: make ingest-tf STATE=path/to/terraform.tfstate"
	@echo "   or: make ingest-tf TFDIR=path/to/tf/project"
endif

# ── Merge Multiple Sources ───────────────────────────────────────────────

.PHONY: ingest-merged

ingest-merged:  ## Merge ARG + Terraform into one graph and load (STATE= required)
	python3 sources/merge.py \
		<(python3 sources/arg_source.py) \
		<(python3 sources/terraform_source.py --state $(STATE)) \
		| curl -s -X POST $(ENGINE_URL)/api/graph/load \
			-H 'Content-Type: application/json' -d @- | python3 -m json.tool

# ── Diagnostics ──────────────────────────────────────────────────────────

.PHONY: status alerts islands health

status:  ## Show engine status
	@curl -sf $(ENGINE_URL)/api/health | python3 -c "import sys,json; h=json.load(sys.stdin); print(f'Status:    {h[\"status\"]}'); print(f'Nodes:     {h[\"nodes\"]:,}'); print(f'Edges:     {h[\"edges\"]:,}'); print(f'Mutations: {h[\"active_mutations\"]}'); print(f'Signals:   {h[\"active_signals\"]}')" || echo "Engine not responding at $(ENGINE_URL)"

alerts:  ## Show current alert groups
	@curl -sf $(ENGINE_URL)/api/alert-groups | python3 -c "import sys,json; groups=json.load(sys.stdin); [print(f'  [{g[\"count\"]}] {g[\"root_cause\"]}: {g[\"confidence\"]*100:.1f}%') for g in groups]; print(f'{len(groups)} alert groups')" || echo "Engine not responding"

islands:  ## Show causal islands (connected components with alerts)
	@curl -sf $(ENGINE_URL)/api/islands | python3 -c "import sys,json; islands=json.load(sys.stdin); big=[i for i in islands if i['node_count']>5 or i['alerts']>0]; [print(f'  Island {i[\"island_id\"]:4d}: {i[\"node_count\"]:5d} nodes  {i[\"representative\"][\"label\"][:40]}') for i in big[:20]]; print(f'{len(big)} significant islands out of {len(islands)}')" || echo "Engine not responding"

health:  ## Quick health check
	@curl -sf $(ENGINE_URL)/api/health > /dev/null && echo "✓ Engine OK" || echo "✗ Engine not responding"

# ── Real-time Receivers ──────────────────────────────────────────────────

.PHONY: webhook-gh webhook-azure watch-k8s

webhook-gh:  ## Start GitHub webhook receiver (real-time CI failure ingestion)
	python3 sources/gh_webhook_receiver.py --port $(GH_WEBHOOK_PORT)

webhook-azure:  ## Start Azure Event Grid receiver (real-time resource mutations/health)
	python3 sources/eventgrid_receiver.py

watch-k8s:  ## Stream K8s events in real-time from configured cluster
	python3 sources/k8s_source.py --watch

# ── Dashboard ────────────────────────────────────────────────────────────

.PHONY: open

open:  ## Open the web dashboard
	open $(ENGINE_URL)

# ── Configuration ────────────────────────────────────────────────────────

.PHONY: config env-init

config:  ## Show current configuration
	@echo "Engine:       $(ENGINE_URL)"
	@echo "Subscription: $(AZURE_SUB)"
	@echo "GH Repos:     $(REPO)"
	@echo "GH Hours:     $(GH_HOURS)"
	@echo "Azure Hours:  $(AZURE_CHANGES_HOURS)"
	@echo "Webhook Port: $(GH_WEBHOOK_PORT)"
	@echo ".env file:    $(if $(wildcard .env),✓ loaded,✗ not found — run 'make env-init')"

env-init:  ## Create .env from .env.example (if not exists)
	@if [ -f .env ]; then echo ".env already exists"; else cp .env.example .env && echo "Created .env — edit it with your values"; fi

# ── Help ─────────────────────────────────────────────────────────────────

.PHONY: help

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' Makefile | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-25s\033[0m %s\n", $$1, $$2}'
