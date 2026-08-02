"""Three-tier benchmark manifests with a builder-facing feedback firewall."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from statistics import fmean
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from modeling_harness.evaluation import (
    EvaluationInputError,
    EvaluationPolicy,
    GeneralizationScore,
    RunObservation,
    StabilityAssessment,
    assess_repeated_runs,
    compose_generalization_score,
)
from modeling_harness.runtime import ExecutionAttestationLedger


SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
PRIVATE_KEY_PATTERN = re.compile(r"^private-[a-f0-9]{12}$")
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")
SEED_POLICIES = frozenset({"fixed-set", "paired-fixed"})


class BenchmarkPolicyError(EvaluationInputError):
    """Raised when a manifest would violate benchmark confidentiality."""


def benchmark_task_id(
    *,
    task_key: str,
    task_content_sha256: str,
    system_version: str,
    manifest_id: str,
    manifest_version: int,
) -> str:
    """Return the task ID that commits an execution to benchmark context."""

    payload = {
        "manifest_id": manifest_id,
        "manifest_version": manifest_version,
        "system_version": system_version,
        "task_content_sha256": task_content_sha256,
        "task_key": task_key,
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return f"benchmark-{digest}"


class BenchmarkTier(str, Enum):
    PUBLIC_HISTORICAL = "public_historical"
    PRIVATE_HIDDEN_VARIANTS = "private_hidden_variants"
    PRIVATE_NOVEL = "private_novel"

    @property
    def is_private(self) -> bool:
        return self is not BenchmarkTier.PUBLIC_HISTORICAL


class ContaminationRisk(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOWEST_NOT_ZERO = "lowest_not_zero"
    EXPOSED = "exposed"


EXPECTED_RISK: Mapping[BenchmarkTier, ContaminationRisk] = MappingProxyType(
    {
        BenchmarkTier.PUBLIC_HISTORICAL: ContaminationRisk.HIGH,
        BenchmarkTier.PRIVATE_HIDDEN_VARIANTS: ContaminationRisk.MEDIUM,
        BenchmarkTier.PRIVATE_NOVEL: ContaminationRisk.LOWEST_NOT_ZERO,
    }
)

EXPOSURE_EVENTS = frozenset(
    {
        "private_text_seen_by_builder",
        "private_reference_seen_by_builder",
        "grader_logic_seen_by_builder",
        "raw_evidence_with_identifying_details_seen_by_builder",
        "canary_found_outside_authorized_store",
    }
)


@dataclass(frozen=True)
class BenchmarkEntry:
    """Metadata only; private task text, data, references, and graders are excluded."""

    task_key: str
    tier: BenchmarkTier
    family: str
    content_sha256: str
    minimum_runs: int
    builder_visible: bool
    contamination_risk: ContaminationRisk
    exposure_events: tuple[str, ...] = ()
    retired: bool = False


@dataclass(frozen=True)
class BenchmarkManifest:
    manifest_id: str
    entries: tuple[BenchmarkEntry, ...]
    manifest_version: int = 1

    def entry(self, task_key: str) -> BenchmarkEntry:
        for entry in self.entries:
            if entry.task_key == task_key:
                return entry
        raise KeyError(f"unknown benchmark task key {task_key!r}")

    def builder_view(self) -> Mapping[str, Any]:
        """Return public details and private aggregates, never private identifiers."""

        public = [
            {
                "task_key": entry.task_key,
                "family": entry.family,
                "contamination_risk": entry.contamination_risk.value,
                "retired": entry.retired,
            }
            for entry in self.entries
            if entry.tier is BenchmarkTier.PUBLIC_HISTORICAL
        ]
        private_entries = [entry for entry in self.entries if entry.tier.is_private]
        counts = Counter(entry.tier.value for entry in private_entries)
        private_families = len({entry.family for entry in private_entries})
        return MappingProxyType(
            {
                "manifest_id": self.manifest_id,
                "public_tasks": tuple(public),
                "private_aggregate": MappingProxyType(
                    {
                        "tier_counts": MappingProxyType(dict(counts)),
                        "family_count": private_families,
                        "details_withheld": True,
                    }
                ),
            }
        )


@dataclass(frozen=True)
class ManifestValidation:
    valid: bool
    release_eligible: bool
    active_task_count: int
    retired_task_count: int
    blocking_reasons: tuple[str, ...]


def _allowed_families(policy: EvaluationPolicy) -> frozenset[str]:
    protocol = policy.benchmark_protocol
    families = protocol.get("task_family_strata")
    if not isinstance(families, list):
        raise BenchmarkPolicyError("benchmark policy task families are malformed")
    return frozenset(str(family) for family in families)


def validate_benchmark_manifest(
    manifest: BenchmarkManifest, policy: EvaluationPolicy
) -> ManifestValidation:
    """Validate metadata, confidentiality, minimum runs, and contamination state."""

    if not manifest.manifest_id:
        raise BenchmarkPolicyError("manifest_id must be non-empty")
    if (
        isinstance(manifest.manifest_version, bool)
        or not isinstance(manifest.manifest_version, int)
        or manifest.manifest_version < 1
    ):
        raise BenchmarkPolicyError("manifest_version must be a positive integer")
    keys = [entry.task_key for entry in manifest.entries]
    if len(keys) != len(set(keys)):
        raise BenchmarkPolicyError("benchmark task keys must be unique")
    allowed_families = _allowed_families(policy)
    tiers = policy.benchmark_protocol["tiers"]
    blocking: list[str] = []
    retired = 0

    for entry in manifest.entries:
        if not isinstance(entry.tier, BenchmarkTier):
            raise BenchmarkPolicyError("benchmark tier must be a BenchmarkTier")
        if not SHA256_PATTERN.fullmatch(entry.content_sha256):
            raise BenchmarkPolicyError(
                f"{entry.task_key}: content_sha256 must be a lowercase SHA-256"
            )
        if entry.family not in allowed_families:
            raise BenchmarkPolicyError(
                f"{entry.task_key}: unknown task-family stratum"
            )
        required_runs = int(tiers[entry.tier.value]["minimum_runs_per_instance"])
        if (
            isinstance(entry.minimum_runs, bool)
            or not isinstance(entry.minimum_runs, int)
            or entry.minimum_runs < 1
        ):
            raise BenchmarkPolicyError(
                f"{entry.task_key}: minimum_runs must be a positive integer"
            )
        if entry.minimum_runs < required_runs:
            raise BenchmarkPolicyError(
                f"{entry.task_key}: minimum_runs must be at least {required_runs}"
            )
        if entry.tier.is_private:
            if not PRIVATE_KEY_PATTERN.fullmatch(entry.task_key):
                raise BenchmarkPolicyError(
                    "private benchmark keys must be opaque private-<12 hex> values"
                )
            if entry.builder_visible:
                raise BenchmarkPolicyError(
                    f"{entry.task_key}: private benchmark metadata cannot be "
                    "builder-visible"
                )
        if entry.contamination_risk not in {
            EXPECTED_RISK[entry.tier],
            ContaminationRisk.EXPOSED,
        }:
            raise BenchmarkPolicyError(
                f"{entry.task_key}: contamination risk does not match its tier"
            )
        unknown_events = set(entry.exposure_events) - EXPOSURE_EVENTS
        if unknown_events:
            raise BenchmarkPolicyError(
                f"{entry.task_key}: unknown exposure event(s)"
            )
        if entry.exposure_events and (
            entry.contamination_risk is not ContaminationRisk.EXPOSED
            or not entry.retired
        ):
            raise BenchmarkPolicyError(
                f"{entry.task_key}: exposed tasks must be marked exposed and retired"
            )
        if entry.contamination_risk is ContaminationRisk.EXPOSED:
            retired += 1
            blocking.append(
                f"{entry.task_key}: exposed task is excluded pending reserve replacement"
            )

    active_entries = [entry for entry in manifest.entries if not entry.retired]
    coverage = policy.benchmark_protocol.get("minimum_family_coverage")
    if not isinstance(coverage, dict):
        raise BenchmarkPolicyError("benchmark family-coverage policy is malformed")
    total_family_count = len({entry.family for entry in active_entries})
    novel_family_count = len(
        {
            entry.family
            for entry in active_entries
            if entry.tier is BenchmarkTier.PRIVATE_NOVEL
        }
    )
    if total_family_count < int(coverage["release_total"]):
        blocking.append("independent task-family coverage is insufficient")
    if novel_family_count < int(coverage["private_novel"]):
        blocking.append("private-novel task-family coverage is insufficient")
    if active_entries:
        maximum_share = max(Counter(entry.family for entry in active_entries).values())
        maximum_share /= len(active_entries)
        if maximum_share > float(coverage["no_single_family_share_above"]):
            blocking.append("one task family exceeds the maximum benchmark share")

    return ManifestValidation(
        valid=True,
        release_eligible=not blocking,
        active_task_count=len(manifest.entries) - retired,
        retired_task_count=retired,
        blocking_reasons=tuple(blocking),
    )


def mark_benchmark_exposed(
    manifest: BenchmarkManifest,
    task_key: str,
    event: str,
    policy: EvaluationPolicy,
) -> BenchmarkManifest:
    """Retire a private benchmark after a recognized exposure event."""

    if event not in EXPOSURE_EVENTS:
        raise BenchmarkPolicyError(f"unknown benchmark exposure event {event!r}")
    original = manifest.entry(task_key)
    if not original.tier.is_private:
        raise BenchmarkPolicyError(
            "private exposure events may only be attached to private tasks"
        )
    events = tuple(dict.fromkeys((*original.exposure_events, event)))
    replacement = replace(
        original,
        contamination_risk=ContaminationRisk.EXPOSED,
        exposure_events=events,
        retired=True,
    )
    updated = BenchmarkManifest(
        manifest_id=manifest.manifest_id,
        manifest_version=manifest.manifest_version + 1,
        entries=tuple(
            replacement if entry.task_key == task_key else entry
            for entry in manifest.entries
        ),
    )
    validate_benchmark_manifest(updated, policy)
    return updated


@dataclass(frozen=True)
class BenchmarkRun:
    task_key: str
    quality_score: float
    hard_gates_passed: bool
    execution_id: str
    attestation_hash: str
    production_attested: bool
    task_id: str
    run_id: str
    attempt_id: str
    seed: int
    seed_policy: str
    container_id: str
    session_id: str
    container_attestation_sha256: str
    session_attestation_sha256: str
    result_sha256: str
    plan_sha256: str
    task_packet_sha256: str
    manifest_id: str
    manifest_version: int
    task_content_sha256: str
    system_version: str
    catastrophic_failure: bool = False

    def __post_init__(self) -> None:
        if not math.isfinite(self.quality_score) or not 0 <= self.quality_score <= 100:
            raise BenchmarkPolicyError(
                "benchmark run quality must be finite and within 0..100"
            )
        for field_name in (
            "execution_id",
            "task_id",
            "run_id",
            "attempt_id",
            "container_id",
            "session_id",
            "manifest_id",
            "system_version",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not IDENTIFIER_PATTERN.fullmatch(value):
                raise BenchmarkPolicyError(f"benchmark run has invalid {field_name}")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise BenchmarkPolicyError("benchmark run seed must be a non-negative integer")
        if self.seed_policy not in SEED_POLICIES:
            raise BenchmarkPolicyError("benchmark run has an unknown seed_policy")
        for field_name in (
            "attestation_hash",
            "container_attestation_sha256",
            "session_attestation_sha256",
            "result_sha256",
            "plan_sha256",
            "task_packet_sha256",
            "task_content_sha256",
        ):
            if not SHA256_PATTERN.fullmatch(getattr(self, field_name)):
                raise BenchmarkPolicyError(
                    f"benchmark run {field_name} must be a lowercase SHA-256"
                )
        if (
            isinstance(self.manifest_version, bool)
            or not isinstance(self.manifest_version, int)
            or self.manifest_version < 1
        ):
            raise BenchmarkPolicyError(
                "benchmark run manifest_version must be a positive integer"
            )
        if not isinstance(self.hard_gates_passed, bool) or not isinstance(
            self.catastrophic_failure, bool
        ):
            raise BenchmarkPolicyError("benchmark run outcome flags must be boolean")
        if not isinstance(self.production_attested, bool):
            raise BenchmarkPolicyError(
                "benchmark run production_attested must be boolean"
            )

    def builder_view(self, *, private: bool) -> Mapping[str, Any]:
        """Return a leak-safe view; private per-run details are never exposed."""

        if private:
            return MappingProxyType({"private": True, "details_withheld": True})
        return MappingProxyType(
            {
                "task_key": self.task_key,
                "quality_score": self.quality_score,
                "hard_gates_passed": self.hard_gates_passed,
                "system_version": self.system_version,
            }
        )


@dataclass(frozen=True)
class BenchmarkAggregate:
    status: str
    tier_scores: Mapping[str, float]
    generalization: GeneralizationScore | None
    stability_by_task: Mapping[str, StabilityAssessment]
    public_reference_only: bool
    private_hard_gate_pass: bool
    pairing_verified: bool
    execution_attestations_verified: bool
    release_ready: bool
    blocking_reasons: tuple[str, ...]


def _validate_run_bindings(
    manifest: BenchmarkManifest, runs: Sequence[BenchmarkRun]
) -> None:
    entries = {entry.task_key: entry for entry in manifest.entries}
    for run in runs:
        if run.manifest_id != manifest.manifest_id:
            raise BenchmarkPolicyError("run is bound to a different benchmark manifest")
        if run.manifest_version != manifest.manifest_version:
            raise BenchmarkPolicyError("run is bound to a different manifest version")
        try:
            entry = entries[run.task_key]
        except KeyError as exc:
            raise BenchmarkPolicyError("run references an unknown opaque task key") from exc
        if run.task_content_sha256 != entry.content_sha256:
            raise BenchmarkPolicyError("run task content hash does not match the manifest")
        expected_task_id = benchmark_task_id(
            task_key=run.task_key,
            task_content_sha256=run.task_content_sha256,
            system_version=run.system_version,
            manifest_id=run.manifest_id,
            manifest_version=run.manifest_version,
        )
        if run.task_id != expected_task_id:
            raise BenchmarkPolicyError(
                "run task_id does not bind task, system, and manifest context"
            )
        if entry.retired:
            raise BenchmarkPolicyError("runs for retired benchmark tasks are forbidden")


def _validate_unique_run_evidence(runs: Sequence[BenchmarkRun]) -> None:
    fields = (
        "run_id",
        "attempt_id",
        "execution_id",
        "attestation_hash",
        "container_id",
        "session_id",
        "container_attestation_sha256",
        "session_attestation_sha256",
        "result_sha256",
    )
    for field_name in fields:
        values = [getattr(run, field_name) for run in runs]
        if any(count > 1 for count in Counter(values).values()):
            raise BenchmarkPolicyError(
                f"benchmark runs contain duplicate {field_name}"
            )


def _load_verified_attestation_rows(
    ledger: ExecutionAttestationLedger,
) -> dict[str, tuple[Mapping[str, Any], str]]:
    """Read and independently verify the durable SQLite hash chain."""

    if type(ledger) is not ExecutionAttestationLedger:
        raise BenchmarkPolicyError(
            "attestation_ledger must be an ExecutionAttestationLedger"
        )
    if ledger.durable is not True or ledger.path == ":memory:":
        raise BenchmarkPolicyError(
            "release requires a persistent execution attestation ledger"
        )
    path = Path(ledger.path)
    if not path.is_file():
        raise BenchmarkPolicyError(
            "persistent execution attestation ledger is unavailable"
        )
    try:
        ExecutionAttestationLedger.verify(ledger)
        uri = f"{path.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            rows = connection.execute(
                """
                SELECT sequence, execution_id, previous_hash,
                       payload_json, attestation_hash
                FROM execution_attestation
                ORDER BY sequence
                """
            ).fetchall()
    except Exception as exc:
        raise BenchmarkPolicyError(
            "execution attestation ledger chain validation failed"
        ) from exc

    verified: dict[str, tuple[Mapping[str, Any], str]] = {}
    previous_hash = "0" * 64
    for expected_sequence, row in enumerate(rows, start=1):
        sequence, execution_id, previous, payload_json, attestation_hash = row
        try:
            payload = json.loads(payload_json)
            encoded = json.dumps(
                {
                    "sequence": sequence,
                    "previous_hash": previous,
                    "payload": payload,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise BenchmarkPolicyError(
                "execution attestation ledger contains malformed payload"
            ) from exc
        actual_hash = hashlib.sha256(encoded).hexdigest()
        if (
            sequence != expected_sequence
            or previous != previous_hash
            or not isinstance(payload, dict)
            or payload.get("execution_id") != execution_id
            or actual_hash != attestation_hash
        ):
            raise BenchmarkPolicyError(
                "execution attestation ledger chain validation failed"
            )
        verified[str(execution_id)] = (payload, str(attestation_hash))
        previous_hash = str(attestation_hash)
    return verified


def _validate_execution_attestations(
    runs: Sequence[BenchmarkRun],
    ledger: ExecutionAttestationLedger | None,
) -> tuple[bool, str | None]:
    if ledger is None:
        return False, "persistent execution attestation ledger is required"
    if type(ledger) is not ExecutionAttestationLedger:
        raise BenchmarkPolicyError(
            "attestation_ledger must be an ExecutionAttestationLedger"
        )
    try:
        rows = _load_verified_attestation_rows(ledger)
    except BenchmarkPolicyError as exc:
        return False, str(exc)

    for run in runs:
        row = rows.get(run.execution_id)
        if row is None:
            return False, "benchmark execution is missing from the attestation ledger"
        payload, attestation_hash = row
        expected = {
            "execution_id": run.execution_id,
            "task_id": run.task_id,
            "run_id": run.run_id,
            "attempt_id": run.attempt_id,
            "container_name": run.container_id,
            "session_id": run.session_id,
            "result_packet_sha256": run.result_sha256,
            "plan_sha256": run.plan_sha256,
            "task_packet_sha256": run.task_packet_sha256,
        }
        if any(payload.get(field) != value for field, value in expected.items()):
            return False, "benchmark execution attestation binding mismatch"
        if (
            payload.get("status") != "succeeded"
            or payload.get("backend_name") != "docker-strict"
            or payload.get("return_code") != 0
            or payload.get("timed_out") is not False
            or payload.get("cancelled") is not False
            or run.production_attested is not True
            or attestation_hash != run.attestation_hash
        ):
            return False, "benchmark execution lacks a successful production attestation"
    return bool(runs), None


def _validate_unique_seeds_within_versions(
    runs: Sequence[BenchmarkRun],
) -> None:
    grouped: dict[tuple[str, str], list[int]] = defaultdict(list)
    for run in runs:
        grouped[(run.task_key, run.system_version)].append(run.seed)
    for seeds in grouped.values():
        if len(seeds) != len(set(seeds)):
            raise BenchmarkPolicyError(
                "repeated runs require unique seeds within each task and version"
            )


def _validate_seed_pairing(
    manifest: BenchmarkManifest,
    candidate_runs: Sequence[BenchmarkRun],
    baseline_runs: Sequence[BenchmarkRun],
) -> bool:
    if not baseline_runs:
        return False
    candidate_versions = {run.system_version for run in candidate_runs}
    baseline_versions = {run.system_version for run in baseline_runs}
    if len(candidate_versions) != 1 or len(baseline_versions) != 1:
        raise BenchmarkPolicyError(
            "candidate and baseline runs must each use exactly one system_version"
        )
    if candidate_versions == baseline_versions:
        raise BenchmarkPolicyError("candidate and baseline versions must be distinct")
    if any(
        run.seed_policy != "paired-fixed"
        for run in (*candidate_runs, *baseline_runs)
    ):
        raise BenchmarkPolicyError("paired comparisons require paired-fixed seed_policy")

    for entry in manifest.entries:
        if entry.retired:
            continue
        candidate_seeds = {
            run.seed for run in candidate_runs if run.task_key == entry.task_key
        }
        baseline_seeds = {
            run.seed for run in baseline_runs if run.task_key == entry.task_key
        }
        if candidate_seeds != baseline_seeds:
            raise BenchmarkPolicyError(
                "candidate and baseline must use identical seed sets per task"
            )
    return True


def aggregate_benchmark_runs(
    manifest: BenchmarkManifest,
    runs: Sequence[BenchmarkRun],
    policy: EvaluationPolicy,
    *,
    baseline_runs: Sequence[BenchmarkRun] = (),
    attestation_ledger: ExecutionAttestationLedger | None = None,
) -> BenchmarkAggregate:
    """Aggregate custodian-side results while keeping public history non-primary."""

    manifest_validation = validate_benchmark_manifest(manifest, policy)
    candidate_runs = tuple(runs)
    paired_baseline_runs = tuple(baseline_runs)
    all_runs = (*candidate_runs, *paired_baseline_runs)
    _validate_run_bindings(manifest, all_runs)
    _validate_unique_run_evidence(all_runs)
    _validate_unique_seeds_within_versions(all_runs)
    attestations_verified, attestation_problem = _validate_execution_attestations(
        all_runs, attestation_ledger
    )
    pairing_verified = _validate_seed_pairing(
        manifest, candidate_runs, paired_baseline_runs
    )
    known_keys = {entry.task_key for entry in manifest.entries}
    unknown_keys = sorted({run.task_key for run in all_runs} - known_keys)
    if unknown_keys:
        raise BenchmarkPolicyError(
            f"runs reference unknown task keys: {', '.join(unknown_keys)}"
        )

    grouped: dict[str, list[BenchmarkRun]] = defaultdict(list)
    for run in candidate_runs:
        grouped[run.task_key].append(run)
    task_stability: dict[str, StabilityAssessment] = {}
    task_scores: dict[BenchmarkTier, list[float]] = defaultdict(list)
    blocking = [
        re.sub(r"private-[a-f0-9]{12}", "private benchmark", reason)
        for reason in manifest_validation.blocking_reasons
    ]
    if not pairing_verified:
        blocking.append("paired baseline runs with identical seed sets are required")
    if attestation_problem is not None:
        blocking.append(attestation_problem)
    private_gate_outcomes: list[bool] = []

    for entry in manifest.entries:
        if entry.retired:
            continue
        task_runs = grouped.get(entry.task_key, [])
        observations = [
            RunObservation(
                quality_score=run.quality_score,
                hard_gates_passed=run.hard_gates_passed,
                catastrophic_failure=run.catastrophic_failure,
            )
            for run in task_runs
        ]
        assessment = assess_repeated_runs(
            observations,
            minimum_runs=entry.minimum_runs,
            # Historical quality is reference-only.  Its runs still have to
            # execute, pass hard gates, and avoid catastrophic failures.
            p10_quality_min=(
                0.0
                if entry.tier is BenchmarkTier.PUBLIC_HISTORICAL
                else 75.0
            ),
        )
        if not entry.tier.is_private:
            task_stability[entry.task_key] = assessment
        if assessment.status == "insufficient_evidence":
            blocking.append(
                "private benchmark: insufficient repeated runs"
                if entry.tier.is_private
                else f"{entry.task_key}: insufficient repeated runs"
            )
        elif assessment.status == "fail":
            blocking.append(
                "private benchmark: repeated-run stability failed"
                if entry.tier.is_private
                else f"{entry.task_key}: repeated-run stability failed"
            )
        if observations:
            task_scores[entry.tier].append(fmean(run.quality_score for run in task_runs))
        if entry.tier.is_private:
            private_gate_outcomes.extend(run.hard_gates_passed for run in task_runs)

    required_tiers = (
        BenchmarkTier.PUBLIC_HISTORICAL,
        BenchmarkTier.PRIVATE_HIDDEN_VARIANTS,
        BenchmarkTier.PRIVATE_NOVEL,
    )
    missing_tiers = [tier.value for tier in required_tiers if not task_scores[tier]]
    if missing_tiers:
        blocking.append("missing active results for tier(s): " + ", ".join(missing_tiers))
    tier_score_values = {
        tier.value: fmean(task_scores[tier]) if task_scores[tier] else 0.0
        for tier in required_tiers
    }
    generalization: GeneralizationScore | None
    if missing_tiers:
        generalization = None
    else:
        generalization = compose_generalization_score(
            public_historical=tier_score_values[
                BenchmarkTier.PUBLIC_HISTORICAL.value
            ],
            private_hidden_variants=tier_score_values[
                BenchmarkTier.PRIVATE_HIDDEN_VARIANTS.value
            ],
            private_novel=tier_score_values[BenchmarkTier.PRIVATE_NOVEL.value],
        )

    stability_policy = policy.gates["generalization_release_gates"]["stability"]
    private_pass_rate = (
        sum(private_gate_outcomes) / len(private_gate_outcomes)
        if private_gate_outcomes
        else 0.0
    )
    private_hard_gate_pass = (
        bool(private_gate_outcomes)
        and private_pass_rate
        >= float(stability_policy["hard_gate_run_pass_rate_min"])
    )
    if not private_hard_gate_pass:
        blocking.append("private benchmark hard-gate pass rate failed")

    primary_minimum = float(
        policy.gates["generalization_release_gates"]["primary_score"][
            "candidate_median_min"
        ]
    )
    if generalization is not None and generalization.primary_score < primary_minimum:
        blocking.append("private primary generalization threshold failed")

    release_ready = not blocking
    return BenchmarkAggregate(
        status="pass" if release_ready else "insufficient_evidence"
        if any("insufficient" in reason or "missing" in reason for reason in blocking)
        else "fail",
        tier_scores=MappingProxyType(tier_score_values),
        generalization=generalization,
        stability_by_task=MappingProxyType(task_stability),
        public_reference_only=True,
        private_hard_gate_pass=private_hard_gate_pass,
        pairing_verified=pairing_verified,
        execution_attestations_verified=attestations_verified,
        release_ready=release_ready,
        blocking_reasons=tuple(blocking),
    )
