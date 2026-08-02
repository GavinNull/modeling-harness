"""Generalization-first evaluation policy and release comparisons.

The scores in this module are internal diagnostics.  They intentionally have no
mapping to competition ranks, awards, or probabilities.  A hard-gate failure is
always represented separately and can never be offset by a weighted score.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from pathlib import Path
from statistics import fmean, median, pstdev
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from modeling_harness.config import (
    AGENT_BODY_PROVENANCE_POLICY,
    AGENT_VERSION,
    ConfigError,
    load_yaml,
)


PHASE_WEIGHTS: Mapping[str, int] = MappingProxyType(
    {
        "definition": 20,
        "understanding": 20,
        "creation": 30,
        "validation": 30,
    }
)
TIER_WEIGHTS: Mapping[str, float] = MappingProxyType(
    {
        "public_historical": 0.0,
        "private_hidden_variants": 0.45,
        "private_novel": 0.55,
    }
)
GATE_COMPOSITION_WEIGHTS: Mapping[str, float] = MappingProxyType(
    {
        "public_historical_tasks": 0.0,
        "hidden_variants": 0.45,
        "private_novel_tasks": 0.55,
    }
)
INTERNAL_SCORE_DISCLAIMER = (
    "Internal diagnostic only; it does not map to competition rank, "
    "award, or award probability."
)


class EvaluationConfigError(ConfigError):
    """Raised when evaluation policy files disagree or are incomplete."""


class EvaluationInputError(ValueError):
    """Raised when an evaluation observation is malformed."""


class ComparisonVerdict(str, Enum):
    """Release-oriented comparison outcomes."""

    IMPROVED = "improved"
    NON_INFERIOR = "non-inferior"
    REGRESSED = "regressed"


class ParetoRelation(str, Enum):
    """Candidate relation to baseline over the full objective vector."""

    DOMINATES = "dominates"
    EQUIVALENT = "equivalent"
    NON_DOMINATED_TRADEOFF = "non-dominated-tradeoff"
    DOMINATED = "dominated"


@dataclass(frozen=True)
class Criterion:
    criterion_id: str
    phase: str
    weight: float
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class EvaluationPolicy:
    """Semantically validated, read-only projection of the four YAML policies."""

    taxonomy: Mapping[str, Any]
    rubric: Mapping[str, Any]
    gates: Mapping[str, Any]
    benchmark_protocol: Mapping[str, Any]
    criteria: Mapping[str, Criterion]
    capability_ids: frozenset[str]
    benchmark_validity_gate_ids: tuple[str, ...]
    hard_gate_ids: tuple[str, ...]

    @property
    def phase_weights(self) -> Mapping[str, int]:
        return PHASE_WEIGHTS

    @property
    def tier_weights(self) -> Mapping[str, float]:
        return TIER_WEIGHTS

    @property
    def all_release_gate_ids(self) -> tuple[str, ...]:
        return self.benchmark_validity_gate_ids + self.hard_gate_ids


def _require_mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise EvaluationConfigError(f"{location} must be a mapping")
    return value


def _require_sequence(value: Any, location: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise EvaluationConfigError(f"{location} must be a list")
    return value


def _load_policy_file(directory: Path, name: str) -> Mapping[str, Any]:
    document = load_yaml(directory / name)
    result = _require_mapping(document, name)
    if str(result.get("schema_version")) != "2.5":
        raise EvaluationConfigError(f"{name} must declare schema_version 2.5")
    return result


def _reject_nonfinite_numbers(value: Any, location: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            _reject_nonfinite_numbers(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_nonfinite_numbers(child, f"{location}[{index}]")
    elif (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and not math.isfinite(float(value))
    ):
        raise EvaluationConfigError(f"{location} must be finite")


def _normalize_config_keys(
    mapping: Mapping[str, Any],
    *,
    aliases: Mapping[str, tuple[str, ...]],
    allowed_other: frozenset[str],
    location: str,
) -> None:
    """Normalize documented aliases while rejecting conflicts and unknown keys."""

    if not isinstance(mapping, dict):
        raise EvaluationConfigError(f"{location} must be a mutable mapping")
    alias_keys = {alias for values in aliases.values() for alias in values}
    known = set(aliases) | alias_keys | set(allowed_other)
    unknown = sorted(set(mapping) - known)
    if unknown:
        raise EvaluationConfigError(
            f"{location} contains unknown configuration key(s): "
            + ", ".join(unknown)
        )
    for canonical, alternatives in aliases.items():
        present = [key for key in (canonical, *alternatives) if key in mapping]
        if len(present) > 1:
            raise EvaluationConfigError(
                f"{location} has conflicting aliases for {canonical}: "
                + ", ".join(present)
            )
        if present and present[0] != canonical:
            mapping[canonical] = mapping.pop(present[0])


def _normalize_policy_aliases(
    gates: Mapping[str, Any], protocol: Mapping[str, Any]
) -> None:
    quality = _require_mapping(gates.get("quality_thresholds"), "gates.quality_thresholds")
    _normalize_config_keys(
        quality,
        aliases={
            "internal_total_score_min": ("internal_score_min",),
            "each_phase_percentage_min": ("phase_percentage_min",),
            "interpretation": (),
        },
        allowed_other=frozenset(),
        location="gates.quality_thresholds",
    )

    generalization = _require_mapping(
        gates.get("generalization_release_gates"),
        "gates.generalization_release_gates",
    )
    primary = _require_mapping(
        generalization.get("primary_score"),
        "gates.generalization_release_gates.primary_score",
    )
    _normalize_config_keys(
        primary,
        aliases={
            "composition": (),
            "candidate_median_min": ("candidate_primary_score_min",),
        },
        allowed_other=frozenset(),
        location="gates.generalization_release_gates.primary_score",
    )
    stability = _require_mapping(
        generalization.get("stability"),
        "gates.generalization_release_gates.stability",
    )
    _normalize_config_keys(
        stability,
        aliases={
            "minimum_runs_per_private_instance": ("minimum_private_runs",),
            "hard_gate_run_pass_rate_min": ("private_hard_gate_pass_rate_min",),
            "p10_quality_score_min": ("p10_quality_min",),
            "catastrophic_failure_rate_max": ("catastrophic_rate_max",),
        },
        allowed_other=frozenset(),
        location="gates.generalization_release_gates.stability",
    )

    tiers = _require_mapping(protocol.get("tiers"), "benchmark_protocol.tiers")
    tier_other_keys = {
        "public_historical": frozenset(
            {"confidentiality", "purpose", "interpretation"}
        ),
        "private_hidden_variants": frozenset(
            {
                "confidentiality",
                "purpose",
                "allowed_transform_families",
                "invariants_required",
            }
        ),
        "private_novel": frozenset(
            {
                "confidentiality",
                "purpose",
                "independent_authoring_required",
                "independent_validation_required",
                "public_source_copy_forbidden",
            }
        ),
    }
    for tier_id, allowed_other in tier_other_keys.items():
        tier = _require_mapping(tiers.get(tier_id), f"benchmark_protocol.tiers.{tier_id}")
        _normalize_config_keys(
            tier,
            aliases={
                "primary_generalization_weight": ("generalization_weight",),
                "minimum_runs_per_instance": ("minimum_runs",),
            },
            allowed_other=allowed_other,
            location=f"benchmark_protocol.tiers.{tier_id}",
        )


def load_evaluation_policy(directory: str | Path) -> EvaluationPolicy:
    """Load and cross-validate taxonomy, rubric, gates, and benchmark protocol."""

    root = Path(directory)
    if not root.is_dir():
        raise EvaluationConfigError(f"evaluation policy directory not found: {root}")

    taxonomy = _load_policy_file(root, "capability_taxonomy.yaml")
    rubric = _load_policy_file(root, "rubric.yaml")
    gates = _load_policy_file(root, "gates.yaml")
    protocol = _load_policy_file(root, "benchmark_protocol.yaml")
    for name, document in (
        ("capability_taxonomy.yaml", taxonomy),
        ("benchmark_protocol.yaml", protocol),
    ):
        if document.get("agent_version") != AGENT_VERSION:
            raise EvaluationConfigError(
                f"{name} must declare agent_version {AGENT_VERSION}"
            )
        if document.get("agent_body_provenance_policy") != AGENT_BODY_PROVENANCE_POLICY:
            raise EvaluationConfigError(
                f"{name}.agent_body_provenance_policy must match the canonical policy"
            )
    for name, document in (
        ("capability_taxonomy.yaml", taxonomy),
        ("rubric.yaml", rubric),
        ("gates.yaml", gates),
        ("benchmark_protocol.yaml", protocol),
    ):
        _reject_nonfinite_numbers(document, name)
    _normalize_policy_aliases(gates, protocol)

    categories = _require_mapping(taxonomy.get("categories"), "taxonomy.categories")
    if set(categories) != {"D", "U", "C", "V", "O"}:
        raise EvaluationConfigError(
            "taxonomy categories must be exactly D, U, C, V, and O"
        )
    capability_ids: set[str] = set()
    for category_id, category in categories.items():
        category_map = _require_mapping(
            category, f"taxonomy.categories.{category_id}"
        )
        capabilities = _require_mapping(
            category_map.get("capabilities"),
            f"taxonomy.categories.{category_id}.capabilities",
        )
        for capability_id in capabilities:
            if not capability_id.startswith(f"{category_id}."):
                raise EvaluationConfigError(
                    f"capability {capability_id!r} has the wrong category prefix"
                )
            if capability_id in capability_ids:
                raise EvaluationConfigError(
                    f"duplicate capability ID {capability_id!r}"
                )
            capability_ids.add(capability_id)

    phases = _require_mapping(rubric.get("phases"), "rubric.phases")
    if set(phases) != set(PHASE_WEIGHTS):
        raise EvaluationConfigError(
            "rubric phases must be exactly definition, understanding, creation, "
            "and validation"
        )
    criteria: dict[str, Criterion] = {}
    for phase_id, expected_weight in PHASE_WEIGHTS.items():
        phase = _require_mapping(phases[phase_id], f"rubric.phases.{phase_id}")
        if phase.get("weight") != expected_weight:
            raise EvaluationConfigError(
                f"{phase_id} weight must be {expected_weight}"
            )
        phase_criteria = _require_sequence(
            phase.get("criteria"), f"rubric.phases.{phase_id}.criteria"
        )
        phase_weight = 0.0
        for raw_criterion in phase_criteria:
            criterion = _require_mapping(
                raw_criterion, f"rubric.phases.{phase_id}.criteria[]"
            )
            criterion_id = criterion.get("id")
            if not isinstance(criterion_id, str) or not criterion_id:
                raise EvaluationConfigError("each rubric criterion needs a string ID")
            if criterion_id in criteria:
                raise EvaluationConfigError(
                    f"duplicate rubric criterion ID {criterion_id!r}"
                )
            weight = criterion.get("weight")
            if (
                isinstance(weight, bool)
                or not isinstance(weight, (int, float))
                or not math.isfinite(float(weight))
                or weight <= 0
            ):
                raise EvaluationConfigError(
                    f"criterion {criterion_id} must have a positive weight"
                )
            refs = _require_sequence(
                criterion.get("capability_refs"),
                f"rubric criterion {criterion_id}.capability_refs",
            )
            unknown_refs = sorted(set(refs) - capability_ids)
            if unknown_refs:
                raise EvaluationConfigError(
                    f"criterion {criterion_id} references unknown capabilities: "
                    f"{', '.join(unknown_refs)}"
                )
            evidence = _require_sequence(
                criterion.get("evidence"), f"rubric criterion {criterion_id}.evidence"
            )
            phase_weight += float(weight)
            criteria[criterion_id] = Criterion(
                criterion_id=criterion_id,
                phase=phase_id,
                weight=float(weight),
                evidence=tuple(str(item) for item in evidence),
            )
        if not math.isclose(phase_weight, expected_weight):
            raise EvaluationConfigError(
                f"{phase_id} criterion weights sum to {phase_weight}, "
                f"expected {expected_weight}"
            )

    scoring = _require_mapping(rubric.get("scoring"), "rubric.scoring")
    if scoring.get("total_max") != 100 or sum(PHASE_WEIGHTS.values()) != 100:
        raise EvaluationConfigError("the internal diagnostic scale must total 100")
    reporting = _require_mapping(rubric.get("reporting"), "rubric.reporting")
    forbidden = set(
        _require_sequence(
            reporting.get("forbidden_interpretations"),
            "rubric.reporting.forbidden_interpretations",
        )
    )
    required_forbidden = {
        "award_probability",
        "competition_rank",
        "equivalence_to_external_judging",
    }
    if not required_forbidden.issubset(forbidden):
        raise EvaluationConfigError(
            "rubric must explicitly forbid external award/rank interpretations"
        )

    status_values = set(
        _require_sequence(gates.get("status_values"), "gates.status_values")
    )
    if status_values != {
        "pass",
        "fail",
        "insufficient_evidence",
        "not_applicable",
    }:
        raise EvaluationConfigError("gate status values do not match the protocol")

    def collect_gate_ids(field: str) -> tuple[str, ...]:
        raw_gates = _require_sequence(gates.get(field), f"gates.{field}")
        ids: list[str] = []
        for raw_gate in raw_gates:
            gate = _require_mapping(raw_gate, f"gates.{field}[]")
            gate_id = gate.get("id")
            if not isinstance(gate_id, str) or not gate_id:
                raise EvaluationConfigError(f"every {field} entry needs an ID")
            ids.append(gate_id)
        if len(ids) != len(set(ids)):
            raise EvaluationConfigError(f"duplicate gate ID in {field}")
        return tuple(ids)

    benchmark_gate_ids = collect_gate_ids("benchmark_validity_gates")
    hard_gate_ids = collect_gate_ids("hard_gates")
    if set(benchmark_gate_ids) & set(hard_gate_ids):
        raise EvaluationConfigError("gate IDs must be globally unique")

    generalization = _require_mapping(
        gates.get("generalization_release_gates"),
        "gates.generalization_release_gates",
    )
    primary_score = _require_mapping(
        generalization.get("primary_score"),
        "gates.generalization_release_gates.primary_score",
    )
    gate_composition = _require_mapping(
        primary_score.get("composition"),
        "gates.generalization_release_gates.primary_score.composition",
    )
    if {
        key: float(gate_composition.get(key, -1))
        for key in GATE_COMPOSITION_WEIGHTS
    } != dict(GATE_COMPOSITION_WEIGHTS):
        raise EvaluationConfigError(
            "generalization composition must be public=0, hidden=0.45, novel=0.55"
        )

    tiers = _require_mapping(protocol.get("tiers"), "benchmark_protocol.tiers")
    if set(tiers) != set(TIER_WEIGHTS):
        raise EvaluationConfigError("benchmark protocol must define exactly three tiers")
    for tier_id, expected_weight in TIER_WEIGHTS.items():
        tier = _require_mapping(tiers[tier_id], f"benchmark_protocol.tiers.{tier_id}")
        actual_weight = tier.get("primary_generalization_weight")
        if not isinstance(actual_weight, (int, float)) or not math.isclose(
            float(actual_weight), expected_weight
        ):
            raise EvaluationConfigError(
                f"benchmark tier {tier_id} weight must be {expected_weight}"
            )

    roles = _require_mapping(protocol.get("roles"), "benchmark_protocol.roles")
    builder = _require_mapping(roles.get("builder"), "benchmark_protocol.roles.builder")
    builder_denied = set(
        _require_sequence(
            builder.get("cannot_access"),
            "benchmark_protocol.roles.builder.cannot_access",
        )
    )
    if not {
        "private_task_text",
        "private_data",
        "private_reference_artifacts",
        "per_private_task_score",
        "raw_audit_evidence",
        "grader_implementation",
    }.issubset(builder_denied):
        raise EvaluationConfigError(
            "builder access policy must deny private benchmark details and raw evidence"
        )

    execution = _require_mapping(
        protocol.get("execution"), "benchmark_protocol.execution"
    )
    isolation = _require_mapping(
        execution.get("isolation"), "benchmark_protocol.execution.isolation"
    )
    isolation_requirements = {
        "new_container_per_run": True,
        "writable_layer_reused": False,
        "raw_inputs_read_only": True,
        "other_agent_workspaces_mounted": False,
        "evaluator_inherits_author_state": False,
        "network_default": "deny",
    }
    for key, expected in isolation_requirements.items():
        if isolation.get(key) != expected:
            raise EvaluationConfigError(
                f"benchmark isolation setting {key} must be {expected!r}"
            )

    return EvaluationPolicy(
        taxonomy=MappingProxyType(dict(taxonomy)),
        rubric=MappingProxyType(dict(rubric)),
        gates=MappingProxyType(dict(gates)),
        benchmark_protocol=MappingProxyType(dict(protocol)),
        criteria=MappingProxyType(criteria),
        capability_ids=frozenset(capability_ids),
        benchmark_validity_gate_ids=benchmark_gate_ids,
        hard_gate_ids=hard_gate_ids,
    )


@dataclass(frozen=True)
class PhaseScore:
    phase: str
    score: float
    maximum: float

    @property
    def percentage(self) -> float:
        return 100.0 * self.score / self.maximum


@dataclass(frozen=True)
class DiagnosticScore:
    total_score: float
    phase_scores: Mapping[str, PhaseScore]
    quality_thresholds_pass: bool
    interpretation: str = INTERNAL_SCORE_DISCLAIMER


def score_diagnostic(
    ratings: Mapping[str, int],
    policy: EvaluationPolicy,
    *,
    evidence_present: Mapping[str, bool] | None = None,
) -> DiagnosticScore:
    """Score the fixed 20/20/30/30 rubric on its anchored zero-to-four scale."""

    expected = set(policy.criteria)
    supplied = set(ratings)
    if supplied != expected:
        missing = sorted(expected - supplied)
        unknown = sorted(supplied - expected)
        details: list[str] = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown: {', '.join(unknown)}")
        raise EvaluationInputError(
            f"ratings must cover every rubric criterion ({'; '.join(details)})"
        )

    raw_phase_scores = {phase: 0.0 for phase in PHASE_WEIGHTS}
    for criterion_id, criterion in policy.criteria.items():
        rating = ratings[criterion_id]
        if isinstance(rating, bool) or not isinstance(rating, int) or not 0 <= rating <= 4:
            raise EvaluationInputError(
                f"rating for {criterion_id} must be an integer from 0 through 4"
            )
        if evidence_present is not None and not evidence_present.get(
            criterion_id, False
        ):
            rating = min(rating, 1)
        raw_phase_scores[criterion.phase] += criterion.weight * rating / 4.0

    phase_scores = {
        phase: PhaseScore(
            phase=phase,
            score=raw_phase_scores[phase],
            maximum=float(maximum),
        )
        for phase, maximum in PHASE_WEIGHTS.items()
    }
    total = sum(item.score for item in phase_scores.values())
    thresholds = _require_mapping(
        policy.gates.get("quality_thresholds"), "gates.quality_thresholds"
    )
    total_minimum = float(thresholds["internal_total_score_min"])
    phase_minimum = float(thresholds["each_phase_percentage_min"])
    return DiagnosticScore(
        total_score=total,
        phase_scores=MappingProxyType(phase_scores),
        quality_thresholds_pass=(
            total >= total_minimum
            and all(item.percentage >= phase_minimum for item in phase_scores.values())
        ),
    )


@dataclass(frozen=True)
class GateDecision:
    passed: bool
    blocking_gate_ids: tuple[str, ...]
    insufficient_evidence_gate_ids: tuple[str, ...]
    evaluated_statuses: Mapping[str, str]


def evaluate_release_gates(
    statuses: Mapping[str, str],
    policy: EvaluationPolicy,
    *,
    approved_not_applicable: frozenset[str] = frozenset(),
) -> GateDecision:
    """Evaluate benchmark-validity and task hard gates without score compensation."""

    allowed = {
        "pass",
        "fail",
        "insufficient_evidence",
        "not_applicable",
    }
    unknown_gate_ids = sorted(set(statuses) - set(policy.all_release_gate_ids))
    if unknown_gate_ids:
        raise EvaluationInputError(
            f"unknown gate IDs: {', '.join(unknown_gate_ids)}"
        )
    unknown_statuses = sorted(set(statuses.values()) - allowed)
    if unknown_statuses:
        raise EvaluationInputError(
            f"unknown gate statuses: {', '.join(unknown_statuses)}"
        )

    normalized: dict[str, str] = {}
    blocking: list[str] = []
    insufficient: list[str] = []
    for gate_id in policy.all_release_gate_ids:
        status = statuses.get(gate_id, "insufficient_evidence")
        normalized[gate_id] = status
        if status == "insufficient_evidence":
            blocking.append(gate_id)
            insufficient.append(gate_id)
        elif status == "fail":
            blocking.append(gate_id)
        elif status == "not_applicable" and gate_id not in approved_not_applicable:
            blocking.append(gate_id)
    return GateDecision(
        passed=not blocking,
        blocking_gate_ids=tuple(blocking),
        insufficient_evidence_gate_ids=tuple(insufficient),
        evaluated_statuses=MappingProxyType(normalized),
    )


@dataclass(frozen=True)
class GeneralizationScore:
    primary_score: float
    public_reference_score: float
    hidden_variant_score: float
    private_novel_score: float
    interpretation: str = INTERNAL_SCORE_DISCLAIMER


def compose_generalization_score(
    *,
    public_historical: float,
    private_hidden_variants: float,
    private_novel: float,
) -> GeneralizationScore:
    """Compose the private generalization score; public history has zero weight."""

    values = (public_historical, private_hidden_variants, private_novel)
    if any(not math.isfinite(value) or not 0 <= value <= 100 for value in values):
        raise EvaluationInputError("all tier scores must be finite and within 0..100")
    primary = (
        TIER_WEIGHTS["private_hidden_variants"] * private_hidden_variants
        + TIER_WEIGHTS["private_novel"] * private_novel
    )
    return GeneralizationScore(
        primary_score=primary,
        public_reference_score=public_historical,
        hidden_variant_score=private_hidden_variants,
        private_novel_score=private_novel,
    )


@dataclass(frozen=True)
class ObjectiveVector:
    """Quality objectives increase; cost, latency, and intervention decrease."""

    quality: float
    stability: float
    evidence_strength: float
    cost: float
    latency: float
    human_interventions: float

    def __post_init__(self) -> None:
        values = (
            self.quality,
            self.stability,
            self.evidence_strength,
            self.cost,
            self.latency,
            self.human_interventions,
        )
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise EvaluationInputError(
                "objective values must be finite and non-negative"
            )


def pareto_relation(
    candidate: ObjectiveVector, baseline: ObjectiveVector
) -> ParetoRelation:
    """Return the strict Pareto relation without collapsing the vector to a sum."""

    candidate_values = (
        candidate.quality,
        candidate.stability,
        candidate.evidence_strength,
        -candidate.cost,
        -candidate.latency,
        -candidate.human_interventions,
    )
    baseline_values = (
        baseline.quality,
        baseline.stability,
        baseline.evidence_strength,
        -baseline.cost,
        -baseline.latency,
        -baseline.human_interventions,
    )
    no_worse = all(c >= b for c, b in zip(candidate_values, baseline_values))
    any_better = any(c > b for c, b in zip(candidate_values, baseline_values))
    no_better = all(c <= b for c, b in zip(candidate_values, baseline_values))
    any_worse = any(c < b for c, b in zip(candidate_values, baseline_values))
    if no_worse and any_better:
        return ParetoRelation.DOMINATES
    if no_better and any_worse:
        return ParetoRelation.DOMINATED
    if candidate_values == baseline_values:
        return ParetoRelation.EQUIVALENT
    return ParetoRelation.NON_DOMINATED_TRADEOFF


@dataclass(frozen=True)
class VersionComparison:
    verdict: ComparisonVerdict
    pareto: ParetoRelation
    quality_delta: float
    relative_resource_changes: Mapping[str, float]
    reasons: tuple[str, ...]


def _relative_increase(candidate: float, baseline: float) -> float:
    if baseline == 0:
        return 0.0 if candidate == 0 else math.inf
    return (candidate - baseline) / baseline


def compare_versions(
    candidate: ObjectiveVector,
    baseline: ObjectiveVector,
    *,
    candidate_hard_gates_passed: bool,
    cross_domain_non_degradation_passed: bool = True,
    unseen_non_degradation_passed: bool = True,
    absolute_budget_passed: bool = True,
    quality_non_inferiority_tolerance: float = 1.0,
    resource_increase_without_gain_limit: float = 0.10,
    quality_gain_for_tradeoff: float = 1.0,
) -> VersionComparison:
    """Compare versions with gates, non-degradation, and Pareto information."""

    thresholds = {
        "quality_non_inferiority_tolerance": quality_non_inferiority_tolerance,
        "resource_increase_without_gain_limit": resource_increase_without_gain_limit,
        "quality_gain_for_tradeoff": quality_gain_for_tradeoff,
    }
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value < 0
        for value in thresholds.values()
    ):
        raise EvaluationInputError(
            "version-comparison thresholds must be finite and non-negative"
        )
    relation = pareto_relation(candidate, baseline)
    quality_delta = candidate.quality - baseline.quality
    resource_changes = {
        "cost": _relative_increase(candidate.cost, baseline.cost),
        "latency": _relative_increase(candidate.latency, baseline.latency),
        "human_interventions": _relative_increase(
            candidate.human_interventions, baseline.human_interventions
        ),
    }
    reasons: list[str] = []

    if not candidate_hard_gates_passed:
        reasons.append("candidate has a hard-gate failure")
    if not cross_domain_non_degradation_passed:
        reasons.append("cross-domain regression gate failed")
    if not unseen_non_degradation_passed:
        reasons.append("unseen-task non-degradation gate failed")
    if not absolute_budget_passed:
        reasons.append("absolute operational budget failed")
    if quality_delta < -quality_non_inferiority_tolerance:
        reasons.append("primary quality exceeded the non-inferiority tolerance")
    if candidate.stability < baseline.stability:
        reasons.append("stability regressed")
    if candidate.evidence_strength < baseline.evidence_strength:
        reasons.append("evidence or reproducibility strength regressed")

    excessive_resources = [
        name
        for name, delta in resource_changes.items()
        if delta > resource_increase_without_gain_limit
    ]
    if excessive_resources and quality_delta < quality_gain_for_tradeoff:
        reasons.append(
            "resource use increased without the required generalization gain: "
            + ", ".join(excessive_resources)
        )

    if reasons:
        verdict = ComparisonVerdict.REGRESSED
    elif relation == ParetoRelation.DOMINATES or quality_delta >= quality_gain_for_tradeoff:
        verdict = ComparisonVerdict.IMPROVED
    else:
        verdict = ComparisonVerdict.NON_INFERIOR
    return VersionComparison(
        verdict=verdict,
        pareto=relation,
        quality_delta=quality_delta,
        relative_resource_changes=MappingProxyType(resource_changes),
        reasons=tuple(reasons),
    )


@dataclass(frozen=True)
class RunObservation:
    quality_score: float
    hard_gates_passed: bool
    catastrophic_failure: bool = False

    def __post_init__(self) -> None:
        if not math.isfinite(self.quality_score) or not 0 <= self.quality_score <= 100:
            raise EvaluationInputError("run quality must be finite and within 0..100")


@dataclass(frozen=True)
class StabilityAssessment:
    status: str
    run_count: int
    mean: float
    median: float
    standard_deviation: float
    p10: float
    worst_run: float
    hard_gate_pass_rate: float
    catastrophic_failure_rate: float

    @property
    def passed(self) -> bool:
        return self.status == "pass"


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def assess_repeated_runs(
    observations: Sequence[RunObservation],
    *,
    minimum_runs: int = 5,
    hard_gate_pass_rate_min: float = 0.90,
    p10_quality_min: float = 75.0,
    catastrophic_failure_rate_max: float = 0.0,
) -> StabilityAssessment:
    """Assess stochastic reliability, including the tail and failed runs."""

    if (
        isinstance(minimum_runs, bool)
        or not isinstance(minimum_runs, int)
        or minimum_runs < 1
    ):
        raise EvaluationInputError("minimum_runs must be positive")
    thresholds = {
        "hard_gate_pass_rate_min": hard_gate_pass_rate_min,
        "p10_quality_min": p10_quality_min,
        "catastrophic_failure_rate_max": catastrophic_failure_rate_max,
    }
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        for value in thresholds.values()
    ):
        raise EvaluationInputError("repeated-run thresholds must be finite")
    if not 0 <= hard_gate_pass_rate_min <= 1:
        raise EvaluationInputError("hard_gate_pass_rate_min must be within 0..1")
    if not 0 <= p10_quality_min <= 100:
        raise EvaluationInputError("p10_quality_min must be within 0..100")
    if not 0 <= catastrophic_failure_rate_max <= 1:
        raise EvaluationInputError(
            "catastrophic_failure_rate_max must be within 0..1"
        )
    if not observations:
        return StabilityAssessment(
            status="insufficient_evidence",
            run_count=0,
            mean=math.nan,
            median=math.nan,
            standard_deviation=math.nan,
            p10=math.nan,
            worst_run=math.nan,
            hard_gate_pass_rate=0.0,
            catastrophic_failure_rate=0.0,
        )
    qualities = [item.quality_score for item in observations]
    gate_rate = sum(item.hard_gates_passed for item in observations) / len(
        observations
    )
    catastrophic_rate = sum(
        item.catastrophic_failure for item in observations
    ) / len(observations)
    if len(observations) < minimum_runs:
        status = "insufficient_evidence"
    elif (
        gate_rate >= hard_gate_pass_rate_min
        and _percentile(qualities, 0.10) >= p10_quality_min
        and catastrophic_rate <= catastrophic_failure_rate_max
    ):
        status = "pass"
    else:
        status = "fail"
    return StabilityAssessment(
        status=status,
        run_count=len(observations),
        mean=fmean(qualities),
        median=median(qualities),
        standard_deviation=pstdev(qualities),
        p10=_percentile(qualities, 0.10),
        worst_run=min(qualities),
        hard_gate_pass_rate=gate_rate,
        catastrophic_failure_rate=catastrophic_rate,
    )


@dataclass(frozen=True)
class FamilyRegression:
    """Pre-computed paired-bootstrap evidence for one anonymized task family."""

    family_tag: str
    delta_lower_bound: float
    private_novel: bool
    unseen: bool

    def __post_init__(self) -> None:
        if not isinstance(self.family_tag, str) or not self.family_tag:
            raise EvaluationInputError("family regression tag must be non-empty")
        if (
            isinstance(self.delta_lower_bound, bool)
            or not isinstance(self.delta_lower_bound, (int, float))
            or not math.isfinite(float(self.delta_lower_bound))
        ):
            raise EvaluationInputError("family delta lower bound must be finite")
        if not isinstance(self.private_novel, bool) or not isinstance(
            self.unseen, bool
        ):
            raise EvaluationInputError("family regression flags must be boolean")


@dataclass(frozen=True)
class NonDegradationAssessment:
    status: str
    regressed_families: tuple[str, ...]
    unseen_regressed_families: tuple[str, ...]
    family_count: int
    private_novel_family_count: int

    @property
    def passed(self) -> bool:
        return self.status == "pass"


def assess_cross_domain_non_degradation(
    families: Sequence[FamilyRegression],
    *,
    overall_delta_lower_bound: float,
    minimum_family_count: int = 4,
    minimum_private_novel_family_count: int = 3,
    overall_tolerance: float = -1.0,
    family_tolerance: float = -3.0,
) -> NonDegradationAssessment:
    """Apply overall, cross-family, and unseen-task non-degradation gates."""

    finite_values = {
        "overall_delta_lower_bound": overall_delta_lower_bound,
        "overall_tolerance": overall_tolerance,
        "family_tolerance": family_tolerance,
    }
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        for value in finite_values.values()
    ):
        raise EvaluationInputError("regression bounds and tolerances must be finite")
    if (
        isinstance(minimum_family_count, bool)
        or not isinstance(minimum_family_count, int)
        or minimum_family_count < 1
        or isinstance(minimum_private_novel_family_count, bool)
        or not isinstance(minimum_private_novel_family_count, int)
        or minimum_private_novel_family_count < 1
    ):
        raise EvaluationInputError("minimum family counts must be positive integers")
    family_tags = [family.family_tag for family in families]
    if len(family_tags) != len(set(family_tags)):
        raise EvaluationInputError("family regression tags must be unique")
    novel_count = sum(family.private_novel for family in families)
    unseen_families = [family for family in families if family.unseen]
    insufficient = (
        len(families) < minimum_family_count
        or novel_count < minimum_private_novel_family_count
        or not unseen_families
    )
    regressed = tuple(
        family.family_tag
        for family in families
        if family.delta_lower_bound < family_tolerance
    )
    unseen_regressed = tuple(
        family.family_tag
        for family in unseen_families
        if family.delta_lower_bound < family_tolerance
    )
    if insufficient:
        status = "insufficient_evidence"
    elif overall_delta_lower_bound < overall_tolerance or regressed:
        status = "fail"
    else:
        status = "pass"
    return NonDegradationAssessment(
        status=status,
        regressed_families=regressed,
        unseen_regressed_families=unseen_regressed,
        family_count=len(families),
        private_novel_family_count=novel_count,
    )
