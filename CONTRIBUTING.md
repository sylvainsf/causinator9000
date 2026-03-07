# Contributing to Causinator 9000

Thanks for your interest in contributing! The Causinator 9000 is a reactive causal inference engine for cloud infrastructure root-cause analysis. We welcome contributions across several key areas.

## How to Contribute

1. Fork the repo
2. Create a feature branch (`git checkout -b my-feature`)
3. Make your changes
4. Run the tests (`cargo test && python3 scripts/demo.py`)
5. Submit a pull request

## Priority Contribution Areas

### 1. CPTs for Common Infrastructure

The solver is only as good as its Conditional Probability Tables. We ship CPTs for 22 resource classes, but real-world infrastructure has hundreds of mutation→signal relationships that we haven't encoded yet.

**What we need:**

- CPTs for cloud services we don't yet cover: RDS, DynamoDB, Cloud SQL, Elasticache, Cloud Functions, Lambda, S3/Blob/GCS, CDN, WAF, API Management, Service Bus/EventHub/SNS/SQS
- Refined probability values for existing CPTs based on real incident data
- Platform-specific variants (e.g., AKS vs. EKS vs. GKE — same concept, different failure modes)
- CPTs for common middleware: nginx, Envoy, Istio, Linkerd, Kafka, RabbitMQ, Consul, Vault

**How to contribute a CPT:**

Add entries to the appropriate layer file under `config/heuristics/` (e.g., `containers.yaml` for container resources, `databases.yaml` for data stores, `networking.yaml` for network infrastructure). The format is the same for all files:

```yaml
- class: YourResourceClass
  default_prior:
    P_failure: 0.003          # Background failure probability
  cpts:
    - mutation: MutationType   # What changed
      signal: SignalType       # What symptom appeared
      table:
        - [0.70, 0.05]        # [P(signal | mutation), P(signal | no mutation)]
        - [0.30, 0.95]
```

You can also contribute CPTs as a new layer file — just add it to `config/heuristics.manifest.yaml`. For private or org-specific overrides, copy `config/heuristics/private.yaml.example` to `config/heuristics/private.yaml` and uncomment the entry in the manifest. Override layers only need to specify the fields being changed ("lean patching").

**Guidelines:**
- The likelihood ratio (`table[0][0] / table[0][1]`) is the most important number. Start around 10× for typical cause-effect links
- Include a comment explaining your reasoning and any incident data backing the values
- Each row must sum to 1.0 across the two columns
- If you're unsure about exact values, submit what you have — a rough CPT is better than no CPT
- Add a test case to `scripts/demo.py` or `scripts/golden_tests.py` that exercises your new CPT

### 2. Graph Creation & Update

The current graph is either built from a Radius ARM template via LLM prompt or generated synthetically. We need more ways to build and maintain the causal graph.

**What we need:**

- **Cloud provider importers** — scripts or tools that query AWS (CloudFormation/CDK), GCP (Deployment Manager/Config Connector), or Azure (ARM/Bicep) and produce the `nodes`/`edges` SQL
- **Terraform graph import** — Terraform's state file contains the dependency graph; a transpiler that reads `terraform show -json` output would cover a huge number of use cases
- **Kubernetes topology discovery** — watch the K8s API for Deployments, Services, Ingress, NetworkPolicy and build the graph live
- **LLM prompt improvements** — the transpiler prompt (`prompts/transpiler.md`) can always be sharper. Better latent node inference, better edge direction reasoning, better handling of edge cases
- **Automated graph updates** — currently the graph is static. We need a mechanism to watch for topology changes (new deployments, removed resources, changed dependencies) and update the graph incrementally via the Drasi CDC pipeline
- **Graph diffing** — detect when the real infrastructure has drifted from the graph model and surface the gaps

### 3. Drasi Sources for Observability Platforms

The engine uses [drasi-lib](https://github.com/drasi-project/drasi-core) for reactive event processing. Currently we watch PostgreSQL via CDC. We need Drasi sources (or webhook receivers) for common observability and alarm platforms so signals flow into the solver automatically.

**What we need:**

- **Azure Monitor** — Action Group webhook receiver that normalizes alerts into signals rows
- **Prometheus/Alertmanager** — webhook receiver for Alertmanager notifications
- **Datadog** — webhook integration for Datadog monitors/alerts
- **PagerDuty** — webhook receiver for PD incidents
- **Grafana** — webhook integration for Grafana alerting
- **AWS CloudWatch** — SNS → webhook bridge for CloudWatch alarms
- **GCP Cloud Monitoring** — notification channel webhook receiver
- **OpsGenie / Splunk On-Call** — webhook receivers
- **Custom Drasi source plugins** — for platforms that offer streaming APIs (CloudWatch Logs, Azure Event Hubs) rather than webhooks

Each receiver should:
1. Accept the platform's native webhook format
2. Normalize it to a `signals` table INSERT with: `node_id`, `signal_type`, `value`, `severity`, `timestamp`
3. Map the platform's resource identifiers to the node IDs in the causal graph
4. Be a small, self-contained Python script (see `scripts/monitor_receiver.py` for the pattern)

Similarly, we need mutation sources for deployment platforms:
- **ArgoCD** — sync events
- **Flux** — reconciliation events
- **GitHub Actions** — deployment events
- **Azure DevOps** — release pipeline events
- **Spinnaker** — pipeline execution events

### 4. Solver Improvements

- **Learned CPTs** — use historical incident data to calibrate probability tables instead of hand-crafting
- **Adaptive temporal windows** — different resource classes should have different window sizes (cert rotation: 6 hours; config change: 15 minutes)
- **RwLock migration** — replace `Mutex<SolverState>` with `RwLock` so concurrent reads (diagnoses) don't block each other
- **Graph islanding** — partition large graphs into autonomous islands with bridge node propagation for 10m+ node scale
- **Approximate inference** — for pathological star patterns with high treewidth, implement loopy belief propagation or mini-bucket elimination

### 5. Web Dashboard

The Cytoscape.js dashboard (`web/index.html`) is functional but basic. Contributions welcome:

- Improved graph layout algorithms for large neighborhoods
- Search/filter by region, class, or alert status
- Historical timeline view of alerts
- Side-by-side diff when the graph changes
- Dark/light theme toggle

## Development Setup

```bash
# Prerequisites
brew install postgresql@17 rust python3   # macOS
# or your platform's equivalents

# Setup
export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"
createdb -p 5433 c9k_poc
psql -p 5433 c9k_poc < scripts/schema.sql
python3 scripts/transpile.py --synthetic

# Build and test
cargo test
cargo build --release

# Run engine + demo
RUST_LOG=info ./target/release/c9k-engine &
python3 scripts/demo.py
```

## Code Style

- **Rust:** follow standard `rustfmt` conventions. Run `cargo fmt` before submitting
- **Python:** scripts are utility code — keep them simple, no frameworks beyond `requests` and `FastAPI` for receivers
- **YAML (CPTs):** include comments explaining the reasoning behind probability values

## Reporting Issues

- Include the output of `curl http://localhost:8080/api/health`
- For incorrect diagnoses, include the mutations/signals you injected and the diagnosis response
- For performance issues, include the output of `python3 scripts/load_test.py`

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
