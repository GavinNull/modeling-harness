from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import sqlite3

import pytest

from modeling_harness.benchmarks import (
    BenchmarkEntry,
    BenchmarkManifest,
    BenchmarkPolicyError,
    BenchmarkRun,
    BenchmarkTier,
    ContaminationRisk,
    aggregate_benchmark_runs,
    benchmark_task_id,
    mark_benchmark_exposed,
    validate_benchmark_manifest,
)
from modeling_harness.evaluation import load_evaluation_policy
from modeling_harness.runtime import ExecutionAttestationLedger, ExecutionRecord


POLICY_DIRECTORY = (
    Path(__file__).resolve().parents[1] / "workspaces/evaluation"
)
HASH = "a" * 64
FAMILIES = (
    "mechanistic_and_dynamical",
    "statistical_and_predictive",
    "optimization_and_decision",
    "spatial_network_and_geometric",
)
CONTENT_BY_TASK = {
    "public-regression-a": "a" * 64,
    "private-111111111111": "b" * 64,
    "private-222222222222": "c" * 64,
    "private-333333333333": "d" * 64,
    "private-444444444444": "e" * 64,
    "private-555555555555": "f" * 64,
}


@pytest.fixture(scope="module")
def policy():
    return load_evaluation_policy(POLICY_DIRECTORY)


def _manifest(*, hidden_builder_visible: bool = False) -> BenchmarkManifest:
    return BenchmarkManifest(
        manifest_id="manifest-generic",
        entries=(
            BenchmarkEntry(
                task_key="public-regression-a",
                tier=BenchmarkTier.PUBLIC_HISTORICAL,
                family=FAMILIES[0],
                content_sha256=HASH,
                minimum_runs=3,
                builder_visible=True,
                contamination_risk=ContaminationRisk.HIGH,
            ),
            BenchmarkEntry(
                task_key="private-111111111111",
                tier=BenchmarkTier.PRIVATE_HIDDEN_VARIANTS,
                family=FAMILIES[1],
                content_sha256="b" * 64,
                minimum_runs=5,
                builder_visible=hidden_builder_visible,
                contamination_risk=ContaminationRisk.MEDIUM,
            ),
            BenchmarkEntry(
                task_key="private-222222222222",
                tier=BenchmarkTier.PRIVATE_HIDDEN_VARIANTS,
                family=FAMILIES[2],
                content_sha256="c" * 64,
                minimum_runs=5,
                builder_visible=False,
                contamination_risk=ContaminationRisk.MEDIUM,
            ),
            BenchmarkEntry(
                task_key="private-333333333333",
                tier=BenchmarkTier.PRIVATE_NOVEL,
                family=FAMILIES[0],
                content_sha256="d" * 64,
                minimum_runs=5,
                builder_visible=False,
                contamination_risk=ContaminationRisk.LOWEST_NOT_ZERO,
            ),
            BenchmarkEntry(
                task_key="private-444444444444",
                tier=BenchmarkTier.PRIVATE_NOVEL,
                family=FAMILIES[2],
                content_sha256="e" * 64,
                minimum_runs=5,
                builder_visible=False,
                contamination_risk=ContaminationRisk.LOWEST_NOT_ZERO,
            ),
            BenchmarkEntry(
                task_key="private-555555555555",
                tier=BenchmarkTier.PRIVATE_NOVEL,
                family=FAMILIES[3],
                content_sha256="f" * 64,
                minimum_runs=5,
                builder_visible=False,
                contamination_risk=ContaminationRisk.LOWEST_NOT_ZERO,
            ),
        ),
    )


def test_manifest_preserves_private_feedback_firewall(policy) -> None:
    manifest = _manifest()
    validation = validate_benchmark_manifest(manifest, policy)
    builder_view = manifest.builder_view()

    assert validation.valid
    assert validation.release_eligible
    assert builder_view["private_aggregate"]["details_withheld"] is True
    rendered = repr(builder_view)
    assert "private-111111111111" not in rendered
    assert "private-222222222222" not in rendered
    assert "private-333333333333" not in rendered
    assert "public-regression-a" in rendered


def test_hidden_task_cannot_be_builder_visible(policy) -> None:
    with pytest.raises(BenchmarkPolicyError, match="builder-visible"):
        validate_benchmark_manifest(
            _manifest(hidden_builder_visible=True), policy
        )


def test_private_exposure_retires_task_and_blocks_release(policy) -> None:
    exposed = mark_benchmark_exposed(
        _manifest(),
        "private-111111111111",
        "private_text_seen_by_builder",
        policy,
    )
    entry = exposed.entry("private-111111111111")
    validation = validate_benchmark_manifest(exposed, policy)

    assert entry.retired
    assert entry.contamination_risk is ContaminationRisk.EXPOSED
    assert not validation.release_eligible


def _runs(
    task_key: str,
    count: int,
    quality: float,
    *,
    gate_pass: bool = True,
    system_version: str = "candidate-v2",
) -> list[BenchmarkRun]:
    runs: list[BenchmarkRun] = []
    for index in range(count):
        identity = f"{system_version}-{task_key}-{index}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        task_id = benchmark_task_id(
            task_key=task_key,
            task_content_sha256=CONTENT_BY_TASK[task_key],
            system_version=system_version,
            manifest_id="manifest-generic",
            manifest_version=1,
        )
        runs.append(
            BenchmarkRun(
                task_key=task_key,
                quality_score=quality,
                hard_gates_passed=gate_pass,
                execution_id=f"execution-{digest}",
                attestation_hash=hashlib.sha256(
                    f"attestation-{identity}".encode("utf-8")
                ).hexdigest(),
                production_attested=False,
                task_id=task_id,
                run_id=f"run-{identity}",
                attempt_id=f"attempt-{identity}",
                seed=index,
                seed_policy="paired-fixed",
                container_id=f"container-{identity}",
                session_id=f"session-{identity}",
                container_attestation_sha256=hashlib.sha256(
                    f"container-attestation-{identity}".encode("utf-8")
                ).hexdigest(),
                session_attestation_sha256=hashlib.sha256(
                    f"session-attestation-{identity}".encode("utf-8")
                ).hexdigest(),
                result_sha256=digest,
                plan_sha256=hashlib.sha256(
                    f"plan-{identity}".encode("utf-8")
                ).hexdigest(),
                task_packet_sha256=hashlib.sha256(
                    f"task-packet-{identity}".encode("utf-8")
                ).hexdigest(),
                manifest_id="manifest-generic",
                manifest_version=1,
                task_content_sha256=CONTENT_BY_TASK[task_key],
                system_version=system_version,
            )
        )
    return runs


def _baseline_for(runs: list[BenchmarkRun]) -> list[BenchmarkRun]:
    baseline: list[BenchmarkRun] = []
    for run in runs:
        identity = f"baseline-v1-{run.task_key}-{run.seed}"
        identity_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        baseline.append(
            replace(
                run,
                system_version="baseline-v1",
                execution_id=f"execution-{identity_hash}",
                attestation_hash=hashlib.sha256(
                    f"attestation-{identity}".encode("utf-8")
                ).hexdigest(),
                production_attested=False,
                task_id=benchmark_task_id(
                    task_key=run.task_key,
                    task_content_sha256=run.task_content_sha256,
                    system_version="baseline-v1",
                    manifest_id=run.manifest_id,
                    manifest_version=run.manifest_version,
                ),
                run_id=f"run-{identity}",
                attempt_id=f"attempt-{identity}",
                container_id=f"container-{identity}",
                session_id=f"session-{identity}",
                container_attestation_sha256=hashlib.sha256(
                    f"container-attestation-{identity}".encode("utf-8")
                ).hexdigest(),
                session_attestation_sha256=hashlib.sha256(
                    f"session-attestation-{identity}".encode("utf-8")
                ).hexdigest(),
                result_sha256=identity_hash,
                plan_sha256=hashlib.sha256(
                    f"plan-{identity}".encode("utf-8")
                ).hexdigest(),
                task_packet_sha256=hashlib.sha256(
                    f"task-packet-{identity}".encode("utf-8")
                ).hexdigest(),
            )
        )
    return baseline


def _execution_record(
    run: BenchmarkRun,
    *,
    backend_name: str = "docker-strict",
    status: str = "succeeded",
    run_id: str | None = None,
) -> ExecutionRecord:
    return ExecutionRecord(
        execution_id=run.execution_id,
        status=status,
        run_id=run_id or run.run_id,
        task_id=run.task_id,
        attempt_id=run.attempt_id,
        session_id=run.session_id,
        backend_name=backend_name,
        container_name=run.container_id,
        workspace="D:/isolated/workspace",
        started_at="2026-01-02T03:04:05Z",
        completed_at="2026-01-02T03:04:06Z",
        duration_seconds=1.0,
        return_code=0,
        timed_out=False,
        cancelled=False,
        command_sha256="1" * 64,
        plan_sha256=run.plan_sha256,
        task_packet_sha256=run.task_packet_sha256,
        result_packet_sha256=run.result_sha256,
        stdout_sha256="2" * 64,
        stderr_sha256="3" * 64,
        stdout_path="D:/isolated/stdout.bin",
        stderr_path="D:/isolated/stderr.bin",
        result_packet_path="D:/isolated/result.json",
        cleanup_actions=("docker-rm-via-run---rm",),
        workspace_preserved=True,
    )


def _attest_runs(
    ledger: ExecutionAttestationLedger,
    runs: list[BenchmarkRun],
    *,
    backend_name: str = "docker-strict",
    first_run_id: str | None = None,
) -> list[BenchmarkRun]:
    attested: list[BenchmarkRun] = []
    for index, run in enumerate(runs):
        receipt = ledger.append(
            _execution_record(
                run,
                backend_name=backend_name,
                run_id=first_run_id if index == 0 else None,
            )
        )
        attested.append(
            replace(
                run,
                attestation_hash=receipt.attestation_hash,
                production_attested=True,
            )
        )
    return attested


def test_public_success_cannot_mask_hidden_failure(policy) -> None:
    runs = [
        *_runs("public-regression-a", 3, 100),
        *_runs("private-111111111111", 5, 40, gate_pass=False),
        *_runs("private-222222222222", 5, 40, gate_pass=False),
        *_runs("private-333333333333", 5, 90),
        *_runs("private-444444444444", 5, 90),
        *_runs("private-555555555555", 5, 90),
    ]

    result = aggregate_benchmark_runs(
        _manifest(), runs, policy, baseline_runs=_baseline_for(runs)
    )

    assert result.generalization is not None
    assert result.generalization.public_reference_score == 100
    assert result.generalization.primary_score == pytest.approx(67.5)
    assert not result.private_hard_gate_pass
    assert not result.release_ready


def test_repeated_run_shortfall_is_insufficient_evidence(policy) -> None:
    runs = [
        *_runs("public-regression-a", 3, 90),
        *_runs("private-111111111111", 4, 90),
        *_runs("private-222222222222", 5, 90),
        *_runs("private-333333333333", 5, 90),
        *_runs("private-444444444444", 5, 90),
        *_runs("private-555555555555", 5, 90),
    ]

    result = aggregate_benchmark_runs(
        _manifest(), runs, policy, baseline_runs=_baseline_for(runs)
    )

    assert result.status == "insufficient_evidence"
    assert not result.release_ready


def test_private_quality_can_pass_without_using_public_as_primary(
    policy, tmp_path: Path
) -> None:
    runs = [
        *_runs("public-regression-a", 3, 20),
        *_runs("private-111111111111", 5, 90),
        *_runs("private-222222222222", 5, 90),
        *_runs("private-333333333333", 5, 90),
        *_runs("private-444444444444", 5, 90),
        *_runs("private-555555555555", 5, 90),
    ]
    baseline = _baseline_for(runs)
    ledger = ExecutionAttestationLedger(tmp_path / "attestations.sqlite3")
    all_attested = _attest_runs(ledger, [*runs, *baseline])
    runs = all_attested[: len(runs)]
    baseline = all_attested[len(runs) :]

    result = aggregate_benchmark_runs(
        _manifest(),
        runs,
        policy,
        baseline_runs=baseline,
        attestation_ledger=ledger,
    )

    assert result.generalization is not None
    assert result.generalization.primary_score == pytest.approx(90)
    assert result.tier_scores["public_historical"] == 20
    assert result.public_reference_only
    assert result.pairing_verified
    assert result.execution_attestations_verified
    assert result.release_ready
    ledger.close()


@pytest.mark.parametrize(
    "field_name",
    ["run_id", "attempt_id", "container_id", "result_sha256"],
)
def test_aggregate_rejects_duplicate_run_evidence(policy, field_name: str) -> None:
    runs = _runs("public-regression-a", 2, 90)
    runs[1] = replace(runs[1], **{field_name: getattr(runs[0], field_name)})

    with pytest.raises(BenchmarkPolicyError, match=f"duplicate {field_name}"):
        aggregate_benchmark_runs(_manifest(), runs, policy)


def test_repeated_runs_require_unique_seeds(policy) -> None:
    runs = _runs("public-regression-a", 2, 90)
    runs[1] = replace(runs[1], seed=runs[0].seed)

    with pytest.raises(BenchmarkPolicyError, match="unique seeds"):
        aggregate_benchmark_runs(_manifest(), runs, policy)


def test_candidate_and_baseline_must_use_identical_seed_sets(policy) -> None:
    runs = [
        *_runs("public-regression-a", 3, 90),
        *_runs("private-111111111111", 5, 90),
        *_runs("private-222222222222", 5, 90),
        *_runs("private-333333333333", 5, 90),
        *_runs("private-444444444444", 5, 90),
        *_runs("private-555555555555", 5, 90),
    ]
    baseline = _baseline_for(runs)
    baseline[0] = replace(baseline[0], seed=999)

    with pytest.raises(BenchmarkPolicyError, match="identical seed sets"):
        aggregate_benchmark_runs(
            _manifest(), runs, policy, baseline_runs=baseline
        )


@pytest.mark.parametrize(
    ("field_name", "invalid"),
    [
        ("manifest_id", "manifest-other"),
        ("manifest_version", 2),
        ("task_content_sha256", "9" * 64),
    ],
)
def test_run_must_bind_manifest_version_and_task_content(
    policy, field_name: str, invalid: object
) -> None:
    run = _runs("private-111111111111", 1, 90)[0]
    run = replace(run, **{field_name: invalid})

    with pytest.raises(BenchmarkPolicyError, match="manifest|content"):
        aggregate_benchmark_runs(_manifest(), [run], policy)


def test_private_run_builder_view_leaks_no_identifier_score_or_metadata() -> None:
    run = _runs("private-111111111111", 1, 90)[0]

    rendered = repr(run.builder_view(private=True))

    assert "private-111111111111" not in rendered
    assert "90" not in rendered
    assert "run-" not in rendered
    assert "metadata" not in rendered
    with pytest.raises(TypeError):
        BenchmarkRun(**{**run.__dict__, "metadata": {"private": "forbidden"}})


def test_private_identifiers_are_redacted_from_aggregate_outputs(policy) -> None:
    runs = [
        *_runs("public-regression-a", 3, 90),
        *_runs("private-111111111111", 4, 90),
        *_runs("private-222222222222", 5, 90),
        *_runs("private-333333333333", 5, 90),
        *_runs("private-444444444444", 5, 90),
        *_runs("private-555555555555", 5, 90),
    ]
    result = aggregate_benchmark_runs(
        _manifest(), runs, policy, baseline_runs=_baseline_for(runs)
    )

    rendered = repr(result)
    assert "private-111111111111" not in rendered
    assert set(result.stability_by_task) == {"public-regression-a"}


def test_release_requires_verified_baseline_pairing(policy) -> None:
    runs = [
        *_runs("public-regression-a", 3, 90),
        *_runs("private-111111111111", 5, 90),
        *_runs("private-222222222222", 5, 90),
        *_runs("private-333333333333", 5, 90),
        *_runs("private-444444444444", 5, 90),
        *_runs("private-555555555555", 5, 90),
    ]

    result = aggregate_benchmark_runs(_manifest(), runs, policy)

    assert result.pairing_verified is False
    assert result.release_ready is False
    assert any("paired baseline" in reason for reason in result.blocking_reasons)


def test_random_unique_run_hashes_without_ledger_cannot_release(policy) -> None:
    runs = [
        *_runs("public-regression-a", 3, 90),
        *_runs("private-111111111111", 5, 90),
        *_runs("private-222222222222", 5, 90),
        *_runs("private-333333333333", 5, 90),
        *_runs("private-444444444444", 5, 90),
        *_runs("private-555555555555", 5, 90),
    ]
    assert len(runs) == 28

    result = aggregate_benchmark_runs(
        _manifest(), runs, policy, baseline_runs=_baseline_for(runs)
    )

    assert not result.release_ready
    assert not result.execution_attestations_verified
    assert any(
        "attestation ledger" in reason for reason in result.blocking_reasons
    )


def test_in_memory_attestation_ledger_cannot_release(policy) -> None:
    run = _runs("public-regression-a", 1, 90)[0]
    ledger = ExecutionAttestationLedger(":memory:")
    attested = _attest_runs(ledger, [run])

    result = aggregate_benchmark_runs(
        _manifest(), attested, policy, attestation_ledger=ledger
    )

    assert not result.release_ready
    assert not result.execution_attestations_verified
    assert any("persistent" in reason for reason in result.blocking_reasons)
    ledger.close()


def test_arbitrary_attestation_verifier_is_rejected(policy) -> None:
    with pytest.raises(BenchmarkPolicyError, match="ExecutionAttestationLedger"):
        aggregate_benchmark_runs(
            _manifest(),
            _runs("public-regression-a", 1, 90),
            policy,
            attestation_ledger=lambda _: True,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("fake_ledger", [True, False])
def test_boolean_attestation_verifier_is_rejected(
    policy, fake_ledger: bool
) -> None:
    with pytest.raises(BenchmarkPolicyError, match="ExecutionAttestationLedger"):
        aggregate_benchmark_runs(
            _manifest(),
            _runs("public-regression-a", 1, 90),
            policy,
            attestation_ledger=fake_ledger,  # type: ignore[arg-type]
        )


def test_cross_run_attestation_payload_is_rejected(
    policy, tmp_path: Path
) -> None:
    run = _runs("public-regression-a", 1, 90)[0]
    ledger = ExecutionAttestationLedger(tmp_path / "cross-run.sqlite3")
    attested = _attest_runs(ledger, [run], first_run_id="run-other")

    result = aggregate_benchmark_runs(
        _manifest(), attested, policy, attestation_ledger=ledger
    )

    assert not result.execution_attestations_verified
    assert any("binding mismatch" in reason for reason in result.blocking_reasons)
    ledger.close()


def test_local_backend_attestation_is_rejected(policy, tmp_path: Path) -> None:
    run = _runs("public-regression-a", 1, 90)[0]
    ledger = ExecutionAttestationLedger(tmp_path / "local.sqlite3")
    attested = _attest_runs(ledger, [run], backend_name="local-test-only")

    result = aggregate_benchmark_runs(
        _manifest(), attested, policy, attestation_ledger=ledger
    )

    assert not result.execution_attestations_verified
    assert any(
        "production attestation" in reason for reason in result.blocking_reasons
    )
    ledger.close()


def test_broken_persistent_attestation_chain_is_rejected(
    policy, tmp_path: Path
) -> None:
    path = tmp_path / "broken.sqlite3"
    ledger = ExecutionAttestationLedger(path)
    attested = _attest_runs(
        ledger, _runs("public-regression-a", 2, 90)
    )
    ledger.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER execution_attestation_no_update")
        connection.execute(
            "UPDATE execution_attestation SET previous_hash = ? "
            "WHERE sequence = 2",
            ("9" * 64,),
        )
        connection.commit()
    reopened = ExecutionAttestationLedger(path)

    result = aggregate_benchmark_runs(
        _manifest(), attested, policy, attestation_ledger=reopened
    )

    assert not result.execution_attestations_verified
    assert any(
        "chain validation failed" in reason for reason in result.blocking_reasons
    )
    reopened.close()


def test_baseline_runs_are_each_required_in_persistent_ledger(
    policy, tmp_path: Path
) -> None:
    runs = [
        *_runs("public-regression-a", 3, 90),
        *_runs("private-111111111111", 5, 90),
        *_runs("private-222222222222", 5, 90),
        *_runs("private-333333333333", 5, 90),
        *_runs("private-444444444444", 5, 90),
        *_runs("private-555555555555", 5, 90),
    ]
    baseline = _baseline_for(runs)
    ledger = ExecutionAttestationLedger(tmp_path / "candidate-only.sqlite3")
    candidates = _attest_runs(ledger, runs)

    result = aggregate_benchmark_runs(
        _manifest(),
        candidates,
        policy,
        baseline_runs=baseline,
        attestation_ledger=ledger,
    )

    assert not result.release_ready
    assert not result.execution_attestations_verified
    assert any("missing" in reason for reason in result.blocking_reasons)
    ledger.close()


def test_benchmark_run_requires_execution_attestation_identifiers() -> None:
    run = _runs("public-regression-a", 1, 90)[0]

    with pytest.raises(BenchmarkPolicyError, match="execution_id"):
        replace(run, execution_id="")
    with pytest.raises(BenchmarkPolicyError, match="attestation_hash"):
        replace(run, attestation_hash="")
