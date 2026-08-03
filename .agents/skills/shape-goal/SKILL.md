---
name: shape-goal
description: Turn a rough idea, ambiguous request, or proposed process into an approved, measurable goal contract before planning or implementation. Use when the user asks to grill, interview, clarify, define success, create a measurable goal, or prevent premature implementation. Do not use for a small task whose outcome and done conditions are already explicit.
---

# Shape a Goal

## Interview

1. Restate the idea in one sentence and identify the largest unresolved decision.
2. Discover facts from available files, tools, and sources. Ask the user only for decisions or unavailable context.
3. Ask one decision at a time. Process corrections immediately; do not finish a prepared question list after the user changes direction.
4. Trace the goal as `result -> user or system outcome -> KPI -> validation -> exit conditions`.
5. Challenge proxy metrics, implied scope, unstated tradeoffs, and done conditions that cannot be tested.
6. Apply the automation-first routing check: ask "Does an owned deterministic command, bot, or API already produce this result?" Prefer that stable path and record the decision; reserve an agent for ambiguity, conflicts, diagnostics, or missing automation.
7. Stop asking when every required decision is resolved. Do not plan or implement yet.

## Produce the goal contract

Return this compact form:

```md
# Goal: <outcome>
Status: awaiting-approval

## Why now
<one sentence>

## Outcome
<observable change for the user or system>

## Scope
- In: ...
- Out: ...

## Constraints
- ...

## Evidence
- KPI: <measure and target>
- Validation: <how and where it is measured>

## Exit conditions
- [ ] <independently testable condition>
- [ ] <independently testable condition>
```

AND-join all exit conditions: work is complete only when every box can be checked with evidence.

For substantial, security-sensitive, visual, or environment-dependent work, append an acceptance-to-evidence table:

```md
| ID | Outcome or constraint | Owner | Authoritative lane | Planned evidence |
|---|---|---|---|---|
| A1 | ... | ... | static / rendered / local-runtime / architecture-specific / hosted-runtime / manual-ui | ... |
```

Give every row one owner and one authoritative validation lane. Use `approved-untested` only with an explicit user-approved exception. After goal approval, persist the table with `agentflow acceptance create`; block the first writable assignment until the matrix validates. Skip the matrix for a small task whose exit conditions and proof are already complete in the compact contract.

## Approval gate

Ask for one final correction or approval. Do not transition to tickets, planning, or implementation until the user approves. After approval, change `Status` to `approved`. When `bd where --json` succeeds, persist the contract and acceptance rows on a parent bead; otherwise preserve it in the repository's approved planning artifact. Do not store interview transcripts.
