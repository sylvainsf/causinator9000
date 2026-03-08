# Inference Algorithm

The Causinator 9000 uses **likelihood-ratio Bayesian inference** to determine the probability that each recent mutation caused the observed symptoms.

## Likelihood Ratio

For each candidate (mutation, signal) pair where a CPT entry matches:

$$LR = \frac{P(\text{signal} \mid \text{mutation present})}{P(\text{signal} \mid \text{mutation absent})}$$

$$P(\text{caused} \mid \text{signal}) = \frac{\text{prior odds} \times LR}{1 + \text{prior odds} \times LR}$$

**Example:** `ImageUpdate → CrashLoopBackOff`, CPT = [0.75, 0.03]:

```
LR = 0.75 / 0.03 = 25×
Starting prior = 0.50 (uninformative), prior_odds = 1.0
Posterior odds = 1.0 × 25 = 25
Posterior = 25/26 = 96.2%
```

## Temporal Decay

The causal prior decays exponentially. Each resource class has its own `decay_half_life_minutes`:

$$\text{prior}(t) = 0.50 \times 2^{-t/\text{half\_life}}$$

| Class | Half-life | Rationale |
|-------|-----------|-----------|
| Container, ToRSwitch | 15 min | Failures manifest immediately |
| VM, LB, MessageQueue | 60 min | Moderate propagation delay |
| AKS, Gateway | 90 min | Orchestration convergence |
| SQL, MongoDB, VNet | 120 min | Storage/infra cascades |
| ManagedIdentity, AZ | 240 min | Token/cert caching delays |
| DNS, IdentityProvider | 360 min | TTL propagation |
| KeyVault, CertAuthority | 480 min | Rotation + cache expiry |
| DenyPolicy | 43200 min (30 days) | Persistent state, not event |
| CIJob, Commit | 30 min | CI feedback loops are fast |

## Upstream Propagation

When a mutation occurs upstream (e.g., CertAuthority) and signals appear downstream (e.g., TLS errors on pods), the solver traces through the DAG.

Three scoring strategies per upstream mutation:
1. **Ancestor's own CPTs** applied to the signal type
2. **Ancestor's CPTs** applied to downstream signals
3. **Target's CPTs** matched against the mutation type

Highest score wins, then attenuated by **8% per hop**:

```
CertAuthority → Gateway → AKS → Pod (3 hops)
  CA CPT: CertificateRotation → TLSError, LR = 44×
  3-hop attenuation: × 0.92³ = 0.779
  Result: ~90% confidence with full causal path
```

## Latent Node Inference

Latent nodes represent unobserved shared infrastructure:

- **ToR Switches** — shared network fabric
- **Availability Zones** — physical isolation boundaries
- **Power Domains** — electrical fault domains
- **GHCR** — container registry availability
- **GitHub Actions Infrastructure** — runner availability
- **FlakyTest** — competing cause for non-deterministic test failures
- **DenyPolicy** — Azure deny-effect policies blocking deployments

Without latent nodes, 50 simultaneous VM failures appear as 50 independent events. With a shared ToR switch, the solver recognizes a single root cause — the *explaining away* pattern.

## Competing Causes

When multiple candidate mutations could explain the same signal, the solver scores each independently and ranks them. The diagnosis shows:
- **Root cause** — highest-scoring candidate
- **Competing causes** — all candidates with scores, ranked

Example: A `TestFailure` signal has both a `CodeChange` commit (LR=24×) and a `FlakyTestRun` latent (LR=11.7×) as upstream causes. The code change wins at 92.3% vs flaky at 84.7%.
