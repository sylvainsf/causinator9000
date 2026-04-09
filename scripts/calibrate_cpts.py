#!/usr/bin/env python3
"""
Causinator 9000 — Data-Driven CPT Calibration Pipeline

Learns Conditional Probability Table (CPT) values from public GitHub Actions
data instead of relying on hand-tuned estimates.

Pipeline steps:
  1. Collect CI failure data from N public repos (configurable)
  2. Classify mutations from commit metadata + changed file paths
  3. Compute empirical rates with bootstrap confidence intervals:
     - P(signal | mutation)  — hit rate
     - P(signal | no_mutation) — background rate
  4. Generate CPT YAML matching the existing heuristics format
  5. Validate spread (≥30pp) and flag low-confidence pairs
  6. Produce a calibration report

Usage:
  # Collect data and generate calibrated CPTs:
  python scripts/calibrate_cpts.py --repos repos.txt --output config/heuristics/ci-pipelines.yaml

  # Dry-run (collect + report, don't overwrite):
  python scripts/calibrate_cpts.py --repos repos.txt --dry-run

  # Use a cached dataset:
  python scripts/calibrate_cpts.py --data collected_data.json --output calibrated.yaml

Environment:
  GITHUB_TOKEN    GitHub token for API access (or use `gh auth login`)
"""

import argparse
import json
import math
import os
import random
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import yaml

# ── Constants ────────────────────────────────────────────────────────────

# Floor for background rate in LR computation — prevents division by zero
# while keeping the ratio meaningful (0.1% is below any observed signal rate).
MIN_BACKGROUND_RATE = 0.001

# CPT probability bounds — probabilities of exactly 0 or 1 break Bayesian
# inference (log-odds become infinite), so we clamp to [0.01, 0.99].
MIN_PROBABILITY = 0.01
MAX_PROBABILITY = 0.99

# ── File-path-based mutation classification ──────────────────────────────

# Patterns mapping changed file paths to mutation types.
# Order matters — first match wins.  More specific patterns before generic ones.
FILE_PATH_MUTATION_PATTERNS: list[tuple[str, str]] = [
    # CI / workflow files
    (r"\.github/workflows/", "WorkflowChange"),
    (r"\.github/actions/", "WorkflowChange"),
    (r"\.github/", "RepoConfigChange"),
    # Dependency manifests
    (r"go\.mod$|go\.sum$", "DependencyFileChange"),
    (r"package\.json$|package-lock\.json$|yarn\.lock$|pnpm-lock\.yaml$", "DependencyFileChange"),
    (r"requirements\.txt$|Pipfile|poetry\.lock$|setup\.py$|setup\.cfg$|pyproject\.toml$",
     "DependencyFileChange"),
    (r"Cargo\.toml$|Cargo\.lock$", "DependencyFileChange"),
    (r"Gemfile$|Gemfile\.lock$", "DependencyFileChange"),
    # Container / infra files
    (r"Dockerfile|docker-compose|\.dockerignore", "ContainerFileChange"),
    (r"\.bicep$|\.tf$|\.tfvars$|bicepconfig\.json", "InfraFileChange"),
    (r"Chart\.yaml$|values\.yaml$|templates/", "HelmChartChange"),
    # Documentation only
    (r"\.md$|\.rst$|\.txt$|docs/|LICENSE|CODEOWNERS", "DocsOnly"),
    # Test files
    (r"_test\.go$|test_.*\.py$|.*_test\.py$|\.test\.(ts|js|tsx|jsx)$|__tests__/",
     "TestFileChange"),
    # Source code (fallback)
    (r"\.(go|py|ts|js|rs|java|cs|cpp|c|h|rb|swift)$", "SourceCodeChange"),
]


def classify_files_mutation(changed_files: list[str]) -> str:
    """Classify mutation type from the set of changed file paths.

    Returns the most significant mutation type.  Priority:
      WorkflowChange > DependencyFileChange > ContainerFileChange
      > InfraFileChange > HelmChartChange > SourceCodeChange
      > TestFileChange > DocsOnly > RepoConfigChange
    """
    if not changed_files:
        return "Unknown"

    seen_types: set[str] = set()
    for fpath in changed_files:
        for pattern, mtype in FILE_PATH_MUTATION_PATTERNS:
            if re.search(pattern, fpath):
                seen_types.add(mtype)
                break

    if not seen_types:
        return "Unknown"

    # Priority ordering — return the highest-priority type present
    priority = [
        "WorkflowChange",
        "DependencyFileChange",
        "ContainerFileChange",
        "InfraFileChange",
        "HelmChartChange",
        "SourceCodeChange",
        "TestFileChange",
        "DocsOnly",
        "RepoConfigChange",
    ]
    for mtype in priority:
        if mtype in seen_types:
            # DocsOnly only if ALL changed files are docs
            if mtype == "DocsOnly" and seen_types != {"DocsOnly"}:
                continue
            return mtype
    return "Unknown"


# ── Data structures ──────────────────────────────────────────────────────


@dataclass
class RunRecord:
    """A single CI run observation for calibration."""
    repo: str
    run_id: int
    sha: str
    event: str
    workflow_name: str
    conclusion: str  # "failure" or "success"
    signal_type: str  # classified signal (for failures); "" for successes
    mutation_type: str  # from commit message / author
    file_mutation_type: str  # from changed file paths
    changed_files: list[str] = field(default_factory=list)


@dataclass
class RateEstimate:
    """An estimated rate with bootstrap confidence interval."""
    rate: float
    ci_lower: float
    ci_upper: float
    n_observations: int
    low_confidence: bool  # True if n < 30


@dataclass
class CptEstimate:
    """A CPT entry computed from empirical data."""
    mutation: str
    signal: str
    p_signal_given_mutation: RateEstimate
    p_signal_given_no_mutation: RateEstimate
    likelihood_ratio: float
    source: str  # "empirical" or "hand-tuned-fallback"


# ── Data collection ──────────────────────────────────────────────────────


DEFAULT_REPOS = [
    "radius-project/radius",
    "prometheus/prometheus",
]


def load_repo_list(path: str | None) -> list[str]:
    """Load a list of repos from a file (one per line) or use defaults."""
    if path and os.path.exists(path):
        repos = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    repos.append(line)
        return repos
    return DEFAULT_REPOS


def gh_run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a gh CLI command with GH_PAGER disabled."""
    env = {**os.environ, "GH_PAGER": "cat"}
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, env=env)


def collect_runs(repo: str, limit: int = 200) -> list[dict]:
    """Collect recent workflow runs (both failed and successful) from a repo.

    Returns raw run dicts from the GitHub API.
    """
    runs = []
    for status in ("failure", "success"):
        cmd = ["gh", "run", "list", "--repo", repo,
               "--limit", str(limit // 2),
               "--status", status,
               "--json", "databaseId,headSha,headBranch,event,"
                         "workflowName,conclusion,createdAt"]
        result = gh_run(cmd, timeout=60)
        if result.returncode != 0:
            print(f"  WARNING: gh run list ({status}) failed for {repo}: "
                  f"{result.stderr[:200]}", file=sys.stderr)
            continue
        try:
            runs.extend(json.loads(result.stdout))
        except json.JSONDecodeError:
            print(f"  WARNING: invalid JSON from gh run list for {repo}",
                  file=sys.stderr)
    return runs


def get_changed_files(repo: str, sha: str) -> list[str]:
    """Get the list of files changed in a commit."""
    cmd = ["gh", "api", f"repos/{repo}/commits/{sha}",
           "--jq", "[.files[].filename] | join(\"\\n\")"]
    result = gh_run(cmd, timeout=15)
    if result.returncode != 0:
        return []
    return [f for f in result.stdout.strip().split("\n") if f]


def get_commit_info(repo: str, sha: str) -> dict:
    """Get commit message and author for mutation classification."""
    cmd = ["gh", "api", f"repos/{repo}/commits/{sha}",
           "--jq", '{message: .commit.message, author: .commit.author.name}']
    result = gh_run(cmd, timeout=15)
    if result.returncode != 0:
        return {"message": "unknown", "author": "unknown"}
    try:
        info = json.loads(result.stdout)
        # Truncate to first line
        info["message"] = info.get("message", "").split("\n")[0][:120]
        return info
    except json.JSONDecodeError:
        return {"message": "unknown", "author": "unknown"}


def get_failed_step_names(repo: str, run_id: int) -> list[str]:
    """Get names of failed steps for signal classification."""
    cmd = ["gh", "api", f"repos/{repo}/actions/runs/{run_id}/jobs",
           "--jq", '.jobs[] | select(.conclusion == "failure") | '
                   '[.steps[] | select(.conclusion == "failure") | .name] | .[]']
    result = gh_run(cmd, timeout=15)
    if result.returncode != 0:
        return []
    return [s.strip() for s in result.stdout.strip().split("\n") if s.strip()]


def get_error_lines(repo: str, run_id: int) -> list[str]:
    """Get error lines from failed run logs."""
    cmd = ["gh", "run", "view", str(run_id), "--repo", repo, "--log-failed"]
    result = gh_run(cmd, timeout=60)
    if result.returncode != 0 or not result.stdout.strip():
        return []
    errors = []
    for line in result.stdout.split("\n"):
        if re.search(r"##\[error\]", line, re.IGNORECASE):
            clean = re.sub(r"^.*?##\[error\]", "", line).strip()
            if clean:
                errors.append(clean)
    return errors[:15]


# Import signal classification from the existing source adapter
_SOURCES_DIR = os.path.join(os.path.dirname(__file__), "..", "sources")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from sources.gh_actions_source import (  # noqa: E402
    classify_error,
    detect_mutation_type,
    ERROR_PATTERNS,
)


def collect_repo_data(repo: str, limit: int = 200,
                      collect_files: bool = True) -> list[RunRecord]:
    """Collect run records from a single repo.

    For each run:
      - Get commit info → mutation_type
      - Get changed files → file_mutation_type
      - For failures: get error lines / failed steps → signal_type
    """
    print(f"  Collecting data from {repo} (limit={limit})...")
    raw_runs = collect_runs(repo, limit=limit)
    if not raw_runs:
        print(f"  No runs found for {repo}", file=sys.stderr)
        return []

    # Deduplicate by run ID
    seen_ids: set[int] = set()
    unique_runs = []
    for r in raw_runs:
        rid = r.get("databaseId")
        if rid and rid not in seen_ids:
            seen_ids.add(rid)
            unique_runs.append(r)

    records: list[RunRecord] = []
    # Cache commit info per SHA to avoid redundant API calls
    commit_cache: dict[str, dict] = {}
    files_cache: dict[str, list[str]] = {}

    for run in unique_runs:
        sha = run.get("headSha", "")
        event = run.get("event", "")
        conclusion = run.get("conclusion", "")
        workflow_name = run.get("workflowName", "")
        run_id = run.get("databaseId", 0)

        # Get commit info (cached)
        if sha not in commit_cache:
            commit_cache[sha] = get_commit_info(repo, sha)
        cinfo = commit_cache[sha]

        mutation = detect_mutation_type(cinfo, event)

        # Get changed files (cached)
        changed_files: list[str] = []
        file_mutation = "Unknown"
        if collect_files:
            if sha not in files_cache:
                files_cache[sha] = get_changed_files(repo, sha)
            changed_files = files_cache[sha]
            file_mutation = classify_files_mutation(changed_files)

        # Signal classification (failures only)
        signal_type = ""
        if conclusion == "failure":
            error_lines = get_error_lines(repo, run_id)
            failed_steps = get_failed_step_names(repo, run_id)
            signal_type = classify_error(error_lines, failed_steps,
                                         workflow_name)

        records.append(RunRecord(
            repo=repo,
            run_id=run_id,
            sha=sha[:8],
            event=event,
            workflow_name=workflow_name,
            conclusion=conclusion,
            signal_type=signal_type,
            mutation_type=mutation,
            file_mutation_type=file_mutation,
            changed_files=changed_files,
        ))

    print(f"  Collected {len(records)} runs from {repo} "
          f"({sum(1 for r in records if r.conclusion == 'failure')} failures)")
    return records


# ── Rate estimation with bootstrap CIs ───────────────────────────────────


def bootstrap_rate(hits: int, total: int,
                   n_bootstrap: int = 2000,
                   ci_level: float = 0.95) -> RateEstimate:
    """Compute a rate estimate with bootstrap confidence interval.

    Args:
        hits: Number of observations where the event occurred
        total: Total number of observations
        n_bootstrap: Number of bootstrap samples
        ci_level: Confidence level (default 95%)

    Returns:
        RateEstimate with point estimate and CI bounds
    """
    if total == 0:
        return RateEstimate(rate=0.0, ci_lower=0.0, ci_upper=0.0,
                            n_observations=0, low_confidence=True)

    rate = hits / total
    low_confidence = total < 30

    if total < 5:
        # Too few observations for meaningful bootstrap
        return RateEstimate(rate=rate, ci_lower=0.0, ci_upper=1.0,
                            n_observations=total, low_confidence=True)

    # Bootstrap: resample and compute rate.
    # Seeded for reproducibility in batch calibration runs — the same input
    # data always produces the same CIs, which makes diffs meaningful.
    rng = random.Random(42)
    # Represent as binary outcomes
    outcomes = [1] * hits + [0] * (total - hits)
    boot_rates = []
    for _ in range(n_bootstrap):
        sample = rng.choices(outcomes, k=total)
        boot_rates.append(sum(sample) / total)

    boot_rates.sort()
    alpha = (1 - ci_level) / 2
    lo_idx = max(0, int(alpha * n_bootstrap))
    hi_idx = min(n_bootstrap - 1, int((1 - alpha) * n_bootstrap))

    return RateEstimate(
        rate=rate,
        ci_lower=boot_rates[lo_idx],
        ci_upper=boot_rates[hi_idx],
        n_observations=total,
        low_confidence=low_confidence,
    )


def compute_rates(records: list[RunRecord]) -> dict[str, dict[str, CptEstimate]]:
    """Compute empirical CPT rates from collected data.

    For each (mutation_type, signal_type) pair, compute:
      - P(signal | mutation): fraction of runs with this mutation that produced
        this signal
      - P(signal | no_mutation): fraction of runs WITHOUT this mutation that
        produced this signal (background rate)

    Returns:
        Dict mapping mutation_type → signal_type → CptEstimate
    """
    # Gather all observed signal types (from failures only)
    all_signals = {r.signal_type for r in records
                   if r.conclusion == "failure" and r.signal_type}
    # Gather all mutation types
    all_mutations = {r.mutation_type for r in records if r.mutation_type}

    # Also include file-based mutation types
    file_mutations = {r.file_mutation_type for r in records
                      if r.file_mutation_type and r.file_mutation_type != "Unknown"}
    all_mutations |= file_mutations

    results: dict[str, dict[str, CptEstimate]] = {}

    for mutation in sorted(all_mutations):
        results[mutation] = {}

        # Runs where this mutation was present
        mut_runs = [r for r in records if r.mutation_type == mutation
                    or r.file_mutation_type == mutation]
        # Runs where this mutation was NOT present
        no_mut_runs = [r for r in records if r.mutation_type != mutation
                       and r.file_mutation_type != mutation]

        for signal in sorted(all_signals):
            # Count: how many runs with this mutation produced this signal?
            mut_hits = sum(1 for r in mut_runs if r.signal_type == signal)
            mut_total = len(mut_runs)

            # Count: how many runs WITHOUT this mutation produced this signal?
            no_mut_hits = sum(1 for r in no_mut_runs if r.signal_type == signal)
            no_mut_total = len(no_mut_runs)

            p_sig_mut = bootstrap_rate(mut_hits, mut_total)
            p_sig_no_mut = bootstrap_rate(no_mut_hits, no_mut_total)

            # Likelihood ratio
            bg = max(p_sig_no_mut.rate, MIN_BACKGROUND_RATE)
            lr = p_sig_mut.rate / bg if p_sig_mut.rate > 0 else 0.0

            source = "empirical"
            if p_sig_mut.low_confidence or p_sig_no_mut.low_confidence:
                source = "hand-tuned-fallback"

            results[mutation][signal] = CptEstimate(
                mutation=mutation,
                signal=signal,
                p_signal_given_mutation=p_sig_mut,
                p_signal_given_no_mutation=p_sig_no_mut,
                likelihood_ratio=lr,
                source=source,
            )

    return results


# ── CPT generation ───────────────────────────────────────────────────────


# Map from calibration mutation types back to the CPT mutation names used
# in the existing heuristics YAML.
CALIBRATION_TO_CPT_MUTATION = {
    "CodeChange": "CodeChange",
    "SourceCodeChange": "CodeChange",
    "TestFileChange": "CodeChange",
    "DependencyFileChange": "DependencyUpdate",
    "WorkflowChange": "CodeChange",
    "ContainerFileChange": "CodeChange",
    "InfraFileChange": "CodeChange",
    "HelmChartChange": "CodeChange",
    "DocsOnly": "CodeChange",
    "RepoConfigChange": "CodeChange",
    "DepMajorBump": "DepMajorBump",
    "DepMinorBump": "DepMinorBump",
    "DepGroupUpdate": "DepGroupUpdate",
    "DepActionsBump": "DepActionsBump",
    "DependencyUpdate": "DependencyUpdate",
    "Release": "Release",
    "Revert": "Revert",
    "FlakyTestRun": "FlakyTestRun",
}


def load_hand_tuned_cpts(yaml_path: str) -> dict[tuple[str, str], list[list[float]]]:
    """Load existing hand-tuned CPTs as fallback values.

    Returns dict mapping (mutation, signal) → 2×2 table.
    """
    fallback: dict[tuple[str, str], list[list[float]]] = {}
    if not os.path.exists(yaml_path):
        return fallback

    with open(yaml_path) as f:
        data = yaml.safe_load(f)

    if not isinstance(data, list):
        return fallback

    for cls_def in data:
        for cpt in cls_def.get("cpts", []):
            key = (cpt["mutation"], cpt["signal"])
            fallback[key] = cpt["table"]

    return fallback


def generate_cpt_yaml(
    rates: dict[str, dict[str, CptEstimate]],
    hand_tuned_path: str,
    min_observations: int = 30,
) -> list[dict]:
    """Generate CPT YAML from empirical rates.

    For each (mutation, signal) pair:
      - If we have ≥ min_observations, use the empirical rate
      - Otherwise fall back to the hand-tuned value
      - Skip pairs with LR ≈ 1 (no causal link)

    Returns a list of class definitions matching the heuristics YAML format.
    """
    hand_tuned = load_hand_tuned_cpts(hand_tuned_path)

    # Collect CIJob CPTs
    cpts: list[dict] = []

    for mutation_cal, signals in sorted(rates.items()):
        # Map calibration mutation to CPT mutation name
        cpt_mutation = CALIBRATION_TO_CPT_MUTATION.get(mutation_cal, mutation_cal)

        for signal, est in sorted(signals.items()):
            # Skip if no signal observations at all
            if est.p_signal_given_mutation.n_observations == 0:
                continue

            key = (cpt_mutation, signal)

            # Determine if we should use empirical or fallback
            if est.source == "hand-tuned-fallback" and key in hand_tuned:
                # Use hand-tuned values
                table = hand_tuned[key]
                comment = f"hand-tuned fallback (n={est.p_signal_given_mutation.n_observations})"
            else:
                p_hit = est.p_signal_given_mutation.rate
                p_bg = est.p_signal_given_no_mutation.rate

                # Clamp to valid probability bounds
                p_hit = max(MIN_PROBABILITY, min(MAX_PROBABILITY, p_hit))
                p_bg = max(MIN_PROBABILITY, min(MAX_PROBABILITY, p_bg))

                # Skip pairs with LR < 1.5 (barely useful)
                lr = p_hit / p_bg
                if lr < 1.5 and key not in hand_tuned:
                    continue

                table = [
                    [round(p_hit, 2), round(p_bg, 2)],
                    [round(1 - p_hit, 2), round(1 - p_bg, 2)],
                ]
                lr_str = f"{lr:.1f}"
                comment = (f"LR={lr_str}× "
                           f"(n_mut={est.p_signal_given_mutation.n_observations}, "
                           f"n_bg={est.p_signal_given_no_mutation.n_observations})")

            cpts.append({
                "mutation": cpt_mutation,
                "signal": signal,
                "table": table,
                "_comment": comment,
                "_source": est.source,
            })

    # Deduplicate: if multiple calibration mutations map to the same CPT
    # mutation, keep the one with most observations
    seen: dict[tuple[str, str], dict] = {}
    for cpt in cpts:
        key = (cpt["mutation"], cpt["signal"])
        if key not in seen:
            seen[key] = cpt
        else:
            # Keep the one with more data or the empirical one
            existing = seen[key]
            if (cpt["_source"] == "empirical"
                    and existing["_source"] != "empirical"):
                seen[key] = cpt

    # Fill in any hand-tuned pairs that we didn't compute empirically
    for (mut, sig), table in hand_tuned.items():
        if (mut, sig) not in seen:
            seen[(mut, sig)] = {
                "mutation": mut,
                "signal": sig,
                "table": table,
                "_comment": "hand-tuned (no empirical data)",
                "_source": "hand-tuned-fallback",
            }

    # Build the YAML structure matching the existing format
    final_cpts = []
    for cpt in sorted(seen.values(), key=lambda c: (c["mutation"], c["signal"])):
        entry: dict[str, Any] = {
            "mutation": cpt["mutation"],
            "signal": cpt["signal"],
            "table": cpt["table"],
        }
        final_cpts.append(entry)

    return final_cpts


# ── Validation ───────────────────────────────────────────────────────────


def validate_spread(cpts: list[dict], target_spread_pp: int = 30) -> dict:
    """Validate that generated CPTs produce sufficient confidence spread.

    The spread is the difference between the highest and lowest LR-implied
    confidence across all CPT entries.

    Returns validation results dict.
    """
    if not cpts:
        return {"valid": False, "spread_pp": 0,
                "message": "No CPT entries to validate"}

    confidences = []
    for cpt in cpts:
        table = cpt["table"]
        if len(table) < 2 or len(table[0]) < 2:
            continue
        p_hit = table[0][0]
        p_bg = table[0][1]
        if p_bg > 0:
            lr = p_hit / p_bg
            # Convert LR to approximate posterior probability
            # assuming uniform prior P(mutation) = 0.5
            posterior = lr / (1 + lr)
            confidences.append(posterior * 100)

    if len(confidences) < 2:
        return {"valid": False, "spread_pp": 0,
                "message": "Too few CPT entries for spread calculation"}

    spread = max(confidences) - min(confidences)
    valid = spread >= target_spread_pp

    return {
        "valid": valid,
        "spread_pp": round(spread, 1),
        "min_confidence": round(min(confidences), 1),
        "max_confidence": round(max(confidences), 1),
        "n_entries": len(confidences),
        "message": (f"Spread {spread:.1f}pp "
                    f"({'≥' if valid else '<'} {target_spread_pp}pp target)"),
    }


# ── Calibration report ──────────────────────────────────────────────────


def generate_report(
    records: list[RunRecord],
    rates: dict[str, dict[str, CptEstimate]],
    validation: dict,
) -> str:
    """Generate a human-readable calibration report."""
    lines = [
        "# CPT Calibration Report",
        "",
        f"## Data Summary",
        f"- Total runs collected: {len(records)}",
        f"- Failed runs: {sum(1 for r in records if r.conclusion == 'failure')}",
        f"- Successful runs: {sum(1 for r in records if r.conclusion == 'success')}",
        f"- Unique repos: {len({r.repo for r in records})}",
        f"- Unique SHAs: {len({r.sha for r in records})}",
        "",
    ]

    # Per-signal background rates
    lines.append("## Per-Signal Background Rates")
    lines.append("")
    lines.append("| Signal Type | Background Rate | 95% CI | N |")
    lines.append("|-------------|----------------|--------|---|")

    all_signals = sorted({r.signal_type for r in records
                          if r.conclusion == "failure" and r.signal_type})
    total_runs = len(records)
    for signal in all_signals:
        hits = sum(1 for r in records if r.signal_type == signal)
        est = bootstrap_rate(hits, total_runs)
        flag = " ⚠️" if est.low_confidence else ""
        lines.append(
            f"| {signal} | {est.rate:.3f} | "
            f"[{est.ci_lower:.3f}, {est.ci_upper:.3f}] | {total_runs}{flag} |"
        )

    lines.extend(["", "## Per-Mutation Hit Rates", ""])
    lines.append("| Mutation | Signal | P(sig|mut) | P(sig|¬mut) | LR | N_mut | N_bg | Source |")
    lines.append("|----------|--------|------------|-------------|-----|-------|------|--------|")

    for mutation in sorted(rates.keys()):
        for signal in sorted(rates[mutation].keys()):
            est = rates[mutation][signal]
            if est.p_signal_given_mutation.rate < 0.01:
                continue  # Skip negligible rates
            lr_str = f"{est.likelihood_ratio:.1f}×"
            flag = "⚠️" if est.source == "hand-tuned-fallback" else "✓"
            lines.append(
                f"| {mutation} | {signal} | "
                f"{est.p_signal_given_mutation.rate:.3f} | "
                f"{est.p_signal_given_no_mutation.rate:.3f} | "
                f"{lr_str} | "
                f"{est.p_signal_given_mutation.n_observations} | "
                f"{est.p_signal_given_no_mutation.n_observations} | "
                f"{flag} |"
            )

    lines.extend(["", "## Validation", ""])
    lines.append(f"- Spread: {validation.get('spread_pp', 0)}pp")
    lines.append(f"- Min confidence: {validation.get('min_confidence', 0)}%")
    lines.append(f"- Max confidence: {validation.get('max_confidence', 0)}%")
    lines.append(f"- Valid: {'✓' if validation.get('valid') else '✗'} "
                 f"({validation.get('message', '')})")
    lines.append("")

    return "\n".join(lines)


# ── Full YAML output ─────────────────────────────────────────────────────


def build_full_yaml(cpts: list[dict], existing_yaml_path: str) -> list[dict]:
    """Build a complete heuristics YAML structure preserving non-CIJob classes.

    Replaces the CIJob CPTs with calibrated values while keeping Commit,
    FlakyTest, CIPlatform, and RunnerEnvironment unchanged.
    """
    existing: list[dict] = []
    if os.path.exists(existing_yaml_path):
        with open(existing_yaml_path) as f:
            existing = yaml.safe_load(f) or []

    result = []
    ci_job_replaced = False

    for cls_def in existing:
        if cls_def.get("class") == "CIJob":
            # Replace with calibrated CPTs
            result.append({
                "class": "CIJob",
                "default_prior": cls_def.get("default_prior", {
                    "P_failure": 0.02,
                    "decay_half_life_minutes": 30,
                }),
                "cpts": cpts,
            })
            ci_job_replaced = True
        else:
            result.append(cls_def)

    if not ci_job_replaced:
        # No existing CIJob — add one
        result.insert(0, {
            "class": "CIJob",
            "default_prior": {
                "P_failure": 0.02,
                "decay_half_life_minutes": 30,
            },
            "cpts": cpts,
        })

    return result


def write_yaml(data: list[dict], output_path: str) -> None:
    """Write heuristics YAML with the calibration header."""
    header = (
        "# Causinator 9000 Heuristic Layer — CI/CD\n"
        "#\n"
        "# AUTO-GENERATED by scripts/calibrate_cpts.py\n"
        "# Manual edits will be overwritten on next calibration run.\n"
        "# To override specific values, use a private overlay layer.\n"
        "#\n"
    )
    with open(output_path, "w") as f:
        f.write(header)
        yaml.dump(data, f, default_flow_style=None, sort_keys=False,
                  allow_unicode=True)
    print(f"  Wrote calibrated CPTs to {output_path}")


# ── Offline mode: load/save collected data ───────────────────────────────


def save_collected_data(records: list[RunRecord], path: str) -> None:
    """Save collected run records to JSON for offline analysis."""
    data = [asdict(r) for r in records]
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  Saved {len(records)} records to {path}")


def load_collected_data(path: str) -> list[RunRecord]:
    """Load previously collected run records from JSON."""
    with open(path) as f:
        data = json.load(f)
    records = []
    for d in data:
        records.append(RunRecord(
            repo=d["repo"],
            run_id=d["run_id"],
            sha=d["sha"],
            event=d["event"],
            workflow_name=d["workflow_name"],
            conclusion=d["conclusion"],
            signal_type=d["signal_type"],
            mutation_type=d["mutation_type"],
            file_mutation_type=d["file_mutation_type"],
            changed_files=d.get("changed_files", []),
        ))
    print(f"  Loaded {len(records)} records from {path}")
    return records


# ── Main pipeline ────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Causinator 9000 — Data-Driven CPT Calibration Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--repos", type=str, default=None,
                        help="File with repo list (one per line). "
                             "Default: radius-project/radius, prometheus/prometheus")
    parser.add_argument("--limit", type=int, default=200,
                        help="Max runs to collect per repo (default: 200)")
    parser.add_argument("--data", type=str, default=None,
                        help="Path to pre-collected data JSON (skip collection)")
    parser.add_argument("--save-data", type=str, default=None,
                        help="Save collected data to JSON for later use")
    parser.add_argument("--output", type=str, default=None,
                        help="Output path for calibrated YAML. "
                             "Default: don't write (dry-run)")
    parser.add_argument("--existing", type=str,
                        default="config/heuristics/ci-pipelines.yaml",
                        help="Path to existing hand-tuned CPTs (for fallback)")
    parser.add_argument("--report", type=str, default=None,
                        help="Write calibration report to file")
    parser.add_argument("--min-observations", type=int, default=30,
                        help="Minimum observations before using empirical rate "
                             "(default: 30)")
    parser.add_argument("--target-spread", type=int, default=30,
                        help="Target confidence spread in pp (default: 30)")
    parser.add_argument("--no-files", action="store_true",
                        help="Skip collecting changed files (faster)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Collect data and report, don't write YAML")

    args = parser.parse_args()

    print("=" * 60)
    print("Causinator 9000 — CPT Calibration Pipeline")
    print("=" * 60)

    # Step 1: Collect or load data
    if args.data:
        records = load_collected_data(args.data)
    else:
        repos = load_repo_list(args.repos)
        print(f"\nStep 1: Collecting data from {len(repos)} repos...")
        records = []
        for repo in repos:
            try:
                repo_records = collect_repo_data(
                    repo, limit=args.limit,
                    collect_files=not args.no_files)
                records.extend(repo_records)
            except Exception as e:
                print(f"  ERROR collecting from {repo}: {e}", file=sys.stderr)

    if not records:
        print("\nERROR: No data collected. Check repo access and gh auth.",
              file=sys.stderr)
        sys.exit(1)

    if args.save_data:
        save_collected_data(records, args.save_data)

    # Step 2: Compute rates
    print(f"\nStep 2: Computing empirical rates...")
    rates = compute_rates(records)
    n_pairs = sum(len(sigs) for sigs in rates.values())
    print(f"  Computed rates for {n_pairs} (mutation, signal) pairs")

    # Step 3: Generate CPTs
    print(f"\nStep 3: Generating CPT entries...")
    cpts = generate_cpt_yaml(rates, args.existing,
                             min_observations=args.min_observations)
    print(f"  Generated {len(cpts)} CPT entries")

    # Step 4: Validate
    print(f"\nStep 4: Validating spread...")
    validation = validate_spread(cpts, target_spread_pp=args.target_spread)
    print(f"  {validation['message']}")

    # Step 5: Report
    report = generate_report(records, rates, validation)
    if args.report:
        with open(args.report, "w") as f:
            f.write(report)
        print(f"\n  Calibration report written to {args.report}")
    else:
        print(f"\n{report}")

    # Step 6: Write output
    if args.output and not args.dry_run:
        full_yaml = build_full_yaml(cpts, args.existing)
        write_yaml(full_yaml, args.output)
    elif args.dry_run:
        print("\n  [dry-run] Not writing YAML output")

    # Exit code: 0 if validation passed, 1 otherwise
    if not validation.get("valid"):
        print("\n⚠️  Validation did not pass — review the report above")
        # Don't fail — the generated CPTs may still be useful
    else:
        print("\n✓  Calibration complete!")


if __name__ == "__main__":
    main()
