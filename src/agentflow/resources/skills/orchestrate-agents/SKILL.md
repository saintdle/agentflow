---
name: orchestrate-agents
description: Split an approved goal into bounded agent assignments, choose Codex, Claude Code, or GitHub Copilot and an appropriate model/effort, create terse handoffs, and coordinate parallel or sequential workers. Use when the user explicitly asks for subagents, delegation, parallel agents, a controller agent, a model-routing plan, or a handoff between coding-agent providers. Do not use merely because multiple tasks exist.
---

# Orchestrate Agents

Read [routing.md](references/routing.md) before choosing providers or models.
When `bd where --json` succeeds in the target worktree, also read
[beads.md](references/beads.md) and use Beads as the durable coordination backend.

## Decide whether to delegate

Delegate only when explicitly requested or required by active instructions. Use the main agent when work shares substantial context, needs frequent user feedback, or is a small targeted change.

Apply the automation-first routing check before dispatching any worker: ask "Does an owned deterministic command, bot, or API already produce this result?" Prefer that stable path for routine maintenance and record the automation decision as durable evidence. Assign an agent to ambiguity, conflicts, diagnostics, and missing automation, not to work a script already owns.

Delegate when a task is independent and bounded, produces noisy output, benefits from a restricted tool set, or can run in parallel without overlapping file ownership.

## Choose an execution lane

Use native subagents by default for bounded work that should run fire-and-forget. Their provider UI is sufficient when the worker is unlikely to need a user decision before returning evidence.

Use an observable external session when work is long-running, crosses providers, needs a separate worktree, or is likely to stop for approval or judgment. For controller-managed external work, use Herdr; Agentflow does not dispatch through tmux. Tmux is for manually managed terminal sessions only, not a controller fallback. Treat the session tool as a cockpit only. Beads is authoritative when initialized; otherwise use the repository's issues and PRs. Handoff prompts are always transient.

Before dispatch, classify every writable artifact as `reader-facing` or
`internal`. When Claude or Copilot Claude is the writer of reader-facing
Markdown such as a blog post, documentation page, or learner-facing lab
assignment, automatically select the conditional prose-quality lane unless the
user opts out. Record the classification and writer provider in the handoff.
After the writer returns, run `agentflow prose prepare` with the matching
domain profile, required skill, and authoritative domain checks. A passing
artifact launches no editor. A failing artifact gets exactly one editing pass
through the exact route configured in `.agentflow/config.json`; the bundled
default is Codex Luna `medium`, while approved Claude or Copilot routes can be
selected when OpenAI access is unavailable. Verify the sibling and let the
controller choose whether to replace the source. With no configured editor,
report the deterministic findings without launching anything. Never apply this
lane to chat, code, research notes, reviews, or internal handoffs.

## Route domain skills

Agentflow routes skills; it does not own domain expertise. Treat the target repository's skills as authoritative for its domain. Inspect the worker's target directory and identify every skill required to complete or review the assignment. Do not rely on a global skill with similar scope when the repository supplies one.

For external handoffs, add one `--require-skill <name>` per required domain skill. Preflight from the worker's actual `--cwd`; it must resolve a readable provider entrypoint and record its path and SHA-256 in the handoff sidecar. Stop before launch if a required skill is unavailable. For native workers, name the required skills explicitly in the assignment and verify that their target working directory exposes them.

Every external worker must be launched by the leased root controller so it
receives a minted authenticated result channel. Never use direct `agentflow
handoff launch` for an external handoff. When the approved outbound file set
must exclude repository instructions or unrelated source, persist
`sterile=true` (or `outbound_context=restricted`) in the task's launch metadata.
The controller then launches from a hash-inventoried package. Sterile skills
must be self-contained; stop rather than widening a package for unresolved
transitive references. The current sterile lane is read-only; never select it
for `shell-write` work.

## Partition work

1. Confirm an approved goal and its exit conditions.
2. Build a dependency graph. Put shared interfaces and decisions before dependent implementation.
3. Assign disjoint scopes. Give each writable file or glob one owner at a time.
4. Default to three or fewer workers and one delegation level. Use separate worktrees for concurrent Git writers. Without Git, assign disjoint output paths and serialize overlaps.
5. Keep the controller responsible for user decisions, sequencing, synthesis, and durable state.

The default root mode is controller-only. The controller must not implement
product-file changes merely because it can; assign each write to an explicit
execution task. For native Codex delegation from a Sol controller, select the
managed Luna worker/explorer profile and use `fork_turns="none"` or the
smallest bounded positive fork. Never allow an omitted model to inherit Sol
for coding or exploration. Terra is permitted only as an explicitly recorded
selective execution route. Keep worker subdelegation disabled by default.

A Beads-enabled pull queue must stay inside one approved root. Label work with
one `af:stage:<name>`, one `af:role:<name>`, and only material
`af:cap:<name>` requirements. Use `agentflow worker pull` so an actor claims its
assigned ready work before shared unassigned work. Never let an agent claim
globally merely because a bead is ready.

A provider used only as the controller or independent reviewer still counts as participating; do not invent implementation work merely to use every provider. Resolve the base SHA, exact scopes, evidence sources, and numeric provider budgets before launching workers. If a provider has no enforceable session cap, give it a time box, maximum retry count, and stop condition in the assignment.

If an external assignment uses structured Agentflow execution limits, provide
both `--deadline-seconds` and `--max-retries`. For a Beads task, persist them in
`metadata.agentflow.launch.execution_limits` (or the compatible
`metadata.agentflow.execution_limits` fallback); direct handoffs use the same
two CLI flags. The positive deadline is fixed on first launch and shared across
resume; retries count additional Agentflow launches and can only tighten the
graph's existing attempt and global-launch budgets. The protected ledger
preserves that first deadline across retries.
Deadline enforcement covers only the supervised provider process group on
supported systems; detached descendants can outlive it. Older handoffs without
`execution_limits` remain advisory/unavailable, and these fields do not enforce
provider spending caps. Keep provider quota provenance separate from local
task outcomes and timing when recording or comparing usage.

For substantial work, validate the approved acceptance-to-evidence matrix before assigning a writer. Give every row one owner and keep lower validation lanes from claiming evidence that only a higher lane can prove.

## Write each assignment

Use `agentflow handoff from-bead <id>` when Beads is active. Use
`agentflow handoff create` for the backwards-compatible file workflow, or this contract:

```text
ASSIGN <task-id>
objective: <one observable result>
lane: <native or external; session tool and target when external>
scope: <files, subsystem, or read-only question>
skills: <required skill names or none>
base: <branch and SHA>
dependencies: <task IDs or none>
acceptance: <testable conditions>
verify: <exact commands or evidence>
constraints: <do-not rules and permissions>
budget: <credit cap, time box, retry cap, and stop condition>
return: outcome, branch/commit, checks, risks, refs; no raw logs
```

`agentflow handoff create` also writes a machine-readable sidecar. For every external worker:

1. Package only required files and record the task class, explicit tool profile, required tools, required domain skills, output boundary, budget, retry cap, delegation mode, and exact base.
2. Run `agentflow handoff preflight <handoff>`. Add `--require-matrix` for substantial or environment-dependent work.
3. Run preflight with `--cwd <worker-worktree>` when the handoff was created elsewhere. Confirm that every required skill resolves to the intended project/provider entrypoint.
4. Do not launch until preflight passes. Treat it as a launcher capability check, not proof of the provider's later reasoning.
5. Resume the leased root controller to launch. It owns the authenticated
   return channel and rejects missing or mismatched runtime model evidence.
6. If the task is direct review, keep delegation disabled.

Workers reply with one leading verb: `REPORTED`, `BLOCKED`, `ADVICE`, `REVIEW`, `FIXED`, or `APPROVE`. Require factual claims to include a file/line, commit, command result, URL, or `untested`.

For `REVIEW`, require each finding to include stable ID, severity, class, exact evidence, reproduction, expected and actual behavior, proposed correction, authoritative gate, and confidence. The controller records one disposition with `agentflow review record`: `accepted`, `rejected-factual`, `rejected-preference`, `duplicate`, or `deferred`. Reproduce every accepted high or medium finding deterministically before returning it to the writer.

For an accepted finding that needs a correction, use `agentflow review
route-fix` to create one fix bead assigned to the original writer when known.
The fix blocks the review until it closes; the same reviewer then gets first
claim on its assigned review before the shared queue.

Every assignment must include this halt contract:

```text
If blocked, stop and return:
BLOCKED: <one decision or dependency>
tried: <brief evidence>
options: <A and B when a choice exists>
recommend: <preferred option and why>
state: <branch/worktree or gitless output boundary, last completed check, and remaining risk>
```

## Coordinate

- Run independent read-only discovery and review in parallel.
- Serialize overlapping writes. Reuse the original worker for fix rounds.
- Integrate completed worktrees one at a time onto an integration branch: confirm the recorded base, fetch the exact worker commit, cherry-pick or merge according to repository policy, run the affected checks, and bounce conflicts back to the owning worker. Do not have the controller silently repair another writer's conflict.
- Let the controller broker worker-to-worker questions; do not create uncontrolled fan-out.
- In an external session, let the user attach directly to a `BLOCKED` worker when that is cheaper or clearer than relaying through the controller. Record the decision in the bead when active, otherwise in the issue or PR, before resuming dependent work.
- Do not use a strong controller as a message bus. Spend its context on decisions, sequencing, synthesis, and final review.
- Stop completed workers. Do not keep a session alive to poll external state.
- Use `agentflow ci watch` as a deterministic external-check watcher. On failure it creates one deduplicated `ci-triage` bead; a bounded low-cost CI worker diagnoses without editing product code and routes a separate correction to the original writer only when required.
- Pass artifact paths and distilled facts instead of transcripts or full logs.
- Record provider, model, effort, task class, cap or allowance delta, elapsed time, retries, input size, findings, accepted findings, checks, and outcome with `agentflow usage record` when available.

## Finish

Map returned evidence to every goal exit condition. For attached matrices, use `agentflow acceptance set` to mark each row `passed`, `failed`, or `waived`, attach actual evidence, and validate the completed matrix. State gaps. If the work uses PR mode, hand ready PRs to `gatekeep-prs`; workers never merge their own PRs.

Before a user-facing status, resolve referenced items with `agentflow beads
explain <id...>`. Write each as `<human title> (<id>)`, state its workflow
stage, why it is ready or blocked, and whether it is merely assigned/claimed or
has a confirmed live session. End with exactly one recommended,
copy/paste-ready `Next request:`. Do not expose internal IDs without translation.
