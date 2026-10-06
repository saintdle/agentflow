---
name: code-review
description: Review a branch, pull request, commit range, or working-tree diff independently for repository standards and fidelity to its approved specification. Use when the user asks for a code review, PR review, review since a fixed point, correctness review, or verification that implementation matches a goal, issue, PRD, or Beads acceptance contract. Review only; do not edit or merge.
---

# Run a Two-Axis Code Review

When the repository provides an `AGENTS.md`, apply it as the shared coding standard. Inspect affected code, callers, tests, and shared utilities, and distinguish missing evidence from a failed behavior check.

## Pin the review contract

1. Read repository instructions and relevant domain skills.
2. Resolve the fixed point supplied by the user. Otherwise use the PR base or verified merge base; ask only when no authoritative base exists.
3. Record the exact base SHA, head SHA, three-dot diff, changed files, and commits.
4. Find the approved specification in this order: Beads root and acceptance matrix, linked issue or PR, user-supplied artifact, then repository spec/ADR. State when no specification exists.
5. Find repository standards in agent instructions, contribution guides, security policy, domain skills, test conventions, and relevant ADRs.

Stop on an invalid base, empty unexpected diff, missing required skill, overlapping live writer, or unreadable authoritative specification.

## Keep the axes independent

Run two read-only passes without sharing findings until both finish:

### Standards axis

Evaluate only:

- correctness, security, data loss, concurrency, compatibility, and operational risk;
- repository instructions and domain conventions;
- test quality, failure paths, cleanup, and maintainability;
- unnecessary complexity not already enforced by deterministic tooling.

### Specification axis

Evaluate only:

- each approved outcome, constraint, acceptance row, and permission boundary;
- missing behavior, incorrect behavior, and unrequested scope;
- whether claimed evidence proves the authoritative validation lane;
- unresolved `untested`, waived, or hosted-only conditions.

When the user explicitly authorizes delegation, use `orchestrate-agents` to assign these as separate read-only reviewers. Otherwise perform the passes sequentially and preserve separate notes. Reviewers never edit, push, merge, or delegate.

## Structure findings

Return only actionable findings. Give each:

```text
<stable-id> <high|medium|low> <standards|spec>
evidence: <file:line, commit, command result, acceptance row, or URL>
reproduction: <exact check or observation>
expected: <required behavior>
actual: <observed behavior>
correction: <smallest safe change>
gate: <authoritative validation lane>
confidence: <high|medium|low>
```

Do not report style already enforced by tooling, unsupported hypotheticals, or personal preferences as defects. Clearly label missing evidence instead of inventing failure.

## Disposition and route

The controller:

1. Deduplicates findings across axes without erasing their source.
2. Reproduces high and medium findings before accepting them.
3. Records each disposition with `agentflow review record`: `accepted`, `rejected-factual`, `rejected-preference`, `duplicate`, or `deferred`.
4. Uses `agentflow review route-fix` for accepted corrections, returning ownership to the original writer when known.
5. Sends the corrected work back to the same independent axis for re-review.

Without an Agentflow controller, return findings and reproduction evidence to the user without mutating durable state.

## Final judgment

Approve only when both axes complete, no accepted blocking finding remains, and required acceptance evidence is present. Otherwise return `REVIEW` with findings ordered by severity.

Report the fixed point, standards sources, specification source, findings by axis, dispositions when recorded, checks run, untested lanes, and merge recommendation. Never merge.
