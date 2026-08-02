# generic-agent v2.5 sound-boundary migration (revision 3E)

Package identity remains `2.5.0`; Agent identity remains
`generic-agent-v2.5`. Revision 3E replaces semantic admission and
artifact-shaped construction ancestry with closed positive carrier types.

Agent-body construction has exactly two control sources:

- a hash-bound `DirectUserExecutableInstructionV1` containing the single
  executable instruction enum; or
- a hash-bound `IndependentGeneralResearchManifestV1`, with `DTPV1` and
  `MAPV1` derived only from that closed manifest.

Both sources enter `AgentBodyControlAuthorizationV1`, then the typed candidate
source, proposal, admission, merge, and construction-provenance carriers. No
carrier has free text, arbitrary metadata, extension fields, generic artifacts,
or arbitrary parent lists.

Evaluation remains permanently tainted and is isolated in the release-gate
type graph. The release gate consumes only `OpaqueEvaluationReceiptV1` and may
release or reject the exact candidate. Its public disposition contains only
the candidate, verification-plan and opaque-receipt hashes plus closed status;
it cannot authorize rollback, reporting, or another construction. Normal
task-plane rollback and reporting remain available outside this boundary.
They can revise, roll back, or report only the current task/evaluation state;
they never generate a new Agent-body authorization, candidate, or modification.

Construction lineage, opaque evaluation receipts, and release dispositions now
use disjoint nominal entry types, stores, and state enums. Authorization carries
exactly the four closed construction permissions. Authorization, admission, and
promotion inspect no natural-language or task/evaluation content.

The main Agent performs exactly dispatch, prompt writing, review, approval, and
rejection. It does not create DirectUser instructions, DTP/MAP, candidate
sources, proposals, merges, build artifacts, solution artifacts, or task
solutions.

The registry, state machine, five governance projections, seventeen prompts,
sixteen profiles, schemas, runtime, and adapters must move as one consistency
unit. This migration does not freeze or release a candidate.
