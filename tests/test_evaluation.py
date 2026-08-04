from __future__ import annotations

from pathlib import Path
import shutil

import pytest
import yaml

from modeling_harness.evaluation import (
    ComparisonVerdict,
    EvaluationConfigError,
    EvaluationInputError,
    FamilyRegression,
    ObjectiveVector,
    ParetoRelation,
    RunObservation,
    assess_cross_domain_non_degradation,
    assess_repeated_runs,
    compare_versions,
    compose_generalization_score,
    evaluate_release_gates,
    load_evaluation_policy,
    score_diagnostic,
)


POLICY_DIRECTORY = (
    Path(__file__).resolve().parents[1] / "workspaces/evaluation"
)


@pytest.fixture(scope="module")
def policy():
    return load_evaluation_policy(POLICY_DIRECTORY)


def test_policy_loads_fixed_phases_and_cross_validates_weights(policy) -> None:
    assert dict(policy.phase_weights) == {
        "definition": 20,
        "understanding": 20,
        "creation": 30,
        "validation": 30,
    }
    assert dict(policy.tier_weights) == {
        "public_historical": 0.0,
        "private_hidden_variants": 0.45,
        "private_novel": 0.55,
    }
    assert len(policy.hard_gate_ids) == 12
    assert "O.ISOLATION.ENFORCE" in policy.capability_ids


def test_inconsistent_rubric_weight_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "evaluation"
    shutil.copytree(POLICY_DIRECTORY, target)
    rubric_path = target / "rubric.yaml"
    rubric = yaml.safe_load(rubric_path.read_text(encoding="utf-8"))
    rubric["phases"]["definition"]["weight"] = 19
    rubric_path.write_text(
        yaml.safe_dump(rubric, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(EvaluationConfigError):
        load_evaluation_policy(target)


def test_diagnostic_score_uses_fixed_100_point_scale(policy) -> None:
    ratings = {criterion_id: 4 for criterion_id in policy.criteria}
    result = score_diagnostic(ratings, policy)

    assert result.total_score == pytest.approx(100)
    assert {
        name: phase.score for name, phase in result.phase_scores.items()
    } == {
        "definition": 20,
        "understanding": 20,
        "creation": 30,
        "validation": 30,
    }
    assert result.quality_thresholds_pass


def test_missing_evidence_caps_a_criterion_at_rating_one(policy) -> None:
    ratings = {criterion_id: 4 for criterion_id in policy.criteria}
    evidence = {criterion_id: True for criterion_id in policy.criteria}
    criterion_id = next(iter(policy.criteria))
    evidence[criterion_id] = False

    result = score_diagnostic(ratings, policy, evidence_present=evidence)

    criterion = policy.criteria[criterion_id]
    expected_deduction = criterion.weight * 3 / 4
    assert result.total_score == pytest.approx(100 - expected_deduction)


def test_hard_gate_failure_cannot_be_offset_by_quality(policy) -> None:
    statuses = {gate_id: "pass" for gate_id in policy.all_release_gate_ids}
    statuses[policy.hard_gate_ids[0]] = "fail"

    decision = evaluate_release_gates(statuses, policy)

    assert not decision.passed
    assert decision.blocking_gate_ids == (policy.hard_gate_ids[0],)


def test_missing_gate_is_insufficient_evidence(policy) -> None:
    decision = evaluate_release_gates({}, policy)

    assert not decision.passed
    assert set(decision.insufficient_evidence_gate_ids) == set(
        policy.all_release_gate_ids
    )


def test_public_score_is_reference_only() -> None:
    low_public = compose_generalization_score(
        public_historical=10,
        private_hidden_variants=80,
        private_novel=90,
    )
    high_public = compose_generalization_score(
        public_historical=100,
        private_hidden_variants=80,
        private_novel=90,
    )

    assert low_public.primary_score == pytest.approx(85.5)
    assert high_public.primary_score == low_public.primary_score
    assert high_public.public_reference_score == 100


def test_pareto_improvement_and_generic_quality_gain() -> None:
    baseline = ObjectiveVector(85, 90, 90, 10, 20, 2)
    candidate = ObjectiveVector(87, 91, 91, 9, 18, 1)

    result = compare_versions(
        candidate, baseline, candidate_hard_gates_passed=True
    )

    assert result.verdict is ComparisonVerdict.IMPROVED
    assert result.pareto is ParetoRelation.DOMINATES


def test_resource_regression_without_quality_gain_is_rejected() -> None:
    baseline = ObjectiveVector(86, 90, 90, 10, 20, 2)
    candidate = ObjectiveVector(86.5, 90, 90, 13, 23, 3)

    result = compare_versions(
        candidate, baseline, candidate_hard_gates_passed=True
    )

    assert result.verdict is ComparisonVerdict.REGRESSED
    assert result.pareto is ParetoRelation.NON_DOMINATED_TRADEOFF
    assert set(result.relative_resource_changes) == {
        "cost",
        "latency",
        "human_interventions",
    }


def test_hard_gate_failure_forces_regression_even_when_vector_is_better() -> None:
    baseline = ObjectiveVector(80, 80, 80, 10, 10, 2)
    candidate = ObjectiveVector(95, 95, 95, 5, 5, 1)

    result = compare_versions(
        candidate, baseline, candidate_hard_gates_passed=False
    )

    assert result.verdict is ComparisonVerdict.REGRESSED


def test_repeated_runs_measure_tail_and_catastrophic_failures() -> None:
    stable = [
        RunObservation(score, True)
        for score in (84, 86, 88, 90, 92)
    ]
    unstable = stable[:-1] + [RunObservation(20, False, True)]

    assert assess_repeated_runs(stable).passed
    failed = assess_repeated_runs(unstable)
    assert failed.status == "fail"
    assert failed.catastrophic_failure_rate > 0
    assert failed.worst_run == 20


def test_cross_domain_and_unseen_non_degradation() -> None:
    families = [
        FamilyRegression("family-a", -0.5, True, True),
        FamilyRegression("family-b", 0.0, True, True),
        FamilyRegression("family-c", 0.5, True, True),
        FamilyRegression("family-d", -1.0, False, False),
    ]
    passed = assess_cross_domain_non_degradation(
        families, overall_delta_lower_bound=-0.5
    )
    regressed = assess_cross_domain_non_degradation(
        [
            *families[:2],
            FamilyRegression("family-c", -3.5, True, True),
            families[3],
        ],
        overall_delta_lower_bound=-0.5,
    )

    assert passed.passed
    assert regressed.status == "fail"
    assert regressed.unseen_regressed_families == ("family-c",)


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf")])
def test_family_and_overall_regression_bounds_must_be_finite(invalid: float) -> None:
    with pytest.raises(EvaluationInputError):
        FamilyRegression("family-invalid", invalid, True, True)

    families = [
        FamilyRegression(f"family-{index}", 0.0, index < 3, index < 3)
        for index in range(4)
    ]
    with pytest.raises(EvaluationInputError):
        assess_cross_domain_non_degradation(
            families,
            overall_delta_lower_bound=invalid,
        )
    with pytest.raises(EvaluationInputError):
        assess_cross_domain_non_degradation(
            families,
            overall_delta_lower_bound=0.0,
            family_tolerance=invalid,
        )


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf")])
def test_runtime_thresholds_must_be_finite(invalid: float) -> None:
    observations = [RunObservation(90, True) for _ in range(5)]
    with pytest.raises(EvaluationInputError):
        assess_repeated_runs(observations, p10_quality_min=invalid)

    baseline = ObjectiveVector(80, 80, 80, 10, 10, 1)
    candidate = ObjectiveVector(81, 81, 81, 10, 10, 1)
    with pytest.raises(EvaluationInputError):
        compare_versions(
            candidate,
            baseline,
            candidate_hard_gates_passed=True,
            quality_gain_for_tradeoff=invalid,
        )


def _copied_policy(tmp_path: Path) -> Path:
    target = tmp_path / "evaluation"
    shutil.copytree(POLICY_DIRECTORY, target)
    return target


def test_known_config_alias_is_normalized_without_silent_mismatch(
    tmp_path: Path,
) -> None:
    target = _copied_policy(tmp_path)
    gates_path = target / "gates.yaml"
    gates = yaml.safe_load(gates_path.read_text(encoding="utf-8"))
    primary = gates["generalization_release_gates"]["primary_score"]
    primary["candidate_primary_score_min"] = primary.pop("candidate_median_min")
    gates_path.write_text(
        yaml.safe_dump(gates, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    loaded = load_evaluation_policy(target)

    normalized = loaded.gates["generalization_release_gates"]["primary_score"]
    assert normalized["candidate_median_min"] == 85
    assert "candidate_primary_score_min" not in normalized


def test_conflicting_or_unknown_config_aliases_fail_closed(tmp_path: Path) -> None:
    conflict = _copied_policy(tmp_path / "conflict")
    gates_path = conflict / "gates.yaml"
    gates = yaml.safe_load(gates_path.read_text(encoding="utf-8"))
    gates["generalization_release_gates"]["primary_score"][
        "candidate_primary_score_min"
    ] = 84
    gates_path.write_text(
        yaml.safe_dump(gates, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    with pytest.raises(EvaluationConfigError):
        load_evaluation_policy(conflict)

    unknown = _copied_policy(tmp_path / "unknown")
    unknown_path = unknown / "gates.yaml"
    gates = yaml.safe_load(unknown_path.read_text(encoding="utf-8"))
    gates["generalization_release_gates"]["stability"]["p10_quality_typo"] = 75
    unknown_path.write_text(
        yaml.safe_dump(gates, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    with pytest.raises(EvaluationConfigError):
        load_evaluation_policy(unknown)


def test_nonfinite_policy_threshold_is_rejected(tmp_path: Path) -> None:
    target = _copied_policy(tmp_path)
    gates_path = target / "gates.yaml"
    gates = yaml.safe_load(gates_path.read_text(encoding="utf-8"))
    gates["generalization_release_gates"]["stability"][
        "p10_quality_score_min"
    ] = float("nan")
    gates_path.write_text(
        yaml.safe_dump(gates, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(EvaluationConfigError):
        load_evaluation_policy(target)
