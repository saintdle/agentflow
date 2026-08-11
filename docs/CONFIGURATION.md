# Configuration

Agentflow uses repository configuration for the model-policy path and custom
skill registrations that should be shared by a team. Workflow rules are kept
in generated provider instructions rather than accepted as unenforced config
switches. Initialization places the shared project file at
`.agentflow/config.json`. `.agentflow/config.local.json` is the ignored
machine-local layer for absolute skill paths or a local model-policy override;
local values override same-name shared values. Runtime state, credentials,
claims, logs, temporary handoffs, and provider sessions must remain ignored.

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
  "model_policy": ".agentflow/models-v1.json",
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
Unavailable, unapproved, or ambiguous model selection fails preflight.

The bundled policy keeps Claude Opus 4.8 as the preferred controller, judgment,
and review route when a user asks generally for Claude or Opus. Claude Opus 5
is an explicit opt-in route: select the pinned `claude-opus-5` model only when
the user specifically asks for Opus 5. Its default effort is `medium`; use a
different permitted level only when the user states one. Keep routine
implementation on the configured Sonnet or Codex Luna lanes to limit cost.
Model availability still depends on the user's provider plan, client version,
and organization policy.

Newly initialized projects receive these routes automatically. Agentflow does
not overwrite an existing project's policy during a package upgrade. To adopt
the route in an existing source checkout, review the bundled policy diff,
replace the project's configured policy only after approval, and then refresh
and migrate the managed profiles:

```sh
diff -u .agentflow/models-v1.json /path/to/agentflow/policies/models-v1.json
cp /path/to/agentflow/policies/models-v1.json .agentflow/models-v1.json
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
provider token estimates. The current defaults recommend a fresh controller
chat after four completed tasks or two completed workflow phases, and halt after
two failures of the same explicitly named approach. Rotation thresholds are
advisory so the deterministic controller can continue autonomously through its
approved root. The repeated-approach limit is enforced and creates a durable
decision point.

Use `agentflow controller progress` to supply different positive thresholds for
a workflow invocation. The resulting policy and evidence live in the ignored,
root-namespaced controller state. They are not project-wide instructions and do
not contain provider transcripts.

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
