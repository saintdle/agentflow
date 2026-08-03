# Agent-led setup contract

This file is the entry point for a coding agent asked to install and configure
Agentflow for a user. Read it completely before changing the machine or target
workspace. Also read [Installation](docs/INSTALLATION.md),
[Security](docs/SECURITY.md), and any instructions in the target repository.

Agentflow is distributed as `saintdle-agentflow` and installs the `agentflow`
command. The unqualified `agentflow` and `agentflow-cli` packages on package
indexes are unrelated projects and must not be installed.

## Installation contract

1. Confirm the platform is supported, Python is 3.10 or later, `git` and
   Beads 1.1 or later are available, and at least one intended provider CLI is
   installed. Report optional tools separately; do not treat them as required.
2. Inspect the latest reviewed GitHub release and its changelog. Install an
   explicit Agentflow tag into an isolated `uv tool`, `pipx`, or virtual
   environment. Never install an unqualified similarly named package.
3. Run `agentflow --version` and `agentflow doctor` without displaying
   credentials, environment variables, provider configuration, or transcript
   content.
4. Preview bundled provider assets with `agentflow install --dry-run`. Apply
   only new or Agentflow-managed assets. Preserve unrelated files, directories,
   hooks, profiles, and skill links. Never use a force option merely to make a
   health check green.
5. If the current command or provider assets point into an older Agentflow
   source checkout, stop the generic installation path and follow
   [the transactional legacy migration](docs/MIGRATION.md). Preview exact
   ownership first, require a quiet controller/worker state, retain the legacy
   checkout, and return the rollback ID after applying.
6. Initialize a target workspace only when the user asked to configure that
   workspace. `agentflow init <path> --beads` is non-overwriting, but inspect
   its result and repository status. Do not silently change Beads from local
   state to a tracked or shared-server mode.
7. Discover repository-local instructions and domain skills. Register external
   or machine-specific skills in ignored local configuration; use shareable
   configuration only for repository-relative skill sources intended for the
   team. Agentflow routes these skills but does not replace them.
8. Finish with the installed version, executable path, configured providers,
   preserved conflicts, workspace/Beads status, checks run, anything not
   configured, and the rollback command when a migration occurred.

## Halt conditions

Stop and ask for a decision instead of guessing when:

- an existing destination is not provably Agentflow-managed;
- a legacy migration finds active Agentflow controllers or external workers;
- installation would modify a repository with unrelated changes;
- the selected release, package identity, or artifact provenance is unclear;
- a prerequisite needs privileged installation that the user did not approve;
- the target workspace's instructions conflict with this setup contract.

Missing optional tools, an uninitialized project, or a provider the user does
not intend to use are reportable conditions, not reasons to rewrite unrelated
configuration.

## Copy/paste request for the user

Send this from a ChatGPT/Codex or Claude coding-agent chat that has terminal
access:

```text
Install and configure Agentflow from https://github.com/saintdle/agentflow for
this machine and the workspace currently open in the IDE. Read AGENT_SETUP.md,
the installation guide, the security guide, and the workspace's own agent
instructions before acting. Use the latest reviewed tagged release, verify the
package identity, install it in an isolated environment, preview provider asset
changes, preserve every unmanaged file and skill, and initialize this workspace
with local Beads state only if it is not already initialized. If you detect a
legacy source-checkout installation, use Agentflow's transactional dry-run and
rollback-capable migration instead of overwriting it. Do not expose credentials
or transcript content. Work through safe setup and verification autonomously;
stop only for an unmanaged conflict, active worker, unclear release provenance,
or permission decision. Return the version, command path, provider and Beads
health, preserved items, checks, and rollback command if applicable.
```

For operation after setup, continue with the
[chat-first workflow guide](docs/CHAT_WORKFLOWS.md).
