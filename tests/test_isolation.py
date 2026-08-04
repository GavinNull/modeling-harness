from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

import modeling_harness.isolation as isolation_module
from modeling_harness.isolation import (
    ApprovedInputRoot,
    DockerSandboxBackend,
    EgressControllerAttestation,
    IsolationError,
    LocalSandboxBackend,
    ProductionIsolationError,
    ReadOnlyMount,
    ResourceLimits,
    ReviewIsolationContext,
    SandboxRequest,
    require_production_backend,
    sha256_path,
)


IMAGE = "example.invalid/modeling@sha256:" + "a" * 64


def request(tmp_path: Path, **overrides: object) -> SandboxRequest:
    input_root = tmp_path / "inputs"
    input_root.mkdir(parents=True, exist_ok=True)
    (input_root / "raw.txt").write_text("immutable input", encoding="utf-8")
    default_inputs: tuple[ReadOnlyMount, ...] = ()
    if "readonly_inputs" not in overrides:
        default_inputs = (
            ReadOnlyMount(
                input_root,
                PurePosixPath("/inputs/raw"),
                "raw-input",
                sha256_path(input_root),
            ),
        )
    values: dict[str, object] = {
        "project_id": "project-001",
        "task_id": "task-001",
        "role_id": "problem_definition_router",
        "attempt_id": "attempt-001",
        "session_id": "session-001",
        "host_write_root": tmp_path / "runs" / "author",
        "image": IMAGE,
        "limits": ResourceLimits(
            cpus=2,
            memory_bytes=512 * 1024 * 1024,
            disk_bytes=1024 * 1024 * 1024,
            wall_time_seconds=600,
        ),
        "readonly_inputs": default_inputs,
    }
    values.update(overrides)
    return SandboxRequest(**values)  # type: ignore[arg-type]


def backend_for(tmp_path: Path) -> DockerSandboxBackend:
    input_root = tmp_path / "inputs"
    input_root.mkdir(parents=True, exist_ok=True)
    (input_root / "raw.txt").write_text("immutable input", encoding="utf-8")
    return DockerSandboxBackend(
        approved_input_roots=(
            ApprovedInputRoot(
                tmp_path / "inputs",
                "raw-input",
                "c" * 64,
            ),
        )
    )


def resource_limits(**overrides: object) -> ResourceLimits:
    values: dict[str, object] = {
        "cpus": 2,
        "memory_bytes": 512 * 1024 * 1024,
        "disk_bytes": 1024 * 1024 * 1024,
        "wall_time_seconds": 600,
        "pids": 256,
    }
    values.update(overrides)
    return ResourceLimits(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "invalid",
    [
        True,
        False,
        float("nan"),
        float("inf"),
        float("-inf"),
        "2",
        None,
        0,
        -1,
        10**100,
    ],
)
def test_cpu_limit_rejects_nonfinite_nonpositive_and_unbounded_values(
    invalid: object,
) -> None:
    with pytest.raises(IsolationError):
        resource_limits(cpus=invalid)


@pytest.mark.parametrize(
    "field_name",
    ["memory_bytes", "disk_bytes", "wall_time_seconds", "pids"],
)
@pytest.mark.parametrize(
    "invalid",
    [
        True,
        False,
        1.0,
        1.5,
        float("nan"),
        float("inf"),
        float("-inf"),
        "1",
        None,
        0,
        -1,
        10**100,
    ],
)
def test_integer_resource_limits_reject_invalid_types_values_and_bounds(
    field_name: str,
    invalid: object,
) -> None:
    with pytest.raises(IsolationError):
        resource_limits(**{field_name: invalid})


def test_runtime_revalidates_limits_before_docker_command(
    tmp_path: Path,
) -> None:
    spec = request(tmp_path)
    object.__setattr__(spec.limits, "cpus", float("nan"))

    with pytest.raises(IsolationError):
        backend_for(tmp_path).plan(spec)


def test_valid_docker_resource_arguments_are_finite_decimal_values(
    tmp_path: Path,
) -> None:
    plan = backend_for(tmp_path).plan(
        request(tmp_path, limits=resource_limits(cpus=1.25))
    )
    command_text = " ".join(plan.command).lower()
    assert "nan" not in command_text
    assert "inf" not in command_text
    assert plan.command[plan.command.index("--cpus") + 1] == "1.25"


def test_docker_plan_is_strict_and_does_not_execute(tmp_path: Path) -> None:
    backend = backend_for(tmp_path)
    plan = backend.plan(request(tmp_path))

    command = plan.command
    assert command[:2] == ("docker", "run")
    assert "--read-only" in command
    assert ("--network", "none") == command[
        command.index("--network") : command.index("--network") + 2
    ]
    assert "--cap-drop" in command
    assert "no-new-privileges" in command
    assert any("dst=/inputs/raw,readonly" in item for item in command)
    assert any("dst=/workspace,rw" in item for item in command)
    assert plan.production_eligible is True
    require_production_backend(plan)


def test_same_attempt_cannot_be_reused(tmp_path: Path) -> None:
    backend = backend_for(tmp_path)
    spec = request(tmp_path)
    backend.plan(spec)
    with pytest.raises(IsolationError):
        backend.plan(spec)


def test_other_agent_workspace_cannot_be_mounted(tmp_path: Path) -> None:
    peer = (tmp_path / "runs" / "peer").resolve()
    spec = request(
        tmp_path,
        readonly_inputs=(
            ReadOnlyMount(
                peer / "candidate.txt",
                PurePosixPath("/inputs/peer"),
                "promoted-artifact",
            ),
        ),
        forbidden_workspace_roots=(peer,),
    )
    with pytest.raises(IsolationError):
        backend_for(tmp_path).plan(spec)


def test_reviewer_cannot_inherit_author_environment(tmp_path: Path) -> None:
    author_root = (tmp_path / "runs" / "author").resolve()
    context = ReviewIsolationContext(
        author_attempt_id="attempt-001",
        author_session_id="session-001",
        author_container_name="author-container",
        author_write_root=author_root,
    )
    inherited = request(
        tmp_path,
        role_id="mathematical_reviewer",
        attempt_id="attempt-002",
        session_id="session-001",
        host_write_root=tmp_path / "runs" / "reviewer",
        review_context=context,
    )
    with pytest.raises(IsolationError):
        backend_for(tmp_path).plan(inherited)

    mounted_state = request(
        tmp_path,
        role_id="mathematical_reviewer",
        attempt_id="attempt-003",
        session_id="session-003",
        host_write_root=tmp_path / "runs" / "reviewer-003",
        review_context=context,
        readonly_inputs=(
            ReadOnlyMount(
                author_root / "cache",
                PurePosixPath("/inputs/candidate"),
                "promoted-artifact",
            ),
        ),
    )
    with pytest.raises(IsolationError):
        backend_for(tmp_path).plan(mounted_state)


def test_fresh_reviewer_plan_has_new_container_and_workspace(tmp_path: Path) -> None:
    context = ReviewIsolationContext(
        author_attempt_id="attempt-001",
        author_session_id="session-001",
        author_container_name="author-container",
        author_write_root=tmp_path / "runs" / "author",
    )
    spec = request(
        tmp_path,
        role_id="reproducibility_reviewer",
        attempt_id="attempt-002",
        session_id="session-002",
        host_write_root=tmp_path / "runs" / "reviewer",
        review_context=context,
    )
    plan = backend_for(tmp_path).plan(spec)
    assert plan.container_name != context.author_container_name
    assert plan.host_write_root != context.author_write_root
    assert plan.isolation_attestation["reviewer_fresh_environment"] is True


def test_local_backend_is_always_rejected_for_production(tmp_path: Path) -> None:
    backend = LocalSandboxBackend()
    plan = backend.plan(request(tmp_path))
    assert plan.test_only is True
    with pytest.raises(ProductionIsolationError):
        require_production_backend(backend)
    with pytest.raises(ProductionIsolationError):
        require_production_backend(plan)


def test_production_rejects_unapproved_host_path_and_kind_spoof(
    tmp_path: Path,
) -> None:
    other = tmp_path / "unapproved"
    other.mkdir()
    (other / "private.txt").write_text("host secret", encoding="utf-8")
    unapproved = request(
        tmp_path,
        readonly_inputs=(
            ReadOnlyMount(
                other,
                PurePosixPath("/inputs/unapproved"),
                "raw-input",
                sha256_path(other),
            ),
        ),
    )
    with pytest.raises(IsolationError):
        backend_for(tmp_path).plan(unapproved)

    spoofed = request(
        tmp_path,
        readonly_inputs=(
            ReadOnlyMount(
                tmp_path / "inputs",
                PurePosixPath("/inputs/spoofed"),
                "promoted-artifact",
                sha256_path(tmp_path / "inputs"),
            ),
        ),
    )
    with pytest.raises(IsolationError):
        backend_for(tmp_path).plan(spoofed)


def test_production_recomputes_content_hash(tmp_path: Path) -> None:
    spec = request(
        tmp_path,
        readonly_inputs=(
            ReadOnlyMount(
                tmp_path / "inputs",
                PurePosixPath("/inputs/raw"),
                "raw-input",
                "d" * 64,
            ),
        ),
    )
    with pytest.raises(IsolationError):
        backend_for(tmp_path).plan(spec)


def test_filesystem_root_and_symlink_escape_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(IsolationError):
        ApprovedInputRoot(
            Path(tmp_path.anchor),
            "raw-input",
            "a" * 64,
        )

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = tmp_path / "inputs" / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not available in this environment")
    mount = ReadOnlyMount(
        link,
        PurePosixPath("/inputs/escape"),
        "raw-input",
        sha256_path(outside),
    )
    spec = request(tmp_path, readonly_inputs=(mount,))
    with pytest.raises(IsolationError):
        backend_for(tmp_path).plan(spec)


def test_reparse_escape_detection_fails_closed_when_platform_reports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    escape = tmp_path / "inputs" / "reported-reparse"
    escape.mkdir(parents=True)
    (escape / "payload.txt").write_text("payload", encoding="utf-8")
    digest = sha256_path(escape)
    original = isolation_module._is_link_or_reparse

    def report_reparse(path: Path) -> bool:
        return path == escape or original(path)

    monkeypatch.setattr(isolation_module, "_is_link_or_reparse", report_reparse)
    spec = request(
        tmp_path,
        readonly_inputs=(
            ReadOnlyMount(
                escape,
                PurePosixPath("/inputs/reparse"),
                "raw-input",
                digest,
            ),
        ),
    )
    with pytest.raises(IsolationError):
        backend_for(tmp_path).plan(spec)


@pytest.mark.parametrize("relation", ["child", "parent"])
def test_reviewer_write_root_cannot_overlap_author_tree(
    tmp_path: Path, relation: str
) -> None:
    author = (tmp_path / "runs" / "author").resolve()
    if relation == "child":
        reviewer = author / "reviewer"
    else:
        reviewer = author.parent
    context = ReviewIsolationContext(
        author_attempt_id="attempt-001",
        author_session_id="session-001",
        author_container_name="author-container",
        author_write_root=author,
    )
    spec = request(
        tmp_path,
        role_id="evidence_communication_reviewer",
        attempt_id="attempt-002",
        session_id="session-002",
        host_write_root=reviewer,
        review_context=context,
    )
    with pytest.raises(IsolationError):
        backend_for(tmp_path).plan(spec)


def test_allowlisted_network_fails_closed_without_trusted_controller(
    tmp_path: Path,
) -> None:
    network_request = request(
        tmp_path,
        network_policy="allowlisted-readonly",
        network_proxy_url="http://egress-proxy:8080",
        allowed_domains=("data.example.org",),
        egress_attestation_id="egress-policy-001",
    )
    with pytest.raises(IsolationError):
        backend_for(tmp_path).plan(network_request)


def test_allowlisted_network_uses_attested_proxy_only_network(tmp_path: Path) -> None:
    attestation = EgressControllerAttestation(
        attestation_id="egress-policy-001",
        controller_network="modeling-egress-proxy-only",
        proxy_url="http://egress-proxy:8080",
        allowed_domains=("data.example.org",),
        policy_sha256="e" * 64,
        attested_by="sandbox_platform_engineer",
        firewall_enforced=True,
        direct_egress_denied=True,
        dns_via_controller=True,
        readonly_http_methods_only=True,
    )
    network_request = request(
        tmp_path,
        network_policy="allowlisted-readonly",
        network_proxy_url=attestation.proxy_url,
        allowed_domains=attestation.allowed_domains,
        egress_attestation_id=attestation.attestation_id,
    )
    backend = DockerSandboxBackend(
        approved_input_roots=(
            ApprovedInputRoot(
                tmp_path / "inputs",
                "raw-input",
                "c" * 64,
            ),
        ),
        egress_attestations=(attestation,),
    )
    plan = backend.plan(network_request)
    index = plan.command.index("--network")
    assert plan.command[index + 1] == "modeling-egress-proxy-only"
    assert "bridge" not in plan.command
    assert plan.isolation_attestation["network_egress_enforced"] is True


def test_release_rechecks_plan_attestations(tmp_path: Path) -> None:
    plan = backend_for(tmp_path).plan(request(tmp_path))
    forged = replace(
        plan,
        isolation_attestation={
            **plan.isolation_attestation,
            "input_hashes_verified": False,
        },
    )
    with pytest.raises(ProductionIsolationError):
        require_production_backend(forged)
