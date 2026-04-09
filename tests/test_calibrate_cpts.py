"""
Tests for the CPT calibration pipeline.

Tests are organized in tiers matching the existing test structure:
  1. Pure functions (no I/O): file mutation classification, rate estimation
  2. CPT generation and validation (uses sample data)
  3. Integration (requires gh CLI — skipped in CI)
"""

import json
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.calibrate_cpts import (
    classify_files_mutation,
    bootstrap_rate,
    compute_rates,
    generate_cpt_yaml,
    validate_spread,
    generate_report,
    load_hand_tuned_cpts,
    build_full_yaml,
    RunRecord,
    RateEstimate,
    CptEstimate,
    FILE_PATH_MUTATION_PATTERNS,
    CALIBRATION_TO_CPT_MUTATION,
)


# ═══════════════════════════════════════════════════════════════════════
# Tier 1: File-based mutation classification (pure functions)
# ═══════════════════════════════════════════════════════════════════════


class TestFilesMutationClassification:
    """Test that changed file paths are classified into correct mutation types."""

    def test_workflow_files(self):
        files = [".github/workflows/ci.yml"]
        assert classify_files_mutation(files) == "WorkflowChange"

    def test_github_actions_dir(self):
        files = [".github/actions/setup/action.yml"]
        assert classify_files_mutation(files) == "WorkflowChange"

    def test_go_dependency_files(self):
        files = ["go.mod", "go.sum"]
        assert classify_files_mutation(files) == "DependencyFileChange"

    def test_npm_dependency_files(self):
        files = ["package.json", "package-lock.json"]
        assert classify_files_mutation(files) == "DependencyFileChange"

    def test_python_dependency_files(self):
        files = ["requirements.txt"]
        assert classify_files_mutation(files) == "DependencyFileChange"

    def test_cargo_dependency_files(self):
        files = ["Cargo.toml", "Cargo.lock"]
        assert classify_files_mutation(files) == "DependencyFileChange"

    def test_dockerfile(self):
        files = ["Dockerfile"]
        assert classify_files_mutation(files) == "ContainerFileChange"

    def test_docker_compose(self):
        files = ["docker-compose.yml"]
        assert classify_files_mutation(files) == "ContainerFileChange"

    def test_bicep_infra(self):
        files = ["deploy/main.bicep"]
        assert classify_files_mutation(files) == "InfraFileChange"

    def test_terraform_infra(self):
        files = ["infra/main.tf", "infra/variables.tfvars"]
        assert classify_files_mutation(files) == "InfraFileChange"

    def test_helm_chart(self):
        files = ["charts/myapp/Chart.yaml", "charts/myapp/values.yaml"]
        assert classify_files_mutation(files) == "HelmChartChange"

    def test_docs_only(self):
        files = ["README.md", "docs/guide.md", "CONTRIBUTING.md"]
        assert classify_files_mutation(files) == "DocsOnly"

    def test_docs_mixed_with_code(self):
        """If docs AND code files are changed, should NOT be DocsOnly."""
        files = ["README.md", "src/main.go"]
        result = classify_files_mutation(files)
        assert result != "DocsOnly"

    def test_test_files_go(self):
        files = ["pkg/controller/handler_test.go"]
        assert classify_files_mutation(files) == "TestFileChange"

    def test_test_files_python(self):
        files = ["tests/test_handler.py"]
        assert classify_files_mutation(files) == "TestFileChange"

    def test_test_files_js(self):
        files = ["src/components/Button.test.tsx"]
        assert classify_files_mutation(files) == "TestFileChange"

    def test_source_code_go(self):
        files = ["pkg/controller/handler.go"]
        assert classify_files_mutation(files) == "SourceCodeChange"

    def test_source_code_python(self):
        files = ["src/main.py"]
        assert classify_files_mutation(files) == "SourceCodeChange"

    def test_source_code_typescript(self):
        files = ["src/index.ts"]
        assert classify_files_mutation(files) == "SourceCodeChange"

    def test_empty_files(self):
        assert classify_files_mutation([]) == "Unknown"

    def test_unknown_file_types(self):
        files = ["data/sample.csv", "assets/logo.png"]
        assert classify_files_mutation(files) == "Unknown"

    def test_priority_workflow_over_code(self):
        """Workflow changes take priority over source code changes."""
        files = [".github/workflows/ci.yml", "src/main.go"]
        assert classify_files_mutation(files) == "WorkflowChange"

    def test_priority_deps_over_code(self):
        """Dependency changes take priority over source code changes."""
        files = ["go.mod", "pkg/handler.go"]
        assert classify_files_mutation(files) == "DependencyFileChange"

    def test_repo_config(self):
        files = [".github/CODEOWNERS"]
        # .github/ matches RepoConfigChange before CODEOWNERS matches DocsOnly
        assert classify_files_mutation(files) == "RepoConfigChange"


# ═══════════════════════════════════════════════════════════════════════
# Tier 1: Bootstrap rate estimation (pure functions)
# ═══════════════════════════════════════════════════════════════════════


class TestBootstrapRate:
    """Test bootstrap confidence interval computation."""

    def test_zero_observations(self):
        est = bootstrap_rate(0, 0)
        assert est.rate == 0.0
        assert est.low_confidence is True
        assert est.n_observations == 0

    def test_all_hits(self):
        est = bootstrap_rate(100, 100)
        assert est.rate == 1.0
        assert est.ci_lower >= 0.95
        assert est.ci_upper == 1.0

    def test_no_hits(self):
        est = bootstrap_rate(0, 100)
        assert est.rate == 0.0
        assert est.ci_lower == 0.0
        assert est.ci_upper <= 0.05

    def test_half_rate(self):
        est = bootstrap_rate(50, 100)
        assert 0.45 <= est.rate <= 0.55
        assert est.ci_lower < est.rate
        assert est.ci_upper > est.rate

    def test_low_confidence_flag(self):
        """Less than 30 observations should be flagged."""
        est = bootstrap_rate(5, 20)
        assert est.low_confidence is True

    def test_sufficient_confidence(self):
        """30+ observations should not be flagged."""
        est = bootstrap_rate(15, 50)
        assert est.low_confidence is False

    def test_very_few_observations(self):
        """Less than 5 observations get wide CI."""
        est = bootstrap_rate(1, 3)
        assert est.ci_lower == 0.0
        assert est.ci_upper == 1.0

    def test_ci_bounds_valid(self):
        """CI bounds should be within [0, 1]."""
        est = bootstrap_rate(30, 100)
        assert 0.0 <= est.ci_lower <= est.rate
        assert est.rate <= est.ci_upper <= 1.0

    def test_reproducible(self):
        """Same inputs should produce same outputs (seeded RNG)."""
        est1 = bootstrap_rate(30, 100)
        est2 = bootstrap_rate(30, 100)
        assert est1.rate == est2.rate
        assert est1.ci_lower == est2.ci_lower
        assert est1.ci_upper == est2.ci_upper


# ═══════════════════════════════════════════════════════════════════════
# Tier 2: Rate computation and CPT generation (uses sample data)
# ═══════════════════════════════════════════════════════════════════════


def make_record(repo="test/repo", run_id=1, conclusion="failure",
                signal_type="TestFailure", mutation_type="CodeChange",
                file_mutation_type="SourceCodeChange", **kwargs) -> RunRecord:
    """Helper to create RunRecord instances for testing."""
    return RunRecord(
        repo=repo,
        run_id=run_id,
        sha="abc12345",
        event="push",
        workflow_name="CI",
        conclusion=conclusion,
        signal_type=signal_type if conclusion == "failure" else "",
        mutation_type=mutation_type,
        file_mutation_type=file_mutation_type,
        changed_files=kwargs.get("changed_files", []),
    )


class TestComputeRates:
    """Test empirical rate computation."""

    def test_basic_rates(self):
        records = [
            # 3 CodeChange runs: 2 fail with TestFailure, 1 succeeds
            make_record(run_id=1, conclusion="failure",
                        signal_type="TestFailure", mutation_type="CodeChange"),
            make_record(run_id=2, conclusion="failure",
                        signal_type="TestFailure", mutation_type="CodeChange"),
            make_record(run_id=3, conclusion="success",
                        mutation_type="CodeChange"),
        ]
        rates = compute_rates(records)
        assert "CodeChange" in rates
        assert "TestFailure" in rates["CodeChange"]

        est = rates["CodeChange"]["TestFailure"]
        # 2 out of 3 CodeChange runs had TestFailure
        assert abs(est.p_signal_given_mutation.rate - 2 / 3) < 0.01

    def test_no_mutation_background_rate(self):
        records = [
            make_record(run_id=1, conclusion="failure",
                        signal_type="TestFailure", mutation_type="CodeChange"),
            make_record(run_id=2, conclusion="failure",
                        signal_type="TestFailure", mutation_type="Release"),
            make_record(run_id=3, conclusion="success",
                        mutation_type="CodeChange"),
        ]
        rates = compute_rates(records)
        # For CodeChange → TestFailure:
        # - runs with CodeChange: [1, 3] → 1 failure out of 2
        # - runs without CodeChange: [2] → 1 failure out of 1
        est = rates["CodeChange"]["TestFailure"]
        assert est.p_signal_given_mutation.rate == 0.5
        assert est.p_signal_given_no_mutation.rate == 1.0

    def test_empty_records(self):
        rates = compute_rates([])
        assert rates == {}


class TestGenerateCptYaml:
    """Test CPT YAML generation from computed rates."""

    def test_generates_entries(self):
        rates = {
            "CodeChange": {
                "TestFailure": CptEstimate(
                    mutation="CodeChange",
                    signal="TestFailure",
                    p_signal_given_mutation=RateEstimate(
                        rate=0.72, ci_lower=0.65, ci_upper=0.79,
                        n_observations=100, low_confidence=False),
                    p_signal_given_no_mutation=RateEstimate(
                        rate=0.03, ci_lower=0.01, ci_upper=0.05,
                        n_observations=500, low_confidence=False),
                    likelihood_ratio=24.0,
                    source="empirical",
                ),
            },
        }
        cpts = generate_cpt_yaml(rates, "/nonexistent/path.yaml")
        assert len(cpts) >= 1
        entry = cpts[0]
        assert entry["mutation"] == "CodeChange"
        assert entry["signal"] == "TestFailure"
        assert len(entry["table"]) == 2
        assert len(entry["table"][0]) == 2
        # Rows should approximately sum to 1
        assert abs(entry["table"][0][0] + entry["table"][1][0] - 1.0) < 0.02
        assert abs(entry["table"][0][1] + entry["table"][1][1] - 1.0) < 0.02

    def test_low_confidence_uses_fallback(self, tmp_path):
        """Low-confidence pairs should fall back to hand-tuned values."""
        # Create a temporary hand-tuned YAML
        ht_yaml = tmp_path / "hand-tuned.yaml"
        ht_data = [{
            "class": "CIJob",
            "cpts": [{
                "mutation": "CodeChange",
                "signal": "TestFailure",
                "table": [[0.72, 0.03], [0.28, 0.97]],
            }],
        }]
        with open(ht_yaml, "w") as f:
            yaml.dump(ht_data, f)

        rates = {
            "CodeChange": {
                "TestFailure": CptEstimate(
                    mutation="CodeChange",
                    signal="TestFailure",
                    p_signal_given_mutation=RateEstimate(
                        rate=0.60, ci_lower=0.30, ci_upper=0.90,
                        n_observations=10, low_confidence=True),
                    p_signal_given_no_mutation=RateEstimate(
                        rate=0.05, ci_lower=0.01, ci_upper=0.10,
                        n_observations=20, low_confidence=True),
                    likelihood_ratio=12.0,
                    source="hand-tuned-fallback",
                ),
            },
        }
        cpts = generate_cpt_yaml(rates, str(ht_yaml))
        # Should use the hand-tuned table values
        entry = [c for c in cpts if c["mutation"] == "CodeChange"
                 and c["signal"] == "TestFailure"][0]
        assert entry["table"] == [[0.72, 0.03], [0.28, 0.97]]

    def test_skips_low_lr_pairs(self):
        """Pairs with LR < 1.5 should be skipped."""
        rates = {
            "CodeChange": {
                "ScorecardFailure": CptEstimate(
                    mutation="CodeChange",
                    signal="ScorecardFailure",
                    p_signal_given_mutation=RateEstimate(
                        rate=0.02, ci_lower=0.01, ci_upper=0.04,
                        n_observations=100, low_confidence=False),
                    p_signal_given_no_mutation=RateEstimate(
                        rate=0.02, ci_lower=0.01, ci_upper=0.04,
                        n_observations=500, low_confidence=False),
                    likelihood_ratio=1.0,
                    source="empirical",
                ),
            },
        }
        cpts = generate_cpt_yaml(rates, "/nonexistent/path.yaml")
        scorecard = [c for c in cpts if c["signal"] == "ScorecardFailure"]
        assert len(scorecard) == 0


import yaml  # noqa: E402 (needed for test that writes YAML)


class TestValidateSpread:
    """Test CPT spread validation."""

    def test_good_spread(self):
        cpts = [
            {"mutation": "A", "signal": "X",
             "table": [[0.90, 0.01], [0.10, 0.99]]},  # LR=90
            {"mutation": "B", "signal": "Y",
             "table": [[0.10, 0.08], [0.90, 0.92]]},  # LR=1.25
        ]
        result = validate_spread(cpts, target_spread_pp=30)
        assert result["valid"] is True
        assert result["spread_pp"] > 30

    def test_poor_spread(self):
        cpts = [
            {"mutation": "A", "signal": "X",
             "table": [[0.50, 0.40], [0.50, 0.60]]},  # LR=1.25
            {"mutation": "B", "signal": "Y",
             "table": [[0.55, 0.42], [0.45, 0.58]]},  # LR=1.31
        ]
        result = validate_spread(cpts, target_spread_pp=30)
        assert result["valid"] is False

    def test_empty_cpts(self):
        result = validate_spread([], target_spread_pp=30)
        assert result["valid"] is False

    def test_single_entry(self):
        cpts = [
            {"mutation": "A", "signal": "X",
             "table": [[0.80, 0.02], [0.20, 0.98]]},
        ]
        result = validate_spread(cpts, target_spread_pp=30)
        assert result["valid"] is False  # Need ≥2 entries


class TestLoadHandTuned:
    """Test loading existing hand-tuned CPTs."""

    def test_load_existing(self):
        yaml_path = os.path.join(
            os.path.dirname(__file__), "..",
            "config", "heuristics", "ci-pipelines.yaml"
        )
        if os.path.exists(yaml_path):
            ht = load_hand_tuned_cpts(yaml_path)
            # Should have entries from the CI pipelines file
            assert len(ht) > 0
            assert ("CodeChange", "TestFailure") in ht
            table = ht[("CodeChange", "TestFailure")]
            assert len(table) == 2
            assert len(table[0]) == 2

    def test_load_nonexistent(self):
        ht = load_hand_tuned_cpts("/nonexistent/path.yaml")
        assert ht == {}


class TestBuildFullYaml:
    """Test full YAML structure generation."""

    def test_preserves_non_cijob_classes(self, tmp_path):
        existing = [
            {"class": "CIJob", "default_prior": {"P_failure": 0.02},
             "cpts": [{"mutation": "Old", "signal": "Old",
                       "table": [[0.5, 0.5], [0.5, 0.5]]}]},
            {"class": "FlakyTest", "default_prior": {"P_failure": 0.03},
             "cpts": []},
        ]
        yaml_path = tmp_path / "existing.yaml"
        with open(yaml_path, "w") as f:
            yaml.dump(existing, f)

        new_cpts = [{"mutation": "New", "signal": "New",
                     "table": [[0.8, 0.02], [0.2, 0.98]]}]
        result = build_full_yaml(new_cpts, str(yaml_path))

        # CIJob should have new CPTs
        cijob = [c for c in result if c["class"] == "CIJob"][0]
        assert cijob["cpts"] == new_cpts

        # FlakyTest should be unchanged
        flaky = [c for c in result if c["class"] == "FlakyTest"][0]
        assert flaky["default_prior"]["P_failure"] == 0.03


class TestGenerateReport:
    """Test calibration report generation."""

    def test_report_structure(self):
        records = [
            make_record(run_id=1, conclusion="failure",
                        signal_type="TestFailure"),
            make_record(run_id=2, conclusion="success"),
        ]
        rates = compute_rates(records)
        validation = {"valid": True, "spread_pp": 40.0,
                       "min_confidence": 50.0, "max_confidence": 90.0,
                       "message": "Spread 40.0pp (≥ 30pp target)"}
        report = generate_report(records, rates, validation)
        assert "# CPT Calibration Report" in report
        assert "Total runs collected: 2" in report
        assert "Failed runs: 1" in report
        assert "Per-Signal Background Rates" in report
        assert "Validation" in report


class TestCalibrationToMutationMapping:
    """Test that calibration mutation types map to valid CPT mutations."""

    def test_all_file_mutations_mapped(self):
        """All file-based mutation types should map to a CPT mutation."""
        for _, mtype in FILE_PATH_MUTATION_PATTERNS:
            assert mtype in CALIBRATION_TO_CPT_MUTATION or mtype == "Unknown", \
                f"File mutation type '{mtype}' not in CALIBRATION_TO_CPT_MUTATION"

    def test_standard_mutations_mapped(self):
        """Standard commit-based mutation types should map correctly."""
        for mtype in ["CodeChange", "DepMajorBump", "DepMinorBump",
                      "DepGroupUpdate", "Release", "Revert"]:
            assert mtype in CALIBRATION_TO_CPT_MUTATION
