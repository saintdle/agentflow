# First workflow

This page exposes the underlying CLI operations for learning and diagnosis. If
you want a coding-agent chat to operate them for you, follow the
[chat-first workflow guide](CHAT_WORKFLOWS.md) instead.

This tutorial creates one approved Beads root with one executable task, lets an
Agentflow controller dispatch it through Herdr, and records evidence before the
controller declares the goal complete. It is a persistent, controller-managed
external workflow: install Beads, Herdr, Agentflow, and the selected provider
CLI before starting. Herdr is required for this path; `tmux` is not a transport
for this controller workflow. For Codex, check `herdr integration status` and
install the Codex integration if needed with `herdr integration install codex`;
Agentflow cannot authenticate its session identity without that integration.
The controller starts a stopped local Herdr server on first launch, but will
not install a global provider integration without your action.
Review the [provider compatibility matrix](PROVIDERS.md) before selecting a
provider, especially for Claude lifecycle evidence and Copilot persistent-worker
fail-closed behavior.

The example uses the Codex 5.6 Luna coding route bundled in `models-v2`.
Check that your signed-in account can use the chosen model; a policy-approved
route is not proof of account availability. If that route is not available to
you, first replace all four launch values—provider, model, role, and
effort—with one exact route approved by your project policy.

## 1. Initialize and discover the workspace

```sh
cd /path/to/project
agentflow init . --beads
agentflow beads status .
bd where --json
bd list --all --tree
```

If the workspace already contains a proposed graph, inspect it with
`bd list --all --tree` and `agentflow beads explain <id>`. Reuse an existing
approved root instead of creating a duplicate.

## 2. Create a root and task

Create a root describing the outcome, then a child task with an exact external
launch route and bounded output:

```sh
ROOT_ID=$(bd create "Publish a first-run note" \
  --type epic \
  --labels "agentflow,af:kind:workflow" \
  --description "Add a short first-run note without changing application code." \
  --acceptance "The child task closes with evidence and docs/first-run-note.md exists." \
  --silent)

TASK_ID=$(bd create "Write the first-run note" \
  --type task \
  --parent "$ROOT_ID" \
  --labels "agentflow,af:stage:code,af:role:writer" \
  --no-inherit-labels \
  --description "Create docs/first-run-note.md with one verified setup example." \
  --acceptance "docs/first-run-note.md exists, is non-empty, and contains no private data." \
  --metadata '{"agentflow":{"launch":{"provider":"codex","model":"gpt-5.6-luna","role":"coding","effort":"medium"},"lane":"external","tool_profile":"shell-write","output_boundary":".","context":["AGENTS.md"],"constraints":["Write only docs/first-run-note.md. Create docs/ if needed. Preserve unrelated files and do not expose secrets."],"checks":["test -s docs/first-run-note.md"],"budget":["20 minutes; one retry; stop if blocked."]}}' \
  --silent)
```

Attach one acceptance-to-evidence row to the task and one to the root:

```sh
agentflow acceptance create --task "$TASK_ID" --bead "$TASK_ID" \
  --row "TASK-1::The first-run note exists and is non-empty::writer::static::test -s docs/first-run-note.md"

agentflow acceptance create --task "$ROOT_ID" --bead "$ROOT_ID" \
  --row "ROOT-1::The approved child task closes with recorded evidence::controller::static::closed task plus file check"

agentflow acceptance validate "bead:$TASK_ID"
agentflow acceptance validate "bead:$ROOT_ID"
agentflow beads explain "$ROOT_ID" "$TASK_ID"
```

Read the root, task, route, output boundary, and evidence plan. The graph becomes
approved only when a human authorized to approve this work agrees to those
exact terms. Do not start the controller before that approval.

## 3. Start and inspect the controller

Use `--once` for the tutorial so each transition returns control to your shell:

```sh
agentflow controller start \
  --root "$PWD" \
  --workflow-root "$ROOT_ID" \
  --controller agentflow-controller \
  --once \
  --json

agentflow controller status \
  --root "$PWD" \
  --workflow-root "$ROOT_ID" \
  --controller agentflow-controller \
  --json
```

A successful start claims the ready child, materializes and preflights its
handoff, and starts the provider through Herdr. The provider writes its bounded
result and submits it through the return command supplied by Agentflow. A pane
being live or exited is not completion evidence.

On a new Codex directory, the controller may return `USER_ACTION_REQUIRED`
with `codex_project_trust_required`, the exact directory, and the live Herdr
pane ID. Verify that directory and approve its trust prompt in that pane if
appropriate. Then run `controller resume` with the same root, workflow root,
and controller name. Agentflow preserves the task and pane; it does not approve
trust for you or relaunch a second worker. Do not blindly trust unfamiliar
directories or use a trust-bypass flag to silence the prompt.

After the provider has submitted its result, advance one transition:

```sh
agentflow controller resume \
  --root "$PWD" \
  --workflow-root "$ROOT_ID" \
  --controller agentflow-controller \
  --once \
  --json
```

If the result is valid, Agentflow records its acceptance disposition and closes
the child task. Confirm the durable state:

```sh
bd show "$TASK_ID" --json
agentflow beads explain "$TASK_ID"
test -s docs/first-run-note.md
```

## 4. Handle a halt

`TASK_BLOCKED` or `USER_ACTION_REQUIRED` is a durable stop, not an instruction
to loop `resume` or relaunch the provider. Inspect the controller and graph:

```sh
agentflow controller status \
  --root "$PWD" \
  --workflow-root "$ROOT_ID" \
  --controller agentflow-controller \
  --json
agentflow beads explain "$ROOT_ID" "$TASK_ID"
bd show "$TASK_ID" --json
```

Resolve the recorded cause—for example a missing tool or skill, an invalid
route, a blocked dependency, rejected evidence, or an unresolved live Herdr
session. Record any human decision in Beads. Resume only after the durable cause
is corrected and any prior live session is accounted for. To abandon the run,
first confirm that no provider session is still mutating the workspace, then
release the controller with the `stop` command in the final section.

## 5. Record root evidence and complete

After the child is closed and the file check passes, mark the root evidence:

```sh
agentflow acceptance set "bead:$ROOT_ID" \
  --id ROOT-1 \
  --status passed \
  --evidence "Child $TASK_ID is closed and test -s docs/first-run-note.md passed."

agentflow acceptance validate "bead:$ROOT_ID"

agentflow controller resume \
  --root "$PWD" \
  --workflow-root "$ROOT_ID" \
  --controller agentflow-controller \
  --once \
  --json
```

The final response should report `stop_reason: GOAL_COMPLETE`. A closed root or
an exited provider pane alone is insufficient; every descendant must be
terminal and the root acceptance matrix must contain evidence.

Inspect the final state, then release the controller lease:

```sh
agentflow controller status \
  --root "$PWD" \
  --workflow-root "$ROOT_ID" \
  --controller agentflow-controller \
  --json

agentflow controller stop \
  --root "$PWD" \
  --workflow-root "$ROOT_ID" \
  --controller agentflow-controller \
  --json
```

Without `--once`, `controller start` or `resume` performs these transitions in
an internal loop until `GOAL_COMPLETE`, `USER_ACTION_REQUIRED`, a task block, or
its deadline.
