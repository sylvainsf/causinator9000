# Backstage + C9K Demo Setup

Step-by-step instructions for running a Backstage demo app with the
Causinator 9000 plugin, using an existing todo app as the base service.

## Prerequisites

- Node.js 20+
- [C9K engine installed](../README.md) (`make install` or `cargo install`)
- `gh` CLI authenticated (`gh auth login`)
- Your todo app repository on GitHub

## 1. Create a Backstage App

```bash
npx @backstage/create-app@latest --skip-install
# When prompted:
#   App name: c9k-demo
cd c9k-demo
```

## 2. Install Dependencies

```bash
yarn install
```

## 3. Add the C9K Plugin

Copy the plugin packages into your Backstage app:

```bash
# From the causinator9000 repo
cp -r /path/to/causinator9000/backstage-plugin/plugins/c9k \
      packages/plugins/c9k

cp -r /path/to/causinator9000/backstage-plugin/plugins/c9k-backend \
      packages/plugins/c9k-backend
```

Add them to the workspace. In the root `package.json`, ensure the
`workspaces.packages` array includes:

```json
{
  "workspaces": {
    "packages": [
      "packages/*",
      "plugins/*"
    ]
  }
}
```

Install the plugin dependencies:

```bash
yarn install
```

## 4. Register the Backend Plugin

Edit `packages/backend/src/index.ts`:

```typescript
import { createBackend } from '@backstage/backend-defaults';

const backend = createBackend();

// Core plugins
backend.add(import('@backstage/plugin-catalog-backend/alpha'));
backend.add(import('@backstage/plugin-scaffolder-backend/alpha'));
backend.add(import('@backstage/plugin-techdocs-backend/alpha'));
backend.add(import('@backstage/plugin-auth-backend'));
backend.add(import('@backstage/plugin-auth-backend-module-guest-provider'));

// C9K plugin
backend.add(import('@c9k/backstage-plugin-c9k-backend'));

backend.start();
```

## 5. Add the Diagnosis Tab to Entity Pages

Edit `packages/app/src/components/catalog/EntityPage.tsx`.

Import the tab component:

```typescript
import { EntityC9kDiagnosisTab } from '@c9k/backstage-plugin-c9k';
```

Add it to the service entity layout (find the `serviceEntityPage` or
`defaultEntityPage` definition):

```tsx
const serviceEntityPage = (
  <EntityLayout>
    <EntityLayout.Route path="/" title="Overview">
      <EntityOverviewContent />
    </EntityLayout.Route>

    {/* Add the Diagnosis tab */}
    <EntityLayout.Route path="/diagnosis" title="Diagnosis">
      <EntityC9kDiagnosisTab />
    </EntityLayout.Route>

    <EntityLayout.Route path="/ci-cd" title="CI/CD">
      {cicdContent}
    </EntityLayout.Route>
  </EntityLayout>
);
```

## 6. Configure the C9K Engine URL

Edit `app-config.yaml` (or `app-config.local.yaml` for local dev):

```yaml
c9k:
  baseUrl: http://localhost:8080
```

## 7. Register Your Todo App in the Catalog

Create a `catalog-info.yaml` in your todo app's repository root:

```yaml
apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: todo-app
  description: My todo application
  annotations:
    github.com/project-slug: <your-org>/todo-app
    c9k/repo: <your-org>/todo-app
spec:
  type: service
  lifecycle: production
  owner: guests
```

Replace `<your-org>/todo-app` with your actual GitHub org and repo name.

Then register it in Backstage's catalog. Add to `app-config.yaml`:

```yaml
catalog:
  locations:
    - type: url
      target: https://github.com/<your-org>/todo-app/blob/main/catalog-info.yaml
```

Or use the "Register Existing Component" button in the Backstage UI and
paste the URL to your `catalog-info.yaml`.

## 8. Start the C9K Engine

In a separate terminal:

```bash
c9k-engine
```

Verify it's running:

```bash
curl http://localhost:8080/api/health
```

## 9. Ingest CI Failures

If your todo app has GitHub Actions, ingest its failures. You can do this
via the MCP server in VS Code, or directly via the Python source adapter:

```bash
# From the causinator9000 repo directory
python3 sources/gh_actions_source.py \
  --repo <your-org>/todo-app \
  --hours 168 \
  --fast
```

Or if you want to also demo with a larger repo that has real failures:

```bash
python3 sources/gh_actions_source.py \
  --repo project-radius/radius \
  --hours 48 \
  --fast
```

## 10. Start Backstage

```bash
cd c9k-demo
yarn dev
```

This starts both the frontend (http://localhost:3000) and backend
(http://localhost:7007).

## Demo Walkthrough

### Showing the Diagnosis Tab

1. Open http://localhost:3000 in your browser.
2. Navigate to the **Catalog** and find your todo app (or the Radius
   component if you registered it).
3. Click on the service to open its entity page.
4. Click the **Diagnosis** tab.
5. You should see:
   - **Correlated Failure Groups**: failures sharing a common root cause
     grouped into cards with member counts.
   - **Root Cause Analysis**: individual diagnoses sorted by confidence,
     each showing the root cause type, confidence bar, causal path, and
     competing causes.

### Showing Flaky vs Real Failures

1. Find a diagnosis where a `CodeChange` and `FlakyTestRun` are competing
   causes.
2. Point out the confidence scores, if `FlakyTestRun` is close to or
   higher than `CodeChange`, the failure is likely a pre-existing flake.
3. Show the causal path: `commit → job` for code changes vs
   `latent://flaky-tests → job` for flakes.

### Showing Correlated Failures

1. Ingest a repo with infrastructure failures (Azure OIDC, GHCR outages).
2. The Alert Groups section will show multiple jobs grouped under a single
   root cause.
3. Expand a group to see all affected jobs.

### Live Ingestion Demo

1. Keep Backstage open on the Diagnosis tab.
2. In another terminal, ingest fresh failures:
   ```bash
   python3 sources/gh_actions_source.py \
     --repo <your-org>/todo-app --hours 1 --fast
   ```
3. Refresh the Diagnosis tab, new failures appear with root-cause
   analysis already computed.

## Troubleshooting

### "C9K engine unreachable" error

The backend plugin can't reach the engine at the configured `c9k.baseUrl`.
Check that `c9k-engine` is running and the URL in `app-config.yaml` is
correct.

### Diagnosis tab shows "No active incidents"

The engine has no data for the repo in the entity's `c9k/repo` annotation.
Run the ingestion step (step 9) to populate the graph.

### Entity not appearing in catalog

Make sure the `catalog-info.yaml` is committed and pushed to your repo,
and that the URL is correct in `app-config.yaml` under
`catalog.locations`.
