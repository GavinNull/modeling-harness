from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from modeling_harness.config import load_yaml
from modeling_harness.state_machine import (
    LedgerIntegrityError,
    LifecycleDefinition,
    RunLedger,
    TransitionEvidence,
    TransitionRejected,
)


ROOT = Path(__file__).parents[1]
H = "a" * 64


def definition() -> LifecycleDefinition:
    return LifecycleDefinition.load(
        ROOT / "workspaces/architect/state_machine.yaml",
        ROOT / "workspaces/architect/role_registry.yaml",
    )


def evidence_for(
    machine: LifecycleDefinition, state: str, event: str, digest: str = H
) -> TransitionEvidence:
    spec = machine.transition_for(state, event)
    return TransitionEvidence(
        guard_results={guard: True for guard in spec.guards},
        guard_evidence_hashes={guard: digest for guard in spec.guards},
        emitted_artifact_hashes={name: digest for name in spec.emits},
        input_manifest_hashes=(digest,),
    )


def test_loads_exactly_twenty_three_validated_transitions() -> None:
    machine = definition()
    assert len(machine.transitions) == 23
    assert machine.initial_state == "RECEIVED"
    assert machine.transition_for("RECEIVED", "intake.accepted").transition_id == (
        "TR-001"
    )
    assert machine.retry_limit == 3


def test_ledger_happy_path_key_stages_and_hash_chain_replay() -> None:
    machine = definition()
    ledger = RunLedger(machine, run_id="run-001", initial_attempt_id="attempt-001")
    route = (
        ("main_agent", "intake.accepted"),
        ("main_agent", "definition.dispatched"),
        ("problem_definition_router", "definition.submitted"),
        ("main_agent", "definition.promoted"),
        ("orchestrator", "understanding.joined"),
        ("main_agent", "understanding.promoted"),
        ("main_agent", "model_blueprint.promoted"),
        ("main_agent", "specialist_outputs.promoted"),
        ("main_agent", "compute_outputs.promoted"),
        ("visualization_report_author", "report.submitted"),
        ("main_agent", "candidate.frozen"),
        ("orchestrator", "reviews.joined"),
        ("main_agent", "reviews.approved"),
        ("main_agent", "regression.authorized"),
        ("benchmark_curator", "regression.completed"),
        ("main_agent", "release.approved"),
    )
    for actor, event in route:
        state = ledger.state
        ledger.transition(
            actor=actor,
            event=event,
            attempt_id="attempt-001",
            evidence=evidence_for(machine, state, event),
        )
    assert ledger.state == "RELEASED"
    assert len({event.event_hash for event in ledger.events}) == len(route)
    assert all(
        right.previous_event_hash == left.event_hash
        for left, right in zip(ledger.events, ledger.events[1:])
    )

    replayed = RunLedger.replay(
        machine,
        run_id="run-001",
        initial_attempt_id="attempt-001",
        events=ledger.export(),
    )
    assert replayed.state == "RELEASED"
    assert replayed.head_hash == ledger.head_hash


def test_evaluation_rejection_cannot_transition_to_agent_body_construction() -> None:
    machine = definition()
    release_review_targets = {
        spec.target for spec in machine.transitions if spec.source == "RELEASE_REVIEW"
    }
    assert release_review_targets == {"RELEASED", "RELEASE_REJECTED"}
    assert not any(
        spec.source in {"INDEPENDENT_VALIDATION", "REVIEW_RECONCILIATION", "REGRESSION_TESTING", "RELEASE_REVIEW"}
        and spec.target == "CONTROL_AUTHORIZATION_REVIEW"
        for spec in machine.transitions
    )
    protocol = load_yaml(
        ROOT / "workspaces/architect/state_machine.yaml"
    )["agent_body_modification_subprotocol"]
    assert protocol["construction_machine"]["evaluation_parent_edges"] == []
    assert protocol["release_gate_machine"]["construction_authorization_edges"] == []

    ledger = RunLedger(
        machine, run_id="run-evaluation-reject", initial_attempt_id="attempt-001"
    )
    route = (
        ("main_agent", "intake.accepted"),
        ("main_agent", "definition.dispatched"),
        ("problem_definition_router", "definition.submitted"),
        ("main_agent", "definition.promoted"),
        ("orchestrator", "understanding.joined"),
        ("main_agent", "understanding.promoted"),
        ("main_agent", "model_blueprint.promoted"),
        ("main_agent", "specialist_outputs.promoted"),
        ("main_agent", "compute_outputs.promoted"),
        ("visualization_report_author", "report.submitted"),
        ("main_agent", "candidate.frozen"),
        ("orchestrator", "reviews.joined"),
        ("main_agent", "reviews.approved"),
        ("main_agent", "regression.authorized"),
        ("benchmark_curator", "regression.completed"),
        ("main_agent", "release.rejected_for_capability_defect"),
    )
    for actor, event in route:
        state = ledger.state
        ledger.transition(
            actor=actor,
            event=event,
            attempt_id="attempt-001",
            evidence=evidence_for(machine, state, event),
        )
    assert ledger.state == "RELEASE_REJECTED"
    last = ledger.events[-1]
    assert set(dict(last.emitted_artifact_hashes)) == {
        "release_rejection_report",
        "rollback_decision",
    }


def test_illegal_actor_transition_and_missing_guard_fail_without_mutation() -> None:
    machine = definition()
    ledger = RunLedger(machine, run_id="run-001", initial_attempt_id="attempt-001")
    with pytest.raises(TransitionRejected):
        ledger.transition(
            actor="problem_definition_router",
            event="intake.accepted",
            attempt_id="attempt-001",
            evidence=evidence_for(machine, "RECEIVED", "intake.accepted"),
        )
    assert ledger.state == "RECEIVED"
    assert ledger.events == ()

    complete = evidence_for(machine, "RECEIVED", "intake.accepted")
    missing_guard = dict(complete.guard_results)
    missing_guard.pop(next(iter(missing_guard)))
    with pytest.raises(TransitionRejected):
        ledger.transition(
            actor="main_agent",
            event="intake.accepted",
            attempt_id="attempt-001",
            evidence=TransitionEvidence(
                guard_results=missing_guard,
                guard_evidence_hashes=complete.guard_evidence_hashes,
                emitted_artifact_hashes=complete.emitted_artifact_hashes,
                input_manifest_hashes=complete.input_manifest_hashes,
            ),
        )
    assert ledger.state == "RECEIVED"


def test_bad_evidence_hash_and_tampered_replay_are_rejected() -> None:
    machine = definition()
    ledger = RunLedger(machine, run_id="run-001", initial_attempt_id="attempt-001")
    complete = evidence_for(machine, "RECEIVED", "intake.accepted")
    corrupted_hashes = dict(complete.guard_evidence_hashes)
    corrupted_hashes[next(iter(corrupted_hashes))] = "not-a-hash"
    with pytest.raises(TransitionRejected):
        ledger.transition(
            actor="main_agent",
            event="intake.accepted",
            attempt_id="attempt-001",
            evidence=TransitionEvidence(
                guard_results=complete.guard_results,
                guard_evidence_hashes=corrupted_hashes,
                emitted_artifact_hashes=complete.emitted_artifact_hashes,
            ),
        )

    ledger.transition(
        actor="main_agent",
        event="intake.accepted",
        attempt_id="attempt-001",
        evidence=complete,
    )
    exported = [deepcopy(item) for item in ledger.export()]
    exported[0]["actor"] = "orchestrator"
    with pytest.raises(LedgerIntegrityError):
        RunLedger.replay(
            machine,
            run_id="run-001",
            initial_attempt_id="attempt-001",
            events=exported,
        )


def test_recoverable_quarantine_cancel_and_validation_rollback_are_constrained() -> None:
    machine = definition()
    ledger = RunLedger(machine, run_id="run-001", initial_attempt_id="attempt-001")
    for actor, event in (
        ("main_agent", "intake.accepted"),
        ("main_agent", "definition.dispatched"),
    ):
        state = ledger.state
        ledger.transition(
            actor=actor,
            event=event,
            attempt_id="attempt-001",
            evidence=evidence_for(machine, state, event),
        )
    ledger.fail_recoverable(
        actor="orchestrator",
        trigger="timeout",
        attempt_id="attempt-001",
        failure_event_hash=H,
    )
    assert ledger.state == "FAILED_RECOVERABLE"
    ledger.transition(
        actor="main_agent",
        event="recovery.retry_authorized",
        attempt_id="attempt-002",
        evidence=evidence_for(
            machine, "FAILED_RECOVERABLE", "recovery.retry_authorized"
        ),
    )
    assert ledger.state == "INTAKE_VALIDATED"

    cancelled = RunLedger(
        machine, run_id="run-cancel", initial_attempt_id="attempt-001"
    )
    cancelled.cancel(
        actor="main_agent",
        attempt_id="attempt-001",
        cancellation_hash=H,
    )
    assert cancelled.state == "CANCELLED"

    quarantined = RunLedger(
        machine, run_id="run-quarantine", initial_attempt_id="attempt-001"
    )
    quarantined.quarantine(
        actor="orchestrator",
        trigger="hash_mismatch",
        attempt_id="attempt-001",
        incident_hash=H,
    )
    assert quarantined.state == "QUARANTINED"

    with pytest.raises(TransitionRejected):
        ledger.rollback_validation(
            actor="main_agent",
            prior_attempt_id="attempt-001",
            new_attempt_id="attempt-003",
            authorization_hash=H,
        )
