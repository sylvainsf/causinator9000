# Conditional Probability Tables (CPTs)

CPTs encode the causal relationship between mutation types and signal types for each resource class. They live in `config/heuristics/` as modular YAML layers.

## Structure

```yaml
- class: Container
  default_prior:
    P_failure: 0.002           # Per-node hourly background failure rate
    decay_half_life_minutes: 15 # How fast the causal prior decays
  cpts:
    - mutation: ImageUpdate
      signal: CrashLoopBackOff
      table:
        - [0.75, 0.03]         # [P(signal|mutation), P(signal|no mutation)]
        - [0.25, 0.97]         # [P(no signal|mutation), P(no signal|no mutation)]
```

## How to Read a CPT

The table `[0.75, 0.03]` means:

| | Mutation present | Mutation absent |
|---|---|---|
| **Signal observed** | 75% | 3% |
| **Signal not observed** | 25% | 97% |

The **likelihood ratio** is `0.75 / 0.03 = 25×`. Higher LR = stronger causal link.

## P_failure Calibration

`P_failure` is the per-instance, per-hour probability of observing a degradation signal without any mutation. Calibrated from Azure SLAs (floor) and empirical per-node data (ceiling):

| Class | P_failure | Source |
|-------|-----------|--------|
| Container (pod) | 0.002 | ~1-5% daily restart/eviction |
| VirtualMachine | 0.0008 | ~1-3% monthly unplanned reboot |
| AKSCluster | 0.0003 | Azure SLA 99.95% + per-instance |
| SqlDatabase | 0.0003 | Throttling/connection issues |
| KeyVault | 0.0002 | Throttling/RBAC issues |
| LoadBalancer | 0.0001 | Health probe flaps |
| VNet/Subnet/DNS | 0.00005 | Very stable platform resources |
| Application | 0.001 | App-level errors are most common |
| DenyPolicy | 0.0001 | Persistently enforced |

## Writing CPTs

**High-confidence (LR > 20×):**
```yaml
- mutation: FirmwareUpdate
  signal: heartbeat
  table:
    - [0.80, 0.001]    # LR = 800×
    - [0.20, 0.999]
```

**Moderate (LR 5–20×):**
```yaml
- mutation: ConfigChange
  signal: error_rate
  table:
    - [0.65, 0.04]     # LR = 16.25×
    - [0.35, 0.96]
```

**Weak (LR 2–5×):**
```yaml
- mutation: ScaleEvent
  signal: memory_rss
  table:
    - [0.40, 0.08]     # LR = 5×
    - [0.60, 0.92]
```

**Rules of thumb:**
- Each row sums to 1.0
- `P(signal|mutation)` ≥ `P(signal|no mutation)` — otherwise the mutation *prevents* the signal
- LR > 100× = near-certain (firmware crash), LR < 3× = barely useful
- Start at LR ≈ 10× and adjust from observed behavior

## Layer System

CPTs are organized as a layered manifest (`config/heuristics.manifest.yaml`):

```yaml
layers:
  # Core library
  - path: heuristics/containers.yaml
  - path: heuristics/compute.yaml
  - path: heuristics/networking.yaml
  - path: heuristics/databases.yaml
  - path: heuristics/identity.yaml
  - path: heuristics/routing.yaml
  - path: heuristics/messaging.yaml
  - path: heuristics/physical-infra.yaml
  - path: heuristics/applications.yaml
  - path: heuristics/ci-pipelines.yaml
  - path: heuristics/kubernetes.yaml
  # Project-specific overrides
  - path: heuristics/private.radius.yaml
    optional: true
```

Later layers override earlier ones with **lean patching** — only specify the fields you want to change.

## Included Classes

The engine ships with CPTs for **30 classes** across 12 YAML files:

| File | Classes |
|------|---------|
| containers.yaml | Container, ContainerRegistry, AKSCluster |
| compute.yaml | VirtualMachine |
| networking.yaml | VirtualNetwork, SubnetGateway, NetworkInterface, DNS |
| routing.yaml | LoadBalancer, Gateway, HttpRoute |
| databases.yaml | SqlDatabase, MongoDatabase, RedisCache |
| identity.yaml | ManagedIdentity, KeyVault, IdentityProvider, CertAuthority |
| messaging.yaml | MessageQueue |
| physical-infra.yaml | ToRSwitch, AvailabilityZone, PowerDomain |
| applications.yaml | Application, Environment, DenyPolicy |
| ci-pipelines.yaml | CIJob, Commit, FlakyTest, CIPlatform |
| kubernetes.yaml | KubernetesNamespace (+ Container/AKS patches) |
| private.radius.yaml | ChecklistMissing, RadiusFunctionalTestFailure |

## Adding Project-Specific CPTs

Copy `config/heuristics/private.yaml.example` and add your own:

```yaml
# config/heuristics/private.myproject.yaml
- class: CIJob
  cpts:
    - mutation: CodeChange
      signal: MyCustomTestFailure
      table:
        - [0.75, 0.03]
        - [0.25, 0.97]
```

Add it to the manifest and reload: `make reload-cpts`
