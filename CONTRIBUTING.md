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

The solver is only as good as its Conditional Probability Tables. We ship CPTs for **30 resource classes** across 12 YAML files (see [CPT reference](docs/cpts.md)), but real-world infrastructure has hundreds of mutation→signal relationships that we haven't encoded yet.

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

### 2. Cloud Provider Source Adapters

We have comprehensive Azure support via `sources/arg_source.py` (topology), `sources/azure_health_source.py` (mutations + signals), and `sources/azure_policy_source.py` (deny policies). **We need equivalent coverage for AWS and GCP.**

**AWS support (highest priority):**

We don't have an AWS topology source yet. The equivalent of our ARG adapter would query:
- **AWS Config** — resource inventory + relationships (like ARG's Resources table)
- **AWS CloudTrail** — API-level mutations (like ARM ResourceChanges)
- **AWS Health** — degradation signals (like Azure Resource Health)
- **AWS Service Control Policies** — deny policies (like Azure Policy)

A `sources/aws_source.py` that uses `boto3` or `aws` CLI to extract EC2 instances, RDS databases, EKS clusters, Lambda functions, S3 buckets, VPCs, subnets, security groups, ALBs, and their dependency edges would immediately make the engine useful for AWS-native teams.

**What we need:**
- `sources/aws_config_source.py` — topology from AWS Config (nodes + edges)
- `sources/aws_cloudtrail_source.py` — mutations from CloudTrail events
- `sources/aws_health_source.py` — signals from AWS Health events
- CPTs for AWS resource types: EC2, RDS, EKS, Lambda, S3, ALB, NLB, Route53, CloudFront, ElastiCache, SQS, SNS, DynamoDB

**GCP support:**
- `sources/gcp_asset_source.py` — topology from Cloud Asset Inventory
- `sources/gcp_audit_source.py` — mutations from Cloud Audit Logs
- CPTs for GCP resource types: GCE, GKE, Cloud SQL, Cloud Run, GCS, Cloud Functions

**Other graph sources:**
- **Kubernetes topology discovery** — already built (`sources/k8s_source.py`), but could be expanded with service mesh topology (Istio, Linkerd)
- **Terraform graph import** — already built (`sources/terraform_source.py`), works for any provider
- **Pulumi** — state file import similar to Terraform

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
brew install rust python3   # macOS (or your platform's equivalents)
pip3 install pytest         # Python test runner

# Clone and configure
git clone https://github.com/sylvainsf/causinator9000.git
cd causinator9000
make env-init               # create .env from template

# Build
make build-release

# Run all tests (110 tests: 39 Rust + 71 Python)
make test
```

## Testing

### Test Structure

```
tests/
├── test_gh_actions_source.py      # GH Actions error classification, mutation detection
├── test_azure_health_source.py    # ARM property classification, health state mapping
└── test_merge.py                  # GraphPayload merge logic

crates/c9k-engine/
├── src/solver/mod.rs              # 27 unit tests (solver math, CPTs, diagnosis)
├── tests/golden.rs                # 6 golden scenario tests
└── ...

crates/c9k-tests/
└── src/topology.rs                # 5 topology builder tests + 1 doctest
```

### Running Tests

```bash
make test              # All tests: Rust (39) + Python (71) = 110
make test-rust         # Rust engine tests only
make test-python       # Python source adapter tests only
cargo fmt --all        # Fix formatting before submitting
```

### Test Tiers (for source adapters)

Each source adapter test file follows a 3-tier pattern:

1. **Tier 1 — Classification** (pure functions, no I/O): Test error patterns, mutation types, signal attribution. These are fast, deterministic, and the most valuable for TDD.

2. **Tier 2 — Event processing** (mocked subprocess/HTTP): Test the full processing pipeline with sample API responses. Use `unittest.mock.patch` to mock CLI commands.

3. **Tier 3 — Integration** (requires running engine): End-to-end tests that POST to the engine API. Skipped by default; run with `C9K_INTEGRATION=1 make test-python`.

### Adding Tests for a New Source Adapter

When adding a new source (e.g., `sources/aws_config_source.py`):

1. Copy `tests/test_azure_health_source.py` as `tests/test_aws_config_source.py`
2. Import your classification functions
3. Write Tier 1 tests first — these drive the design of your classification logic
4. Implement the classification functions to pass the tests
5. Add Tier 2 tests with mocked `boto3`/`aws` CLI responses
6. Add sample API responses in `tests/fixtures/` if needed

### CI Pipeline

- **Push to main**: `fmt` + `clippy` (smoke — fast feedback, no heavy tests)
- **Pull requests**: `fmt` + `clippy` + `cargo test` + `pytest` (full 110-test suite — merge gate)

All PRs must pass the full test suite before merging.

## Code Style

- **Rust:** follow standard `rustfmt` conventions. Run `cargo fmt` before submitting
- **Python:** keep source adapters simple — stdlib + `requests` only. Tests use `pytest`
- **YAML (CPTs):** include comments explaining the reasoning behind probability values
- **Makefile:** every user-facing command gets a `make` target with `## description`

## Reporting Issues

- Include the output of `curl http://localhost:8080/api/health`
- For incorrect diagnoses, include the mutations/signals you injected and the diagnosis response
- For performance issues, include the output of `python3 scripts/load_test.py`

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
