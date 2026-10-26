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

## Expired launch for a task already closed in Beads

If the exact task is already closed while its signed preidentity launch still
has an issued, unbound return channel, an operator can retire that launch with
the current protected controller credential:

```sh
agentflow controller retire-closed-preidentity \
  --root /path/to/workspace --workflow-root ROOT \
  --controller agentflow-controller \
  --task TASK --launch-id LAUNCH_ID --pane-id PANE_ID
```

The command requires the exact owned task to remain a descendant of the
workflow root, the canonical credential for the current active controller
epoch, and a signed launch from an earlier positive epoch whose protected
budget ledger proves expiry. The checkpoint must match the current controller
epoch, and the task must be the sole unbound identity-pending claim. The
command also verifies the native claim-generation seal, the unbound Herdr
record, absence of a result, and Herdr's structured `pane_not_found`
response. It records a signed budgeted retirement and revokes the old return
channel before placing the controller checkpoint in a halted state. The task
stays closed with its claim metadata and fence evidence intact; the original
attempt and budget remain unchanged. Retries finish the same disposition after
an interrupted checkpoint write.

Some closed claims have no native claim-generation seal. Because the signed
launch contract does not establish whether the seal was never created or later
deleted, the legacy acknowledgement flag alone cannot authorize retirement.
An owner may explicitly accept that origin as unresolved for this one negative
retirement:

```sh
agentflow controller retire-closed-preidentity \
  --root /path/to/workspace --workflow-root ROOT \
  --controller agentflow-controller \
  --task TASK --launch-id LAUNCH_ID --pane-id PANE_ID \
  --accept-unresolved-generation-origin \
  --reason "Generation origin is unknown; revoke this closed launch only."
```

The signed audit binds the owner's reason to the exact root, controller lease
and fence, checkpoint, launch contract, claim identity, current claim-history
digest, and expired budget record. It records the origin as unresolved and only
revokes that closed launch. It does not classify the launch as pre-seal,
create a generation seal, renew the claim, accept a result, reset its budget, or
dispatch work. Present seals must still match exactly; the legacy
`--acknowledge-missing-native-generation` flag alone is rejected. Retrying the
same retirement requires the same signed decision and unchanged evidence.
Typed drain causes still require their supported authenticated resolution
paths. Unknown causes require the separate owner decision described below.

Retire the closed launch first. Then inspect the resulting blocked checkpoint.
If its complete drain-cause set is still unknown, an owner may explicitly
accept that exact set as **unresolved** with the current protected credential:

```sh
agentflow controller accept-unresolved-drain-causes \
  --root /path/to/workspace --workflow-root ROOT \
  --controller agentflow-controller \
  --acknowledge-unresolved-causes \
  --reason "Historical cause provenance is unavailable; retain this uncertainty."
```

The command requires a current lease, a blocked checkpoint at that lease's
epoch, no active task, and a non-empty owner reason. It signs and audits the
complete cause IDs and digest together with the checkpoint fingerprint, root,
epoch, and current fence. The audit retains the original cause records. It
does not mark them resolved or supply missing task, launch, diagnostic, or
outcome bindings. It does not change the checkpoint or authorize work by
itself. Any later checkpoint change, including another retirement, makes the
receipt stale; inspect and explicitly accept the new snapshot after that
change.

With the exact acceptance recorded, use the explicit continuation command in
the expired preidentity recovery section, passing this task as `TASK` and a
distinct ready descendant as `READY_TASK`. Continuation still checks the
signed retirement, root ancestry, unchanged claim-history evidence, budgets,
absence of other active work, and the exact ready target. The unresolved-origin
receipt only authenticates negative retirement; it does not start continuation
or authorize work by itself. Without matching owner receipts, unknown causes
remain a blocker.

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

## One explicitly authorized abandoned-epoch repair

Ordinary recovery rejects a controller state epoch that no longer matches the
signed launch. A separate owner repair command exists only for an explicitly
authorized, exact unowned legacy epoch gap. It requires the canonical
protected credential, the signed unbudgeted launch, a definitively absent
pane, and the exact state, checkpoint, and contract SHA-256 fingerprints from
the approved snapshot. It does not dispatch work or reset a claim, history, or
budget. Do not substitute newly calculated fingerprints if any snapshot has
changed; the command must fail closed and be reassessed.

```sh
agentflow controller repair-abandoned-epoch \
  --root /path/to/workspace --workflow-root ROOT \
  --controller agentflow-controller \
  --task TASK --launch-id LAUNCH_ID --pane-id PANE_ID \
  --acknowledge-abandoned-epoch --abandoned-epoch EPOCH \
  --expected-state-sha256 STATE_SHA256 \
  --expected-checkpoint-sha256 CHECKPOINT_SHA256 \
  --expected-contract-sha256 CONTRACT_SHA256
```

The repair records a protected signed intent before rotating the controller
credential and lease. Rerun only this exact command after an interrupted
transaction. After the old launch is durably cancelled, the command leaves
the workflow halted; continue only by explicitly selecting a distinct ready
descendant with the continuation command above.
