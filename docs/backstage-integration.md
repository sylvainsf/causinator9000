# Backstage Integration Plan

## Overview

Integrate Causinator 9000 into [Backstage](https://backstage.io) as a plugin
so that development teams can see root-cause analysis for CI failures and
infrastructure incidents directly in their service pages, without leaving
the portal they already use.

Backstage's existing GitHub Actions plugin shows *what* failed. C9K answers
*why*.

## Phase 1: Backend Plugin + Diagnosis Tab

**Goal:** A Backstage plugin that adds a "Diagnosis" tab to service entity
pages, showing active root-cause analyses from C9K.

### Architecture

```
Backstage Frontend                    Backstage Backend
┌──────────────────┐                 ┌──────────────────┐
│ Entity Page      │                 │ C9K Backend      │
│ ┌──────────────┐ │   REST          │ Plugin           │
│ │ Diagnosis Tab│─┼────────────────►│                  │
│ └──────────────┘ │                 │ - /diagnose      │
│                  │                 │ - /alert-groups  │
│                  │                 │ - /health        │
└──────────────────┘                 └────────┬─────────┘
                                              │
                                              │ REST (proxied)
                                              ▼
                                     ┌──────────────────┐
                                     │ C9K Engine       │
                                     │ :8080            │
                                     └──────────────────┘
```

### Entity Annotation

Services opt in with a single annotation in their `catalog-info.yaml`:

```yaml
apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: my-api
  annotations:
    github.com/project-slug: myorg/my-api
    c9k/repo: myorg/my-api           # repo for CI failure ingestion
    c9k/node-id: service:my-api      # optional: map to a specific C9K node
spec:
  type: service
  owner: platform-team
```

If `c9k/node-id` is omitted, the plugin derives it from the repo slug.

### User Stories

#### US-1: Developer sees why their service's CI is failing

> As a developer, when I open my service's Backstage page I want to see a
> diagnosis of recent CI failures so I can tell whether a failure is my code
> or infrastructure.

**Flow:**
1. Developer navigates to their service in Backstage.
2. "Diagnosis" tab shows a summary card with root causes sorted by confidence.
3. Each root cause shows: type (code change, infra, flaky test), confidence %,
   commit or latent node, and affected jobs.
4. Developer clicks a root cause to see the causal path and competing causes.

**Acceptance criteria:**
- Tab loads within 2 seconds.
- Shows "No active incidents" when the graph has no signals for this service.
- Root causes link to the GitHub commit or workflow run.

#### US-2: Developer distinguishes flaky tests from real regressions

> As a developer, when my PR's CI fails I want to know if the failure is a
> known flake or a real regression so I don't waste time debugging infra
> problems.

**Flow:**
1. PR triggers CI. Tests fail.
2. Developer opens the service page in Backstage.
3. Diagnosis tab shows:
   - `CodeChange (commit abc123)`: 91% confidence
   - `FlakyTestRun`: 78% confidence (competing cause)
4. If the flaky-test score is higher, the card says "Likely a pre-existing
   flake, this test has failed on 3 other commits this week."
5. Developer can click through to the specific failing test and its history.

**Acceptance criteria:**
- Competing causes are always shown alongside the primary diagnosis.
- Flaky test history (how many times this test failed on other commits) is
  surfaced when available.

#### US-3: On-call engineer sees correlated failures across services

> As an on-call engineer, when multiple services fail at once I want to see
> them grouped by root cause so I can focus on the single upstream issue.

**Flow:**
1. Engineer opens the Backstage "Incidents" page (or a sidebar widget).
2. Alert groups from C9K are shown as cards:
   - "Azure OIDC outage, 5 services affected, 89% confidence"
   - "Runner image update, 3 services affected, 85% confidence"
3. Each card expands to show the member jobs/services.
4. Engineer clicks through to the C9K web UI for full graph visualization.

**Acceptance criteria:**
- Alert groups poll every 30 seconds.
- Groups with 0 active members are hidden.
- Each group links to affected services in the Backstage catalog.

#### US-4: Team lead reviews CI health trends for their services

> As a team lead, I want a dashboard showing CI failure trends and top root
> causes for my team's services over the last 7 days.

**Flow:**
1. Team lead opens their team's page in Backstage.
2. A "CI Health" card aggregates diagnoses across all services owned by the
   team.
3. Shows:
   - Failure rate trend (sparkline)
   - Top 3 root cause categories (code changes, infra, flaky tests)
   - Services with the most failures

**Acceptance criteria:**
- Aggregates across all services with the team as `spec.owner`.
- Time range is configurable (24h, 7d, 30d).

**Note:** This story requires C9K to support historical queries. Currently
the engine holds only the active graph state. A persistence layer or export
mechanism would be needed.

## Phase 2: GitHub Actions Plugin Enhancement

**Goal:** Enrich the existing Backstage GitHub Actions plugin with C9K
diagnosis data inline, so developers don't need to switch to the Diagnosis
tab for routine CI failures.

### User Stories

#### US-5: Developer sees root cause inline on a failed workflow run

> As a developer viewing a failed GitHub Actions run in Backstage, I want to
> see the likely root cause without navigating to a separate tab.

**Flow:**
1. Developer opens the GitHub Actions tab on their service page.
2. Failed runs show a small badge: "Root Cause: CodeChange (91%)" or
   "Root Cause: FlakyTest (85%)".
3. Clicking the badge expands to show the full diagnosis.

**Acceptance criteria:**
- Badge is non-intrusive, doesn't clutter the run list.
- Only appears for runs that C9K has analyzed.

#### US-6: Developer triggers on-demand analysis of a specific run

> As a developer, I want to analyze a specific failed run that C9K hasn't
> automatically ingested.

**Flow:**
1. Developer clicks "Analyze with C9K" button on a failed run.
2. Backstage backend calls C9K to ingest that run.
3. Diagnosis appears after ingestion completes (typically 5 to 15 seconds).

**Acceptance criteria:**
- Button is only shown for failed runs.
- Loading state is shown while ingestion is in progress.
- Works for any repo the user has access to.

## Phase 3: Incident Entities in Catalog

**Goal:** Surface C9K alert groups as first-class `Incident` entities in the
Backstage catalog, making them searchable and linkable.

### User Stories

#### US-7: Engineer searches for active incidents

> As an engineer, I want to search "OIDC" in Backstage and find the active
> incident caused by Azure OIDC failures.

**Flow:**
1. C9K entity provider polls `/alert_groups` and creates `kind: Incident`
   entities.
2. Incidents are indexed by Backstage search.
3. Searching "OIDC" returns the incident with its root cause, affected
   services, confidence, and timeline.

#### US-8: Service page shows related incidents

> As a developer, I want my service page to show any active incidents
> affecting it.

**Flow:**
1. Incident entities have relations to affected Component entities.
2. Service page shows a "Related Incidents" card with active incidents.
3. Card disappears when the incident is resolved (alert group has no active
   signals).

## Future: Bidirectional Catalog Sync

> **Note:** This integration pattern is highly opinionated and may not be
> appropriate for all organizations. It assumes C9K should be the
> authoritative source for infrastructure topology and that Backstage catalog
> metadata should flow into C9K's causal graph. This coupling introduces
> complexity and requires agreement on ownership boundaries between the two
> systems. We call it out here as a potential direction, not a recommendation.

### Backstage → C9K: Ownership enrichment

Import Backstage catalog entities as C9K nodes, bringing ownership metadata
(team, system, lifecycle) into the causal graph. This would let C9K diagnoses
include "owned by team X" in their output.

**Trade-offs:**
- Pro: C9K diagnoses become actionable ("page the owning team").
- Con: Creates a dependency on Backstage being the source of truth for
  ownership. Organizations using other systems (PagerDuty, ServiceNow) for
  ownership would need adapters.

### C9K → Backstage: Live topology

Export C9K's infrastructure dependency graph (from Azure Resource Graph,
Kubernetes, etc.) into Backstage as `Resource` entities with dependency
relations. This would give Backstage a live view of actual runtime
dependencies, not just what's declared in YAML.

**Trade-offs:**
- Pro: Backstage catalog graph reflects reality, not just intent.
- Con: High entity churn (pods, containers come and go). Backstage catalog
  is designed for slower-moving metadata. May need aggressive filtering to
  avoid flooding the catalog.

## Plugin Structure

The Phase 1 plugin is implemented in `backstage-plugin/`:

```
backstage-plugin/
├── README.md
├── tsconfig.json
├── plugins/
│   ├── c9k/                      # Frontend plugin
│   │   ├── package.json
│   │   ├── tsconfig.json
│   │   └── src/
│   │       ├── index.ts           # Public exports
│   │       ├── plugin.ts          # createPlugin + EntityC9kDiagnosisTab
│   │       ├── api/
│   │       │   ├── index.ts
│   │       │   ├── types.ts       # Diagnosis, AlertGroup, EngineHealth
│   │       │   └── C9kClient.ts   # API client via DiscoveryApi
│   │       └── components/
│   │           ├── DiagnosisTab/   # Main entity page tab (US-1, US-2)
│   │           ├── RootCauseCard/  # Individual diagnosis card
│   │           └── AlertGroupCard/ # Correlated failure group (US-3)
│   │
│   └── c9k-backend/              # Backend plugin
│       ├── package.json
│       ├── tsconfig.json
│       └── src/
│           ├── index.ts
│           ├── plugin.ts          # createBackendPlugin (new backend system)
│           └── router.ts          # Read-only proxy to C9K engine
```

### Implementation Decisions

- **Backend proxies read-only endpoints only.** The backend plugin exposes
  `/health`, `/diagnosis`, `/diagnosis/all`, `/alert-groups`, and
  `/neighborhood`. Write operations (ingestion, mutations) are not proxied,
  they happen via the MCP server or CLI, not through Backstage.

- **Filtering by repo annotation.** The DiagnosisTab reads the entity's
  `c9k/repo` annotation (falling back to `github.com/project-slug`) and
  filters diagnoses and alert groups to only show results matching that repo.
  This means the tab works even when the engine has data for multiple repos.

- **No CausalPath component yet.** The plan included a visual causal chain
  component. This is deferred: the RootCauseCard shows the causal path as
  text for now. A visual component with clickable nodes can be added later.

- **`react-use` for async hooks.** The DiagnosisTab uses `useAsync` from
  `react-use` for data fetching, which is the standard Backstage pattern.

## Configuration

```yaml
# app-config.yaml
c9k:
  baseUrl: http://c9k-engine:8080
  # Default repo for ingestion (can be overridden per entity)
  defaultRepo: myorg/my-service
  # Polling interval for alert groups (Phase 3)
  pollIntervalSeconds: 30
```

## Open Questions

1. **Authentication:** C9K engine currently has no auth. Should the Backstage
   backend plugin add token-based auth, or is network-level isolation (same
   cluster) sufficient?

2. **Historical data:** C9K holds only the active graph. For US-4 (trend
   dashboards), do we add a persistence layer to C9K, or export snapshots
   to a time-series store?

3. **Multi-engine:** Should the plugin support multiple C9K instances (e.g.,
   one per environment: staging, production)?

4. **Node ID convention:** How do we consistently map Backstage entity names
   to C9K node IDs? The annotation approach works but requires each service
   to opt in. An automatic convention (e.g., `service:{metadata.name}`)
   would reduce friction.
