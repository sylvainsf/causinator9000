# Alert Rules

Alert rules control which alerts are shown, suppressed, or de-prioritized. Defined in `config/alert-rules.yaml` and loaded on engine startup.

## Rule Format

```yaml
rules:
  - signal_type: ChecklistMissing
    action: suppress
    reason: "Shown in PR UI — not an infrastructure concern"
```

## Match Fields

All fields are optional. When multiple fields are specified, ALL must match (AND logic).

| Field | Type | Description |
|-------|------|-------------|
| `signal_type` | string | Exact match on signal type |
| `class` | string | Resource class of the affected node |
| `source` | string | Source adapter that produced the signal |
| `pattern` | regex | Match against the node ID |
| `min_confidence` | float | Only match if confidence ≥ this |
| `max_confidence` | float | Only match if confidence ≤ this |

## Actions

| Action | Effect |
|--------|--------|
| `suppress` | Hidden from alert groups entirely |
| `low` | Forced into the collapsed "low confidence" section |
| `normal` | Default behavior (no override) |

Rules are evaluated top-to-bottom. **First matching rule wins.**

## Examples

```yaml
rules:
  # Suppress a project-specific non-issue
  - signal_type: ChecklistMissing
    action: suppress
    reason: "Shown in PR UI"

  # Suppress noisy WVD automation
  - source: "azure-resource-changes"
    pattern: ".*cwvdp.*"
    action: suppress
    reason: "WVD pool VMs managed by automation"

  # De-prioritize very low confidence noise
  - max_confidence: 0.05
    action: low
    reason: "Below 5% confidence"

  # De-prioritize expected maintenance signals
  - signal_type: PodEviction
    action: low
    reason: "Expected during AKS upgrades"
```

## Runtime Suppression

In addition to config rules, the UI has a dismiss button (✕) on each alert group. This calls `POST /api/alerts/suppress` which adds a runtime suppression that resets on engine restart.

The suppressed signals bar at the bottom of the alerts panel shows runtime suppressions. Click a pill to unsuppress.

## API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/alerts/suppress` | POST | `{"signal_type": "..."}` — suppress at runtime |
| `/api/alerts/unsuppress` | POST | `{"signal_type": "..."}` — remove runtime suppression |
| `/api/alerts/suppressed` | GET | List currently suppressed signal types |
