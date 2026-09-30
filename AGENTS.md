# Coding Agent Workflow

## Start here

- Inspect the target repository for `AGENTS.md`, `CLAUDE.md`, `.github/copilot-instructions.md`, and relevant skills before acting.
- Keep messages and handoffs terse. Report outcomes, evidence, blockers, and next action; omit narration.
- Use the smallest context that can change the result. Point to files instead of pasting them.
- When `bd where --json` succeeds, use Beads for durable goals, task dependencies, decisions, and review dispositions. Keep provider prompts and transcripts out of Beads.
- In user-facing Beads reports, write `<human title> (<id>)`, translate ready/blocked reasons, distinguish a claimed bead from a live agent session, and end with one copy/paste-ready next request. Never make the user decode bare IDs.

## Shape work

- For ambiguous or substantial work, create a measurable goal contract before implementation: outcome, scope, constraints, evidence, and done conditions.
- For substantial, security-sensitive, visual, or environment-dependent work, map every acceptance condition to one owner, authoritative validation lane, and planned evidence before assigning a writer.
- Ask only questions whose answers materially change the result. Do not start implementation while a required goal decision is unresolved.
- Give the user one final checkpoint after the goal is shaped and before implementation begins.

## Delegate deliberately

- Delegate only when the user or applicable instructions explicitly request agents or parallel work.
- Delegate independent, bounded work. Prefer read-heavy exploration, research, tests, or review.
- Default to at most three workers, one nesting level, and one writer for any Git branch/worktree or gitless output boundary.
- Give each worker a return contract: concise result, evidence/file references, checks run, and blockers. Do not return raw logs.
- Route queue work with scoped `af:stage:*`, `af:role:*`, and `af:cap:*` labels. Pull only below one approved root, preferring work assigned to the actor before shared unassigned work.
- Use native subagents for bounded fire-and-forget work. Use a controller-managed
  Herdr session only when cross-provider work, a long run, or likely human
  decisions justify it; tmux is for manually managed terminal sessions, not
  controller dispatch.
- A blocked external worker must stop with the decision needed, evidence tried, options, recommendation, and current branch/check state. The user may intervene directly, but the decision must be recorded in durable state.
- Preflight every external handoff for readable context, explicit tool profile, required tools, required project domain skills, output boundary, exact base, budget, retry cap, and delegation mode before launch.
- Treat project-local domain skills as authoritative. Agentflow routes skills; it does not own domain expertise.
- Require structured review findings and record controller dispositions. Reproduce accepted high and medium findings before returning them to the writer.
- Use a cheaper/faster model at low or medium effort for routine scans. Reserve the strongest model and high effort for ambiguous planning, hard debugging, conflict resolution, or final review.
- Classify writable artifacts before dispatch. For Claude-authored reader-facing Markdown, automatically use the conditional `agentflow prose prepare` lane unless the user opts out. It launches no editor when checks pass and at most one edit through the exact configured, policy-approved route when they fail; the controller validates and accepts any replacement. With no route, report findings without a model call. Exclude code, chat, research notes, reviews, and internal handoffs.

## GitHub workflow

- In repositories configured for PR mode, connect implementation to a bead or required GitHub issue before coding and use one branch/worktree per writable bead.
- Agents open PRs; they do not merge their own PRs.
- A gatekeeper processes ready PRs by priority, then FIFO. It never fixes a contributor branch.
- If checks fail or conflicts exist, leave one deduplicated diagnostic comment and return ownership to the original worker.
- Use deterministic CI watchers; no LLM session waits on checks. Route a failure through one bounded CI-triage bead, then return a separate code fix to the original writer only when diagnosis requires it.
- Merge only when checks, review, and repository policy pass and the current run is authorized to merge.

## Safety and quality

- Never expose credentials or copy provider session transcripts into handoffs.
- Treat provider-reported dashboards and usage commands as authoritative; label local cost figures as estimates.
- Record task class, model, effort, allowance or credits, elapsed time, retries, input size, findings, accepted findings, checks, and outcome when available.
- Run the narrowest relevant checks, inspect the diff, and state anything not verified.
- Keep one approved root per controller chat. Resume from Beads and linked files rather than transcripts; use `agentflow controller rotate` when context rotation is recommended, and stop after two failures of the same named approach.
- Preserve unrelated user changes. Do not overwrite existing agent configuration during installation.

## Repository checks

- Run `python3 -m unittest discover -s tests -p 'test_*.py'`.
- Run `python3 scripts/validate.py`.
- Run `git diff --check` and inspect `git status --short`.
