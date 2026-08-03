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
configured and resolved skills, and `doctor` checks entrypoints, conflicts, and
provenance without exposing file contents unnecessarily.

Use a relative path for a skill committed with the project. A team can then
clone the repository and run `agentflow skills sync` without editing config.

## External local skills

An individual can register a skill maintained outside the project:

```sh
agentflow skills add /path/to/my-skill
agentflow skills sync
```

Do not commit another user's absolute path. In `0.0.1`, keep a project config
containing a machine-specific source untracked. For a shareable team setup, use
a repository-relative package or a separately versioned imported asset with an
immutable revision and licence metadata.

## Provider synchronization and discovery

Agentflow resolves a provider-compatible entrypoint instead of copying domain
knowledge into handoff prompts. `skills sync` links configured skills into the
provider user directories below (or their corresponding `CODEX_HOME`,
`CLAUDE_HOME`, and `COPILOT_HOME` overrides):

| Provider | Synchronization destination |
| --- | --- |
| Codex | `~/.codex/skills/<name>` |
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
- require hardened isolation for untrusted execution where supported.

Synchronization does not grant a skill controller credentials, provider
credentials, or permission to bypass handoff preflight.

## Updates and removal

After changing a configured skill, rerun:

```sh
agentflow skills sync
agentflow skills doctor
```

Review the reported source and digest before delegating work. Use the CLI's
documented removal command for your installed version; do not recursively
delete shared provider directories because they may contain unrelated skills.
