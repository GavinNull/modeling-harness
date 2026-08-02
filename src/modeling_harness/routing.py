"""Star-topology authorization for control and artifact messages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal


MAIN_AGENT_ID = "main_agent"
REVIEWER_ROLES = frozenset(
    {
        "mathematical_reviewer",
        "reproducibility_reviewer",
        "evidence_communication_reviewer",
    }
)
CORE_BUILDER_ROLES = frozenset(
    {
        "system_architect",
        "sandbox_platform_engineer",
        "standards_delivery_manager",
    }
)
MAIN_TO_SUBAGENT_PACKETS = frozenset(
    {"TaskPacket", "AgentBodyControlAuthorizationV1"}
)
SUBAGENT_TO_MAIN_PACKETS = frozenset(
    {
        "ResultPacket",
        "ArtifactManifest",
        "ReviewPacket",
        "RoleCharter",
        "AnonymizedBenchmarkReport",
    }
)


class RoutingError(PermissionError):
    """Raised when a message or privileged action violates the star topology."""


@dataclass(frozen=True)
class RouteDecision:
    sender: str
    recipient: str
    packet_type: str
    allowed: bool
    reason: str


class StarRouter:
    """Fail-closed star router with ``main_agent`` as its only hub."""

    def __init__(
        self,
        subagent_ids: Iterable[str],
        *,
        builder_ids: Iterable[str] | None = None,
    ) -> None:
        subagents = frozenset(subagent_ids)
        if not subagents:
            raise ValueError("at least one subagent role is required")
        if MAIN_AGENT_ID in subagents:
            raise ValueError("subagent_ids must exclude main_agent")
        self._subagents = subagents
        if builder_ids is None:
            builders = subagents - REVIEWER_ROLES - {"benchmark_curator"}
        else:
            builders = frozenset(builder_ids)
            if not builders <= subagents:
                raise ValueError("builder_ids must be registered subagents")
        self._builders = builders

    @property
    def subagent_ids(self) -> frozenset[str]:
        return self._subagents

    def decide(self, sender: str, recipient: str, packet_type: str) -> RouteDecision:
        if sender == recipient:
            return RouteDecision(sender, recipient, packet_type, False, "self route denied")
        known = self._subagents | {MAIN_AGENT_ID}
        if sender not in known or recipient not in known:
            return RouteDecision(
                sender, recipient, packet_type, False, "unknown route endpoint"
            )
        if sender in self._subagents and recipient in self._subagents:
            return RouteDecision(
                sender,
                recipient,
                packet_type,
                False,
                "direct subagent-to-subagent communication is forbidden",
            )
        if sender == MAIN_AGENT_ID:
            if packet_type not in MAIN_TO_SUBAGENT_PACKETS:
                return RouteDecision(
                    sender,
                    recipient,
                    packet_type,
                    False,
                    "main_agent may dispatch only approved outbound packet types",
                )
            if (
                packet_type == "AgentBodyControlAuthorizationV1"
                and recipient not in CORE_BUILDER_ROLES
            ):
                return RouteDecision(
                    sender,
                    recipient,
                    packet_type,
                    False,
                    "Agent-body control authorization may target only a core builder",
                )
            return RouteDecision(sender, recipient, packet_type, True, "hub dispatch")

        if recipient != MAIN_AGENT_ID:
            return RouteDecision(
                sender, recipient, packet_type, False, "subagents submit only to main_agent"
            )
        if packet_type not in SUBAGENT_TO_MAIN_PACKETS:
            return RouteDecision(
                sender,
                recipient,
                packet_type,
                False,
                "packet type is not authorized for subagent submission",
            )
        if packet_type == "ReviewPacket" and sender not in REVIEWER_ROLES:
            return RouteDecision(
                sender,
                recipient,
                packet_type,
                False,
                "only registered reviewer roles may submit ReviewPacket",
            )
        return RouteDecision(sender, recipient, packet_type, True, "hub submission")

    def authorize(self, sender: str, recipient: str, packet_type: str) -> RouteDecision:
        decision = self.decide(sender, recipient, packet_type)
        if not decision.allowed:
            raise RoutingError(decision.reason)
        return decision

    @staticmethod
    def authorize_promotion(actor: str) -> None:
        if actor != MAIN_AGENT_ID:
            raise RoutingError("only main_agent may promote artifacts")

    @staticmethod
    def authorize_dispatch(actor: str) -> None:
        if actor != MAIN_AGENT_ID:
            raise RoutingError("only main_agent may dispatch tasks")

    @staticmethod
    def authorize_raw_review_read(actor: str) -> None:
        if actor != MAIN_AGENT_ID:
            raise RoutingError("raw ReviewPacket is main-agent-only")


PrivilegedAction = Literal["dispatch", "receive", "promote"]


def authorize_control_action(actor: str, action: PrivilegedAction) -> None:
    """Authorize controller operations explicitly, outside message routing."""

    if actor != MAIN_AGENT_ID:
        raise RoutingError(f"only main_agent may {action}")
