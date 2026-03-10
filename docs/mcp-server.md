# Causinator 9000 MCP Server

The C9K MCP (Model Context Protocol) server lets AI agents interact with the
Causinator 9000 causal inference engine. It can ingest CI failure data, run
diagnoses, and verify predictions — all from within an AI chat session.

## Quick Start

### Option 1: Docker (recommended)

Pull the pre-built image and configure your VS Code MCP settings:

```bash
docker pull ghcr.io/sylvainsf/causinator9000:latest
```

Add to your VS Code settings (`.vscode/mcp.json` or user settings):

```jsonc
{
  "inputs": [
    {
      "type": "promptString",
      "id": "github_token",
      "description": "GitHub Personal Access Token (repo, actions scope)",
      "password": true
    }
  ],
  "servers": {
    "c9k": {
      "type": "stdio",
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-e", "GITHUB_PERSONAL_ACCESS_TOKEN",
        "ghcr.io/sylvainsf/causinator9000:latest",
        "mcp-server"
      ],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "${input:github_token}"
      }
    }
  }
}
```

> **GitHub auth:** VS Code prompts for your token once on first start and
> stores it securely. The token needs `repo` and `actions` scopes.

### Option 2: Local (for development)

If you have the repo checked out and the engine built:

```bash
# Install the MCP SDK
pip install mcp

# Start the engine
C9K_DRASI_ENABLED=false ./target/release/c9k-engine &

# Run the MCP server (stdio transport)
python3 mcp-server/server.py
```

Configure VS Code to use the local server:

```jsonc
{
  "servers": {
    "c9k": {
      "command": "python3",
      "args": ["mcp-server/server.py"],
      "cwd": "/path/to/rcie",
      "env": {
        "C9K_ENGINE_URL": "http://127.0.0.1:8080"
      }
    }
  }
}
```

## Available Tools

### Engine Management

| Tool | Description |
|------|-------------|
| `c9k_health` | Check engine status and graph statistics |
| `c9k_clear` | Clear the entire causal graph |
| `c9k_reload_cpts` | Reload heuristic definitions from disk |

### Ingestion

| Tool | Description |
|------|-------------|
| `c9k_ingest_github` | Ingest GitHub Actions failures for a repository |

**Parameters for `c9k_ingest_github`:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `repo` | string | yes | GitHub repo in `owner/name` format |
| `hours` | integer | no | Lookback window (default: 48) |
| `subscription` | string | no | Azure subscription ID for cloud resource linking |

### Analysis

| Tool | Description |
|------|-------------|
| `c9k_diagnose_all` | Get all diagnoses sorted by confidence |
| `c9k_alert_groups` | Get correlated failure groups |
| `c9k_diagnose` | Diagnose a specific node |
| `c9k_neighborhood` | Explore a node's causal neighborhood |

### Verification

| Tool | Description |
|------|-------------|
| `c9k_verify_run` | Download actual failure logs for a GH Actions run |
| `c9k_commit_info` | Get commit details (message, author, date) |
| `c9k_compare_commits` | Check ancestry between two commits |

## Example Conversation

Once configured, you can interact with C9K through chat:

> **You:** Analyze CI failures in dapr/dapr from the last 48 hours
>
> **Agent:** *(calls `c9k_clear`, then `c9k_ingest_github` with repo="dapr/dapr", hours=48)*
>
> Found 22 failures across 12 alert groups:
> | Root Cause | Confidence | Members |
> |---|---|---|
> | latent://runner-env/linux (RunnerImageUpdate) | 90% | 4 |
> | commit://dapr/dapr/dd3d5570 (CodeChange) | 90% | 3 |
> | ...
>
> **You:** Verify the dd3d5570 group — is it actually a code problem?
>
> **Agent:** *(calls `c9k_commit_info` for dd3d5570, then `c9k_verify_run` for each run)*
>
> Commit dd3d5570: "Fix flaky TestStartInternalCallbackSpan sampling rate test"
> The unit test failures are TestCircuitBreaker_ConsecutiveFailures — a different
> test from what the commit modified. This looks like a pre-existing flake that
> the commit inherited, not caused.

## Adding to Another Repository

### As a GitHub Action (CI integration)

Add C9K as a step in your CI workflow. It runs after your tests and posts a
failure diagnosis as a **job summary** and optionally as a **PR comment**.

```yaml
# .github/workflows/ci.yml
name: CI
on: [push, pull_request]

jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run tests
        run: make test
        # Tests may fail — that's what we're analyzing

  diagnose:
    runs-on: ubuntu-latest
    needs: tests
    if: failure()  # Only run when tests fail
    steps:
      - name: Diagnose CI failures
        uses: sylvainsf/causinator9000@main
        with:
          repo: ${{ github.repository }}
          hours: 48
          min-confidence: 50
          post-comment: true
          github-token: ${{ secrets.GITHUB_TOKEN }}
```

This will:
1. Spin up the C9K engine inside the action container
2. Ingest the repo's recent GitHub Actions failures
3. Run causal inference to identify root causes
4. Post a summary to the **job summary** (visible in the Actions web UI)
5. If on a PR, post a **comment** with the diagnosis

The job summary appears as a rich markdown report directly in the GitHub
Actions web UI under the workflow run's "Summary" tab.

**Inputs:**

| Input | Default | Description |
|-------|---------|-------------|
| `repo` | current repo | Repository to analyze |
| `hours` | 48 | Lookback window |
| `min-confidence` | 50 | Minimum confidence % to report |
| `post-comment` | true | Post as PR comment |
| `github-token` | `GITHUB_TOKEN` | Token for API access |

### As an MCP server (chat integration)

To use C9K for a different project (e.g., the Radius repo):

1. **Add the MCP server configuration** to the repo's `.vscode/mcp.json`:

```jsonc
{
  "inputs": [
    {
      "type": "promptString",
      "id": "github_token",
      "description": "GitHub Personal Access Token (repo, actions scope)",
      "password": true
    }
  ],
  "servers": {
    "c9k": {
      "type": "stdio",
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-e", "GITHUB_PERSONAL_ACCESS_TOKEN",
        "ghcr.io/sylvainsf/causinator9000:latest",
        "mcp-server"
      ],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "${input:github_token}"
      }
    }
  }
}
```

3. **Optionally add project-specific heuristics.** Create a
   `config/heuristics/private.your-project.yaml` file with lean patches for
   your project's specific failure patterns. See
   `config/heuristics/private.radius.yaml` for an example.

4. **Start chatting.** Ask the agent to analyze your CI failures:

> "Ingest CI failures from project-radius/radius for the last 72 hours and
> diagnose the root causes."

## Architecture

```
┌──────────────────────────────────────────────┐
│         Docker Container                      │
│                                              │
│  ┌──────────┐     ┌─────────────────┐        │
│  │ c9k-engine│◄────│  MCP Server     │◄─stdio─┤─── VS Code / AI Agent
│  │ :8080     │     │  (Python)       │        │
│  └──────────┘     └─────────────────┘        │
│       ▲                   │                  │
│       │                   ▼                  │
│  config/           sources/*.py              │
│  heuristics/       (gh_actions_source.py)    │
│                         │                    │
│                         ▼                    │
│                    gh CLI ──► GitHub API      │
└──────────────────────────────────────────────┘
```

## GitHub Copilot Extension (chat on github.com)

The C9K Copilot Extension lets users type `@c9k diagnose dapr/dapr` directly
in GitHub Copilot Chat on github.com, in PRs, and in the IDE.

### How It Works

1. The extension runs as a hosted service (Azure Container App, Cloud Run, etc.)
2. A **webhook** receives `workflow_run` events and keeps the engine warm
3. When a user chats with `@c9k`, the **agent endpoint** queries the warm engine
4. Responses are streamed back as markdown

```
┌─────────────────────────────────────────────────────┐
│  Hosted Service                                     │
│                                                     │
│  ┌───────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │ c9k-engine│◄─│ Webhook      │  │ Agent        │ │
│  │ (always-on)│  │ POST /webhook│  │ POST /agent  │ │
│  └───────────┘  └──────────────┘  └──────────────┘ │
│       ▲              ▲                    ▲         │
└───────┼──────────────┼────────────────────┼─────────┘
        │         workflow_run          Chat from
    Fast API       webhook            github.com
```

### Setup

1. **Create a GitHub App** at https://github.com/settings/apps/new:
   - Name: `Causinator 9000`
   - Homepage: your hosted URL
   - Callback URL: `https://<your-host>/agent`
   - Webhook URL: `https://<your-host>/webhook`
   - Webhook secret: generate one
   - Permissions: `actions: read`, `checks: read`, `metadata: read`
   - Events: `workflow_run`
   - Under "Copilot": enable as a Copilot Extension, set the agent endpoint

2. **Deploy the container**:

```bash
docker run -d \
  -e GITHUB_TOKEN=ghp_... \
  -e GITHUB_WEBHOOK_SECRET=your-secret \
  -p 8090:8090 \
  ghcr.io/sylvainsf/causinator9000:latest \
  copilot-extension
```

3. **Install the app** on your organization or repository.

4. **Chat**: In any GitHub Copilot Chat, type:
   - `@c9k diagnose dapr/dapr`
   - `@c9k what's breaking in the last 24 hours?`
   - `@c9k verify run 22891725877`

### Fast Ingestion

The extension uses `--fast` mode by default (no log downloads, classifies
from step names). This makes ingestion **~20x faster** — a full 48h analysis
of a repo like dapr/dapr completes in ~5 seconds instead of ~3 minutes.

For deeper analysis, the webhook handler continuously ingests failures
as they happen, so the engine is always warm and queries are instant.

The MCP server speaks JSON-RPC over stdio. When a tool is called:

1. **Engine queries** (`c9k_health`, `c9k_diagnose_all`, etc.) go directly to the
   engine's REST API at `http://127.0.0.1:8080`.
2. **Ingestion tools** (`c9k_ingest_github`) run the corresponding Python source
   adapter as a subprocess. The adapter downloads failure data via the `gh` CLI,
   classifies errors, and posts nodes/mutations/signals to the engine API.
3. **Verification tools** (`c9k_verify_run`, `c9k_commit_info`) call the GitHub
   API via `gh` CLI to download logs and commit info.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `C9K_ENGINE_URL` | `http://127.0.0.1:8080` | Engine API URL |
| `C9K_DRASI_ENABLED` | `false` | Enable Drasi CDC engine (needs PostgreSQL) |
| `GITHUB_TOKEN` | — | GitHub token for `gh` CLI authentication |
| `C9K_APP_DIR` | auto-detected | Path to the app directory (for finding sources/) |

## Building the Image Locally

```bash
# Build for current architecture
docker build -t c9k:local .

# Test it
echo '{"jsonrpc":"2.0","method":"initialize","params":{"capabilities":{}},"id":1}' | \
  docker run -i --rm -e GITHUB_TOKEN c9k:local

# Run just the engine (no MCP)
docker run -p 8080:8080 c9k:local engine
```
