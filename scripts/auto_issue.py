#!/usr/bin/env python3
"""
Causinator 9000 — Auto-Issue helper.

Consumes the JSON report emitted by `c9k-engine report --format json` and
manages GitHub issues for the alert groups it contains. Designed to be invoked
from the C9K GitHub Action when `auto-issue: true`.

Behaviour summary:

  * One issue per *root cause* (alert group). The c9k engine already collapses
    failures with the same root cause into one group, so dedup across runs of
    *this action* is a stable-key lookup by `<!-- c9k-root-cause: ... -->`.

  * Cross-tool dedup: searches existing OPEN issues in the repo for any that
    reference any of the failing run URLs in the group. When matches are found
    they are linked from the c9k issue. By default we *do not* close them
    (other automation may own them); set --close-cross-tool-duplicates to
    actively close them with a "Duplicate of #N" comment.

  * Copilot assignment: high-confidence `commit://` and `broken://` groups get
    Copilot added as an assignee. `latent://flaky-tests` groups never get
    Copilot — flakiness is owned by a separate (yet-to-be-built) flaky-trend
    feature; for now, flaky issues are commented and closed automatically.

  * Stale groups (issues whose root cause no longer appears in the latest
    report) are left alone here; the action's scheduled runs will refresh
    matching ones with `Last seen` data. If a previously closed issue's group
    reappears, we REOPEN it with a fresh comment.

  * Dry-run mode prints exactly what it would do (create / update / close /
    reopen / assign) without performing any mutating gh calls.

Usage:
    auto_issue.py --report report.json --repo owner/name [options]

Required:
    --report PATH        Path to JSON report from `c9k-engine report --format json`
    --repo OWNER/NAME    Target repository (where issues are created)

Filters (which alert groups warrant an issue):
    --min-confidence N        Confidence floor 0-100 (default 90)
    --min-members N           Minimum failing jobs in a group (default 2)
    --classes LIST            Comma-separated root-cause classes to file
                              (default "CodeChange,BrokenTestRun,DepMajorBump,DepGroupUpdate")

Behaviour switches:
    --label LABEL                       Label applied to all auto-issues
                                        (default "c9k-auto")
    --assign-copilot                    Add Copilot as assignee on commit/broken issues
    --auto-close-flaky                  Comment-and-close flaky-test issues
    --auto-close-resolved               Close issues whose group is no longer present
    --close-cross-tool-duplicates       Close other-tool issues that match a c9k group
    --reopen-stale                      Reopen previously closed c9k issues if the
                                        group reappears (default on)

Diagnostics:
    --dry-run                Don't make any mutating API calls; print plan
    --output-summary PATH    Write a markdown summary of actions taken to PATH
                             (used by the digest mode to surface outcomes)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

# ── Constants ────────────────────────────────────────────────────────────

ROOT_CAUSE_MARKER = "c9k-root-cause"
CLASS_MARKER = "c9k-class"
CONFIDENCE_MARKER = "c9k-confidence"
MANAGED_BLOCK_START = "<!-- c9k-managed:start -->"
MANAGED_BLOCK_END = "<!-- c9k-managed:end -->"

# Root cause classes we treat as "Copilot can fix this".
COPILOT_CLASSES = {"CodeChange", "BrokenTestRun", "DepMajorBump", "DepGroupUpdate"}

# Root cause classes that should be auto-closed (flakiness etc.).
FLAKY_CLASSES = {"FlakyTestRun"}


# ── Data types ───────────────────────────────────────────────────────────


@dataclass
class Action:
    """One action taken (or planned) against an issue. Used for the summary."""

    kind: str  # "create" | "update" | "close-flaky" | "close-resolved" | "reopen" | "close-cross-tool" | "skip"
    root_cause_id: str
    root_cause_class: str = ""
    confidence_pct: int = 0
    member_count: int = 0
    cause_human: str = ""
    first_seen: str = ""
    last_seen: str = ""
    issue_number: int | None = None
    issue_url: str | None = None
    title: str | None = None
    note: str = ""


@dataclass
class FilterStats:
    """Counts of alert groups excluded by each filter."""

    below_min_confidence: int = 0
    below_min_members: int = 0
    class_not_in_allow_list: int = 0


@dataclass
class Plan:
    actions: list[Action] = field(default_factory=list)
    filter_stats: FilterStats = field(default_factory=FilterStats)

    def add(self, a: Action) -> None:
        self.actions.append(a)


# ── gh CLI wrappers ──────────────────────────────────────────────────────


def gh(*args: str, input_text: str | None = None, check: bool = True) -> str:
    """Run gh and return stdout. Raises on non-zero unless check=False."""
    proc = subprocess.run(
        ["gh", *args],
        input=input_text,
        capture_output=True,
        text=True,
        env={**os.environ, "GH_PAGER": "cat"},
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def gh_json(*args: str) -> Any:
    out = gh(*args)
    return json.loads(out) if out.strip() else None


# ── Issue search / dedup helpers ─────────────────────────────────────────


def find_existing_c9k_issue(repo: str, label: str, root_cause_id: str) -> dict | None:
    """Find an open OR closed c9k-managed issue for this root cause.

    The search is by label (cheap) followed by a body marker check.
    Returns the first match or None.
    """
    # Search both open and closed; we need closed to support reopen-stale.
    issues = gh_json(
        "issue",
        "list",
        "--repo",
        repo,
        "--label",
        label,
        "--state",
        "all",
        "--limit",
        "200",
        "--json",
        "number,title,state,body,url,assignees",
    )
    if not issues:
        return None
    marker = f"<!-- {ROOT_CAUSE_MARKER}: {root_cause_id} -->"
    for issue in issues:
        body = issue.get("body") or ""
        if marker in body:
            return issue
    return None


def find_cross_tool_duplicates(
    repo: str, our_label: str, run_urls: list[str]
) -> list[dict]:
    """Find OPEN issues NOT owned by us that reference any of the failing run URLs.

    These are typically created by per-workflow failure-issue automations
    (e.g. radius's existing 'open issue on workflow failure' bot).
    Used for linking and (optionally) closing them to reduce duplication.

    Strategy: GitHub's search API supports the run URL string. We do one
    `gh search issues` call per URL and union the results. URLs are unique
    per run so this is bounded by group size.
    """
    found: dict[int, dict] = {}
    for url in run_urls[:25]:  # cap to avoid API hammering on huge groups
        try:
            results = gh_json(
                "search",
                "issues",
                f'"{url}"',
                "--repo",
                repo,
                "--state",
                "open",
                "--json",
                "number,title,url,labels",
                "--limit",
                "20",
            )
        except RuntimeError:
            continue
        for issue in results or []:
            num = issue["number"]
            if num in found:
                continue
            labels = {lbl.get("name") for lbl in issue.get("labels", [])}
            if our_label in labels:
                # That's one of ours — handled by the c9k dedup path.
                continue
            found[num] = issue
    return list(found.values())


# ── Issue body construction ──────────────────────────────────────────────


def build_issue_body(
    repo: str,
    group: dict,
    cross_tool_dupes: list[dict],
    last_run_label: str,
) -> tuple[str, str]:
    """Return (title, body) for an issue representing this alert group."""
    rc_id = group["root_cause_id"]
    rc_class = group.get("root_cause_class") or "Unknown"
    confidence_pct = int(round(group["confidence"] * 100))
    member_count = group["member_count"]
    branch = group.get("branch")
    pr = group.get("pr") or {}

    short_id = _short_id(rc_id)

    if rc_class == "FlakyTestRun":
        title = f"[c9k:{short_id}] Flaky test pattern ({member_count} runs affected)"
    elif rc_class == "BrokenTestRun":
        title = f"[c9k:{short_id}] Broken workflow: {short_id} ({member_count} consecutive failures)"
    elif rc_class == "DepMajorBump":
        title = f"[c9k:{short_id}] Dependency major bump regression ({member_count} failed jobs)"
    elif rc_class == "DepGroupUpdate":
        title = f"[c9k:{short_id}] Grouped dependency bump regression ({member_count} failed jobs)"
    else:
        title = f"[c9k:{short_id}] CI regression in commit {short_id} ({member_count} failed jobs)"

    parts: list[str] = []
    parts.append(f"<!-- {ROOT_CAUSE_MARKER}: {rc_id} -->")
    parts.append(f"<!-- {CLASS_MARKER}: {rc_class} -->")
    parts.append(f"<!-- {CONFIDENCE_MARKER}: {confidence_pct} -->")
    parts.append("")
    parts.append("## Root cause")
    parts.append(f"**{_format_rc_for_humans(rc_id, repo)}** — {rc_class}, {confidence_pct}% confidence")

    commit = group.get("commit") or {}
    if commit:
        msg = (commit.get("message") or "").splitlines()[0] if commit.get("message") else ""
        author = commit.get("author") or "unknown"
        parts.append(f"_Author:_ `{author}` — _Subject:_ {msg!r}")
        files = commit.get("files") or []
        if files:
            parts.append("")
            parts.append("Changed files (top 10):")
            for f in files[:10]:
                parts.append(f"- `{f}`")

    if branch:
        if pr:
            parts.append(f"_Branch:_ `{branch}` — PR [#{pr.get('number')}]({pr.get('url')})")
        else:
            parts.append(f"_Branch:_ `{branch}`")

    parts.append("")
    parts.append(f"## Affected runs ({member_count})")
    for m in group["members"]:
        url = m.get("url")
        job = m.get("job") or m.get("node_id")
        run_id = m.get("run_id") or "?"
        sigs = ", ".join(m.get("signal_types") or [])
        if url:
            parts.append(f"- [`{run_id}`/{job}]({url}) — {sigs}")
        else:
            parts.append(f"- {job} — {sigs}")

    parts.append("")
    parts.append("## Insight from c9k")
    sig_summary = ", ".join(group.get("signal_types") or []) or "(no signals)"
    parts.append(f"- Signal types observed: {sig_summary}")
    parts.append(
        "- See the full Causinator 9000 report at the top of the workflow run that filed this issue."
    )

    if cross_tool_dupes:
        parts.append("")
        parts.append("## Related issues (other automation)")
        parts.append(
            "These open issues reference one or more of the failing runs above and "
            "may be duplicates of this root cause. Close them once this is fixed:"
        )
        for d in cross_tool_dupes:
            parts.append(f"- #{d['number']} — {d['title']}")

    parts.append("")
    parts.append("---")
    parts.append("## Success criteria")
    parts.append("")
    parts.append(
        "Resolution of this issue requires that **the proposed fix demonstrably addresses every failing run listed above**, not just one or two of them."
    )
    parts.append("")
    parts.append("- [ ] Read the linked failing runs and confirm they share the diagnosed root cause.")
    parts.append(f"- [ ] Identify the change in `{short_id}` that broke the affected jobs.")
    parts.append(f"- [ ] Confirm the fix would resolve **all {member_count}** failing runs above.")
    parts.append("- [ ] Re-run (or simulate) each affected job and verify it passes.")
    parts.append("- [ ] Add or update a regression test that would have caught this regression.")
    parts.append("- [ ] Update this issue with the fix PR link and the list of jobs verified green.")
    parts.append("")
    parts.append(
        "@Copilot please enumerate every linked failing run before proposing a fix. "
        "Do not declare this resolved until each one is accounted for."
    )

    parts.append("")
    parts.append(MANAGED_BLOCK_START)
    parts.append(f"_{last_run_label}_")
    parts.append(f"- Member count: {member_count}")
    parts.append(f"- Confidence: {confidence_pct}%")
    parts.append(MANAGED_BLOCK_END)
    parts.append("")
    parts.append("<sub>Filed automatically by [Causinator 9000](https://github.com/sylvainsf/causinator9000). To stop receiving issues, disable `auto-issue` in the workflow.</sub>")

    return title, "\n".join(parts)


def _cause_human(group: dict, repo: str) -> str:
    """Build a short human-readable cause string from a group."""
    rc_id = group.get("root_cause_id", "")
    commit = group.get("commit") or {}
    if commit:
        sha = (commit.get("sha") or rc_id.rsplit("/", 1)[-1])[:8] if rc_id.startswith("commit://") else ""
        author = commit.get("author") or "unknown"
        subject = (commit.get("message") or "").splitlines()[0][:60] if commit.get("message") else ""
        parts = []
        if sha:
            parts.append(f"[{sha}](https://github.com/{repo}/commit/{sha})")
        parts.append(f"by @{author}")
        if subject:
            parts.append(f'"{subject}"')
        return " ".join(parts)
    rc_class = group.get("root_cause_class") or ""
    if rc_class == "BrokenTestRun":
        return f"`{_short_id(rc_id)}`"
    if rc_class == "FlakyTestRun":
        return f"`{_short_id(rc_id)}`"
    return f"`{_short_id(rc_id)}`"


def _group_time_range(group: dict) -> tuple[str, str]:
    """Return (first_seen, last_seen) ISO timestamps from members."""
    timestamps: list[str] = []
    for m in group.get("members", []):
        ts = m.get("latest_signal")
        if ts:
            timestamps.append(ts)
    group_ts = group.get("latest_signal")
    if group_ts:
        timestamps.append(group_ts)
    if not timestamps:
        return ("", "")
    timestamps.sort()
    # Trim to minute precision for readability
    first = timestamps[0][:16] + "Z" if len(timestamps[0]) >= 16 else timestamps[0]
    last = timestamps[-1][:16] + "Z" if len(timestamps[-1]) >= 16 else timestamps[-1]
    return (first, last)


def _short_id(rc_id: str) -> str:
    """Render a stable, short identifier for use in issue titles."""
    if rc_id.startswith("commit://"):
        sha = rc_id.rsplit("/", 1)[-1]
        return sha[:8]
    if rc_id.startswith("broken://"):
        # broken://owner/repo/<workflow_slug>/<branch>
        rest = rc_id[len("broken://") :]
        parts = rest.split("/")
        if len(parts) >= 4:
            return f"{parts[2]}@{'/'.join(parts[3:])}"
        return rest
    if rc_id.startswith("latent://"):
        return rc_id[len("latent://") :]
    return rc_id[:32]


def _format_rc_for_humans(rc_id: str, repo: str) -> str:
    if rc_id.startswith("commit://"):
        sha = rc_id.rsplit("/", 1)[-1]
        return f"commit [`{sha[:8]}`](https://github.com/{repo}/commit/{sha})"
    if rc_id.startswith("broken://"):
        return f"`{rc_id}`"
    if rc_id.startswith("latent://"):
        return rc_id[len("latent://") :]
    return rc_id


def update_managed_block(body: str, replacement: str) -> str:
    """Replace the c9k-managed:start..end block in `body` with `replacement`."""
    pattern = re.compile(
        re.escape(MANAGED_BLOCK_START) + r".*?" + re.escape(MANAGED_BLOCK_END),
        re.DOTALL,
    )
    if pattern.search(body):
        return pattern.sub(replacement, body)
    return body.rstrip() + "\n\n" + replacement + "\n"


# ── Issue mutation ops (or no-op in dry-run) ─────────────────────────────


def ensure_label(repo: str, label: str, dry_run: bool) -> None:
    if dry_run:
        return
    # gh label create returns non-zero if it already exists; that's fine.
    subprocess.run(
        ["gh", "label", "create", label, "--repo", repo, "--color", "D93F0B",
         "--description", "Causinator 9000 auto-filed issue"],
        capture_output=True, text=True,
    )


def create_issue(
    repo: str,
    title: str,
    body: str,
    label: str,
    assign_copilot: bool,
    dry_run: bool,
) -> dict | None:
    if dry_run:
        return None
    args = [
        "issue", "create",
        "--repo", repo,
        "--title", title,
        "--body", body,
        "--label", label,
    ]
    out = gh(*args, check=False)
    # gh issue create prints the URL on success.
    url = out.strip().splitlines()[-1] if out.strip() else None
    if not url or "issues/" not in url:
        return None
    try:
        number = int(url.rsplit("/", 1)[-1])
    except ValueError:
        return None
    # Assign Copilot after creation. The --assignee flag on `gh issue create`
    # is unreliable for service/bot accounts, so we use a separate edit call.
    if assign_copilot:
        gh("issue", "edit", str(number), "--repo", repo,
           "--add-assignee", "Copilot", check=False)
    return {"number": number, "url": url}


def update_issue(repo: str, number: int, body: str, dry_run: bool) -> None:
    if dry_run:
        return
    gh("issue", "edit", str(number), "--repo", repo, "--body", body, check=False)


def comment_issue(repo: str, number: int, body: str, dry_run: bool) -> None:
    if dry_run:
        return
    gh("issue", "comment", str(number), "--repo", repo, "--body", body, check=False)


def close_issue(repo: str, number: int, reason: str, comment: str | None, dry_run: bool) -> None:
    if dry_run:
        return
    if comment:
        gh("issue", "comment", str(number), "--repo", repo, "--body", comment, check=False)
    gh("issue", "close", str(number), "--repo", repo, "--reason", reason, check=False)


def reopen_issue(repo: str, number: int, comment: str | None, dry_run: bool) -> None:
    if dry_run:
        return
    if comment:
        gh("issue", "comment", str(number), "--repo", repo, "--body", comment, check=False)
    gh("issue", "reopen", str(number), "--repo", repo, check=False)


# ── Main planning loop ───────────────────────────────────────────────────


def filter_groups(
    groups: list[dict],
    min_confidence: float,
    min_members: int,
    classes: set[str],
) -> tuple[list[dict], FilterStats]:
    out: list[dict] = []
    stats = FilterStats()
    for g in groups:
        if g.get("confidence", 0) < min_confidence:
            stats.below_min_confidence += 1
            continue
        if g.get("member_count", 0) < min_members:
            stats.below_min_members += 1
            continue
        cls = g.get("root_cause_class") or ""
        if cls not in classes:
            stats.class_not_in_allow_list += 1
            continue
        out.append(g)
    return out, stats


def process_group(
    group: dict,
    repo: str,
    label: str,
    args: argparse.Namespace,
    last_run_label: str,
) -> Action:
    rc_id = group["root_cause_id"]
    rc_class = group.get("root_cause_class") or ""
    member_count = group["member_count"]
    confidence_pct = int(round(group.get("confidence", 0) * 100))
    cause_human = _cause_human(group, repo)
    first_seen, last_seen = _group_time_range(group)
    run_urls = [m.get("url") for m in group["members"] if m.get("url")]

    # Common fields shared by every Action from this group.
    common = dict(
        root_cause_id=rc_id,
        root_cause_class=rc_class,
        confidence_pct=confidence_pct,
        member_count=member_count,
        cause_human=cause_human,
        first_seen=first_seen,
        last_seen=last_seen,
    )

    cross_tool_dupes = find_cross_tool_duplicates(repo, label, run_urls) if run_urls else []
    title, body = build_issue_body(repo, group, cross_tool_dupes, last_run_label)

    existing = find_existing_c9k_issue(repo, label, rc_id)

    # Flaky → comment-and-close (never assign Copilot, never reopen).
    if rc_class in FLAKY_CLASSES:
        if not args.auto_close_flaky:
            return Action(
                **common,
                kind="skip",
                note=f"flaky group skipped (auto-close-flaky off): {member_count} runs",
            )
        comment = (
            f"Detected {member_count} flaky-test failures (confidence {int(group['confidence']*100)}%). "
            "Closing automatically — flakiness is tracked separately and is not "
            "Copilot's responsibility to fix one-by-one. See the c9k digest issue "
            "for the full flakiness picture."
        )
        if existing:
            if existing.get("state") == "OPEN":
                close_issue(repo, existing["number"], "not_planned", comment, args.dry_run)
            return Action(
                **common,
                kind="close-flaky",
                issue_number=existing["number"],
                issue_url=existing.get("url"),
                title=title,
                note=f"flaky group, member_count={member_count}",
            )
        # No existing issue — don't even create one for flaky.
        return Action(
            **common,
            kind="skip",
            title=title,
            note=f"flaky group, no issue created (member_count={member_count})",
        )

    # Non-flaky path: create / update / reopen.
    if existing is None:
        new = create_issue(
            repo,
            title,
            body,
            label,
            assign_copilot=args.assign_copilot and rc_class in COPILOT_CLASSES,
            dry_run=args.dry_run,
        )
        if args.close_cross_tool_duplicates and cross_tool_dupes:
            for dup in cross_tool_dupes:
                target = (new or {}).get("url") or "the c9k root-cause issue"
                close_issue(
                    repo,
                    dup["number"],
                    "not_planned",
                    f"Closed as a duplicate of {target} (Causinator 9000 grouped this with a shared root cause).",
                    args.dry_run,
                )
        return Action(
            **common,
            kind="create",
            issue_number=(new or {}).get("number"),
            issue_url=(new or {}).get("url"),
            title=title,
            note=f"new issue, members={member_count}, copilot={args.assign_copilot and rc_class in COPILOT_CLASSES}",
        )

    # Existing issue.
    state = existing.get("state", "OPEN")
    number = existing["number"]
    issue_url = existing.get("url")

    if state == "CLOSED" and args.reopen_stale:
        reopen_issue(
            repo,
            number,
            f"Causinator 9000 detected this root cause again ({member_count} failing runs, "
            f"{int(group['confidence']*100)}% confidence). Reopening with refreshed data.",
            args.dry_run,
        )
        update_issue(repo, number, body, args.dry_run)
        return Action(
            **common,
            kind="reopen",
            issue_number=number,
            issue_url=issue_url,
            title=title,
            note=f"reopened, members={member_count}",
        )

    # Open → refresh body (cheap; preserves marker comments) and add a "still
    # seen" comment summarising what's new.
    update_issue(repo, number, body, args.dry_run)
    comment_issue(
        repo,
        number,
        f"_{last_run_label}_ — still detected. Member count: {member_count}, "
        f"confidence {int(group['confidence']*100)}%.",
        args.dry_run,
    )
    return Action(
        **common,
        kind="update",
        issue_number=number,
        issue_url=issue_url,
        title=title,
        note=f"updated, members={member_count}",
    )


def auto_close_resolved_issues(
    repo: str,
    label: str,
    seen_root_cause_ids: set[str],
    args: argparse.Namespace,
    last_run_label: str,
) -> list[Action]:
    """Close issues whose root cause didn't appear in the latest report."""
    if not args.auto_close_resolved:
        return []
    open_issues = gh_json(
        "issue", "list",
        "--repo", repo,
        "--label", label,
        "--state", "open",
        "--limit", "200",
        "--json", "number,title,body,url",
    ) or []
    actions: list[Action] = []
    marker_re = re.compile(rf"<!--\s*{re.escape(ROOT_CAUSE_MARKER)}:\s*(.+?)\s*-->")
    for issue in open_issues:
        m = marker_re.search(issue.get("body") or "")
        if not m:
            continue
        rc_id = m.group(1).strip()
        if rc_id in seen_root_cause_ids:
            continue
        close_issue(
            repo,
            issue["number"],
            "completed",
            f"_{last_run_label}_ — Causinator 9000 no longer detects this root cause "
            "above the configured thresholds. Closing as resolved. If failures recur, "
            "the issue will be reopened automatically.",
            args.dry_run,
        )
        actions.append(Action(
            kind="close-resolved",
            root_cause_id=rc_id,
            issue_number=issue["number"],
            issue_url=issue.get("url"),
            title=issue.get("title"),
            note="auto-closed as resolved",
        ))
    return actions


# ── Summary rendering ────────────────────────────────────────────────────


def render_summary(plan: Plan, repo: str, dry_run: bool, args: argparse.Namespace | None = None) -> str:
    lines: list[str] = []
    header = "## Causinator 9000 — Auto-Issue Outcomes"
    if dry_run:
        header += " (DRY RUN — no changes made)"
    lines.append(header)
    lines.append("")
    if not plan.actions:
        lines.append("_No alert groups matched the auto-issue thresholds._")
        if _has_filter_stats(plan.filter_stats):
            lines.append("")
            _render_filter_stats(lines, plan.filter_stats, args)
        return "\n".join(lines)

    # ── Section 1: Per-action count broken down by class ──
    action_kinds = ("create", "update", "reopen", "close-flaky", "close-resolved",
                    "close-cross-tool", "skip")
    classes_seen: dict[str, dict[str, int]] = {}
    for a in plan.actions:
        cls = a.root_cause_class or "Unknown"
        if cls not in classes_seen:
            classes_seen[cls] = {}
        classes_seen[cls][a.kind] = classes_seen[cls].get(a.kind, 0) + 1

    present_kinds = [k for k in action_kinds if any(c.get(k, 0) for c in classes_seen.values())]
    if present_kinds:
        lines.append("### Actions by class")
        lines.append("")
        lines.append("| Class | " + " | ".join(present_kinds) + " |")
        lines.append("|---" + "|---" * len(present_kinds) + "|")
        for cls in sorted(classes_seen):
            row = " | ".join(str(classes_seen[cls].get(k, 0)) for k in present_kinds)
            lines.append(f"| {cls} | {row} |")
        lines.append("")

    # ── Section 2: Detailed per-row planned actions ──
    lines.append("### Planned actions")
    lines.append("")
    lines.append("| Action | Class | Confidence | Title (proposed) | Cause | Members | First seen | Last seen | Issue |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for a in plan.actions:
        issue_link = (
            f"[#{a.issue_number}]({a.issue_url})"
            if a.issue_number and a.issue_url
            else (str(a.issue_number) if a.issue_number else "_(planned)_")
        )
        title_cell = a.title or ""
        # Escape pipes in title to avoid breaking the table
        title_cell = title_cell.replace("|", "\\|")
        cause_cell = a.cause_human.replace("|", "\\|") if a.cause_human else ""
        cls = a.root_cause_class or ""
        conf = f"{a.confidence_pct}%" if a.confidence_pct else ""
        members = str(a.member_count) if a.member_count else ""
        lines.append(
            f"| {a.kind} | {cls} | {conf} | {title_cell} | {cause_cell} "
            f"| {members} | {a.first_seen} | {a.last_seen} | {issue_link} |"
        )

    # ── Section 3: Filtered / skipped counts ──
    if _has_filter_stats(plan.filter_stats):
        lines.append("")
        _render_filter_stats(lines, plan.filter_stats, args)

    return "\n".join(lines)


def _has_filter_stats(stats: FilterStats) -> bool:
    return (stats.below_min_confidence + stats.below_min_members + stats.class_not_in_allow_list) > 0


def _render_filter_stats(
    lines: list[str],
    stats: FilterStats,
    args: argparse.Namespace | None,
) -> None:
    lines.append("### Filtered out")
    lines.append("")
    lines.append("```")
    conf_threshold = ""
    members_threshold = ""
    if args is not None:
        conf_threshold = f" ({int(args.min_confidence)})"
        members_threshold = f" ({args.min_members})"
    if stats.below_min_confidence:
        lines.append(f"  below min-confidence{conf_threshold}: {stats.below_min_confidence}")
    if stats.below_min_members:
        lines.append(f"  below min-members{members_threshold}:   {stats.below_min_members}")
    if stats.class_not_in_allow_list:
        lines.append(f"  class not in allow-list:   {stats.class_not_in_allow_list}")
    lines.append("```")


# ── CLI ─────────────────────────────────────────────────────────────────


def main() -> int:
    p = argparse.ArgumentParser(description="C9K auto-issue helper")
    p.add_argument("--report", required=True, help="Path to JSON report from c9k-engine")
    p.add_argument("--repo", required=True, help="Target repo (owner/name)")
    p.add_argument("--min-confidence", type=float, default=90.0,
                   help="Confidence floor 0-100 (default 90)")
    p.add_argument("--min-members", type=int, default=2,
                   help="Minimum failing jobs in a group (default 2)")
    p.add_argument("--classes", default="CodeChange,BrokenTestRun,DepMajorBump,DepGroupUpdate",
                   help="Comma-separated root-cause classes to file (default "
                        "'CodeChange,BrokenTestRun,DepMajorBump,DepGroupUpdate')")
    p.add_argument("--label", default="c9k-auto", help="Label for c9k auto-issues")
    p.add_argument("--assign-copilot", action="store_true",
                   help="Assign Copilot on commit/broken issues")
    p.add_argument("--auto-close-flaky", action="store_true",
                   help="Comment-and-close flaky-test issues")
    p.add_argument("--auto-close-resolved", action="store_true",
                   help="Close issues whose root cause is no longer detected")
    p.add_argument("--close-cross-tool-duplicates", action="store_true",
                   help="Close other-tool issues that match a c9k root cause")
    p.add_argument("--reopen-stale", action="store_true", default=True,
                   help="Reopen previously closed c9k issues if the group reappears (default on)")
    p.add_argument("--no-reopen-stale", dest="reopen_stale", action="store_false",
                   help="Disable reopening of stale closed issues")
    p.add_argument("--dry-run", action="store_true",
                   help="Plan only; do not call gh mutating commands")
    p.add_argument("--output-summary", help="Write markdown summary to PATH")
    p.add_argument("--last-run-label", default="",
                   help="Free-text label inserted into managed blocks "
                        "(e.g. 'Last updated: 2026-04-22T12:00Z by run #...')")

    args = p.parse_args()

    if not shutil.which("gh"):
        print("error: gh CLI is required", file=sys.stderr)
        return 2

    # Normalise threshold to 0-1 to match the engine.
    min_conf = args.min_confidence / 100.0 if args.min_confidence > 1 else args.min_confidence
    classes = {c.strip() for c in args.classes.split(",") if c.strip()}

    with open(args.report) as f:
        report = json.load(f)

    if report.get("schema_version") != 1:
        print(
            f"warning: unexpected schema_version {report.get('schema_version')}; "
            "this script was written for schema_version=1",
            file=sys.stderr,
        )

    repo = args.repo
    label = args.label
    last_run_label = args.last_run_label or f"c9k auto-issue run at {report.get('generated_at', '')}"

    ensure_label(repo, label, args.dry_run)

    groups = report.get("alert_groups", [])
    eligible, filter_stats = filter_groups(
        groups,
        min_conf,
        args.min_members,
        classes | FLAKY_CLASSES,  # always evaluate flaky for close-flaky behaviour
    )

    plan = Plan(filter_stats=filter_stats)
    seen_ids: set[str] = set()
    for g in eligible:
        seen_ids.add(g["root_cause_id"])
        try:
            action = process_group(g, repo, label, args, last_run_label)
        except Exception as e:  # noqa: BLE001
            action = Action(
                kind="skip",
                root_cause_id=g.get("root_cause_id", "?"),
                note=f"error: {e}",
            )
        plan.add(action)

    plan.actions.extend(
        auto_close_resolved_issues(repo, label, seen_ids, args, last_run_label)
    )

    summary = render_summary(plan, repo, args.dry_run, args)
    print(summary)
    if args.output_summary:
        with open(args.output_summary, "w") as f:
            f.write(summary + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
