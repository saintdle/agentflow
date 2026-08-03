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

## Skills

Skill sources are configured separately from workflow policy so teams can
bring domain expertise without modifying Agentflow. See [SKILLS.md](SKILLS.md).

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
