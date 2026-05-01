"""Tests for scripts/auto_issue.py."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Make `scripts/` importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import auto_issue  # noqa: E402


# ── Fixtures / helpers ───────────────────────────────────────────────────

def _make_group(
    *,
    rc_id: str = "commit://owner/repo/abc123def456",
    rc_class: str = "CodeChange",
    confidence: float = 0.95,
    member_count: int = 3,
    branch: str = "main",
    pr: dict | None = None,
    commit: dict | None = None,
    members: list[dict] | None = None,
) -> dict:
    """Build a minimal alert-group dict for testing."""
    if commit is None:
        commit = {
            "sha": "abc123def456",
            "author": "alice",
            "message": "feat: add widget\nsecond line",
            "files": ["src/widget.rs", "tests/test_widget.rs"],
        }
    if members is None:
        members = [
            {
                "run_id": "123",
                "job": "build",
                "url": "https://github.com/owner/repo/actions/runs/123",
                "signal_types": ["log-match"],
                "latest_signal": "2026-04-22T10:30:00Z",
            },
            {
                "run_id": "124",
                "job": "test",
                "url": "https://github.com/owner/repo/actions/runs/124",
                "signal_types": ["exit-code"],
                "latest_signal": "2026-04-22T11:00:00Z",
            },
            {
                "run_id": "125",
                "job": "lint",
                "url": "https://github.com/owner/repo/actions/runs/125",
                "signal_types": ["log-match", "exit-code"],
                "latest_signal": "2026-04-22T12:15:00Z",
            },
        ]
    return {
        "root_cause_id": rc_id,
        "root_cause_class": rc_class,
        "confidence": confidence,
        "member_count": member_count,
        "branch": branch,
        "pr": pr,
        "commit": commit,
        "members": members,
        "signal_types": ["log-match", "exit-code"],
    }


def _make_args(**overrides) -> argparse.Namespace:
    defaults = dict(
        dry_run=True,
        assign_copilot=True,
        auto_close_flaky=False,
        auto_close_resolved=False,
        close_cross_tool_duplicates=False,
        reopen_stale=True,
        min_confidence=90,
        min_members=2,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ── _short_id ────────────────────────────────────────────────────────────


class TestShortId:
    def test_commit_prefix(self):
        assert auto_issue._short_id("commit://owner/repo/abc123def456") == "abc123de"

    def test_broken_prefix(self):
        result = auto_issue._short_id("broken://owner/repo/workflow/main")
        assert result == "workflow@main"

    def test_latent_prefix(self):
        assert auto_issue._short_id("latent://flaky-tests") == "flaky-tests"

    def test_unknown_prefix(self):
        long = "a" * 64
        assert auto_issue._short_id(long) == "a" * 32


# ── _cause_human ─────────────────────────────────────────────────────────


class TestCauseHuman:
    def test_commit_with_author_and_subject(self):
        group = _make_group()
        result = auto_issue._cause_human(group, "owner/repo")
        assert "@alice" in result
        assert "abc123de" in result
        assert "add widget" in result

    def test_no_commit(self):
        group = _make_group(commit={}, rc_class="BrokenTestRun",
                            rc_id="broken://owner/repo/ci/main")
        result = auto_issue._cause_human(group, "owner/repo")
        assert "ci@main" in result

    def test_commit_no_message(self):
        group = _make_group(commit={"sha": "abc123def456", "author": "bob"})
        result = auto_issue._cause_human(group, "owner/repo")
        assert "@bob" in result
        # No subject, so no quotes
        assert '"' not in result


# ── _group_time_range ────────────────────────────────────────────────────


class TestGroupTimeRange:
    def test_basic_range(self):
        group = _make_group()
        first, last = auto_issue._group_time_range(group)
        assert first == "2026-04-22T10:30Z"
        assert last == "2026-04-22T12:15Z"

    def test_empty_members(self):
        group = _make_group(members=[])
        first, last = auto_issue._group_time_range(group)
        assert first == ""
        assert last == ""

    def test_single_member(self):
        group = _make_group(members=[
            {"run_id": "1", "job": "build", "latest_signal": "2026-04-22T09:00:00Z"},
        ])
        first, last = auto_issue._group_time_range(group)
        assert first == last == "2026-04-22T09:00Z"

    def test_group_level_timestamp_included(self):
        group = _make_group(members=[
            {"run_id": "1", "job": "build", "latest_signal": "2026-04-22T09:00:00Z"},
        ])
        group["latest_signal"] = "2026-04-22T14:00:00Z"
        first, last = auto_issue._group_time_range(group)
        assert first == "2026-04-22T09:00Z"
        assert last == "2026-04-22T14:00Z"


# ── filter_groups ────────────────────────────────────────────────────────


class TestFilterGroups:
    def test_passes_eligible_groups(self):
        groups = [_make_group()]
        result, stats = auto_issue.filter_groups(groups, 0.9, 2, {"CodeChange"}, "main", True)
        assert len(result) == 1

    def test_filters_low_confidence(self):
        groups = [_make_group(confidence=0.5)]
        result, stats = auto_issue.filter_groups(groups, 0.9, 2, {"CodeChange"}, "main", True)
        assert len(result) == 0
        assert stats.below_min_confidence == 1

    def test_filters_low_members(self):
        groups = [_make_group(member_count=1)]
        result, stats = auto_issue.filter_groups(groups, 0.9, 2, {"CodeChange"}, "main", True)
        assert len(result) == 0
        assert stats.below_min_members == 1

    def test_filters_wrong_class(self):
        groups = [_make_group(rc_class="UnknownClass")]
        result, stats = auto_issue.filter_groups(groups, 0.9, 2, {"CodeChange"}, "main", True)
        assert len(result) == 0
        assert stats.class_not_in_allow_list == 1

    def test_multiple_filters(self):
        groups = [
            _make_group(confidence=0.5),  # low confidence
            _make_group(member_count=1),   # low members
            _make_group(rc_class="Nope"),   # wrong class
            _make_group(),                  # passes
        ]
        result, stats = auto_issue.filter_groups(groups, 0.9, 2, {"CodeChange"}, "main", True)
        assert len(result) == 1
        assert stats.below_min_confidence == 1
        assert stats.below_min_members == 1
        assert stats.class_not_in_allow_list == 1


# ── branch policy ───────────────────────────────────────────────────────────


class TestBranchPolicy:
    def test_default_branch_allowed(self):
        assert auto_issue._branch_allowed_for_issue("main", "main")
        assert auto_issue._branch_allowed_for_issue("MAIN", "main")
        assert auto_issue._branch_allowed_for_issue("trunk", "trunk")

    def test_dependabot_allowed(self):
        assert auto_issue._branch_allowed_for_issue(
            "dependabot/go_modules/foo-1.2.3", "main"
        )

    def test_release_branches_allowed(self):
        assert auto_issue._branch_allowed_for_issue("release/0.50", "main")
        assert auto_issue._branch_allowed_for_issue("release-0.50", "main")
        assert auto_issue._branch_allowed_for_issue("v0.50.x", "main")
        assert auto_issue._branch_allowed_for_issue("v1.2.0", "main")

    def test_contributor_branches_blocked(self):
        assert not auto_issue._branch_allowed_for_issue("users/alice/feature", "main")
        assert not auto_issue._branch_allowed_for_issue("fix/bug-123", "main")
        assert not auto_issue._branch_allowed_for_issue("add-terraform-bicep-config", "main")

    def test_filter_blocks_contributor_branch_group(self):
        groups = [_make_group(branch="users/alice/wip")]
        result, stats = auto_issue.filter_groups(
            groups, 0.9, 2, {"CodeChange"}, "main", True
        )
        assert len(result) == 0
        assert stats.branch_not_in_policy == 1

    def test_filter_allows_dependabot_branch_group(self):
        groups = [_make_group(branch="dependabot/go_modules/foo-1.2.3")]
        result, stats = auto_issue.filter_groups(
            groups, 0.9, 2, {"CodeChange"}, "main", True
        )
        assert len(result) == 1
        assert stats.branch_not_in_policy == 0

    def test_filter_allows_release_branch_group(self):
        groups = [_make_group(branch="release/0.50")]
        result, stats = auto_issue.filter_groups(
            groups, 0.9, 2, {"CodeChange"}, "main", True
        )
        assert len(result) == 1

    def test_no_branch_policy_disables_gate(self):
        groups = [_make_group(branch="users/alice/wip")]
        result, stats = auto_issue.filter_groups(
            groups, 0.9, 2, {"CodeChange"}, "main", False
        )
        assert len(result) == 1
        assert stats.branch_not_in_policy == 0

    def test_broken_root_cause_uses_branch_from_uri(self):
        # broken:// groups don't carry `branch` directly; the branch is
        # encoded as the last URI segment.
        groups = [
            _make_group(
                rc_id="broken://owner/repo/long-running-test-on-azure/main",
                rc_class="BrokenTestRun",
                branch=None,
            )
        ]
        result, _ = auto_issue.filter_groups(
            groups, 0.9, 2, {"BrokenTestRun"}, "main", True
        )
        assert len(result) == 1

    def test_broken_root_cause_on_contributor_branch_blocked(self):
        groups = [
            _make_group(
                rc_id="broken://owner/repo/cloud-tests/users-alice-feature",
                rc_class="BrokenTestRun",
                branch=None,
            )
        ]
        result, stats = auto_issue.filter_groups(
            groups, 0.9, 2, {"BrokenTestRun"}, "main", True
        )
        assert len(result) == 0
        assert stats.branch_not_in_policy == 1

    def test_flaky_class_exempt_from_branch_policy(self):
        # Latent flaky-tests groups have no branch info; they must not be
        # blocked by the branch policy (auto-close-flaky handles them).
        groups = [
            _make_group(
                rc_id="latent://flaky-tests",
                rc_class="FlakyTestRun",
                branch=None,
            )
        ]
        result, stats = auto_issue.filter_groups(
            groups, 0.9, 2, {"FlakyTestRun"}, "main", True
        )
        assert len(result) == 1
        assert stats.branch_not_in_policy == 0


# ── _has_filter_stats / _render_filter_stats ─────────────────────────────


class TestFilterStats:
    def test_empty_stats_are_falsy(self):
        assert not auto_issue._has_filter_stats(auto_issue.FilterStats())

    def test_nonzero_stats_are_truthy(self):
        assert auto_issue._has_filter_stats(auto_issue.FilterStats(below_min_confidence=1))
        assert auto_issue._has_filter_stats(auto_issue.FilterStats(pr_closed=1))

    def test_render_filter_stats(self):
        lines: list[str] = []
        stats = auto_issue.FilterStats(below_min_confidence=3, pr_closed=1)
        args = _make_args()
        auto_issue._render_filter_stats(lines, stats, args)
        text = "\n".join(lines)
        assert "below min-confidence" in text
        assert "3" in text
        assert "PR already closed" in text

    def test_render_filter_stats_no_args(self):
        lines: list[str] = []
        stats = auto_issue.FilterStats(below_min_members=2)
        auto_issue._render_filter_stats(lines, stats, None)
        text = "\n".join(lines)
        assert "below min-members" in text


# ── render_summary ───────────────────────────────────────────────────────


class TestRenderSummary:
    def test_empty_plan(self):
        plan = auto_issue.Plan()
        result = auto_issue.render_summary(plan, "owner/repo", dry_run=True)
        assert "DRY RUN" in result
        assert "No alert groups matched" in result

    def test_empty_plan_with_filter_stats(self):
        plan = auto_issue.Plan(filter_stats=auto_issue.FilterStats(below_min_confidence=5))
        result = auto_issue.render_summary(plan, "owner/repo", dry_run=True)
        assert "Filtered out" in result
        assert "below min-confidence" in result

    def test_plan_with_actions(self):
        plan = auto_issue.Plan()
        plan.add(auto_issue.Action(
            kind="create",
            root_cause_id="commit://owner/repo/abc123",
            root_cause_class="CodeChange",
            confidence_pct=95,
            member_count=3,
            cause_human="abc123 by @alice",
            first_seen="2026-04-22T10:30Z",
            last_seen="2026-04-22T12:15Z",
            title="[c9k:abc123] CI regression",
        ))
        result = auto_issue.render_summary(plan, "owner/repo", dry_run=False)
        assert "DRY RUN" not in result
        assert "Actions by class" in result
        assert "Planned actions" in result
        assert "CodeChange" in result
        assert "95%" in result
        assert "abc123" in result

    def test_plan_with_issue_link(self):
        plan = auto_issue.Plan()
        plan.add(auto_issue.Action(
            kind="update",
            root_cause_id="commit://owner/repo/abc123",
            root_cause_class="CodeChange",
            confidence_pct=90,
            member_count=2,
            issue_number=42,
            issue_url="https://github.com/owner/repo/issues/42",
            title="test",
        ))
        result = auto_issue.render_summary(plan, "owner/repo", dry_run=False)
        assert "[#42]" in result

    def test_plan_not_live_shows_planned(self):
        plan = auto_issue.Plan()
        plan.add(auto_issue.Action(
            kind="create",
            root_cause_id="commit://owner/repo/abc123",
            root_cause_class="CodeChange",
            confidence_pct=95,
            member_count=3,
            title="test",
        ))
        result = auto_issue.render_summary(plan, "owner/repo", dry_run=True)
        assert "_(planned)_" in result


# ── _is_pr_closed ────────────────────────────────────────────────────────


class TestIsPrClosed:
    @patch("auto_issue.gh_json")
    def test_closed_pr(self, mock_gh_json):
        mock_gh_json.return_value = {"state": "CLOSED"}
        assert auto_issue._is_pr_closed("owner/repo", 42) is True

    @patch("auto_issue.gh_json")
    def test_merged_pr(self, mock_gh_json):
        mock_gh_json.return_value = {"state": "MERGED"}
        assert auto_issue._is_pr_closed("owner/repo", 42) is True

    @patch("auto_issue.gh_json")
    def test_open_pr(self, mock_gh_json):
        mock_gh_json.return_value = {"state": "OPEN"}
        assert auto_issue._is_pr_closed("owner/repo", 42) is False

    @patch("auto_issue.gh_json")
    def test_error_returns_false(self, mock_gh_json):
        mock_gh_json.side_effect = RuntimeError("API error")
        assert auto_issue._is_pr_closed("owner/repo", 42) is False


# ── process_group ────────────────────────────────────────────────────────


class TestProcessGroup:
    @patch("auto_issue.find_cross_tool_duplicates", return_value=[])
    @patch("auto_issue.find_existing_c9k_issue", return_value=None)
    @patch("auto_issue.create_issue", return_value=None)
    def test_dry_run_create(self, mock_create, mock_find, mock_xtool):
        group = _make_group()
        args = _make_args()
        action = auto_issue.process_group(group, "owner/repo", "c9k-auto", args, "run label")
        assert action.kind == "create"
        assert action.root_cause_class == "CodeChange"
        assert action.confidence_pct == 95
        assert action.member_count == 3
        assert action.cause_human != ""

    @patch("auto_issue._is_pr_closed", return_value=True)
    def test_skip_closed_pr(self, mock_closed):
        group = _make_group(pr={"number": 100, "url": "https://github.com/owner/repo/pull/100"})
        args = _make_args()
        action = auto_issue.process_group(group, "owner/repo", "c9k-auto", args, "run label")
        assert action.kind == "skip"
        assert "closed/merged" in action.note

    @patch("auto_issue.find_cross_tool_duplicates", return_value=[])
    @patch("auto_issue.find_existing_c9k_issue")
    @patch("auto_issue.update_issue")
    @patch("auto_issue.comment_issue")
    def test_update_existing(self, mock_comment, mock_update, mock_find, mock_xtool):
        mock_find.return_value = {
            "number": 50,
            "state": "OPEN",
            "url": "https://github.com/owner/repo/issues/50",
            "body": "<!-- c9k-root-cause: commit://owner/repo/abc123def456 -->",
        }
        group = _make_group()
        args = _make_args()
        action = auto_issue.process_group(group, "owner/repo", "c9k-auto", args, "run label")
        assert action.kind == "update"
        assert action.issue_number == 50

    @patch("auto_issue.find_cross_tool_duplicates", return_value=[])
    @patch("auto_issue.find_existing_c9k_issue")
    @patch("auto_issue.reopen_issue")
    @patch("auto_issue.update_issue")
    def test_reopen_closed(self, mock_update, mock_reopen, mock_find, mock_xtool):
        mock_find.return_value = {
            "number": 50,
            "state": "CLOSED",
            "url": "https://github.com/owner/repo/issues/50",
            "body": "<!-- c9k-root-cause: commit://owner/repo/abc123def456 -->",
        }
        group = _make_group()
        args = _make_args()
        action = auto_issue.process_group(group, "owner/repo", "c9k-auto", args, "run label")
        assert action.kind == "reopen"
        assert action.issue_number == 50

    @patch("auto_issue.find_cross_tool_duplicates", return_value=[])
    @patch("auto_issue.find_existing_c9k_issue", return_value=None)
    @patch("auto_issue.close_issue")
    def test_flaky_skip_when_disabled(self, mock_close, mock_find, mock_xtool):
        group = _make_group(rc_class="FlakyTestRun", rc_id="latent://flaky-tests")
        args = _make_args(auto_close_flaky=False)
        action = auto_issue.process_group(group, "owner/repo", "c9k-auto", args, "run label")
        assert action.kind == "skip"
        assert "flaky" in action.note

    @patch("auto_issue.find_cross_tool_duplicates", return_value=[])
    @patch("auto_issue.find_existing_c9k_issue")
    @patch("auto_issue.close_issue")
    def test_flaky_close_existing(self, mock_close, mock_find, mock_xtool):
        mock_find.return_value = {
            "number": 60,
            "state": "OPEN",
            "url": "https://github.com/owner/repo/issues/60",
            "body": "<!-- c9k-root-cause: latent://flaky-tests -->",
        }
        group = _make_group(rc_class="FlakyTestRun", rc_id="latent://flaky-tests")
        args = _make_args(auto_close_flaky=True)
        action = auto_issue.process_group(group, "owner/repo", "c9k-auto", args, "run label")
        assert action.kind == "close-flaky"
        assert action.issue_number == 60


# ── build_issue_body ─────────────────────────────────────────────────────


class TestBuildIssueBody:
    def test_basic_body(self):
        group = _make_group()
        title, body = auto_issue.build_issue_body("owner/repo", group, [], "run label")
        assert "[c9k:abc123de]" in title
        assert "Root cause" in body
        assert "abc123de" in body
        assert "c9k-root-cause:" in body
        assert "Success criteria" in body

    def test_cross_tool_dupes_in_body(self):
        group = _make_group()
        dupes = [{"number": 100, "title": "Build failed"}]
        title, body = auto_issue.build_issue_body("owner/repo", group, dupes, "run label")
        assert "Related issues" in body
        assert "#100" in body

    def test_flaky_title(self):
        group = _make_group(rc_class="FlakyTestRun", rc_id="latent://flaky-test",
                            commit={})
        title, _ = auto_issue.build_issue_body("owner/repo", group, [], "run label")
        assert "Flaky test" in title

    def test_dep_group_update_title(self):
        group = _make_group(rc_class="DepGroupUpdate")
        title, _ = auto_issue.build_issue_body("owner/repo", group, [], "run label")
        assert "dependency bump" in title.lower()


# ── update_managed_block ─────────────────────────────────────────────────


class TestUpdateManagedBlock:
    def test_replaces_existing_block(self):
        body = (
            "Some text\n"
            f"{auto_issue.MANAGED_BLOCK_START}\n"
            "old content\n"
            f"{auto_issue.MANAGED_BLOCK_END}\n"
            "more text"
        )
        replacement = (
            f"{auto_issue.MANAGED_BLOCK_START}\n"
            "new content\n"
            f"{auto_issue.MANAGED_BLOCK_END}"
        )
        result = auto_issue.update_managed_block(body, replacement)
        assert "new content" in result
        assert "old content" not in result
        assert "more text" in result

    def test_appends_when_no_block(self):
        body = "Some text without a managed block"
        replacement = (
            f"{auto_issue.MANAGED_BLOCK_START}\n"
            "new content\n"
            f"{auto_issue.MANAGED_BLOCK_END}"
        )
        result = auto_issue.update_managed_block(body, replacement)
        assert "Some text" in result
        assert "new content" in result
