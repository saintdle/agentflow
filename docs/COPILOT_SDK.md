# Copilot SDK model-evidence groundwork

Agentflow includes experimental, fail-closed diagnostic groundwork for
validating GitHub Copilot SDK model-evidence events. It is not an executable
provider adapter: the current code does not launch Copilot, subscribe to a
live SDK session, or contact a provider. Its status command checks local
prerequisites only; its event gate is exercised with test/caller-supplied
events.

## Why the SDK is being evaluated

The SDK reports `assistant.usage.model` for each observed model API call,
including subagent calls. The planned evidence path is the SDK session event
channel rather than a same-user-editable OpenTelemetry file. The current
gate can validate a supplied per-session event sequence against an exact
requested route and buffer supplied response text until a single-turn idle
event. It does not yet own or subscribe to the SDK session that would provide
those events.

The evidence is still cooperative local evidence. It is not signed by GitHub
or the model provider, and the first API call has already incurred its cost
before its `assistant.usage` event reports the model. A mismatch is detected
post-inference: the response is suppressed and the workflow must stop.

## Current limitation: tools remain disabled

The official Python SDK's `on_pre_tool_use` callback identifies the SDK session
but does not expose the originating `toolCallId` or subagent `agentId`. The
event stream includes subagent attribution, but Agentflow cannot safely prove
that a particular pre-tool callback belongs to the specific model call that
was just checked. Therefore the gate denies tools and marks subagent output
unsupported. No Copilot SDK coding-worker transport is implemented.

This limitation keeps the new code diagnostic-only. Persistent Herdr Copilot
launches remain blocked before provider spawn or cost. Direct/native Copilot
use is unchanged.

## Check local prerequisites

Python 3.10 remains supported by Agentflow core. The optional Copilot SDK
requires Python 3.11 or later.

```sh
agentflow adapter copilot-sdk
```

The status command is local-only: it checks the Python and optional SDK package
versions, makes no provider call, and always reports that the workflow
transport is disabled. Do not interpret SDK availability as an enabled or
secure provider route. A live model mismatch can only be observed after the
first inference, so that first call may already incur cost.

When running from a source checkout, install the extra with:

```sh
python3.11 -m pip install '.[copilot-sdk]'
```

## Future promotion gates

Before this can become a Copilot worker transport, Agentflow needs a supported
way to bind every tool authorization to the exact model API call and originating
agent, without relying on ambiguous event ordering or matching only tool names
and arguments. The controller must also own the session lifecycle, subscribe
before sending the first prompt, abort on missing/mismatched events, preserve a
bounded authenticated result contract, and retain the existing external
handoff and permission boundaries. The acceptance suite must prove fail-closed
behavior for missing, stale, replayed, mismatched, out-of-order, and
subagent-originated events without making a live paid provider request.

Official references:

- [Copilot SDK streaming events](https://docs.github.com/en/copilot/how-tos/copilot-sdk/use-copilot-sdk/streaming-events)
- [Copilot SDK hooks](https://docs.github.com/en/copilot/how-tos/copilot-sdk/hooks)
- [Copilot Python SDK prerequisites](https://github.com/github/copilot-sdk/blob/main/python/README.md)
