# GitHub Action

Causinator 9000 is available as a GitHub Action that analyzes your CI failures
using Bayesian causal inference and produces a markdown report.

## How it works

1. Downloads the `c9k-engine` binary (~15MB, precompiled for Linux x86_64)
2. Ingests failure data from the GitHub Actions API using the `gh` CLI
3. Classifies each failure by error pattern (lint, test, timeout, auth, etc.)
4. Runs Bayesian inference to attribute failures to root causes (commits, flaky tests, infra)
5. Outputs a markdown report to the job summary

No Docker image, no sidecar processes, no API keys beyond your `GITHUB_TOKEN`.

## Quick start

Add this file to your repo:

```yaml
# .github/workflows/c9k-weekly.yml
name: C9K Weekly Digest
on:
  schedule:
    - cron: '0 9 * * MON'   # Every Monday at 9am UTC

permissions:
  issues: write

jobs:
  digest:
    runs-on: ubuntu-latest
    steps:
      - uses: sylvainsf/causinator9000@v1
        with:
          create-issue: 'true'
          issue-label: 'c9k-weekly'
```

That's it. Every Monday you'll get a GitHub Issue with a failure analysis
of the past week.

## Usage patterns

### Weekly digest issue (recommended)

Best for: teams that want a regular overview of CI health.

Creates (or updates) a single open issue each week. New runs append a
comment to the existing issue so you get a history.

```yaml
name: C9K Weekly Digest
on:
  schedule:
    - cron: '0 9 * * MON'

permissions:
  issues: write

jobs:
  digest:
    runs-on: ubuntu-latest
    steps:
      - uses: sylvainsf/causinator9000@v1
        with:
          create-issue: 'true'
          issue-label: 'c9k-weekly'
```

### Nightly job summary

Best for: quick daily visibility without creating issues.

The report appears on the workflow run's Summary tab in GitHub Actions.

```yaml
name: C9K Nightly
on:
  schedule:
    - cron: '0 6 * * *'

jobs:
  report:
    runs-on: ubuntu-latest
    steps:
      - uses: sylvainsf/causinator9000@v1
        with:
          hours: '24'
```

### Comment on failed PRs

Best for: giving PR authors immediate context on why CI broke.

Uses `workflow_run` to trigger after your CI workflow fails, then
posts the diagnosis as a PR comment.

```yaml
name: C9K Diagnosis
on:
  workflow_run:
    workflows: ["CI"]          # Replace with your CI workflow name
    types: [completed]

permissions:
  pull-requests: write

jobs:
  diagnose:
    if: ${{ github.event.workflow_run.conclusion == 'failure' }}
    runs-on: ubuntu-latest
    steps:
      - uses: sylvainsf/causinator9000@v1
        with:
          hours: '48'
          post-comment: 'true'
```

### Analyze a different repo

You can point C9K at any repo you have read access to:

```yaml
      - uses: sylvainsf/causinator9000@v1
        with:
          repo: 'my-org/my-other-repo'
          github-token: ${{ secrets.CROSS_REPO_TOKEN }}
```

The token needs `actions:read` on the target repo, plus `issues:write`
or `pull-requests:write` if you use `create-issue` or `post-comment`.

### Use the report in a subsequent step

The report is available as an output for further processing:

```yaml
      - uses: sylvainsf/causinator9000@v1
        id: c9k
      - run: echo "${{ steps.c9k.outputs.report }}"
```

## Inputs

| Input | Default | Description |
|---|---|---|
| `repo` | Current repo | Repository to analyze (`owner/name`) |
| `hours` | `168` | Lookback window in hours (168 = 1 week) |
| `min-confidence` | `50` | Minimum confidence threshold (0-100) |
| `post-comment` | `false` | Post diagnosis as a PR comment |
| `create-issue` | `false` | Create/update a digest issue |
| `issue-label` | `c9k-digest` | Label for digest issues |
| `github-token` | `${{ github.token }}` | Token for API access |
| `version` | `latest` | Engine version to download |

## Outputs

| Output | Description |
|---|---|
| `report` | The full markdown report |
| `alert-count` | Number of alert groups found |

## What the report contains

The report includes:

- **Alert groups**: failures clustered by shared root cause, with confidence scores
- **Diagnoses**: each failure mapped to its most likely cause (commit, dependency update, flaky test, infra)
- **Signal types**: what kind of failure was detected (LintFailure, TestFailure, Timeout, ChecklistMissing, etc.)
- **Mutation types**: what triggered the failure (CodeChange, DepMajorBump, DependencyUpdate, FlakyTestRun, etc.)

For richer analysis, paste the report into an LLM with a prompt like:
"Summarize the top issues and recommend fixes."

## How confidence scores work

C9K uses Bayesian inference with conditional probability tables (CPTs)
to score each possible root cause for a failure:

- **85-90%**: Strong attribution. The commit or dependency update very likely caused this failure.
- **50-84%**: Moderate attribution. The cause is plausible but there are competing explanations.
- **<50%**: Weak. The engine can't confidently attribute the failure.

When the same job fails on 3+ different commits, C9K automatically boosts
the flaky-test prior, making it more likely to attribute those failures to
flakiness rather than code changes.

## Troubleshooting

**No failures found**: The lookback window may be too short, or the repo
may not have had failures in that period. Try increasing `hours`.

**Low diagnosis count**: The engine may not recognize the failure patterns
in your repo. C9K ships with heuristics tuned for common CI patterns
(Go, Node.js, Python, Docker, Helm, Azure OIDC). If your failures use
unusual step names or error formats, they may fall through to the generic
`TestFailure` classification.

**Permission errors**: Ensure the `github-token` has `actions:read` on
the target repo. For `create-issue`, add `issues:write`. For
`post-comment`, add `pull-requests:write`.
