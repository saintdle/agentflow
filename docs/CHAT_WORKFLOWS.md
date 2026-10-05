# Use Agentflow from an agent chat

You do not need to type Agentflow's coordination commands yourself. A coding
agent in ChatGPT/Codex or Claude, or GitHub Copilot Chat in a supported IDE
Agent mode, can inspect the workspace, create the Beads graph, operate the
controller, collect worker results, and translate durable state back into
ordinary language. The CLI remains the execution and safety boundary underneath
the chat.

Use a chat with terminal access to the target workspace. Open the repository
you want changed in the IDE before sending these prompts. The agent must read
that repository's instructions and domain skills; pointing it only at the
Agentflow repository does not provide project expertise.

In a VS Code Codex/ChatGPT chat, paste the prompt into an agent-mode session
whose workspace is the target repository. In Claude Code, start or resume a
session from that repository and paste the same prompt. In
[GitHub Copilot Chat Agent mode](https://docs.github.com/en/copilot/how-tos/chat-with-copilot/chat-in-ide),
open the target repository in a supported IDE and use the same prompt when
workspace and terminal tools are enabled; approve terminal commands as the IDE
requests. The same guide also works in [GitHub Copilot CLI](https://docs.github.com/en/copilot/get-started/cli-quickstart)
started from the target directory. Browser-only chat without access to the
workspace and terminal cannot operate the local Agentflow CLI. No
Agentflow-specific slash command is required; the agent invokes the installed
CLI and skills on your behalf.

The persistent external-dispatch path in this guide requires Herdr and the
selected provider CLI. Herdr is not needed for planning-only sessions or work
dispatched exclusively through native subagents. `tmux` may be used for manual
terminal sessions, but is not a controller-managed persistent worker transport.
See the [provider compatibility matrix](PROVIDERS.md) for per-provider native,
direct-CLI, and persistent-worker limits.

The workflow has one deliberate human approval gate:

1. Ask the chat to inspect, shape, and persist a proposed goal without workers.
2. Review the goal, boundaries, model routes, evidence, and hosted actions.
3. Approve it explicitly, then let one persistent controller run until a real
   halt or completion.

Use one approved workflow root per controller chat. Related corrections belong
under that root; a materially different goal gets a new root and a fresh chat.
After `GOAL_COMPLETE`, start the next goal in a fresh chat instead of extending
the completed controller conversation indefinitely.

Do not start a second controller for the same root. Do not interpret a claimed
Bead, launched pane, or exited provider process as completed work.

## 1. Install and set up through chat

If Agentflow is not installed, send the setup request from
[AGENT_SETUP.md](../AGENT_SETUP.md). For an already installed machine, use:

```text
Check Agentflow and Beads health for the workspace currently open in the IDE.
Read the workspace's AGENTS.md, CLAUDE.md, Copilot instructions, and relevant
skills. Bring the existing Agentflow installation and managed provider assets
up to the latest explicitly reviewed release without overwriting unmanaged
configuration. Initialize this workspace with local Beads state only if needed.
Do not create a work graph or launch agents yet. Return a concise readiness
report in ordinary language, including anything I must decide.
```

## 2. Shape and persist the proposed work

Replace the bracketed request and constraints, then send:

```text
Use the installed shape-goal, to-tickets, and orchestrate-agents skills with
Agentflow for this workspace.

Goal: [describe the outcome you want]

Inputs: [files, issue or PR links, feedback, specifications, or directories]

Constraints: [write boundaries, allowed local tests, model/provider allowlist,
and actions such as push, merge, hosted tests, deployment, or external messages
that require separate approval]

Inspect the repository and relevant domain skills. Turn this into one measurable
goal contract and a dependency-aware Beads graph with acceptance-to-evidence
rows, exact task ownership, model routes, budgets, review lanes, and halt
conditions. Prefer one primary writer per coherent output, bounded parallel
review, and deterministic validation. Persist the proposed contract and graph,
but do not claim tasks, launch workers, edit implementation files, push, merge,
or mutate hosted state. Explain the plan using human titles followed by Bead IDs,
surface unresolved decisions, and end with one exact approval message I can
copy and paste.
```

For an existing feedback document, a useful concrete opening is:

```text
Read [absolute path to feedback or specification] completely and use it as an
input, not as unquestioned truth. Reconcile every requested change against the
current repository, its tests, and its domain skills. Map every accepted item
to an owner and evidence row; explicitly disposition anything rejected,
superseded, already fixed, or requiring hosted validation.
```

## 3. Approve and run one persistent controller

After reviewing the proposed graph, send the approval in the same planning chat
or a fresh controller chat. Include the root ID returned by planning:

```text
Approved. Use Agentflow root [root-id] in the workspace currently open in the
IDE. Treat its persisted contract, graph, model routes, permissions, evidence
lanes, and halt conditions as authoritative.

Acquire or reattach the single root-controller lease and run the persistent
controller without --once. Continue autonomously through ready implementation,
result collection, independent review, accepted remediation, deterministic
validation, integration, and local delivery. Do not return merely because one
wave was dispatched or because a provider pane exited. Poll through Agentflow
and Herdr without using an LLM to watch idle CI. Stop only at GOAL_COMPLETE,
USER_ACTION_REQUIRED, a durable task block that needs my decision, or the
persisted deadline.

Do not create a replacement root or duplicate tasks. Do not push, merge, run
hosted tests, deploy, or mutate hosted state unless the persisted contract
explicitly authorizes that action. At completion or halt, report human task
titles with IDs, accepted evidence, changes and checks, remaining risk, and one
recommended next request. Never make me relay routine worker outputs between
chats.
```

The phrase `without --once` matters: it tells the chat to keep the controller's
internal transition loop running instead of returning after each dispatch.
Agentflow will still stop at its durable safety boundaries.

## 4. Resume after a disconnect or provider error

Controller state is durable. Start a fresh chat in the same workspace and send:

```text
Resume the existing Agentflow workflow root [root-id]. Do not create a new root,
graph, controller identity, or replacement tasks. Inspect the current controller
lease, Beads state, exact claims, Herdr sessions, submitted results, acceptance
evidence, and any recorded halt. Reattach when authorized, reconcile work that
completed while the chat was disconnected, and continue the persistent loop
until GOAL_COMPLETE or a genuine USER_ACTION_REQUIRED decision. Preserve the
original permissions and model policy. Return a human-readable outcome, not a
list of unexplained Bead IDs.
```

A `503`, IDE reconnect, laptop sleep, or closed chat does not itself authorize a
new graph or duplicate worker. Resume first and let Agentflow determine whether
an existing provider session is live, complete, blocked, or missing.

## 5. Rotate an expensive controller context

Agentflow records task-aware controller progress. Coding can progress through an
edit or check, research and review through evidence or decisions, and an external
wait through a deterministic watcher or state change. It does not use a generic
"ten minutes without an edit" halt because that would misclassify legitimate
read-only and runtime work.

`controller status` includes `session_control.rotation`. After the configured
number of completed tasks or phases it becomes required, but only after the
current worker result has been authenticated and dispositioned. Agentflow then
stops before launching another wave and writes a protected, transcript-free
handoff packet. In the same or a fresh chat, send the normal resume request; an
authenticated `controller resume` acknowledges the rotation and continues the
same root. You can also create the packet explicitly at a safe boundary:

```sh
agentflow controller rotate \
  --root . \
  --workflow-root <root-id>
```

The command writes an ignored, public-safe handoff packet under
`.agentflow/controller/` and prints a copy/paste-ready fresh-chat prompt. The
packet contains the root, checkpoint, ready task summaries, and continuity
identity. It never contains the prior transcript, resume secret, signing key, or
provider credentials. Paste the returned prompt into a fresh agent chat opened
in the same workspace.

For a read-only cost and context review, ask the chat to run:

```text
Synchronize Agentflow's local session-history metadata, then run a 30-day
context audit. Do not read or copy provider transcript content. Explain any
expensive execution routes, excessive delegation, context pressure, or
unattributed sessions in plain language, and recommend only bounded policy
changes. Provider billing pages remain authoritative for cost.
```

When an approach fails, name it precisely. A controller can record the failure:

```sh
agentflow controller progress \
  --root . \
  --workflow-root <root-id> \
  --event failure \
  --task-class coding \
  --task <task-id> \
  --approach "<specific hypothesis or correction>"
```

The second failure of the same named approach produces a durable
`USER_ACTION_REQUIRED` halt. Change the hypothesis or record a decision instead
of silently repeating the same fix.

## 6. Ask for status without changing anything

```text
Report read-only status for Agentflow root [root-id] in this workspace. Inspect
the controller lease, ready and blocked tasks, live Herdr provider sessions,
submitted results, review findings, acceptance evidence, and repository state.
Do not claim, launch, resume, stop, edit, commit, push, or merge anything.
Translate every relevant Bead ID into its human title and explain exactly why it
is ready, active, blocked, or complete. End with the single best next request.
```

## 7. Authorize a bounded delivery action

Keep external actions separate from implementation approval. For example:

```text
The local Agentflow goal [root-id] is accepted. You are now authorized to push
its reviewed branch and open a pull request against [base branch]. Do not merge
the pull request or trigger hosted product tests. Use the repository's PR
template, link the durable goal/evidence without copying private transcripts,
and return the PR URL plus CI status.
```

Only add `merge`, deployment, hosted testing, or external messaging when you
intend to grant that exact authority.

## What good chat output looks like

A controller response should say what happened and what you need to do, for
example:

```text
USER_ACTION_REQUIRED

Governance Timescape integration (lab-42) is blocked because it requires a new
cluster install and cannot be validated in the approved local lane. The writer
completed the other accepted rows; pedagogy review is ready, while lifecycle
review remains blocked by this decision.

Recommendation: create a separately scoped hosted-validation task. No push,
merge, hosted test, or hosted-state mutation occurred.
```

Bare IDs, “workers launched” without live-session evidence, or a request for you
to copy every worker result into another chat are incomplete controller output.
