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
