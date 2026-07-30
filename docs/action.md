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

### Auto-issue mode (per-root-cause issues, with Copilot assignment)

Best for: teams that want every detected regression to land as its own
trackable, assignable issue, with duplicates handled automatically.

What it does, in order, on each scheduled run:

1. Runs the normal report (markdown still goes to the job summary).
2. For each high-confidence alert group above the configured thresholds:
   - **Creates** a new issue for the root cause (one issue per group), or
   - **Updates** the existing c9k-managed issue if one already exists for
     the same root cause (the engine produces stable IDs so dedup is exact),
     or
   - **Reopens** a previously closed c9k issue if the same root cause
     reappears.
3. Optionally **assigns Copilot** to commit-level and broken-workflow
   issues. Flaky-test groups never get Copilot.
4. Optionally **finds and links** open issues filed by other automation
   (e.g. per-workflow failure-issue bots) that reference the same
   failing runs, and optionally closes them as duplicates.
5. Optionally **closes flaky-test groups** with an explanatory comment.
   Flakiness is a separate concern with its own (future) tooling; we
   don't want Copilot working on individual flaky failures one-by-one.
6. Optionally **auto-closes** issues whose root cause is no longer
   detected.
7. Appends the outcomes to the digest issue (when `create-issue` is also
   on), so the digest reflects what was filed/closed/reopened.

> **PR safety:** auto-issue mode never runs on `pull_request` or
> `pull_request_target` events, by design. Don't try to override that;
> filing repo-wide issues from PR runs would let any contributor's
> branch mutate global issue state.

#### Recommended first deployment (dry run)

Always run with `auto-issue-dry-run: 'true'` first. It prints exactly
what the action *would* do without creating, updating, closing, or
reopening anything. Use it to size the issue volume and tune the
thresholds before flipping it to live mode.

```yaml
name: C9K Auto-Issue (DRY RUN)
on:
  schedule:
    - cron: '0 9 * * *'
  workflow_dispatch:

permissions:
  issues: write       # required even in dry-run for the search API path
  contents: read
  actions: read

jobs:
  diagnose:
    runs-on: ubuntu-latest
    steps:
      - uses: sylvainsf/causinator9000@v1
        with:
          hours: '24'
          auto-issue: 'true'
          auto-issue-dry-run: 'true'
```

The job summary will contain a table like:

```
## Causinator 9000: Auto-Issue Outcomes (DRY RUN, no changes made)

| Action | Count |
|---|---|
| create | 3 |
| update | 1 |
| close-flaky | 5 |

| Action | Issue | Root cause | Note |
|---|---|---|---|
| create | _(planned)_ | commit://owner/repo/9b5f3778 | new issue, members=11, copilot=true |
...
```

#### Live mode (recommended config)

```yaml
name: C9K Auto-Issue
on:
  schedule:
    - cron: '0 9 * * *'

# Prevent duplicate issues from concurrent runs.
concurrency:
  group: c9k-auto-issue
  cancel-in-progress: false

permissions:
  issues: write
  contents: read
  actions: read
  pull-requests: read

jobs:
  diagnose:
    runs-on: ubuntu-latest
    steps:
      - uses: sylvainsf/causinator9000@v1
        with:
          hours: '24'
          auto-issue: 'true'
          assign-copilot: 'true'
          auto-close-flaky: 'true'
          auto-close-resolved: 'true'
          # Combine with the digest mode for a single rolling overview
          # that also lists what was filed during this run:
          create-issue: 'true'
          issue-label: 'c9k-digest'
```

#### Cross-tool deduplication

If your repo already has automation that opens an issue for every
workflow failure (radius does, for example), c9k can find those issues
and link them in the c9k root-cause issue, or close them as duplicates.
This is **off by default** because we can't tell whether the other
automation owns those issues. Enable explicitly:

```yaml
        with:
          auto-issue: 'true'
          close-cross-tool-duplicates: 'true'
```

When on, for each c9k root-cause group the action:

1. Searches open issues in the repo for any that reference any of the
   failing run URLs in the group.
2. Excludes anything already labelled `c9k-auto` (those are handled by
   the c9k dedup path).
3. Links the matches in the c9k issue body under "Related issues
   (other automation)".
4. Closes them with a comment that points back to the c9k root-cause
   issue: `Closed as a duplicate of <c9k issue URL> (Causinator 9000
   grouped this with a shared root cause).`

If you want the linking but not the closing, leave
`close-cross-tool-duplicates: 'false'` (the default), links are added
to the c9k issue body unconditionally.

#### Success criteria for resolution

Every auto-issue body includes an explicit checklist that Copilot (and
human reviewers) must satisfy before closing:

> Resolution of this issue requires that the proposed fix demonstrably
> addresses every failing run listed above, not just one or two of them.
>
> - [ ] Read the linked failing runs and confirm they share the
>       diagnosed root cause.
> - [ ] Identify the change in `<sha>` that broke the affected jobs.
> - [ ] Confirm the fix would resolve **all N** failing runs above.
> - [ ] Re-run (or simulate) each affected job and verify it passes.
> - [ ] Add or update a regression test that would have caught this
>       regression.
> - [ ] Update this issue with the fix PR link and the list of jobs
>       verified green.

This is the lever that ensures Copilot enumerates every failing run
before proposing a fix, rather than fixing the first one and calling it
done.

#### How dedup works (so you can predict it)

- Every c9k auto-issue body contains a stable HTML comment marker:
  `<!-- c9k-root-cause: commit://owner/repo/9b5f3778 -->`.
- On every run, before creating an issue for a root cause, the action
  searches existing issues with the auto-issue label for that exact
  marker. A match means update (or reopen) instead of create.
- The root-cause ID is produced by the engine from causal evidence;
  it does not depend on commit message wording, signal text, or run
  IDs, so it survives across runs even when individual failing runs
  rotate.
- For cross-tool dedup, the engine's failing-run URLs are searched
  against open issue bodies via the GitHub Search API (limited to the
  first 25 URLs per group to bound API usage on large groups).

#### Why flaky-test groups are special

Flakiness is a population-level signal: one flaky test causes many
failures across many unrelated commits. Filing one issue per flaky
*occurrence* would spam the repo, and assigning Copilot to "fix" a
single flaky run rarely produces a useful change. So:

- Flaky-test groups are **excluded** from `auto-issue-classes` by
  default. No issue is created.
- If a flaky-test issue does exist (e.g. a previous config let it
  through, or you added `FlakyTestRun` to the classes), it is
  **commented and closed** when `auto-close-flaky: 'true'`.
- Copilot is **never** assigned to a flaky-test issue, regardless of
  any other setting.

A future c9k feature will surface flakiness trends and recommended
quarantine actions in aggregate. For now, treat flakiness as a
non-actionable signal at the per-issue level.

## Inputs

| Input | Default | Description |
|---|---|---|
| `repo` | Current repo | Repository to analyze (`owner/name`) |
| `hours` | `168` | Lookback window in hours (168 = 1 week) |
| `min-confidence` | `50` | Minimum confidence threshold (0-100) |
| `post-comment` | `false` | Post diagnosis as a PR comment |
| `create-issue` | `false` | Create/update a single rolling digest issue (skipped on `pull_request` triggers) |
| `issue-label` | `c9k-digest` | Label for the digest issue |
| `github-token` | `${{ github.token }}` | Token for API access |
| `version` | `latest` | Engine version to download |
| `auto-issue` | `false` | Enable per-root-cause auto-issue mode |
| `auto-issue-min-confidence` | `90` | Confidence floor (0-100) for opening an auto-issue |
| `auto-issue-min-members` | `2` | Minimum failing jobs in a group to open an auto-issue |
| `auto-issue-classes` | `CodeChange,BrokenTestRun,DepMajorBump` | Root-cause classes to file |
| `auto-issue-label` | `c9k-auto` | Label applied to all c9k auto-issues |
| `assign-copilot` | `false` | Assign Copilot on commit/broken root-cause issues |
| `auto-close-flaky` | `true` | Comment-and-close flaky-test groups |
| `auto-close-resolved` | `false` | Close c9k issues when their root cause is no longer detected |
| `close-cross-tool-duplicates` | `false` | Close other-tool issues that match a c9k root cause |
| `auto-issue-no-branch-policy` | `false` | Disable the branch policy gate (issues are limited to default branch, release branches, and Dependabot PRs by default) |
| `auto-issue-dry-run` | `false` | Plan only; print what would happen without changes |

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

**Auto-issue mode skipped on PR**: This is intentional. Auto-issue mode
ignores `pull_request` and `pull_request_target` events to prevent
contributors from mutating repo-wide issue state from a branch. Use a
schedule, `workflow_dispatch`, or `workflow_run` trigger instead.

**Copilot not assigned**: The action attempts the assignment and falls
back to creating the issue without an assignee if the API call fails.
Causes: Copilot coding agent is not enabled for the org/repo, or the
token does not have permission to assign Copilot. Enable the agent or
grant the permission and re-run; the next pass will refresh the issue.

**Too many issues filed on first run**: Run with
`auto-issue-dry-run: 'true'` to see the full plan, then raise
`auto-issue-min-confidence` (e.g. to 95) and/or `auto-issue-min-members`
(e.g. to 3) until the volume is manageable. The defaults (90% / 2) are
conservative but a noisy repo can still produce many groups.

**Cross-tool issues are not being closed**: `close-cross-tool-duplicates`
is off by default. Even when on, the action only closes issues that
reference one of the failing run URLs in a c9k group. Issues created by
other automation that don't reference the run URLs (e.g. they only
reference the workflow name) won't be matched.

**An auto-issue keeps reappearing after I close it manually**: That's
the reopen-stale behaviour. The c9k engine still detects the same root
cause, so the next run reopens the issue. Either wait for the underlying
failures to stop, or set `auto-close-resolved: 'true'` so c9k owns the
close decision (it will only close issues whose group is no longer
detected).
