# Data Sources

Causinator 9000 ingests data from multiple sources that provide topology (nodes + edges), mutations (changes), and signals (symptoms).

## Source Adapters

| Source | Script | Mode | Provides | Auth |
|--------|--------|------|----------|------|
| Azure Resource Graph | `sources/arg_source.py` | Polling | Topology (nodes + edges) | `az login` |
| Azure Health + Changes | `sources/azure_health_source.py` | Polling | Mutations + Signals | `az login` |
| Azure Policy | `sources/azure_policy_source.py` | Polling | Latent deny nodes + Signals | `az login` |
| GitHub Actions | `sources/gh_actions_source.py` | Polling | Nodes + Mutations + Signals | `gh auth login` |
| GitHub Webhook | `sources/gh_webhook_receiver.py` | Real-time | Nodes + Mutations + Signals | Webhook secret |
| Azure Event Grid | `sources/eventgrid_receiver.py` | Real-time | Mutations + Signals | Event Grid subscription |
| Kubernetes | `sources/k8s_source.py` | Both | Topology + Mutations + Signals | `kubectl` context |
| Terraform State | `sources/terraform_source.py` | Polling | Topology (nodes + edges) | Local `.tfstate` |
| Merge Utility | `sources/merge.py` | — | Combines GraphPayloads | — |

## Makefile Targets

```bash
# Polling (batch ingestion)
make ingest-all              # ARG + health/changes + policies + GH Actions
make ingest-arg              # Azure topology only (replaces graph)
make ingest-arg-merge        # Azure topology (additive merge)
make ingest-azure-health     # Resource Health signals + Resource Changes mutations
make ingest-azure-policy     # Deny policy latent nodes
make ingest-gh               # GH Actions failures
make ingest-k8s              # K8s cluster state snapshot
make ingest-tf STATE=...     # Terraform state

# Real-time (webhook receivers)
make webhook-gh              # GitHub workflow events → :8090
make webhook-azure           # Azure Event Grid → :8091
make watch-k8s               # K8s event stream

# Dry runs
make ingest-gh-dry
make ingest-azure-health-dry
make ingest-azure-policy-dry
make ingest-k8s-dry
```

## Mutations (Changes)

Mutations represent things that changed — the potential root causes.

### From Azure Resource Changes
| Mutation Type | Source | What Changed |
|---------------|--------|-------------|
| ProvisioningStateChange | ARM | VM provisioning state |
| VMExtensionChange | ARM | Monitoring agent update |
| DiskAttachDetach | ARM | Disk attached/detached from VM |
| LoadBalancerPoolChange | ARM | LB backend pool members changed |
| ContainerConfigChange | ARM | Container instance config |
| KubernetesUpgrade | ARM | AKS version upgrade |
| NodePoolChange | ARM | AKS node pool scaling/config |
| AccessPolicyChange | ARM | Key Vault access policy |
| RedisInstanceChange | ARM | Redis port/config change |
| SKUChange | ARM | Resource SKU/tier change |
| NetworkACLChange | ARM | Network ACL rule change |
| SecurityConfigChange | ARM | TLS, public access settings |

### From GitHub Actions
| Mutation Type | Source | What Changed |
|---------------|--------|-------------|
| CodeChange | Commit | Code pushed to the repo |
| PRMerge | Commit | PR merged |
| Release | Commit | Release commit/tag |
| Revert | Commit | Code reverted |
| DepMajorBump | Commit | Dependabot major version bump |
| DepMinorBump | Commit | Dependabot minor/patch bump |
| DepGroupUpdate | Commit | Dependabot multi-package update |
| DepActionsBump | Commit | GH Actions version bump |
| FlakyTestRun | Latent | Competing cause for test failures |

### From Kubernetes
| Mutation Type | Source | What Changed |
|---------------|--------|-------------|
| ImagePull | K8s event | Container image pulled |
| ContainerCreated | K8s event | Container started |
| ScaleEvent | K8s event | Replica set scaled |
| PodCreated/Deleted | K8s event | Pod lifecycle |

### From Azure Policy
| Mutation Type | Source | What Changed |
|---------------|--------|-------------|
| PolicyEnforcement | Policy | Deny policy is enforced (persistent) |

## Signals (Symptoms)

Signals represent observed symptoms — what went wrong.

### Infrastructure Signals
| Signal Type | Source | Meaning |
|-------------|--------|---------|
| Unavailable | Azure Health | Resource marked unavailable |
| Degraded | Azure Health | Resource marked degraded |
| PolicyViolation | Azure Policy | Resource violates deny policy |
| ConnectionTimeout | Various | Network connectivity failure |
| heartbeat | Various | Heartbeat/health check lost |

### CI/CD Signals (classified from GH Actions error logs)
| Signal Type | Pattern | Attribution |
|-------------|---------|-------------|
| TestFailure | Generic `exit code != 0` | CODE |
| UnitTestFailure | `make test` / unit tests | CODE |
| BicepBuildError | Bicep template build | CODE |
| HelmChartError | Helm chart validation | CODE |
| AzureAuthFailure | `az login` / OIDC | INFRA |
| ImagePullError | Container image pull | INFRA |
| Timeout | HTTP/operation timeout | INFRA |
| DependabotUpdateFailure | Dependabot's own infra | INFRA |
| ChecklistMissing | PR checklist missing | CODE (project-specific) |

### Kubernetes Signals
| Signal Type | Source | Meaning |
|-------------|--------|---------|
| CrashLoopBackOff | K8s event | Container restarting repeatedly |
| ImagePullError | K8s event | Failed to pull image |
| OOMKilled | K8s event | Container killed by OOM |
| PodPending | K8s event | Pod stuck in Pending |
| SchedulingFailure | K8s event | No node available |
| HealthCheckFailed | K8s event | Liveness/readiness probe failing |
| VolumeMountFailure | K8s event | PV/PVC mount failed |
| PodEviction | K8s event | Pod evicted (resource pressure) |

## Adding a New Source

1. Create `sources/my_source.py` following the pattern of existing adapters
2. Output `GraphPayload` JSON to stdout (for topology) or POST to engine APIs
3. Use real event timestamps (`"timestamp": "2026-03-07T13:00:00Z"`)
4. Add a Makefile target
5. Add CPT entries for any new mutation/signal type combinations
6. Add env vars to `.env.example`

## Node ID Conventions

| Source | Format | Example |
|--------|--------|---------|
| Azure ARG | ARM resource ID (lowercase) | `/subscriptions/.../providers/microsoft.compute/virtualmachines/myvm` |
| GitHub Actions | `job://repo/run_id/job-slug` | `job://project-radius/radius/22797031763/run-functional-tests` |
| GitHub Commits | `commit://repo/sha8` | `commit://project-radius/radius/9f403647` |
| Kubernetes | `k8s://cluster/ns/kind/name` | `k8s://lrt/radius-system/pod/controller-abc123` |
| Azure Policy | `policy://assignment-slug` | `policy://storhard-denypubacc-v3` |
| Latent nodes | `latent://name` | `latent://flaky-tests` |
| Terraform | `tf://address` (fallback) | `tf://azurerm_kubernetes_cluster.main` |
