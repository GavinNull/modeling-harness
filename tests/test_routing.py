import pytest

from modeling_harness.routing import RoutingError, StarRouter, authorize_control_action


SUBAGENTS = {
    "problem_definition_router",
    "model_architect",
    "system_architect",
    "mathematical_reviewer",
    "benchmark_curator",
}


def test_subagent_to_subagent_route_is_denied() -> None:
    router = StarRouter(SUBAGENTS)
    with pytest.raises(RoutingError):
        router.authorize(
            "problem_definition_router", "model_architect", "ResultPacket"
        )


def test_only_main_dispatches_and_subagent_submits_to_main() -> None:
    router = StarRouter(SUBAGENTS)
    assert router.authorize(
        "main_agent", "problem_definition_router", "TaskPacket"
    ).allowed
    assert router.authorize(
        "problem_definition_router", "main_agent", "ResultPacket"
    ).allowed
    with pytest.raises(RoutingError):
        authorize_control_action("problem_definition_router", "dispatch")


def test_review_packet_can_only_reach_main_from_reviewer() -> None:
    router = StarRouter(SUBAGENTS)
    assert router.authorize(
        "mathematical_reviewer", "main_agent", "ReviewPacket"
    ).allowed
    with pytest.raises(RoutingError):
        router.authorize(
            "problem_definition_router", "main_agent", "ReviewPacket"
        )
    with pytest.raises(RoutingError):
        router.authorize(
            "main_agent", "problem_definition_router", "ReviewPacket"
        )


def test_closed_agent_body_authorization_only_reaches_core_builder() -> None:
    router = StarRouter(SUBAGENTS, builder_ids={"model_architect", "system_architect"})
    assert router.authorize(
        "main_agent", "system_architect", "AgentBodyControlAuthorizationV1"
    ).allowed
    with pytest.raises(RoutingError):
        router.authorize(
            "main_agent", "model_architect", "AgentBodyControlAuthorizationV1"
        )
    with pytest.raises(RoutingError):
        router.authorize(
            "main_agent",
            "system_architect",
            "DefectTranslation" + "Packet",
        )


def test_only_main_can_promote() -> None:
    StarRouter.authorize_promotion("main_agent")
    with pytest.raises(RoutingError):
        StarRouter.authorize_promotion("model_architect")
