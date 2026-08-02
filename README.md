# Modeling Harness

`modeling-harness` is an isolated multi-agent runtime for rigorous mathematical
modeling. Agent identity is `generic-agent-v2.5`; package version is `2.5.0`.

## Revision 3E boundary

Agent-body construction is limited to the closed direct-user and independent
general-research authorization lineages. Evaluation and verification remain in
nominally disjoint ledgers and can only release or reject the exact candidate.
They never authorize a successor Agent-body change. Task-plane evaluation,
rollback, and reporting remain separate.

## Source-freeze and release state

The promotable source-freeze record is
`generic-agent-v2.5-source-freeze-s002`, classified
`SOURCE_FROZEN_DIAGNOSTIC_CANDIDATE`. It binds the independently reproduced
ten-member cross-component source manifest. The s001 freeze with SHA-256
`3a706c7064a4bf54b038442c1d890e3a29ee365b47704a84a5315acc3938c846`
remains immutable historical evidence and does not identify s002.

The deterministic P0 container-startup parameter contract is closed for the
exact s002 source: the bundled worker entrypoint receives the canonical
`--role` and `--charter` arguments through the adapter/orchestrator chain.
Docker execution and a runtime seal were not established. Runtime eligibility,
production eligibility, formal release, and formal generalization remain
`NO` / `CLOSED`; no opaque exact-candidate release receipt is present.

## Commands

```console
modeling-harness validate-config --project-root .
modeling-harness validate-codex-adapter --project-root .
modeling-harness production-preflight --json
modeling-harness doctor --project-root .
```

The equivalent source-tree form is `python -m modeling_harness.cli <command>`.
Host-side source verification and P0 parameter-contract closure do not seal the
runtime or establish production readiness.
