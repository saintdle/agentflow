---
name: wayfinder
description: Map a large, uncertain, multi-session effort into a Beads graph of decisions and investigations before implementation planning. Use when the destination is understood but the route is unclear, major architectural or product choices remain unresolved, or the work cannot yet be safely decomposed into implementation tickets. Do not use when an approved plan is already ready for to-tickets.
---

# Wayfind an Uncertain Effort

Wayfinding resolves uncertainty. It does not disguise implementation work as research.

## Define the destination

1. State the desired destination, why it matters, constraints, non-goals, and evidence that the route is clear.
2. Inspect repository instructions, existing decisions, source artifacts, current issues, and Beads state.
3. Identify unresolved decisions whose answers materially change the plan. Separate decisions from facts that can be discovered directly.
4. Process new user information immediately. Rework the frontier when an answer invalidates earlier assumptions.

If Beads is unavailable, stop and recommend `agentflow beads init`. Do not create a competing Markdown tracker.

## Create the decision map

Create one parent bead:

- Type: `epic` or `feature`
- Label: `af:kind:workflow`
- Purpose: destination, boundaries, route-clear exit conditions, and concise decisions-so-far

Create child beads only for unresolved decisions or bounded investigations:

- Type: `decision` for a choice; `task` only for evidence-gathering
- Labels: exactly one `af:stage:plan`, one `af:role:decision`, and only material `af:cap:*` labels
- Acceptance: the evidence and decision required to close the node
- Dependencies: an edge only when one answer is required before another can be decided
- Metadata: sources, owner, approved model/tool budget when delegated, and expected decision artifact

Use `--no-inherit-labels` for children. Keep the initial frontier small. Do not pre-create speculative downstream branches.

## Resolve the frontier

1. Query only ready children below the approved map. Refer to every item as `<human title> (<id>)`, never a bare identifier.
2. Claim one ready decision. Prefer deterministic inspection and primary sources before using an agent.
3. If delegation is explicitly authorized, use `orchestrate-agents` for bounded read-only investigation. The worker returns evidence and options; the controller owns the decision.
4. Record the decision, evidence, rejected alternatives, consequences, and newly exposed questions on its bead, then close it.
5. Update the parent with a one-line decision gist and link; keep full detail on the child.
6. Add, remove, or rewire child decisions as the route changes. Check dependency cycles after structural changes.

Do not store prompts, transcripts, or duplicated research documents in Beads. Store conclusions and artifact references.

## Finish at the planning boundary

The map is complete when:

- every route-changing question is resolved or explicitly accepted as a constraint;
- no decision child remains ready, blocked, or in progress;
- decisions are mutually consistent and traceable to evidence;
- the destination can be expressed as an approvable goal or implementation plan.

Synthesize the route into `shape-goal` when the goal contract still needs approval, or `to-tickets` when an approved plan is ready for implementation decomposition. Do not implement from the Wayfinder map unless the user separately authorizes execution.

Return the destination, decisions made, remaining constraints, closed and open nodes in human terms, the resulting plan/spec artifact, and exactly one copy/paste-ready next request.
