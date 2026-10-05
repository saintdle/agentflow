# Provider compatibility

This matrix describes Agentflow integration lanes, not provider account
entitlements, subscription access, or current model availability. A route in
the [local model policy](CONFIGURATION.md#model-policy) is only an Agentflow
allowlist entry; the provider may still reject that model for a particular
account. Unknown provider/model/role/effort combinations fail closed.

| Provider | Chat + Agentflow controller | Provider-native agents | Direct provider CLI | Persistent Herdr worker |
| --- | --- | --- | --- | --- |
| Codex | Supported from a workspace chat with terminal access | Supported by Codex multi-agent/subagent mode | Supported for manual provider use | Supported through the leased root controller; install and verify the Codex Herdr integration first |
| Claude Code | Supported from a repository session with terminal access | Supported by Claude Code subagents | Supported for manual provider use | Conditional: Claude Code 2.1.251 or newer, controlled lifecycle hooks, and the primary/fallback model pin described below |
| GitHub Copilot | Supported from Copilot Chat Agent mode or Copilot CLI with workspace/terminal access | Supported by IDE agent mode and Copilot CLI `/fleet` | Supported for manual provider use | Disabled and fail-closed: Copilot remains usable for chat/controller and native work, but Agentflow cannot verify the resolved model for a persistent Copilot worker |

“Chat + controller” means a capable coding-agent chat may ask Agentflow to
inspect and operate one approved workflow root. A separate native agent is
still provider-managed. “Direct provider CLI” is for manual provider use; it is
not an external Agentflow handoff and does not carry the controller's
authenticated result channel. Persistent external workers are launched only by
the leased root controller through Herdr. Agentflow does not substitute a
direct provider command or `tmux` when that lane is unavailable.

The provider command contracts are intentionally not one shared flag template:

| Provider | Pinned model/effort form used by Agentflow |
| --- | --- |
| Codex | `codex --model MODEL -c model_reasoning_effort=EFFORT PROMPT` |
| Claude Code | `claude --model MODEL --effort EFFORT PROMPT` |
| Copilot CLI | `copilot --model MODEL --effort EFFORT --interactive PROMPT` |

These are CLI argument shapes, not proof that a model is available to your
account. The current tests exercise the real argv builder against routes from
the packaged policy; controller preflight remains authoritative for an actual
launch.

## Claude persistent-worker compatibility

Agentflow checks that Claude Code is at least **2.1.251** before a
lifecycle-evidence Herdr launch. That release floor is required for native
`PostModelSwitch` observations; Claude's event reports session-level changes,
including fallback or resume changes. The controller also requires the
controlled `SessionStart` and `PostModelSwitch` hooks in the project settings
before spawn. See [Claude Code hooks](https://code.claude.com/docs/en/hooks)
and Agentflow's [security limits](SECURITY.md#preflight-and-provider-policy).

`PostModelSwitch` does not report a different model for every response in a
fallback chain. At spawn, Agentflow pins both Claude's primary `--model` and
`--fallback-model` to the exact approved primary model. This prevents a
configured fallback from silently serving an unreported model. The local hook
events remain cooperative observations, not cryptographic provider proof.
Unavailable, unparsable, or too-old Claude versions fail closed.

## Copilot persistent-worker boundary

Copilot's documented `sessionStart` payload carries a session ID, timestamp,
working directory, and source, but no resolved model field; the native hook
shape therefore cannot establish the requested worker model. Agentflow's
additional local Copilot telemetry is not protected from the same-UID worker.
The persistent Copilot worker path is consequently rejected before provider
lookup/spawn. This restriction does **not** disable Copilot Chat Agent mode,
Copilot CLI `/fleet`, or controller use from a Copilot chat. See the primary
[Copilot hooks reference](https://docs.github.com/en/copilot/reference/hooks-reference),
[Copilot CLI fleet documentation](https://docs.github.com/en/copilot/concepts/agents/copilot-cli/fleet),
and Agentflow's [security limits](SECURITY.md#preflight-and-provider-policy).

## Opt-in live smoke

The default compatibility checks are local and make no provider calls. Do not
run a live smoke unless a human has explicitly approved a bounded provider
call. There is no direct handoff/CLI smoke path: use an existing approved root
and its normal Agentflow controller.

Use a dedicated approved workflow root where the named `$TASK_ID` is the only
ready nonterminal descendant. Check `agentflow beads explain "$ROOT_ID"
"$TASK_ID"` and `bd show "$TASK_ID" --json` first. Confirm that the task's
persisted provider, exact model, role, and effort match `$PROVIDER`, `$MODEL`,
`$ROLE`, and `$EFFORT`; its model route must be present in the approved policy.
The root launch policy must admit only one attempt (`max_attempts_per_task: 1`)
and one launch (`launch_budget_multiplier: 1`), with one parallel worker at
most (`max_parallel_workers: 1`). The task budget must say “three minutes; no
retry; stop if blocked.” Use a read-only task with no delegation. If any check
is unclear, do not run it.

The `TASK_ID` guard is graph-based: controller start accepts a root, not a task
selector, so the one-ready-task requirement prevents dispatching another child.
Copilot is deliberately excluded. The controller and provider-task budgets
are each bounded at three minutes. A deadline, provider error, or halt means
stop, inspect durable state and the existing Herdr session, and do not retry or
resume this smoke. The deadline does not forcibly terminate a provider pane.

```sh
: "${LIVE:?Set LIVE=1 only after approving one bounded provider smoke}"
test "$LIVE" = "1"
: "${ROOT:?Set ROOT to the exact workspace path}"
: "${ROOT_ID:?Set ROOT_ID to the approved workflow root ID}"
: "${TASK_ID:?Set TASK_ID to the one ready task under that root}"
: "${PROVIDER:?Set PROVIDER to codex or claude}"
: "${MODEL:?Set MODEL to the exact model in the task contract}"
: "${ROLE:?Set ROLE to the exact role in the task contract}"
: "${EFFORT:?Set EFFORT to the exact effort in the task contract}"
cd "$ROOT"
case "$PROVIDER" in
  codex|claude) ;;
  *) echo "Live persistent Herdr smoke is limited to Codex or Claude Code." >&2; exit 2 ;;
esac

printf 'Approved smoke tuple: root=%s task=%s provider=%s model=%s role=%s effort=%s\n' \
  "$ROOT_ID" "$TASK_ID" "$PROVIDER" "$MODEL" "$ROLE" "$EFFORT"
agentflow beads explain "$ROOT_ID" "$TASK_ID"
bd show "$TASK_ID" --json
LIVE=1 agentflow controller start \
  --root "$ROOT" \
  --workflow-root "$ROOT_ID" \
  --controller agentflow-controller \
  --deadline 180 \
  --json
```

The values above are deliberate human guards; the controller independently
loads the task's persisted route and validates it against the approved policy
before provider spawn. Do not change task metadata to force a route during the
smoke. If the controller returns a deadline or durable halt, inspect status and
the existing Herdr session before taking any separate action; this recipe
never authorizes another launch.

Native feature references: [Codex subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents),
[Claude Code subagents](https://code.claude.com/docs/en/sub-agents),
[Copilot CLI `/fleet`](https://docs.github.com/en/copilot/concepts/agents/copilot-cli/fleet),
and [Copilot Chat Agent mode](https://docs.github.com/en/copilot/how-tos/chat-with-copilot/chat-in-ide).
