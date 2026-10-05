# Configuration

Agentflow uses repository configuration for the model-policy path and custom
skill registrations that should be shared by a team. Workflow rules are kept
in generated provider instructions rather than accepted as unenforced config
switches. Initialization places the shared project file at
`.agentflow/config.json`. `.agentflow/config.local.json` is the ignored
machine-local layer for absolute skill paths or a local model-policy override;
local values override same-name shared values. Runtime state, credentials,
claims, logs, temporary handoffs, and provider sessions must remain ignored.
The optional `memory` section is disabled by default; schema-v1 files without
it remain valid. See [governed memory](MEMORY.md) for the metadata-only hook,
scope, budget, and retention contract.

## Principles

- Commit portable policy and relative paths.
- Keep machine-specific paths and secrets in a local, ignored override.
- Never put provider credentials or controller authority in project config.
- Prefer exact provider/model identifiers and versioned policies.
- Treat repository-local instructions and skills as authoritative.
- Review generated changes; configuration migration must be explicit.

An initialized project starts with a schema-versioned configuration like this:

```json
{
  "schema": "agentflow.project@1",
  "version": 1,
  "model_policy": ".agentflow/models-v2.json",
  "prose": {
    "editor": {
      "provider": "codex",
      "model": "gpt-6-luna",
      "effort": "medium"
    }
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
    "enabled": false,
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

## Model policy

The initialized model policy is a reference safety policy, not an assertion that a
model is available to every user. Teams can maintain a versioned policy for
their approved providers, exact model identifiers, roles, and effort levels.
The bundled `editing` role permits Codex `gpt-6-luna` (and retained
`gpt-5.6-luna` compatibility), Claude Code
`claude-sonnet-5`, or Copilot `claude-sonnet-4.6`, each at `medium`. The project
chooses one exact route under `prose.editor`; Luna is the generated default.
Unavailable, unapproved, or ambiguous model selection fails preflight.
The requested route is also bound into the authenticated return contract.
Agentflow accepts the result only when native provider lifecycle metadata for
the Herdr-bound session reports the same exact model; missing or mismatched
runtime evidence fails closed.

The bundled policy keeps Claude Opus 4.8 as the preferred controller, judgment,
and review route when a user asks generally for Claude or Opus. Claude Opus 5
is an explicit opt-in route: select the pinned `claude-opus-5` model only when
the user specifically asks for Opus 5. Its default effort is `medium`; use a
different permitted level only when the user states one. Keep routine
implementation on the configured Sonnet or Codex Luna lanes to limit cost.
Model availability still depends on the user's provider plan, client version,
and organization policy.

Codex Terra is permitted, not banned. It is a selective execution route for
coding or exploration at `medium` or `high`: the approved task must persist the
exact `gpt-5.6-terra` route, set `selective_model=true`, and explain why Luna is
not sufficient. An unmarked Terra launch fails closed. Generic, Auto, and Haiku
routes remain disallowed. New bundled Codex profiles use GPT-6 Sol for
controller and judgment work and GPT-6 Luna for coding, exploration, and
editing. GPT-5.6 Sol and Luna remain valid exact routes for projects that have
not migrated their own model settings.

## Controller-only execution policy

The default controller shapes the root, dispatches work, evaluates returned
evidence, and integrates accepted results; it does not edit product files.
Routine coding and exploration use Luna or Sonnet worker profiles. A native
Codex worker should receive `fork_turns="none"` or the smallest bounded context
fork so it cannot silently inherit an expensive Sol controller context.

Launch admission uses the `execution` values above. The root launch budget is
the number of planned launchable tasks multiplied by
`launch_budget_multiplier`; retries consume the same budget as first attempts.
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
