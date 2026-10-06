# Optional Codex SDK integration

The optional Codex SDK diagnostics are read-only. The App Server worker
transport is an experimental prototype and is currently blocked before
inference for configured worker tasks. Herdr remains the default; installing
the optional extra or selecting `app-server` does not override this guard or
make worker execution available or alter existing defaults and profiles. A
read-only sandbox alone does not isolate same-user controller authority and
result-return capabilities. Do not attempt to bypass the guard; use the
existing Herdr route for worker execution.

## Permission-profile probe status

The repository includes a standalone generator and validator for a synthetic
Codex named-permission profile, plus an opt-in, no-thread/no-turn App Server
`command/exec` canary. It checks a known fixture read against fake controller
token/lease files inside the workspace, fake authority files outside it, a
symlink alias, and write attempts. Its profile denies filesystem root and
network access, grants minimal and workspace reads, explicitly denies
`.agentflow` controller state and the external authority directory, and sets
approval policy to `never`. Run the direct canary only in a disposable local
environment with the pinned optional SDK installed:

```sh
PYTHONPATH=src AGENTFLOW_CODEX_PERMISSION_PROBE=1 \
  python -m unittest discover -s tests -p 'test_codex_permissions.py'
```

The deterministic profile tests run without the optional SDK. A passing direct
canary is narrow evidence about that SDK runtime's `command/exec` behavior for
the synthetic paths only. It does not establish the active profile of a model
thread/turn, constrain inherited MCP or other external tools, or prove
same-user isolation for production workers. The helper is not connected to
worker admission; the existing pre-inference hard guard remains unchanged.
Treat unsupported or ambiguous canary results as a failed proof, not as
permission to enable App Server workers.

## Install and configure

Install the optional `codex` extra (`openai-codex==0.160.1`) only when you
intend to use this integration. For a local source checkout, for example:

```sh
pipx install '.[codex]'
```

Keep the extra out of the default installation if you do not need the SDK.
Merge this setting into the existing top-level config in
`.agentflow/config.json` or the ignored `.agentflow/config.local.json`; do not
replace the full config file. Existing configurations continue to use Herdr:

```json
{
  "codex": {
    "transport": "app-server"
  }
}
```

This transport-only setting remains valid. An optional
`codex.worker_timeout_seconds` integer sets the App Server worker timeout; it
defaults to `1800` seconds and accepts values from `1` through `86400`. The
`180`-second value below is a test timeout only; configuring it does not enable
or authorize a worker turn. Merge the optional field into the same existing
`codex` object rather than replacing project configuration:

```json
{
  "codex": {
    "transport": "app-server",
    "worker_timeout_seconds": 180
  }
}
```

Use `"herdr"` to explicitly retain the existing transport. The setting selects
the transport only for leased root-controller tasks whose persisted provider
is `codex`; Claude and Copilot tasks continue to use their existing Herdr
routes. An explicit App Server task route for a non-Codex provider is rejected.
The App Server adapter is controller-owned, not a standalone worker command;
selecting it does not bypass the pre-inference guard. Review the resolved
config and run diagnostics; do not copy provider credentials into project
configuration.

The SDK uses its own pinned Codex runtime, not necessarily the globally
installed `codex` executable, and the user's existing ChatGPT/Codex
authentication. Authentication and availability remain controlled by OpenAI
and the user's account. Check the optional integration without starting an
inference or paid worker turn:

```sh
agentflow codex diagnostics
agentflow codex diagnostics --json
```

Diagnostics report SDK/runtime support, version, authentication type, and
supported numeric quota/token fields only. They do not expose email or account
IDs, raw provider errors or output, or private history; they do not perform
login, logout, or token refresh. These checks are best-effort: unsupported
endpoints, missing authentication, or unavailable account metadata must be
reported as unavailable, not interpreted as permission to launch or as
evidence of available capacity. Diagnostic values and catalogue entries do not
establish model entitlement or guaranteed capacity. See the official [Codex SDK guide](https://learn.chatgpt.com/docs/codex-sdk)
and [App Server protocol](https://learn.chatgpt.com/docs/app-server).

If a future release enables App Server worker execution, each task will still
require explicit approval of its exact model, effort, workspace, allowed
skills, and launch budget. The prototype supports only the existing
`shell-readonly` handoff profile and pins the SDK thread and turn to
`Sandbox.read_only` and approval policy `never`. These restrictions are
necessary but not sufficient to establish isolation from same-user controller
authority, so the current hard guard rejects App Server worker tasks before
inference. `shell-write`, `no-shell`, `provider-default`, and sterile or
restricted-outbound tasks are also rejected; SDK subdelegation is disabled.
Do not weaken the guard or permissions to make a task eligible. Diagnostics do
not start a worker and are not workflow consent.

For example, a chat can check readiness without starting work:

```text
Run the read-only Agentflow Codex diagnostics for this workspace. Do not start
an inference or worker, change authentication, or modify configuration. Report
only the supported/redacted diagnostic fields and any unavailable checks.
```

The next prompt is planning-only; App Server worker execution remains blocked.
For worker execution, select the existing Herdr transport. After reviewing the
persisted contract, use [the chat workflow's approval and run step](CHAT_WORKFLOWS.md#3-approve-and-run-one-persistent-controller).
That approval authorizes the persistent controller to continue within the
root's saved limits; it does not require approval for each wave.

```text
For Agentflow workflow [workflow-id], plan a bounded read-only Codex task using
the existing Herdr transport; App Server worker tasks are currently blocked.
Inspect the exact model, effort, workspace, allowed skills, and launch budget.
Do not start a worker until I explicitly approve this workflow and those
limits. Do not substitute another model or provider.
```

## Keep the workflow contract authoritative

The approved workflow contract and Agentflow policy remain authoritative for
the exact model and effort, workspace boundary, allowed skills, retries, and
launch budget. The Codex thread and turn are explicitly pinned to the approved
read-only sandbox and `never` approval policy. Do not let an SDK thread,
environment default, or provider-side setting broaden those limits. If the
approved route cannot be honored, stop and report the blocker; do not substitute
another model, effort, workspace, transport, permission, or skill set.

The guarded prototype returns a structured response through a detached local
helper. The model/helper does not receive the authenticated result capability;
the controller-owned collector validates helper output against the signed
return contract before submitting through the existing result path. This
prototype path remains behind the pre-inference guard. If enabled in a future
version, normal ingestion and acceptance checks would still decide whether the
task passed; provider output, thread state, or process completion alone is not
acceptance.

Agentflow workflow resume and Codex thread resume are separate operations.
Workflow resume reattaches to the existing approved root and reconciles its
durable tasks and results; it does not imply restarting a provider turn. The
guarded prototype uses a detached supervised helper designed to keep its SDK
connection and exact thread/turn alive when the controller exits. If this
worker path is enabled in a future version, same-owner controller resume can
inspect persisted helper state and observe or collect that same turn; it does
not issue a new `thread/start` or `turn/start`. This is not recovery from a dead
helper or lost App Server: if helper liveness, thread/turn identity, or turn
outcome is missing or ambiguous, Agentflow stops for operator reconciliation
and does not retry.

The automated helper-process fixture uses a fake worker to exercise durable
helper output and controller-side collection; it does not run the Codex SDK or
prove a real provider turn. A real SDK trial remains a separately approved,
potentially billable action.

## Observability limits

The SDK exposes only the session and runtime information it makes available to
the local client. Protocol model/config fields and reroute notifications are
cooperative local evidence, not provider-signed proof of the model actually
served. They may help identify or reconcile a session, but must not be
described as stronger evidence than they are. This integration does not
promise lower model prices, discounts, or reduced provider usage; provider
usage and billing remain authoritative.
