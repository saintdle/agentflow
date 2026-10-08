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

## Stage-0 fake-only rehearsal

A separate source-only harness rehearses request accounting, the pre-turn
profile-proof gate, ambiguous outcomes, and same-owner restart using fixed
synthetic data and an in-process fake client. It is not part of the installed
CLI or worker admission. It has no SDK/runtime
imports, transport selection, live mode, or arbitrary client injection. Its
profile events, helper/server state, and result consumption are simulated and
cannot establish real model-worker isolation or SDK restart behavior, or
advance any live trial gate.

Run only the fixed fake simulation and its focused tests:

```sh
python3 scripts/trials/codex_model_worker_trial.py
python3 -m unittest tests.test_codex_model_worker_trial
```

The simulation persists only its synthetic state in a temporary directory.
Tests additionally use the production controller lease and turn-identity
recovery APIs with fake identifiers; they do not start an SDK process, thread,
turn, or inference. The ledger reserves each fake `thread/start` and
`turn/start` attempt before its fake call, caps persisted turn starts at two
and no-inference profile attempts at two, and stops after stale identity,
replay, dead-helper/server, ambiguous outcome, unknown/unattributable usage,
or the cumulative reported-usage threshold. Catalogue entries and legacy
sandbox metadata are not accepted as active-profile evidence. The fixed
synthetic outbound inventory and sanitized result are hash-checked and
bounded.

Reported local timing and token thresholds are stop-rule metadata, not hard
provider guarantees. Cancellation is best-effort; output-size checks happen
after generation; no hard model-token or currency ceiling is proven. The fake
rehearsal does not enforce a provider spend cap and must not be presented as
evidence that time, byte, cancellation, or usage limits constrain a live SDK
call. Future live stages have no runnable command here: each remains blocked
pending separately approved admission work and evidence, and the existing
pre-inference worker guard must remain unchanged.

## No-inference SDK preflight diagnostic

`scripts/trials/codex_preflight_no_inference.py` is a separate opt-in
no-inference diagnostic, not a worker entry point or an admission switch. Its default
invocation and `--help` do not create files or start a process; `--run` is
required, along with a new private `--trial-dir`. The directory holds only a
synthetic fixture, a generated permission-profile config, isolated home/cache
directories, a separate Codex auth home, and a private two-attempt ledger.
Do not point it at a project checkout or the normal Codex home.

The opt-in child process receives a strict environment allowlist and lazily
loads only the pinned SDK. It forces file-backed auth in its separate Codex
home and reads account state without refreshing tokens. Missing or unsupported
authentication blocks before `thread/start`. If a user separately signs into
that disposable home, the diagnostic may create at most one thread per
attempt, then requires a typed active-permission-profile notification before
its bounded wait expires. It never starts a turn, runs a command, logs in, logs
out, or changes global settings. It makes no fallback to the normal home.
The hidden child route also rechecks the exact allowlisted environment,
canonical disposable paths, private directories, reserved attempt, synthetic
fixture, and generated profile before loading the SDK. Its mode marker and
one-shot stdin token are invocation controls, not authentication or same-user
attestation; the checks reject accidental/path-confused entry but do not create
a same-user security boundary.
On macOS, the parent may pass `__CF_USER_TEXT_ENCODING` when its runtime provides
it. The child accepts this single platform marker only on Darwin, bound to the
current POSIX user ID and two bounded decimal selectors; other injected
environment keys remain rejected.
The normal auth-home boundary is compared with the POSIX account-home path
metadata; credential files in that home are not opened. Environments whose
effective `HOME` does not match the account metadata fail closed.

Even a matching profile notification is not a pass: the pinned SDK can report
active permission-profile metadata and list some integration surfaces, but it
does not provide a complete per-thread inventory of effective tools and
instruction sources. Unknown or uncontrolled inventory remains blocked.
Attempts are durably reserved before the child starts; ambiguous timeouts are
not retried, and the directory allows at most two explicit attempts. Output is
a bounded, sanitized status record. A fixed early layout rejection is
identified as occurring before SDK import and does not claim that a thread
start was attempted; malformed or unexpected child output remains ambiguous.
Bounded-call failures report a fixed RPC phase and `exception` or `deadline`
kind. The nullable `failure_category` is populated only for an observed
immediate exception in result schema `agentflow.codex_preflight_result.v2`; it
is one of a small allowlist of built-in, already-loaded
pinned-SDK, or already-loaded Pydantic exception families, with unrecognized
types reduced to `unknown`. Deadlines and non-exception halts have a null
category. Exception class names, messages, arguments, causes, contexts,
tracebacks, and server data are never serialized. A family category identifies
only the observed exception type boundary; it does not establish the underlying
SDK cause. Source inspection of SDK 0.160.1 confirms its no-argument typed
`initialize()` method supplies the client information and capabilities and
sends the `initialized` notification itself. That establishes call
compatibility, not successful runtime initialization or permission enforcement.
Do not interpret this diagnostic as model-tool isolation evidence, approval for
inference, or authorization to weaken the App Server worker guard. No SDK
runtime result is included in the source-only fake tests.

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
establish model entitlement or guaranteed capacity. See the official [Codex SDK guide](https://developers.openai.com/codex/codex-sdk)
and [App Server protocol](https://developers.openai.com/codex/app-server).

If a future release enables App Server worker execution, each task will still
require explicit approval of its exact model, effort, workspace, allowed
skills, and launch budget. The pinned schema can carry thread configuration
and report active permission-profile metadata, but those fields do not prove
that the requested rules govern every effective tool or isolate controller
authority. The current hard guard rejects App Server worker tasks before
inference until the full tool inventory and runtime boundary are verified.
`shell-write`, `no-shell`, `provider-default`, and sterile or
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
