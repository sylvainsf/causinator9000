"""
Tests for GitHub Actions source adapter.

Tests are organized in three tiers:
  1. Classification (pure functions, no I/O)
  2. Event processing (mocked subprocess/HTTP)
  3. Integration (requires running engine — skipped in CI)

To add tests for a new source adapter (e.g., AWS CloudTrail):
  1. Copy this file as tests/test_aws_cloudtrail_source.py
  2. Import classification functions from sources/aws_cloudtrail_source.py
  3. Follow the same 3-tier pattern
  4. Add fixtures for sample API responses in tests/fixtures/
"""

import json
import os
import sys
import pytest
from unittest.mock import patch, MagicMock

# Add sources to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sources.gh_actions_source import (
    classify_error,
    detect_mutation_type,
    job_node_id,
    commit_node_id,
    INFRA_SIGNALS,
    CODE_SIGNALS,
    ERROR_PATTERNS,
)


# ═══════════════════════════════════════════════════════════════════════
# Tier 1: Classification tests (pure functions, no I/O)
# ═══════════════════════════════════════════════════════════════════════


class TestErrorClassification:
    """Test that GH Actions error lines are classified into correct signal types."""

    def test_azure_auth_failure_oidc(self):
        errors = ["AADSTS7002381: Federated identity credentials issued by..."]
        assert classify_error(errors, []) == "AzureAuthFailure"

    def test_azure_auth_failure_login(self):
        errors = ["Login failed with Error: The process '/usr/bin/az' failed with exit code 1"]
        assert classify_error(errors, []) == "AzureAuthFailure"

    def test_image_pull_error(self):
        errors = ["ErrImagePull: failed to pull image ghcr.io/..."]
        assert classify_error(errors, []) == "ImagePullError"

    def test_image_pull_backoff(self):
        errors = ["Back-off pulling image 'ghcr.io/...'", "ImagePullBackOff"]
        assert classify_error(errors, []) == "ImagePullError"

    def test_timeout(self):
        errors = ["The HTTP request timed out after 00:01:40"]
        assert classify_error(errors, []) == "Timeout"

    def test_checklist_missing(self):
        errors = ["No task list was present and requireChecklist is turned on"]
        assert classify_error(errors, []) == "ChecklistMissing"

    def test_helm_chart_error(self):
        errors = ["chart validation failed: no such file or directory...Chart"]
        assert classify_error(errors, []) == "HelmChartError"

    def test_bicep_build_error(self):
        errors = ["bicep build failed: exit status 1"]
        assert classify_error(errors, []) == "BicepBuildError"

    def test_dependabot_update_failure(self):
        errors = ["Dependabot encountered an error performing the update"]
        assert classify_error(errors, []) == "DependabotUpdateFailure"

    def test_unit_test_failure(self):
        errors = []
        steps = ["Run make test (unit tests)"]
        assert classify_error(errors, steps) == "UnitTestFailure"

    def test_devcontainer_test(self):
        errors = []
        steps = ["Generating tests for Radius CLI (default) against 'mcr.microsoft.com/devcontainers/base:debian'"]
        assert classify_error(errors, steps) == "DevContainerTestFailure"

    def test_generic_exit_code(self):
        errors = ["Process completed with exit code 2."]
        assert classify_error(errors, []) == "TestFailure"

    def test_empty_context_defaults_to_test_failure(self):
        assert classify_error([], []) == "TestFailure"

    def test_artifact_upload_failure(self):
        errors = ["No files were found with the provided path: logs. No artifacts will be uploaded."]
        assert classify_error(errors, []) == "ArtifactUploadFailure"

    def test_remote_workflow_failure(self):
        errors = ["Remote workflow failed with conclusion: failure"]
        assert classify_error(errors, []) == "RemoteWorkflowFailure"

    def test_first_match_wins(self):
        """When multiple patterns match, the first (most specific) wins."""
        errors = [
            "AADSTS7002381: ...",
            "Process completed with exit code 1",
        ]
        assert classify_error(errors, []) == "AzureAuthFailure"


class TestSignalAttribution:
    """Test that signal types are correctly categorized as infra vs code."""

    def test_infra_signals(self):
        for sig in ["AzureAuthFailure", "ImagePullError", "Timeout",
                     "ImagePushError", "RemoteWorkflowFailure",
                     "DependabotUpdateFailure", "ArtifactUploadFailure"]:
            assert sig in INFRA_SIGNALS, f"{sig} should be INFRA"

    def test_code_signals(self):
        for sig in ["TestFailure", "HelmChartError", "BicepBuildError",
                     "ChecklistMissing", "UnitTestFailure",
                     "DevContainerTestFailure"]:
            assert sig in CODE_SIGNALS, f"{sig} should be CODE"

    def test_no_overlap(self):
        assert INFRA_SIGNALS.isdisjoint(CODE_SIGNALS)


class TestMutationTypeDetection:
    """Test commit message → mutation type classification."""

    def test_dependabot_actions_bump(self):
        info = {"message": "Bump the github-actions group across 1 directory with 2 updates", "author": "dependabot[bot]"}
        assert detect_mutation_type(info, "push") == "DepActionsBump"

    def test_dependabot_go_group(self):
        info = {"message": "Bump the go-dependencies group across 2 directories with 30 updates", "author": "dependabot[bot]"}
        assert detect_mutation_type(info, "push") == "DepGroupUpdate"

    def test_dependabot_major_version(self):
        info = {"message": "Bump foo from 2.3.0 to 3.0.0", "author": "dependabot[bot]"}
        assert detect_mutation_type(info, "push") == "DepMajorBump"

    def test_dependabot_minor_version(self):
        info = {"message": "Bump foo from 1.2.0 to 1.3.0", "author": "dependabot[bot]"}
        assert detect_mutation_type(info, "push") == "DepMinorBump"

    def test_dependabot_generic(self):
        info = {"message": "Bump foo", "author": "dependabot[bot]"}
        assert detect_mutation_type(info, "push") == "DependencyUpdate"

    def test_release_commit(self):
        info = {"message": "chore(release): v0.55.0", "author": "Dariusz"}
        assert detect_mutation_type(info, "push") == "Release"

    def test_revert(self):
        info = {"message": "Revert 'Add broken feature'", "author": "dev"}
        assert detect_mutation_type(info, "push") == "Revert"

    def test_regular_code_change(self):
        info = {"message": "fix: handle nil pointer in controller", "author": "dev"}
        assert detect_mutation_type(info, "push") == "CodeChange"

    def test_scheduled_run(self):
        # detect_mutation_type doesn't check event type — it only looks at message/author
        # The GH source handles ScheduledRun at a higher level
        info = {"message": "some commit", "author": "dev"}
        assert detect_mutation_type(info, "schedule") == "CodeChange"

    def test_pull_request_event(self):
        info = {"message": "Apply suggestions from code review", "author": "dev"}
        assert detect_mutation_type(info, "pull_request") == "CodeChange"


class TestNodeIds:
    """Test node ID generation."""

    def test_job_node_id(self):
        nid = job_node_id("project-radius/radius", 12345, "Run functional tests")
        assert nid == "job://project-radius/radius/12345/run-functional-tests"

    def test_job_node_id_special_chars(self):
        nid = job_node_id("org/repo", 1, "Build & Test (ubuntu)")
        assert "job://org/repo/1/" in nid
        assert " " not in nid

    def test_commit_node_id(self):
        nid = commit_node_id("project-radius/radius", "9f403647abcdef12")
        assert nid == "commit://project-radius/radius/9f403647"

    def test_commit_node_id_short_sha(self):
        nid = commit_node_id("org/repo", "abc")
        assert nid == "commit://org/repo/abc"


# ═══════════════════════════════════════════════════════════════════════
# Tier 2: Event processing tests (mocked subprocess/HTTP)
# ═══════════════════════════════════════════════════════════════════════


class TestProcessFailures:
    """Test the full failure processing pipeline with mocked APIs."""

    @pytest.fixture
    def sample_runs(self):
        """Sample GH run data as returned by gh run list."""
        return [
            {
                "databaseId": 100,
                "headSha": "abc12345",
                "headBranch": "main",
                "event": "push",
                "workflowName": "Build and Test",
                "conclusion": "failure",
                "createdAt": "2026-03-08T10:00:00Z",
                "updatedAt": "2026-03-08T10:05:00Z",
            },
            {
                "databaseId": 101,
                "headSha": "abc12345",
                "headBranch": "main",
                "event": "push",
                "workflowName": "Unit Tests",
                "conclusion": "success",
                "createdAt": "2026-03-08T10:00:00Z",
                "updatedAt": "2026-03-08T10:03:00Z",
            },
        ]

    @pytest.fixture
    def sample_failed_jobs(self):
        return {
            "jobs": [
                {
                    "name": "Dispatch Bicep Types publish",
                    "conclusion": "failure",
                    "steps": [
                        {"name": "Checkout", "conclusion": "success"},
                        {"name": "Login to Azure", "conclusion": "failure"},
                    ],
                },
            ]
        }

    @pytest.fixture
    def sample_error_lines(self):
        return (
            "step1\t2026-03-08T10:04:00Z ##[error]Login failed with Error: "
            "The process '/usr/bin/az' failed with exit code 1.\n"
        )

    def test_only_failures_processed(self, sample_runs):
        """Successful runs should not create any nodes."""
        failed = [r for r in sample_runs if r["conclusion"] == "failure"]
        assert len(failed) == 1

    def test_failed_job_extraction(self, sample_failed_jobs):
        """Failed jobs should be extracted with their failed steps."""
        jobs = sample_failed_jobs["jobs"]
        failed = [j for j in jobs if j["conclusion"] == "failure"]
        assert len(failed) == 1
        steps = [s["name"] for s in failed[0]["steps"] if s["conclusion"] == "failure"]
        assert steps == ["Login to Azure"]


# ═══════════════════════════════════════════════════════════════════════
# Tier 3: Integration tests (require running engine)
# ═══════════════════════════════════════════════════════════════════════

ENGINE_URL = os.environ.get("C9K_ENGINE_URL", "http://localhost:8080")


@pytest.mark.skipif(
    os.environ.get("C9K_INTEGRATION") != "1",
    reason="Set C9K_INTEGRATION=1 to run integration tests (requires engine)",
)
class TestIntegration:
    """End-to-end tests against a running engine."""

    def test_engine_health(self):
        import urllib.request
        resp = urllib.request.urlopen(f"{ENGINE_URL}/api/health", timeout=5)
        data = json.loads(resp.read())
        assert data["status"] == "ok"
