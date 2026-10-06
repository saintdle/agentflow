# Optional Codex SDK integration

The Codex SDK lane is experimental and opt-in. The existing Herdr-backed
provider transport remains the default; installing the optional integration
does not change existing workflows or silently move them to another transport.
An SDK request that may have started but whose outcome is unclear must be
reconciled before another launch. Agentflow does not automatically fall back
to Herdr after an ambiguous SDK start.

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
evidence of available capacity. Catalogue entries do not establish model
entitlement. See the official [Codex SDK guide](https://learn.chatgpt.com/docs/codex-sdk)
and [App Server protocol](https://learn.chatgpt.com/docs/app-server).

Before any workflow that can start a paid worker turn, obtain the user's
explicit consent for that workflow and its bounded model, effort, workspace,
skills, and launch budget. A read-only diagnostic is not that consent.
App Server transport currently rejects sterile or restricted-outbound tasks;
do not broaden their context boundary to make them eligible.

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
For Agentflow workflow [workflow-id], inspect the approved contract and exact
Codex model, effort, workspace, allowed skills, and launch budget. Do not start
any paid worker until I explicitly approve this workflow and those limits. Do
not change the transport or fall back to another provider or model.
```

## Keep the workflow contract authoritative

The approved workflow contract and Agentflow policy remain authoritative for
the exact model and effort, workspace boundary, allowed skills, retries, and
launch budget. Do not let an SDK thread, environment default, or provider-side
setting broaden those limits. If the approved route cannot be honored, stop and
report the blocker; do not substitute another model, effort, workspace,
transport, or skill set.

Provider output, thread state, and process completion are not Agentflow
acceptance. A result is successful only after it returns through Agentflow's
authenticated result path and passes the normal contract, evidence, and
acceptance checks. A failed, interrupted, timed-out, or ambiguous SDK run is
never success and is not authorization to retry or launch elsewhere.

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
the local client. That information may help identify or reconcile a session,
but it is not a cryptographic attestation of the model actually served. A
requested model or reported thread model must not be described as stronger
proof than the available local evidence supports. This integration does not
promise lower model prices, discounts, or reduced provider usage; provider
usage and billing remain authoritative.
