# Controller recovery

Controller checkpoints are durable workflow authority. A terminal halt is not
automatically reopened just because Beads later exposes ready work. For the
specific `USER_ACTION_REQUIRED: NO_READY_WORK` halt (including its known
pre-marker legacy wording), an operator may run
`agentflow controller resume --workflow-root ROOT --acknowledge-no-ready-halt`
using the same protected controller credential. Agentflow requires a valid
reattach proof for the existing workflow/controller incarnation, an unchanged
root binding, no active or ambiguous worker/session, no repeated-approach
block, and a fresh read-only ready-descendant check. The acknowledgement is
recorded in the checkpoint before ordinary scheduling proceeds; the scheduler
still performs its normal atomic claim. It cannot reopen completed/failed
workflows or other safety/user-action halts, and it does not clear prior
results, evidence, or checkpoint history.
