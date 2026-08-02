# Codex role adapter

`.codex/agents/` exposes the harness's sixteen canonical roles as project-scoped
Codex custom agents. These profiles are a **control plane**, not a shortcut
around the runtime: every profile is read-only, has no peer-to-peer channel,
and reports only to `main_agent`.

Task creation, code execution, and review artifacts remain in the per-attempt
isolated workspace defined by the task packet and executed through the Docker
runtime. Only the main agent may promote an approved snapshot or translate raw
reviewer feedback into a `DefectTranslationPacket`.

## Non-specialization rule

Historical tasks are measurement instruments, never targets for tuning. A role
must not add a branch, prompt instruction, threshold, parameter, answer range,
or template keyed to a task identity. A change is admissible only when the
failure is translated into a named general capability and replicated across two
independent task families (or one family plus two independent variants), then
survives cross-domain and unseen-task regression.

Raw review evidence, hidden task content, answer keys, and task-specific repair
advice must never enter a builder prompt. Real competition tasks and every
derived artifact are permanently evaluation-only: recurrence,
de-identification, abstraction, or aggregation never permits them to support an
Agent-body change. Builders receive only generic evidence from isolated
synthetic tests or independent general research, or a direct user governance
requirement, after approval by the main agent.

## Checks

Run the adapter check whenever `.codex` profiles or role prompts change:

```powershell
modeling-harness validate-codex-adapter
modeling-harness codex-agent-entrypoint model_architect
modeling-harness validate-config
modeling-harness verify-benchmarks
```

`modeling-harness doctor` includes the adapter check. It also requires an
available Docker daemon, because a successful configuration check alone is not
execution evidence.

## Codex execution image

`containers/codex-agent/Dockerfile` builds the isolated runtime image. Its
entrypoint runs `codex exec --ephemeral --json --sandbox workspace-write
--ignore-user-config`; the outer Docker sandbox is still responsible for the
read-only filesystem, unique `/workspace`, resource limits, and network
allowlist. The worker reads the role charter bundled in the image and rejects a
result packet whose task, run, attempt, role, or prompt hash is not bound to
the task packet.

Builds must pin both the base-image digest and the reviewed Codex CLI version.
Supply authentication only through a short-lived runtime secret such as
`CODEX_API_KEY`; never bake it into an image, task packet, prompt, artifact, or
log. A run needing Codex service access must use the existing explicit egress
allowlist and include the required API endpoint.
