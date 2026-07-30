# Backstage Plugin for Causinator 9000

A Backstage plugin that adds root-cause analysis to your developer portal.

See failed CI runs grouped by shared root cause, with confidence scores,
causal paths, and competing-cause analysis, directly on your service's
entity page.

## Packages

| Package | Description |
|---|---|
| `plugins/c9k-backend` | Backend plugin, proxies the C9K engine REST API |
| `plugins/c9k` | Frontend plugin, Diagnosis tab for entity pages |

## Installation

### 1. Backend

In your Backstage backend (`packages/backend/src/index.ts`):

```typescript
const backend = createBackend();
// ... other plugins
backend.add(import('@c9k/backstage-plugin-c9k-backend'));
backend.start();
```

### 2. Frontend

In your entity page (`packages/app/src/components/catalog/EntityPage.tsx`):

```tsx
import { EntityC9kDiagnosisTab } from '@c9k/backstage-plugin-c9k';

// Inside your EntityLayout for services:
<EntityLayout.Route path="/diagnosis" title="Diagnosis">
  <EntityC9kDiagnosisTab />
</EntityLayout.Route>
```

### 3. Configuration

In `app-config.yaml`:

```yaml
c9k:
  baseUrl: http://c9k-engine:8080   # Your C9K engine address
```

### 4. Entity Annotations

Add to your service's `catalog-info.yaml`:

```yaml
apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: my-api
  annotations:
    github.com/project-slug: myorg/my-api
    c9k/repo: myorg/my-api          # optional: defaults to project-slug
spec:
  type: service
  owner: platform-team
```

## What It Shows

### Correlated Failure Groups

When multiple CI jobs fail from the same root cause (e.g., an Azure OIDC
outage), they appear as a single group with a member count and shared
confidence score.

### Root Cause Analysis

Each failed CI job gets an individual diagnosis showing:

- **Root cause**: the commit, dependency update, or infrastructure issue
  most likely responsible
- **Confidence**: a 0 to 100% score based on Bayesian inference
- **Causal path**: the chain of nodes from cause to failure
- **Competing causes**: alternative explanations (e.g., flaky tests) with
  their own confidence scores

## Development

```bash
cd backstage-plugin
yarn install
yarn tsc
yarn build
```

## Requirements

- Backstage 1.0+
- A running C9K engine (see [main README](../README.md) for setup)
- `gh` CLI authenticated (for the engine's GitHub ingestion)
