# Copilot SDK usage proof prototype

Agentflow includes an optional, import-lazy observer for the GitHub Copilot SDK.
Install it with `pip install 'saintdle-agentflow[copilot]'`. The SDK wheel
requires Python 3.11 or later. This prototype accepts only SDK `1.0.17`, whose
declared CLI release is `1.0.93`. It resolves the exact SDK-managed
`darwin-arm64` runtime bundle published as
`github-copilot-1.0.93-darwin-arm64.tgz` and pins its GitHub release digest,
canonical wrapper and payload paths, and extracted file hashes before launch.
The bundle SHA-256 is `d3a64c4f9387efeee9de96fa85aef4362ca33c13ef8908793219ce34d9827335`,
from the [published Copilot CLI release](https://github.com/github/copilot-cli/releases/tag/v1.0.93).
The connected status API separately must report release `1.0.93` and protocol
`3` before the code creates a session. The status API release is not treated as
the executable's internal console build string; the trusted pin is the exact
published SDK-resolved artifact identity.

The observer reads `assistant.usage` directly from the session event callback.
Copilot documents this event as one record per model API call, including
sub-agent calls, with `data.model` naming the model used for that call. These
events are ephemeral and are not replayed on resume. Agentflow records that
actual model and rejects missing usage, model mismatches, duplicate calls,
resume events, tool or sub-agent activity, and incomplete tool inventories. It
does not use `model.getCurrent`, which reports a selected model and cannot
distinguish a service fallback.

The proof client uses the SDK-managed stdio runtime, `mode="empty"`, no allowed
or custom tools, no plugin directories, and disabled config discovery,
custom instructions, skills, file hooks, and host Git operations. The runtime
receives a small explicit environment with a private home and temporary
directory; controller environment variables are not inherited. Plugin
discovery is disabled in that process. It checks the runtime's tool inventory
before and after its one request. A callback rejects every permission request.
Any observed tool execution, external tool, skill, or sub-agent event makes the
report fail. The wrapper accepts one request with a 180-second limit and does
not provide resume or retry behavior.

```python
from agentflow.copilot_sdk import (
    CopilotLaunchIdentity,
    COPILOT_NEGATIVE_CONTROL_PROMPT,
    CopilotProofRun,
    derive_evidence_key,
)

identity = CopilotLaunchIdentity(
    workflow_root="root-id",
    task_id="task-id",
    claim_id="claim-id",
    lease_epoch=1,
    continuity_id="continuity-id",
    launch_id="launch-id",
    role="writer",
    requested_model="claude-sonnet-4.6",
    effort="medium",
)
evidence_key = derive_evidence_key(
    controller_authority_secret,
    identity,
    run_nonce=controller_generated_nonce,
)

async with await CopilotProofRun.open(
    identity=identity,
    evidence_key=evidence_key,
    base_directory=private_empty_home,
    workspace_root=workspace_root,
    github_token=credential_held_in_memory,
) as run:
    report = await run.send_once(COPILOT_NEGATIVE_CONTROL_PROMPT)
```

The authority secret is used only to derive a launch-scoped collector key; the
SDK client, child runtime environment, prompt, and usage ledger receive only
the derived key or no key. The token is passed to the SDK in memory and is
never added to the report. The SDK home must be a private empty directory
outside the worktree. The built-in negative-control prompt asks for a harmless shell
print, a read of a unique nonexistent path, and a request to the reserved
`.invalid` domain. Do not substitute operations that access user data or have
side effects. Never pass worker-writable event files into the collector.
Persist the signed report only in controller-owned protected state.

The returned chain binds the supplied root/task/claim/lease-continuity/launch
identity, the hashed SDK session ID, each observed call and model, the SDK
package and declared CLI release, the published runtime bundle digest and
local artifact paths/hashes, plus the connected status API release and
protocol. It is a local observation from the pinned Copilot runtime,
not provider-signed evidence. The current prototype keeps its chain in memory;
it does not anchor a persisted head across controller restarts or authenticate
the supplied launch identity itself. Test fixtures can exercise rejection
logic but cannot enable admission. The report always has
`persistent_admission: false`, and Copilot's persistent Herdr guard remains
disabled.

This is a no-tools proof route; it does not confine a persistent coding worker.
The synchronous Agentflow OS sandbox does not confine a later Herdr session.
Before any persistent route can be considered, a controller-owned live run
must prove the runtime and tool boundary, and protected ledger continuity must
be integrated. A service fallback/mismatch and a resume-gap negative control
also require live evidence; fixtures do not supply it.

The implementation follows GitHub's pinned
[Python SDK reference](https://github.com/github/copilot-sdk/blob/v1.0.17/python/README.md),
[per-call usage and billing reference](https://github.com/github/copilot-sdk/blob/v1.0.17/docs/features/usage-and-billing.md),
[streaming event schema](https://github.com/github/copilot-sdk/blob/v1.0.17/docs/features/streaming-events.md),
and [SDK v1.0.17 release](https://github.com/github/copilot-sdk/releases/tag/v1.0.17).
