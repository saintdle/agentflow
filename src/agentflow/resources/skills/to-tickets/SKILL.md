---
name: to-tickets
description: Convert an approved goal or implementation plan into a dependency-aware, independently verifiable Beads graph or GitHub issues for coding agents. Use when the user asks to break work into tickets, issues, a task graph, or agent-assignable work, or when an active repository workflow requires an issue before implementation. Do not publish external GitHub issues until the user approves the proposed breakdown unless explicit prior authorization says to publish.
---

# Turn a Plan into Tickets

Read [issue-template.md](references/issue-template.md) before drafting or publishing issues.

## Prepare

1. Confirm the goal status is approved. If not, use `shape-goal`.
2. Inspect repository instructions, Beads state when `bd where --json` succeeds, remotes, labels, issue templates, and current open issues. Do not duplicate existing work.
3. Split work into tracer bullets: each ticket delivers a narrow end-to-end behavior and fits in one fresh agent context.
4. Create early prerequisite tickets for shared interfaces or decisions. Serialize overlapping file ownership; parallelize disjoint scopes.
5. Use expand-migrate-contract for wide mechanical changes that cannot be sliced vertically.

## Draft and approve

Present the ticket titles, dependency edges, expected file ownership, and acceptance checks. Ask for approval before external writes unless the user already explicitly authorized issue creation for this plan.

## Persist

When Beads is active, create the approved graph there first. Use dependency
edges for prerequisites, parent-child relationships for controlled work, and
acceptance criteria for verifiable completion. Store exact branch/base,
ownership, required skills, provider budget, and validation lanes in the bead
metadata. Use the `agentflow-controlled-rework` formula for controller-led
reworks with dynamic sub-primary writers and two read-only review gates.
Give each claimable node one `af:stage:<name>` and one `af:role:<name>` label;
add only material `af:cap:<name>` requirements. Keep every pull scoped to its
approved parent root. Label the root `af:kind:workflow` without a stage, role,
or capability. Use `--no-inherit-labels` when creating dynamic children.

Do not copy transient prompts or chat transcripts into the graph.

## Publish to GitHub

Create GitHub issues only when repository policy or the user requires a human-facing issue. Sync selected parent beads, not every internal worker bead. Use `gh issue create` with the repository's templates and labels. Assign the authenticated user or named worker only after verifying the identity with `gh api user --jq .login`; do not print credential data.

After creation, return a compact table of human title, bead ID, stage, blockers
in human terms, suggested worker tier, GitHub issue when present, and URL. End
with one copy/paste-ready next request. Store GitHub issue URLs as external
references on the matching bead.
