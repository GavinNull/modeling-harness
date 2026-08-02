"""Fail-closed lifecycle state machine with an append-only hash-chained ledger.

The normal lifecycle is loaded from ``workspaces/architect/state_machine.yaml``.
No caller can invent a forward edge: actor, event, source state, guards, emitted
evidence and every recorded digest are checked before the state is changed.
Recovery, quarantine and cancellation use the error policies in the same YAML
document.  A single audited validation-revision rollback exists because the
approved configuration defines revision edges for definition, understanding and
creation, but not for validation; it is deliberately narrower than a normal
transition and is never treated as a twenty-fourth forward edge.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Literal

from modeling_harness.config import (
    AGENT_BODY_PROVENANCE_POLICY,
    AGENT_VERSION,
    ConfigError,
    load_yaml,
)


SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
TRANSITION_ID_RE = re.compile(r"^TR-[0-9]{3}$")
EVENT_HASH_GENESIS = "0" * 64
EXPECTED_NORMAL_TRANSITIONS = 23


class StateMachineError(ValueError):
    """Base class for lifecycle definition and execution failures."""


class StateMachineConfigError(ConfigError, StateMachineError):
    """Raised when the YAML lifecycle definition is inconsistent."""


class TransitionRejected(StateMachineError):
    """Raised when a requested state change is not fully authorized."""


class LedgerIntegrityError(StateMachineError):
    """Raised when replay detects a modified, reordered or invented event."""


def _require_sha256(value: Any, description: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise TransitionRejected(f"{description} must be a lowercase SHA-256")
    return value


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise StateMachineError(f"ledger value is not canonical JSON: {exc}") from exc
    return encoded.encode("utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _string_tuple(value: Any, location: str, *, nonempty: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise StateMachineConfigError(f"{location} must be a list of non-empty strings")
    result = tuple(value)
    if nonempty and not result:
        raise StateMachineConfigError(f"{location} cannot be empty")
    if len(result) != len(set(result)):
        raise StateMachineConfigError(f"{location} contains duplicates")
    return result


@dataclass(frozen=True)
class TransitionSpec:
    transition_id: str
    source: str
    target: str
    actor: str
    event: str
    guards: tuple[str, ...]
    emits: tuple[str, ...]


@dataclass(frozen=True)
class ErrorPolicy:
    source_states: frozenset[str] | Literal["*", "* except terminal"]
    target: str
    triggers: frozenset[str]

    def accepts_source(self, state: str, terminal_states: frozenset[str]) -> bool:
        if self.source_states == "*":
            return True
        if self.source_states == "* except terminal":
            return state not in terminal_states
        return state in self.source_states


@dataclass(frozen=True)
class LifecycleDefinition:
    """Validated immutable projection of the lifecycle YAML."""

    machine_id: str
    initial_state: str
    terminal_states: frozenset[str]
    states: frozenset[str]
    transitions: tuple[TransitionSpec, ...]
    recoverable: ErrorPolicy
    security: ErrorPolicy
    cancellation: ErrorPolicy
    retry_limit: int
    actors: frozenset[str]
    _by_source_event: Mapping[tuple[str, str], TransitionSpec]

    @classmethod
    def load(
        cls,
        state_machine_path: str | Path,
        role_registry_path: str | Path,
    ) -> "LifecycleDefinition":
        document = load_yaml(state_machine_path)
        roles_document = load_yaml(role_registry_path)
        if not isinstance(document, dict):
            raise StateMachineConfigError("state machine YAML must be a mapping")
        if not isinstance(roles_document, dict):
            raise StateMachineConfigError("role registry YAML must be a mapping")
        for name, projection in (
            ("state machine", document),
            ("role registry", roles_document),
        ):
            if projection.get("agent_version") != AGENT_VERSION:
                raise StateMachineConfigError(
                    f"{name} must target {AGENT_VERSION}"
                )
            if projection.get("agent_body_provenance_policy") != (
                AGENT_BODY_PROVENANCE_POLICY
            ):
                raise StateMachineConfigError(
                    f"{name} does not enforce the canonical Agent-body "
                    "source-provenance policy"
                )

        role_entries = roles_document.get("roles")
        if not isinstance(role_entries, list) or not all(
            isinstance(entry, dict) for entry in role_entries
        ):
            raise StateMachineConfigError("role registry must contain a roles list")
        role_ids = [entry.get("id") for entry in role_entries]
        if not all(isinstance(role_id, str) and role_id for role_id in role_ids):
            raise StateMachineConfigError("role registry contains an invalid role id")
        if len(role_ids) != len(set(role_ids)):
            raise StateMachineConfigError("role registry contains duplicate role ids")
        actors = frozenset((*role_ids, "orchestrator", "ingress_service", "user"))

        states_document = document.get("states")
        if not isinstance(states_document, dict) or not states_document:
            raise StateMachineConfigError("states must be a non-empty mapping")
        states = frozenset(states_document)
        initial = document.get("initial_state")
        if initial not in states:
            raise StateMachineConfigError("initial_state is not declared")
        terminal = _string_tuple(
            document.get("terminal_states"), "terminal_states", nonempty=True
        )
        if not set(terminal) <= states:
            raise StateMachineConfigError("terminal_states contains an unknown state")

        for state_id, state_value in states_document.items():
            if not isinstance(state_value, dict):
                raise StateMachineConfigError(f"state {state_id} must be a mapping")
            required = state_value.get("required_artifacts")
            _string_tuple(
                required,
                f"states.{state_id}.required_artifacts",
                nonempty=False,
            )
            writers = _string_tuple(
                state_value.get("allowed_writers"),
                f"states.{state_id}.allowed_writers",
                nonempty=False,
            )
            unknown_writers = set(writers) - actors
            if unknown_writers:
                raise StateMachineConfigError(
                    f"state {state_id} has unknown writer(s): "
                    f"{', '.join(sorted(unknown_writers))}"
                )

        raw_transitions = document.get("transitions")
        if not isinstance(raw_transitions, list):
            raise StateMachineConfigError("transitions must be a list")
        if len(raw_transitions) != EXPECTED_NORMAL_TRANSITIONS:
            raise StateMachineConfigError(
                "approved lifecycle must contain exactly "
                f"{EXPECTED_NORMAL_TRANSITIONS} normal transitions"
            )
        transitions: list[TransitionSpec] = []
        transition_ids: set[str] = set()
        keys: set[tuple[str, str]] = set()
        for index, raw in enumerate(raw_transitions):
            if not isinstance(raw, dict):
                raise StateMachineConfigError(f"transitions[{index}] must be a mapping")
            transition_id = raw.get("id")
            source = raw.get("from")
            target = raw.get("to")
            actor = raw.get("actor")
            event = raw.get("event")
            if not isinstance(transition_id, str) or not TRANSITION_ID_RE.fullmatch(
                transition_id
            ):
                raise StateMachineConfigError(
                    f"transitions[{index}].id must match TR-NNN"
                )
            if transition_id in transition_ids:
                raise StateMachineConfigError(f"duplicate transition {transition_id}")
            transition_ids.add(transition_id)
            if source not in states or target not in states:
                raise StateMachineConfigError(
                    f"{transition_id} references an unknown state"
                )
            if actor not in actors:
                raise StateMachineConfigError(
                    f"{transition_id} references unknown actor {actor!r}"
                )
            if not isinstance(event, str) or not event.strip():
                raise StateMachineConfigError(f"{transition_id} has no event")
            key = (source, event)
            if key in keys:
                raise StateMachineConfigError(
                    f"ambiguous transition from {source} on {event}"
                )
            keys.add(key)
            transitions.append(
                TransitionSpec(
                    transition_id=transition_id,
                    source=source,
                    target=target,
                    actor=actor,
                    event=event,
                    guards=_string_tuple(
                        raw.get("guards"),
                        f"{transition_id}.guards",
                        nonempty=True,
                    ),
                    emits=_string_tuple(
                        raw.get("emits"),
                        f"{transition_id}.emits",
                        nonempty=True,
                    ),
                )
            )

        error_document = document.get("error_transitions")
        if not isinstance(error_document, dict):
            raise StateMachineConfigError("error_transitions must be a mapping")

        def parse_error(name: str) -> ErrorPolicy:
            raw = error_document.get(name)
            if not isinstance(raw, dict):
                raise StateMachineConfigError(f"error_transitions.{name} is missing")
            source_raw = raw.get("from_states")
            if isinstance(source_raw, str) and source_raw in {
                "*",
                "* except terminal",
            }:
                source: frozenset[str] | Literal["*", "* except terminal"] = source_raw
            elif (
                isinstance(source_raw, list)
                and len(source_raw) == 1
                and source_raw[0] in {"*", "* except terminal"}
            ):
                source = source_raw[0]
            else:
                source_values = _string_tuple(
                    source_raw,
                    f"error_transitions.{name}.from_states",
                    nonempty=True,
                )
                if not set(source_values) <= states:
                    raise StateMachineConfigError(
                        f"error_transitions.{name} has an unknown source state"
                    )
                source = frozenset(source_values)
            target = raw.get("to")
            if target not in states:
                raise StateMachineConfigError(
                    f"error_transitions.{name} has an unknown target"
                )
            triggers = frozenset(
                _string_tuple(
                    raw.get("triggers"),
                    f"error_transitions.{name}.triggers",
                    nonempty=True,
                )
            )
            return ErrorPolicy(source, target, triggers)

        recoverable = parse_error("recoverable")
        security = parse_error("security")
        cancellation = parse_error("cancellation")
        retry_raw = error_document["recoverable"].get("retry_policy")
        if not isinstance(retry_raw, dict):
            raise StateMachineConfigError("recoverable retry_policy is missing")
        retry_limit = retry_raw.get("maximum_attempts_per_stage")
        if not isinstance(retry_limit, int) or retry_limit < 1:
            raise StateMachineConfigError("retry limit must be a positive integer")

        by_source_event = MappingProxyType(
            {(item.source, item.event): item for item in transitions}
        )
        return cls(
            machine_id=str(document.get("machine_id", "")),
            initial_state=initial,
            terminal_states=frozenset(terminal),
            states=states,
            transitions=tuple(transitions),
            recoverable=recoverable,
            security=security,
            cancellation=cancellation,
            retry_limit=retry_limit,
            actors=actors,
            _by_source_event=by_source_event,
        )

    def transition_for(self, state: str, event: str) -> TransitionSpec:
        try:
            return self._by_source_event[(state, event)]
        except KeyError as exc:
            raise TransitionRejected(
                f"event {event!r} is not allowed from state {state!r}"
            ) from exc


@dataclass(frozen=True)
class TransitionEvidence:
    """Evidence bundle required for one configured normal transition."""

    guard_results: Mapping[str, bool]
    guard_evidence_hashes: Mapping[str, str]
    emitted_artifact_hashes: Mapping[str, str]
    input_manifest_hashes: tuple[str, ...] = ()


@dataclass(frozen=True)
class LedgerEvent:
    sequence: int
    kind: str
    transition_id: str | None
    source_state: str
    target_state: str
    actor: str
    event: str
    attempt_id: str
    guard_results: tuple[tuple[str, bool], ...]
    guard_evidence_hashes: tuple[tuple[str, str], ...]
    emitted_artifact_hashes: tuple[tuple[str, str], ...]
    input_manifest_hashes: tuple[str, ...]
    created_at: str
    previous_event_hash: str
    event_hash: str

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "kind": self.kind,
            "transition_id": self.transition_id,
            "source_state": self.source_state,
            "target_state": self.target_state,
            "actor": self.actor,
            "event": self.event,
            "attempt_id": self.attempt_id,
            "guard_results": dict(self.guard_results),
            "guard_evidence_hashes": dict(self.guard_evidence_hashes),
            "emitted_artifact_hashes": dict(self.emitted_artifact_hashes),
            "input_manifest_hashes": list(self.input_manifest_hashes),
            "created_at": self.created_at,
            "previous_event_hash": self.previous_event_hash,
        }

    def as_dict(self) -> dict[str, Any]:
        result = self.unsigned_dict()
        result["event_hash"] = self.event_hash
        return result


class RunLedger:
    """Append-only run state backed by a SHA-256 event chain."""

    def __init__(
        self,
        definition: LifecycleDefinition,
        *,
        run_id: str,
        initial_attempt_id: str,
    ) -> None:
        if not run_id.strip() or not initial_attempt_id.strip():
            raise StateMachineError("run_id and initial_attempt_id are required")
        self.definition = definition
        self.run_id = run_id
        self.initial_attempt_id = initial_attempt_id
        self._state = definition.initial_state
        self._events: list[LedgerEvent] = []

    @property
    def state(self) -> str:
        return self._state

    @property
    def events(self) -> tuple[LedgerEvent, ...]:
        return tuple(self._events)

    @property
    def head_hash(self) -> str:
        return self._events[-1].event_hash if self._events else EVENT_HASH_GENESIS

    def transition(
        self,
        *,
        actor: str,
        event: str,
        attempt_id: str,
        evidence: TransitionEvidence,
        created_at: str | None = None,
    ) -> LedgerEvent:
        if self._state in self.definition.terminal_states:
            raise TransitionRejected(f"terminal state {self._state} cannot advance")
        spec = self.definition.transition_for(self._state, event)
        if actor != spec.actor:
            raise TransitionRejected(
                f"{event} requires actor {spec.actor!r}, got {actor!r}"
            )
        self._validate_normal_evidence(spec, evidence)
        return self._append(
            kind="transition",
            transition_id=spec.transition_id,
            source=spec.source,
            target=spec.target,
            actor=actor,
            event=event,
            attempt_id=attempt_id,
            guard_results=evidence.guard_results,
            guard_evidence_hashes=evidence.guard_evidence_hashes,
            emitted=evidence.emitted_artifact_hashes,
            inputs=evidence.input_manifest_hashes,
            created_at=created_at,
        )

    def fail_recoverable(
        self,
        *,
        actor: str,
        trigger: str,
        attempt_id: str,
        failure_event_hash: str,
        created_at: str | None = None,
    ) -> LedgerEvent:
        if actor != "orchestrator":
            raise TransitionRejected("only orchestrator may classify a recoverable failure")
        return self._error_event(
            kind="recoverable",
            policy=self.definition.recoverable,
            actor=actor,
            trigger=trigger,
            attempt_id=attempt_id,
            artifact_name="failure_event",
            artifact_hash=failure_event_hash,
            created_at=created_at,
        )

    def quarantine(
        self,
        *,
        actor: str,
        trigger: str,
        attempt_id: str,
        incident_hash: str,
        created_at: str | None = None,
    ) -> LedgerEvent:
        if actor not in self.definition.actors:
            raise TransitionRejected(f"unknown quarantine actor {actor!r}")
        if self._state == "QUARANTINED":
            raise TransitionRejected("run is already quarantined")
        return self._error_event(
            kind="security",
            policy=self.definition.security,
            actor=actor,
            trigger=trigger,
            attempt_id=attempt_id,
            artifact_name="security_incident",
            artifact_hash=incident_hash,
            created_at=created_at,
        )

    def cancel(
        self,
        *,
        actor: str,
        attempt_id: str,
        cancellation_hash: str,
        created_at: str | None = None,
    ) -> LedgerEvent:
        trigger = (
            "user_cancelled" if actor == "user" else "main_agent_cancelled"
            if actor == "main_agent"
            else ""
        )
        if not trigger:
            raise TransitionRejected("only user or main_agent may cancel a run")
        return self._error_event(
            kind="cancellation",
            policy=self.definition.cancellation,
            actor=actor,
            trigger=trigger,
            attempt_id=attempt_id,
            artifact_name="cancellation_record",
            artifact_hash=cancellation_hash,
            created_at=created_at,
        )

    def rollback_validation(
        self,
        *,
        actor: str,
        prior_attempt_id: str,
        new_attempt_id: str,
        authorization_hash: str,
        created_at: str | None = None,
    ) -> LedgerEvent:
        """Restart validation with fresh state after a validation-only defect.

        The YAML has no normal edge for this case.  This audited rollback is
        therefore available only from ``REVISION_AUTHORIZED``, only to
        ``CANDIDATE_REVIEW``, only to ``main_agent`` and only with a distinct
        attempt plus a hashed authorization record.
        """

        if self._state != "REVISION_AUTHORIZED":
            raise TransitionRejected(
                "validation rollback requires REVISION_AUTHORIZED state"
            )
        if actor != "main_agent":
            raise TransitionRejected("only main_agent may authorize revision rollback")
        if not prior_attempt_id or not new_attempt_id or prior_attempt_id == new_attempt_id:
            raise TransitionRejected("validation rollback requires a new attempt_id")
        _require_sha256(authorization_hash, "authorization_hash")
        return self._append(
            kind="revision-validation-rollback",
            transition_id=None,
            source="REVISION_AUTHORIZED",
            target="CANDIDATE_REVIEW",
            actor=actor,
            event="revision.restart_from_validation",
            attempt_id=new_attempt_id,
            guard_results={
                "defect earliest impact is validation": True,
                "new attempt_id allocated": True,
                "prior workspace sealed": True,
            },
            guard_evidence_hashes={
                "defect earliest impact is validation": authorization_hash,
                "new attempt_id allocated": authorization_hash,
                "prior workspace sealed": authorization_hash,
            },
            emitted={"validation_revision_authorization": authorization_hash},
            inputs=(authorization_hash,),
            created_at=created_at,
        )

    def _error_event(
        self,
        *,
        kind: str,
        policy: ErrorPolicy,
        actor: str,
        trigger: str,
        attempt_id: str,
        artifact_name: str,
        artifact_hash: str,
        created_at: str | None,
    ) -> LedgerEvent:
        if not policy.accepts_source(self._state, self.definition.terminal_states):
            raise TransitionRejected(
                f"{kind} transition is not allowed from {self._state}"
            )
        if trigger not in policy.triggers:
            raise TransitionRejected(f"unknown {kind} trigger {trigger!r}")
        digest = _require_sha256(artifact_hash, artifact_name)
        return self._append(
            kind=kind,
            transition_id=None,
            source=self._state,
            target=policy.target,
            actor=actor,
            event=trigger,
            attempt_id=attempt_id,
            guard_results={"configured error policy matched": True},
            guard_evidence_hashes={"configured error policy matched": digest},
            emitted={artifact_name: digest},
            inputs=(),
            created_at=created_at,
        )

    @staticmethod
    def _validate_normal_evidence(
        spec: TransitionSpec, evidence: TransitionEvidence
    ) -> None:
        expected_guards = set(spec.guards)
        if set(evidence.guard_results) != expected_guards:
            missing = sorted(expected_guards - set(evidence.guard_results))
            extra = sorted(set(evidence.guard_results) - expected_guards)
            raise TransitionRejected(
                f"guard results must exactly match configuration; "
                f"missing={missing}, extra={extra}"
            )
        failed = sorted(
            guard
            for guard, outcome in evidence.guard_results.items()
            if outcome is not True
        )
        if failed:
            raise TransitionRejected(f"transition guard(s) failed: {failed}")
        if set(evidence.guard_evidence_hashes) != expected_guards:
            raise TransitionRejected("every configured guard requires hashed evidence")
        expected_emits = set(spec.emits)
        if set(evidence.emitted_artifact_hashes) != expected_emits:
            missing = sorted(expected_emits - set(evidence.emitted_artifact_hashes))
            extra = sorted(set(evidence.emitted_artifact_hashes) - expected_emits)
            raise TransitionRejected(
                f"emitted evidence must exactly match configuration; "
                f"missing={missing}, extra={extra}"
            )
        for name, digest in (
            *evidence.guard_evidence_hashes.items(),
            *evidence.emitted_artifact_hashes.items(),
        ):
            _require_sha256(digest, f"hash for {name}")
        for index, digest in enumerate(evidence.input_manifest_hashes):
            _require_sha256(digest, f"input_manifest_hashes[{index}]")

    def _append(
        self,
        *,
        kind: str,
        transition_id: str | None,
        source: str,
        target: str,
        actor: str,
        event: str,
        attempt_id: str,
        guard_results: Mapping[str, bool],
        guard_evidence_hashes: Mapping[str, str],
        emitted: Mapping[str, str],
        inputs: Iterable[str],
        created_at: str | None,
    ) -> LedgerEvent:
        if self._state != source:
            raise TransitionRejected(
                f"ledger state changed concurrently: expected {source}, got {self._state}"
            )
        if not isinstance(attempt_id, str) or not attempt_id.strip():
            raise TransitionRejected("attempt_id is required")
        unsigned = {
            "sequence": len(self._events) + 1,
            "kind": kind,
            "transition_id": transition_id,
            "source_state": source,
            "target_state": target,
            "actor": actor,
            "event": event,
            "attempt_id": attempt_id,
            "guard_results": dict(sorted(guard_results.items())),
            "guard_evidence_hashes": dict(sorted(guard_evidence_hashes.items())),
            "emitted_artifact_hashes": dict(sorted(emitted.items())),
            "input_manifest_hashes": list(inputs),
            "created_at": created_at or _utc_now(),
            "previous_event_hash": self.head_hash,
        }
        event_hash = hashlib.sha256(_canonical_bytes(unsigned)).hexdigest()
        record = LedgerEvent(
            sequence=unsigned["sequence"],
            kind=kind,
            transition_id=transition_id,
            source_state=source,
            target_state=target,
            actor=actor,
            event=event,
            attempt_id=attempt_id,
            guard_results=tuple(unsigned["guard_results"].items()),
            guard_evidence_hashes=tuple(unsigned["guard_evidence_hashes"].items()),
            emitted_artifact_hashes=tuple(
                unsigned["emitted_artifact_hashes"].items()
            ),
            input_manifest_hashes=tuple(unsigned["input_manifest_hashes"]),
            created_at=unsigned["created_at"],
            previous_event_hash=unsigned["previous_event_hash"],
            event_hash=event_hash,
        )
        self._events.append(record)
        self._state = target
        return record

    def export(self) -> tuple[dict[str, Any], ...]:
        return tuple(deepcopy(event.as_dict()) for event in self._events)

    @classmethod
    def replay(
        cls,
        definition: LifecycleDefinition,
        *,
        run_id: str,
        initial_attempt_id: str,
        events: Sequence[Mapping[str, Any]],
    ) -> "RunLedger":
        ledger = cls(
            definition,
            run_id=run_id,
            initial_attempt_id=initial_attempt_id,
        )
        for expected_sequence, raw in enumerate(events, start=1):
            if not isinstance(raw, Mapping):
                raise LedgerIntegrityError("ledger event must be a mapping")
            event = deepcopy(dict(raw))
            recorded_hash = event.pop("event_hash", None)
            if not isinstance(recorded_hash, str) or not SHA256_RE.fullmatch(
                recorded_hash
            ):
                raise LedgerIntegrityError("ledger event has an invalid event_hash")
            computed = hashlib.sha256(_canonical_bytes(event)).hexdigest()
            if computed != recorded_hash:
                raise LedgerIntegrityError("ledger event hash mismatch")
            if event.get("sequence") != expected_sequence:
                raise LedgerIntegrityError("ledger sequence is not contiguous")
            if event.get("previous_event_hash") != ledger.head_hash:
                raise LedgerIntegrityError("ledger hash chain is broken")
            if event.get("source_state") != ledger.state:
                raise LedgerIntegrityError("ledger state sequence is invalid")
            try:
                ledger._replay_verified_event(event)
            except (StateMachineError, KeyError, TypeError) as exc:
                raise LedgerIntegrityError(f"ledger event is not authorized: {exc}") from exc
            if ledger.head_hash != recorded_hash:
                raise LedgerIntegrityError("replayed event does not reproduce its hash")
        return ledger

    def _replay_verified_event(self, event: Mapping[str, Any]) -> None:
        common = {
            "actor": event["actor"],
            "attempt_id": event["attempt_id"],
            "created_at": event["created_at"],
        }
        kind = event["kind"]
        if kind == "transition":
            evidence = TransitionEvidence(
                guard_results=event["guard_results"],
                guard_evidence_hashes=event["guard_evidence_hashes"],
                emitted_artifact_hashes=event["emitted_artifact_hashes"],
                input_manifest_hashes=tuple(event["input_manifest_hashes"]),
            )
            replayed = self.transition(
                event=event["event"],
                evidence=evidence,
                **common,
            )
            if replayed.transition_id != event["transition_id"]:
                raise LedgerIntegrityError("transition id mismatch")
            if replayed.target_state != event["target_state"]:
                raise LedgerIntegrityError("transition target mismatch")
            return
        emitted = event["emitted_artifact_hashes"]
        if kind == "recoverable":
            self.fail_recoverable(
                trigger=event["event"],
                failure_event_hash=emitted["failure_event"],
                **common,
            )
        elif kind == "security":
            self.quarantine(
                trigger=event["event"],
                incident_hash=emitted["security_incident"],
                **common,
            )
        elif kind == "cancellation":
            self.cancel(
                cancellation_hash=emitted["cancellation_record"],
                **common,
            )
        elif kind == "revision-validation-rollback":
            if event["target_state"] != "CANDIDATE_REVIEW":
                raise LedgerIntegrityError("invalid validation rollback target")
            self.rollback_validation(
                actor=event["actor"],
                prior_attempt_id="sealed-prior-attempt",
                new_attempt_id=event["attempt_id"],
                authorization_hash=emitted["validation_revision_authorization"],
                created_at=event["created_at"],
            )
        else:
            raise LedgerIntegrityError(f"unknown ledger event kind {kind!r}")
