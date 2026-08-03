---
name: gatekeep-prs
description: Operate a single integration queue for agent-authored GitHub pull requests, ordering ready work by priority then FIFO, checking reviews, CI, and conflicts, posting deduplicated diagnostics, and merging only when authorized. Use when the user asks for a PR gatekeeper, merge shepherd, integration controller, PR queue scan, or conflict/CI bounce-back. Do not fix contributor code or merge without explicit authorization.
---

# Gatekeep Pull Requests

Read [bounce-back.md](references/bounce-back.md) before commenting on blocked PRs.

## Scan

1. Query fresh state with `gh pr list` and `gh pr view`; do not rely on cached session state.
2. Exclude drafts, closed PRs, and PRs missing required ownership metadata.
3. Sort by `agent-priority:p0` to `p3`, then creation time FIFO. Use FIFO alone when priority labels are absent.
4. Process one integration at a time. Do not occupy the session polling pending checks; report `PENDING` and revisit on the next pass or event.

## Gate

Capture an immutable identity record before any integration action: PR number, remote head ref and OID, base ref and OID, local branch when present, authorized mutation set, and intended merge or push mode. Treat any unapproved change as `BLOCKED`.

For the first eligible PR, verify:

- the linked issue and source branch are current;
- the linked bead is current when the repository uses Beads;
- required reviews and repository policy pass;
- every required check is successful;
- mergeability is clean against the current base;
- the PR still satisfies its acceptance conditions.

Use read-only conflict prediction such as `git merge-tree` when local refs are available. Re-fetch immediately before merge.

Do not rename the remote head of an open PR without an explicitly approved migration plan. After an authorized history rewrite, require an exact lease tied to the recorded remote OID, then verify local, remote, and PR head OIDs are equal. Never use a broad force push.

## Bounce back

If checks fail or conflicts exist, do not edit the branch. Create or update one comment containing the marker from [bounce-back.md], exact failed checks or conflict files, a small relevant excerpt, reproduction command, and ownership return. Deduplicate by marker.

## Merge

Merge only when the user explicitly authorized this gatekeeper run and all gates pass. Re-fetch and compare the identity record immediately before mutation; if the head or base changed, restart the gate. Follow repository merge policy. Never use an administrator override unless separately and explicitly authorized.

Return exactly one status per PR: `MERGED`, `BLOCKED`, `PENDING`, or `APPROVE`, followed by the PR URL and one-line evidence. Record the result on the linked bead when present. GitHub remains authoritative for PR checks, reviews, mergeability, and the merge itself.
