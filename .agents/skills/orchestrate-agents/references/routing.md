# Provider and model routing

The controlling session selects the exact provider, model and effort. Consult
the project's optional `modelrouter.routes` preferences and the provider's
current availability; these preferences are not a model allowlist. Newly
released models do not need an Agentflow update. Do not silently substitute a
model if the provider rejects the selected identifier or effort.

Use cost-appropriate models for bounded work and stronger reasoning when the
task warrants it. Persist the selected provider/model/role/effort with each
assignment. Generated profiles do not pin these values. Existing explicit
legacy model policies remain enforced until the owner runs
`agentflow config modelrouter migrate --apply`.

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
- From the controller, launch the managed `agentflow-worker` or
  `agentflow-explorer` profile with `fork_turns="none"` (or the smallest
  explicitly bounded positive fork). Explicitly select model and effort rather
  than accidentally inheriting the controller's route.
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
