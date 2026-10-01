# Provider and model routing

Use the versioned `policies/models-v2.json` matrix (`models-v2@1`). Launches
must carry an exact provider, model, role, effort, and policy version; unlisted
values fail closed before lease/claim reservation.

| Work | Provider/model | Effort |
|---|---|---|
| Goal shaping, architecture, difficult debugging | Codex `gpt-6-sol` | `high` or `xhigh` |
| Bounded coding workers | Codex `gpt-6-luna` | `max` |
| File discovery and exploration | Codex `gpt-6-luna` | `medium` or `high` |
| Selective complex implementation or exploration, only when explicitly recorded | Codex `gpt-5.6-terra` | `medium` or `high` |
| Conditional plain-language edit (default) | Codex `gpt-6-luna` | `medium` |
| Conditional edit without OpenAI access | Claude `claude-sonnet-5` or Copilot `claude-sonnet-4.6` | `medium` |
| Judgment, review, and queue integration (default when Claude or Opus is requested) | Claude `claude-opus-4-8` | `high` |
| Judgment, review, and queue integration (only when Opus 5 is explicitly requested) | Claude `claude-opus-5` | `medium` unless the user states another level |
| Bounded Claude execution and exploration | Claude `claude-sonnet-5` | `medium` or `high` |
| Judgment, review, and queue integration (default when Claude or Opus is requested) | Copilot `claude-opus-4.8` | `high` |
| Judgment, review, and queue integration (only when Opus 5 is explicitly requested) | Copilot `claude-opus-5` | `medium` unless the user states another level |
| Bounded Copilot execution and exploration | Copilot `claude-sonnet-4.6` | `medium` or `high` |

## Execution lanes

| Signal | Native worker | Observable external session |
|---|---|---|
| Work shape | Bounded, independent, fire-and-forget | Long-running, cross-provider, or decision-prone |
| Intervention | Controller steers or stops it through the provider | User can attach directly when the worker reports `BLOCKED` |
| Isolation | Provider-managed; use read-only work or disjoint ownership | Use a dedicated worktree and authenticated confinement |
| UI | Provider agent view | Herdr for controller-managed sessions; tmux for manual sessions only |

Choose the native lane for routine work. For controller-managed external work,
Herdr is the supported transport; tmux is limited to manually managed terminal
sessions and is not an Agentflow dispatch fallback. Use Herdr when direct
intervention, cross-provider identity, or a durable pane binding has concrete value.

## External task classes

- `scan`: small read-only discovery with a terse evidence return.
- `focused-review`: narrow source set and one review question.
- `source-heavy-review`: larger source set whose ingestion cost is material.
- `media-review`: rendered pages or images; attach only selected evidence.
- `implementation`: one writable owner with exact checks and an isolated worktree.

Calibrate caps from measured successful runs by task class. Do not increase a
failed cap until packaging, path access, tools, and delegation mode pass
preflight.

## Context budget

- Keep the root controller controller-only: it shapes, routes, dispositions,
  and judges; product-file edits belong to a bounded execution worker.
- From a Sol controller, launch the managed `agentflow-worker` or
  `agentflow-explorer` profile with `fork_turns="none"` (or the smallest
  explicitly bounded positive fork). Never omit the model/profile and inherit
  Sol for coding or exploration.
- Terra is not banned. It is a selective Codex execution route and requires an
  explicit per-launch selection recorded with the task; Luna remains the
  default Codex execution model.
- GPT-5.6 Sol and Luna remain accepted exact routes for existing project
  configurations. New bundled profiles use GPT-6 Sol and Luna.
- Keep worker delegation disabled unless the approved graph records a deeper
  lane. The default maximum depth is one, with two attempts per task and a
  graph-derived total launch budget.
- Load repository instructions plus the smallest relevant file set.
- Prefer a fresh worker context to copying a long controller transcript.
- Treat project-local skills as authoritative; Agentflow routes them but does
  not replace domain expertise.
- Keep provider/model/effort, elapsed time, retries, checks, and outcome in the
  task record when available.
- Run `agentflow context audit` for metadata-only lineage, model, token, and
  context-pressure findings. Rotate only at a safe boundary and resume from
  the protected Agentflow packet, never a transcript.
