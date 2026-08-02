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
`generic-agent-v2.5-source-freeze-s001`, classified
`SOURCE_FROZEN_DIAGNOSTIC_CANDIDATE`. It binds the independently reproduced
seven-member successor source manifest. The earlier v2.5 freeze with SHA-256
`d5713697043ff2582118c21f5328fc65114c885a070124e2d6dc947685322e6a`
remains immutable historical evidence and does not identify this successor.

Runtime isolation is `UNSEALED`; runtime eligibility, formal release, production
eligibility, and formal generalization are `NO` / `CLOSED`. The independent
reproducibility review establishes source-freeze eligibility only. No opaque
exact-candidate release receipt is present. Historical modeling-plane evidence
is predecessor context only and is not exact-identity evaluation of s001.

## Commands

```console
modeling-harness validate-config --project-root .
modeling-harness validate-codex-adapter --project-root .
modeling-harness production-preflight --json
modeling-harness doctor --project-root .
```

The equivalent source-tree form is `python -m modeling_harness.cli <command>`.
Production execution requires a positively verified isolated backend;
host-side source verification does not seal the runtime.
