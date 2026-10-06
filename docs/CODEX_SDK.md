# Optional Codex SDK integration

The Codex SDK lane is experimental and opt-in, and currently supports only
bounded, read-only Codex tasks. The existing Herdr-backed provider transport
remains the default; installing the optional integration does not change
existing workflows, profiles, or transport selection. Unsupported task
permissions are rejected before inference. An SDK request that may have
started but whose outcome is unclear must be reconciled before another launch;
Agentflow does not automatically fall back to Herdr or another model.

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
defaults to `1800` seconds and accepts values from `1` through `86400`. For a
brief, bounded trial, use `180` seconds. Merge the optional field into the same
existing `codex` object rather than replacing project configuration:

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
The App Server adapter is driven only by the leased root controller; it is not
a standalone unauthenticated worker-run command. Review the resolved
configuration and run local diagnostics before launching work. Do not copy
provider credentials into project configuration.

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

Before any workflow that can start a paid worker turn, obtain the user's
explicit consent for that workflow and its exact model, effort, workspace,
allowed skills, and launch budget. A read-only diagnostic is not that consent.
The supported task must explicitly use `shell-readonly` with
`Sandbox.read_only` and approval policy `never` on the thread and turn. The
handoff's existing `tool_profile` selects `shell-readonly`; this is not a new
Beads metadata field. `never` disables approval prompts inside the read-only
sandbox; it does not grant write permission. The adapter rejects
`shell-write`, `no-shell`, `provider-default`, and sterile or restricted-outbound
tasks before inference. It does not support SDK subdelegation. Do not relax
these limits to make a task eligible.

For example, a chat can check readiness without starting work:

```text
Run the read-only Agentflow Codex diagnostics for this workspace. Do not start
an inference or worker, change authentication, or modify configuration. Report
only the supported/redacted diagnostic fields and any unavailable checks.
```

The next prompt is planning-only. To approve execution after reviewing the
persisted contract, use [the chat workflow's approval and run step](CHAT_WORKFLOWS.md#3-approve-and-run-one-persistent-controller).
That approval authorizes the persistent controller to continue within the
root's saved limits; it does not require approval for each wave.

```text
For Agentflow workflow [workflow-id], plan a bounded read-only Codex task only.
Inspect the exact model, effort, workspace, allowed skills, and launch budget;
require `shell-readonly`, `Sandbox.read_only`, and approval policy `never` for
the thread and turn. Do not start a worker until I explicitly approve this
workflow and those limits. Do not change the transport or substitute a model.
```

## Keep the workflow contract authoritative

The approved workflow contract and Agentflow policy remain authoritative for
the exact model and effort, workspace boundary, allowed skills, retries, and
launch budget. The Codex thread and turn are explicitly pinned to the approved
read-only sandbox and `never` approval policy. Do not let an SDK thread,
environment default, or provider-side setting broaden those limits. If the
approved route cannot be honored, stop and report the blocker; do not substitute
another model, effort, workspace, transport, permission, or skill set.

The planned result path has the model return a structured final response; it
must not write to Agentflow's result inbox or receive its authenticated result
capability. A controller-owned collector will submit the response through the
existing authenticated result and acceptance path. Until that path is
implemented and verified, an SDK response is not an accepted result. Provider
output, thread state, and process completion are not acceptance: the result
must pass the normal contract, evidence, and acceptance checks. A failed,
interrupted, timed-out, or ambiguous SDK run is never success and is not
authorization to retry or launch elsewhere.

Agentflow workflow resume and Codex thread resume are separate operations.
Workflow resume reattaches to the existing approved root and reconciles its
durable tasks and results; it does not imply continuing a provider thread.
For an App Server worker, recovery reattaches to the exact persisted Codex
thread and turn; it never starts a duplicate turn. If the required identity is
missing or the outcome is ambiguous or interrupted, stop for operator
reconciliation. Do not create a replacement workflow to recover a disconnected
session.

## Observability limits

The SDK exposes only the session and runtime information it makes available to
the local client. Protocol model/config fields and reroute notifications are
cooperative local evidence, not provider-signed proof of the model actually
served. They may help identify or reconcile a session, but must not be
described as stronger evidence than they are. This integration does not
promise lower model prices, discounts, or reduced provider usage; provider
usage and billing remain authoritative.
