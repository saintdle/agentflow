# Controller recovery

Controller checkpoints are durable workflow authority. A terminal halt is not
automatically reopened just because Beads later exposes ready work. For the
specific `USER_ACTION_REQUIRED: NO_READY_WORK` halt (including its known
pre-marker legacy wording), an operator may run the following command using the
same protected controller credential:

```sh
agentflow controller resume --root /path/to/workspace --workflow-root ROOT --controller agentflow-controller --acknowledge-no-ready-halt
```

Replace `/path/to/workspace` and `ROOT` with the exact workspace path and
workflow-root ID from the original controller command. Keep the same
`--controller` value as that command (`agentflow-controller` is the default).
When the original command used the default protected credential location, omit
`--resume-token` and `--resume-key-file` so resume reuses that credential. If it
used a non-default external `--resume-key-file`, pass that same protected path.
This is an authenticated resume, not a reset or takeover; do not add
`--takeover`.

Agentflow requires a valid reattach proof for the existing workflow/controller
incarnation, an unchanged root binding, no active or ambiguous worker/session,
no repeated-approach block, and a fresh read-only ready-descendant check. The
acknowledgement is recorded in the checkpoint before ordinary scheduling
proceeds; the scheduler still performs its normal atomic claim. It cannot
reopen completed/failed workflows or other safety/user-action halts, and it
does not clear prior results, evidence, or checkpoint history.

## Expired provider identity

When a controller is halted because a provider identity never resolved, first
inspect the exact task, launch, and pane recorded by Agentflow. Recovery is
available only when the protected launch ledger proves that the original
deadline has expired, the signed launch still matches the checkpoint and
current Beads claim, and Herdr returns its structured `pane_not_found` response
for that exact pane. A timeout, daemon error, or any other pane response leaves
the halt in place.

Use the launch ID and pane ID from the authenticated lifecycle record:

```sh
agentflow controller recover-preidentity \
  --root /path/to/workspace --workflow-root ROOT \
  --controller agentflow-controller \
  --task TASK --launch-id LAUNCH_ID --pane-id PANE_ID
```

This records a separate signed cancellation disposition, revokes the old
return channel, and then clears the checkpoint's active pointer. It preserves
the original task, claim, launch attempt, deadline, and result history. A late
provider submission cannot be accepted, and the cancelled task cannot be
launched again under the expired budget.

Recovery leaves the workflow halted until an operator explicitly selects a
different ready descendant using the same canonical protected controller
credential:

```sh
agentflow controller resume \
  --root /path/to/workspace --workflow-root ROOT \
  --controller agentflow-controller \
  --continue-after-cancelled-preidentity TASK --continue-task READY_TASK
```

`TASK` must be the cancelled preidentity task and `READY_TASK` must be a
distinct current ready descendant. Agentflow verifies the signed cancellation,
controller incarnation, root, and absence of other active work before
reopening the checkpoint. This flow does not close the cancelled Beads task
or claim a provider result for it.

If this explicit continuation is interrupted while its protected credential
is being rotated, rerun the same command with the same task IDs. Ordinary
controller start, resume, supervise, recovery, stop, progress, rotate, and
waiver commands remain fenced until that exact continuation finishes. Mutating
controller commands always use the namespaced checkpoint; `--checkpoint-path`
is available only to read-only `controller status`.

## Superseded legacy launch without budget metadata

Some older signed launches predate structured execution budgets. Their return
contracts and session records have no deadline or retry ledger, so Agentflow
cannot establish that their launch deadline expired. When the owning scope has
been superseded, retire that launch explicitly instead of treating an advisory
identity timer as a deadline:

```sh
agentflow controller retire-superseded-legacy-preidentity \
  --root /path/to/workspace --workflow-root ROOT \
  --controller agentflow-controller \
  --task TASK --launch-id LAUNCH_ID --pane-id PANE_ID
```

This command accepts only an authenticated signed legacy contract with no
structured-budget fields or protected task ledger row, the exact retained
identity-pending checkpoint and claim, no committed binding or result, no
other active work, and Herdr's definitive `pane_not_found` response. Any
present or malformed budget evidence, a live or uncertain pane, or mismatched
identity leaves the task unchanged. The signed retirement says
`superseded_legacy_scope` and records budget availability as unknown; it does
not manufacture an expiry, attempt count, or ledger entry. It revokes the old
result channel before clearing the checkpoint pointer. Continue only through
the distinct-ready-task command above, using the same protected controller
credential and the cancelled task ID.
