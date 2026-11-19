# Configuration

Agentflow uses repository configuration for model-routing preferences and custom
skill registrations that should be shared by a team. Workflow rules are kept
in generated provider instructions rather than accepted as unenforced config
switches. Initialization places the shared project file at
`.agentflow/config.json`. `.agentflow/config.local.json` is the ignored
machine-local layer for absolute skill paths or a local model-policy override;
local values override same-name shared values. Existing explicit model policies
remain enforced until migrated. Runtime state, credentials,
claims, logs, temporary handoffs, and provider sessions must remain ignored.
`agentflow init` enables metadata-only memory in a newly created shared
configuration, for both Git and gitless workspaces, while keeping `on_prompt`
false. Pass `--no-memory` to create that config with memory disabled. This is
an init-only default: uninitialized workspaces and schema-v1 files without a
`memory` section retain the disabled runtime fallback, and init does not
rewrite existing shared settings or local overrides. See
[governed memory](MEMORY.md) for the hook, scope, budget, and retention
contract.

## Principles

- Commit portable policy and relative paths.
- Keep machine-specific paths and secrets in a local, ignored override.
- Never put provider credentials or controller authority in project config.
- Let the controlling session select exact provider/model identifiers and effort.
- Treat repository-local instructions and skills as authoritative.
- Review generated changes; configuration migration must be explicit.

An initialized project starts with a schema-versioned configuration like this:

```json
{
  "schema": "agentflow.project@1",
  "version": 1,
  "modelrouter": {
    "mode": "controller-selected",
    "routes": []
  },
  "prose": {
    "editor": null
  },
  "execution": {
    "controller_only": true,
    "max_parallel_workers": 3,
    "max_delegation_depth": 1,
    "max_attempts_per_task": 2,
    "launch_budget_multiplier": 2,
    "max_expensive_execution_children": 0
  },
  "guidance": {
    "strategic_compaction": false,
    "verification": false,
    "context_pressure_percent": 75,
    "max_children_per_parent": 12
  },
  "memory": {
    "enabled": true,
    "on_prompt": false,
    "capture_failures": true,
    "max_items": 5,
    "max_chars": 2000,
    "max_age_days": 30,
    "scopes": ["project"],
    "scope_id": "",
    "max_events": 10000,
    "max_event_bytes": 5242880,
    "retention_days": 30,
    "session_retention_days": 30,
    "maintenance_interval_seconds": 300,
    "session_ledger_limit": 256,
    "startup_query": "agentflow"
  },
  "skills": [
    {
      "name": "my-domain-skill",
      "path": "skills/my-domain-skill",
      "providers": ["codex", "claude", "copilot"]
    }
  ]
}
```

Unknown or malformed security-relevant settings should be treated as errors,
not silently downgraded. Use `agentflow doctor` after changing configuration.

The local layer uses its own schema and normally contains only skills:

```json
{
  "schema": "agentflow.project-local@1",
  "version": 1,
  "skills": [
    {
      "name": "personal-domain-skill",
      "path": "/path/on/this/machine/personal-domain-skill",
      "providers": ["codex"]
    }
  ]
}
```

Prefer the CLI over hand-editing either layer:

```sh
agentflow skills add ./skills/team-skill --shared
agentflow skills add /path/on/this/machine/personal-domain-skill --local
agentflow skills list
```

Enable or disable the optional memory feature without replacing other policy
values. Use `--local` only for an intentional machine override; a shared toggle
is refused when a local memory section shadows it. Both layers are validated
before either can be changed. A dry run previews the exact diff without writing
or creating a backup:

```sh
agentflow config memory enable --root . --dry-run
agentflow config memory enable --root .
agentflow config memory disable --root . --local
```

To add current Agentflow hook handlers to an existing configuration while
preserving custom handlers and metadata, preview the explicit target first.
Codex uses the user-level `~/.codex/hooks.json`; Claude uses the selected
project's `.claude/settings.json`:

```sh
agentflow config hooks merge --provider codex --root . --dry-run
agentflow config hooks merge --provider claude --root . --dry-run
```

See [Installation](INSTALLATION.md#upgrade) for applying the reviewed merge.

## Beads state

`agentflow init . --beads` creates a local Beads workspace. The default is a
stealth/local arrangement. Use tracked state only when work items are intended
to be part of repository history. Select shared-server mode only for a
Git-backed workspace where multiple actors genuinely need concurrent graph
writes.

## Model router

New projects use `modelrouter` with `mode: controller-selected`. The controlling
session chooses the provider, exact model identifier, role, and effort for
each task. Agentflow does not make an extra decision-model call, maintain an
allowlist of released models, or silently pick a fallback. Newly released
models need no Agentflow catalog update.

The optional `routes` list supplies preferences to the controlling session,
not an exhaustive catalog or an automatically evaluated rules engine:

```json
{
  "mode": "controller-selected",
  "routes": [
    {
      "provider": "codex",
      "model": "your-exact-model-id",
      "effort": "medium",
      "when": "Small implementation tasks and read-only exploration"
    }
  ]
}
```

An unlisted exact identifier is valid. Missing identifiers, automatic selection
(`auto`, `default`, `latest`), malformed input and unsupported providers are
rejected. CLI capability checks do not prove account entitlement; the provider
can reject a model or effort. Runtime session/model evidence and authenticated
results are still required for managed workers. Copilot's managed-worker
restriction is unchanged; model routing does not enable an unsupported transport.

Generated agent profiles no longer pin a model or effort. Supply both when
dispatching rather than accidentally inheriting the controller's route. New
projects have no default prose editor; set `prose.editor` explicitly when wanted.

## Legacy model policy and migration

Existing `model_policy` files and explicit `--policy` arguments retain their
closed-policy behavior. Initialization never rewrites them or custom profiles.
Preview and then explicitly apply a migration:

```sh
agentflow config modelrouter migrate --root .
agentflow config modelrouter migrate --root . --apply
```

Use `--local` to migrate an ignored local override. To migrate shared routing,
first remove any local routing override; migrating the override alone does not
remove its precedence. Each migration validates both layers,
preserves unrelated settings and policy files, creates a private backup, and
is idempotent. Former allowed routes become advisory preferences; forbidden and
selective model lists no longer restrict the migrated project. Legacy
model-name-based expensive-worker restrictions no longer select models in router
mode. Task, parallelism, deadline, retry, and global-launch budgets still apply.
Review the preview before removing a team's intentional model restrictions.
Explicit prose-editor settings are preserved. When an older configuration omits
the prose section, the preview adds `prose.editor: null` and warns that its
implicit bundled editor will be disabled. Configure an exact editor explicitly
to continue automatic prose editing.

## Controller-only execution policy

The default controller shapes the root, dispatches work, evaluates returned
evidence, and integrates accepted results; it does not edit product files.
The controlling session chooses an appropriate model for coding and exploration. A native
Codex worker should receive `fork_turns="none"` or the smallest bounded context
fork so it cannot silently inherit an unintended controller context.

Launch admission uses the `execution` values above. The root launch budget is
the number of planned launchable tasks multiplied by
`launch_budget_multiplier`; retries consume the same budget as first attempts.
After `agentflow budget freeze`, the root uses its frozen cap instead, so adding
tasks no longer raises the budget; only `agentflow budget revise` can raise it.
The parallel-worker, delegation-depth, per-task retry, and expensive execution
caps are also enforced before Herdr spawn.

A workflow may persist a complete root-specific override in its Beads metadata:

```json
{
  "schema": "agentflow.execution-policy@1",
  "controller_only": true,
  "max_parallel_workers": 2,
  "max_delegation_depth": 1,
  "max_attempts_per_task": 2,
  "launch_budget_multiplier": 2,
  "max_expensive_execution_children": 0
}
```

Partial or untyped root overrides fail closed. Omit the root override to use the
project policy.

Per-task launch metadata may opt into restricted outbound packaging:

```json
{
  "agentflow": {
    "launch": {
      "provider": "codex",
      "model": "gpt-5.6-luna",
      "role": "coding",
      "effort": "medium",
      "sterile": true
    }
  }
}
```

`"outbound_context": "restricted"` is an equivalent explicit spelling. The
task must list every local context file it needs, and referenced skill packages
must be self-contained. Agentflow refuses instruction files, directories,
symlinks, transitive multi-package skills, or changed/unlisted package files
instead of broadening the outbound boundary. The current sterile lane also
rejects `shell-write`; writable results need a future explicit import/review
contract rather than implicit copying back into the source workspace.

Configure a machine-local alternative without changing the shared project:

```json
{
  "schema": "agentflow.project-local@1",
  "version": 1,
  "prose": {
    "editor": {
      "provider": "copilot",
      "model": "claude-sonnet-4.6",
      "effort": "medium",
      "max_ai_credits": 30
    }
  },
  "skills": []
}
```

Set `"editor": null` to keep deterministic prose findings but disable editor
launches. Explicit CLI editor flags override configuration for one invocation;
all three route fields are required, Copilot also requires a cap of at least 30
AI credits, and the model policy still applies.

Newly initialized projects receive these routes automatically. Agentflow does
not overwrite an existing project's policy during a package upgrade. To adopt
the route in an existing source checkout, review the bundled policy diff,
replace the project's configured policy only after approval, and then refresh
and migrate the managed profiles:

```sh
diff -u .agentflow/models-v1.json /path/to/agentflow/policies/models-v2.json
cp /path/to/agentflow/policies/models-v2.json .agentflow/models-v2.json
# Then set model_policy to .agentflow/models-v2.json in the shared config.
agentflow install --refresh-bundled
agentflow policy migrate --root . --dry-run
agentflow policy migrate --root .
agentflow policy audit --root .
```

The explicit copy is intentional: a team-owned custom policy must never be
silently replaced by a package upgrade. Agentflow never rewrites unmanaged
custom agents during profile migration.

## Skills

Skill sources are configured separately from workflow policy so teams can
bring domain expertise without modifying Agentflow. See [SKILLS.md](SKILLS.md).

## Outbound package limits

`agentflow handoff preflight` measures the exact package an external worker
would receive: the handoff, its sidecar manifest, each declared context file,
and every file in the pinned skill closure. It reports the file count, total
bytes, duplicate bytes, and a package digest. A task-linked handoff also records
the digest, size, and limit status on its task bead. An input listed twice
counts twice. A missing or unreadable input fails preflight.

Agentflow ships no byte limit. Without a declared limit for the task class and
provider, the package is reported as `uncalibrated`, never as within a limit.
An owner who has calibrated a limit can declare it per `<task-class>/<provider>`
key in either layer; local keys override shared keys one at a time:

```json
{
  "package_limits": {
    "focused-review/claude": 120000
  }
}
```

Task classes are `scan`, `focused-review`, `source-heavy-review`,
`media-review`, and `implementation`. A package at or under the limit passes;
one byte over fails preflight with the measured size, the excess, and the
package digest. An owner can approve one exact oversized package on a manual
preflight:

```sh
agentflow handoff preflight handoff.md \
  --package-exception <package-digest> --decision-ref <decision-reference>
```

The exception needs both values and applies only to that digest; any change to
the package invalidates it. Controller dispatch rebuilds the handoff, so it has
no exception path. An oversized dispatch must shrink its context or skill
closure, or the owner must raise the declared limit.

## Controller context budgets

Controller context budgets measure durable workflow evidence rather than
provider token estimates. The current defaults require rotation after four
completed tasks or two completed workflow phases, and halt after two failures
of the same explicitly named approach. Agentflow waits for a safe boundary: the
current result is authenticated and dispositioned before another wave stops.
The next authenticated `controller resume` advances only the context generation
and continues from the protected packet; root, claims, budgets, permissions,
and evidence remain unchanged.

Use `agentflow controller progress` to supply different positive thresholds for
a workflow invocation. The resulting policy and evidence live in the ignored,
root-namespaced controller state and do not contain provider transcripts.

## Context audit and optional guidance

After a metadata-only history sync, audit the last 30 days:

```sh
agentflow history sync --dry-run
agentflow history sync
agentflow context audit --root . --days 30
```

The audit reads only the sanitized archive manifest. When available, it reports
provider, exact model, effort, role, lineage depth, recorded token counters, and
peak context pressure. It flags expensive models used for execution, excessive
child/depth budgets, ambiguous models, and selective Terra use for review.
Provider dashboards remain authoritative for billed usage.

`guidance.strategic_compaction` adds transcript-free compaction guidance to the
audit. `guidance.verification` adds a deterministic, planned-only verification
section to `controller status`. Both default to `false`; calling
`agentflow context compact` or `agentflow verify plan` is also an explicit
one-time opt-in. Verification guidance never runs checks or marks them passed.

Existing schema-v1 configs remain valid when `execution` and `guidance` are
absent; safe defaults are applied at runtime. To install the new worker profiles
and refresh only Agentflow-managed profiles, review and run:

```sh
agentflow install --dry-run
agentflow install --refresh-bundled
agentflow policy migrate --root . --dry-run
agentflow policy migrate --root .
agentflow policy audit --root .
```

## CodeBurn advisory reports

Agentflow can reconcile a local CodeBurn optimize report with its recorded
delivery evidence:

```sh
npx --yes codeburn optimize -p 30days --format json > /tmp/codeburn.json
agentflow usage optimize --codeburn /tmp/codeburn.json \
  --root . --workflow-root <root-id> --json
```

This does not install CodeBurn, send its output elsewhere, or accept its savings
estimate as billing evidence. Agentflow labels the estimate as heuristic,
reports completed/check evidence from its usage records plus aggregate Beads and
Herdr delivery evidence for the optional root, and refuses to recommend automatic
removal of Agentflow-managed skills or agents. Provider usage pages remain
authoritative for billed usage.

## Local and generated state

Agentflow manages an ignore block for runtime paths. Do not weaken these ignore
rules or commit their contents:

```gitignore
# BEGIN agentflow local artifacts
.agentflow/controller/
.agentflow/herdr/
.agentflow/claims/
.agentflow/runtime/
.agentflow/handoffs/
.agentflow/tmp/
.agentflow/logs/
.agentflow/worktrees/
.agentflow/config.local.json
.agentflow/managed-skill-links.json
# END agentflow local artifacts
```

Before committing configuration, inspect it for absolute home-directory paths,
tokens, account data, provider output, and internal repository references.
