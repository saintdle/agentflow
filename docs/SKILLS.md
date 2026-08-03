# Bring your own skills

Agentflow discovers and synchronizes user- and project-provided skill packages.
A skill is a directory whose provider entrypoint is a readable `SKILL.md`.
Supporting files referenced by that entrypoint should remain inside the skill
package so they can be validated and pinned together.

The `saintdle-agentflow` wheel already contains seven generic Agentflow workflow
skills and provider-role profiles. Install those with `agentflow install`.
`agentflow skills add`, `sync`, `list`, and `doctor` manage additional domain
skills supplied by the user or project; they do not modify the bundled copies.

## Add a project skill

From the repository root:

```sh
agentflow skills add ./skills/my-domain-skill
agentflow skills sync
agentflow skills list
agentflow skills doctor
```

`add` records the source in configuration, `sync` makes the configured package
available through supported provider discovery locations, `list` reports
configured and resolved skills, and `doctor` checks entrypoints, provider links,
and a content digest without exposing file contents unnecessarily. Asset-lock
provenance remains a separate `agentflow assets` workflow.

Use a relative path for a skill committed with the project. A team can then
clone the repository and run `agentflow skills sync` without editing config.

## External local skills

An individual can register a skill maintained outside the project:

```sh
agentflow skills add /path/to/my-skill --local
agentflow skills sync
```

Agentflow stores that registration in the ignored
`.agentflow/config.local.json` machine-local layer. An external absolute path
selects the local layer by default; `--local` makes that choice explicit. Local
entries override shared entries with the same name. For a shareable team setup,
use a repository-relative package in `.agentflow/config.json` or a separately
versioned imported asset with an immutable revision and licence metadata.

## Provider synchronization and discovery

Agentflow resolves a provider-compatible entrypoint instead of copying domain
knowledge into handoff prompts. `skills sync` links configured skills into the
provider user directories below (or their corresponding `CODEX_HOME`,
`CLAUDE_HOME`, and `COPILOT_HOME` overrides):

| Provider | Synchronization destination |
| --- | --- |
| Codex | `~/.agents/skills/<name>` (or `$CODEX_HOME/skills/<name>`) |
| Claude Code | `~/.claude/skills/<name>` |
| GitHub Copilot | `~/.copilot/skills/<name>` |

Handoff preflight also understands provider-supported project locations,
including `.agents/skills`, `.claude/skills`, and `.github/skills`, so a
repository-owned skill can remain local to that repository.

Provider conventions can change independently. `agentflow skills doctor` is
the authoritative check for the installed Agentflow version.

## Handoffs and trust

Name each required domain skill explicitly in delegated work. External
handoff preflight resolves the skill from the worker's actual directory,
checks package safety and readability, and records a content digest. A missing,
changed, cyclic, unsafe, or unreadable required skill blocks launch.

Treat third-party skills as executable supply-chain inputs:

- review their source and requested tools;
- pin an immutable revision;
- record licence and provenance;
- use Agentflow's asset lock and verification commands;
- use the separate synchronous `agentflow isolation launch` path for untrusted
  execution where supported; persistent provider sessions are not confined in
  `0.0.1`.

Synchronization does not grant a skill controller credentials, provider
credentials, or permission to bypass handoff preflight.

## Updates and removal

After changing a configured skill, rerun:

```sh
agentflow skills sync
agentflow skills doctor
```

Review the reported source and digest before delegating work. Remove a
registration and its Agentflow-owned provider links with:

```sh
agentflow skills remove my-domain-skill
```

Removal preserves the source package and any provider path no longer pointing
to the registered source. Use `--local` or `--shared` when the same name exists
in both layers. Do not recursively delete shared provider directories.
