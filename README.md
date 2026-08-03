# Agentflow

Agentflow is a provider-neutral CLI for coordinating coding agents around an
approved goal. It keeps durable work and dependencies in
[Beads](https://github.com/steveyegge/beads), validates handoffs before launch,
and records evidence without treating chat transcripts as project state.

The Python distribution is named `saintdle-agentflow`; the product, repository,
Python import package, and installed command are all named `agentflow`. The
unqualified `agentflow` and `agentflow-cli` names on package indexes belong to
unrelated projects.

> [!IMPORTANT]
> Agentflow `0.0.1` is a public preview. Its commands, configuration schema,
> and compatibility guarantees may change before `1.0`.

## What it provides

- A controller workflow built around goals, bounded assignments, claims,
  acceptance evidence, review, and integration.
- Provider-neutral handoffs for Codex, Claude Code, and GitHub Copilot CLI.
- Beads-backed durable coordination, including Git-backed and Gitless work.
- Seven bundled generic workflow skills and four provider-role profiles for
  each supported coding-agent provider.
- Project initialization that preserves existing agent instructions and hooks.
- Config-driven discovery and installation of your own skills.
- Optional observable external-agent sessions through Herdr.
- Fail-closed preflight, model policy, imported-asset verification, and
  macOS-only hardened subprocess isolation.

Agentflow routes skills; it does not replace domain expertise. A repository's
own skills and instructions remain authoritative.

## Requirements

- Python 3.10 or later.
- [Beads](https://github.com/steveyegge/beads) 1.1 or later and its `bd` CLI.
- Git for Git-backed projects.
- At least one supported coding-agent CLI for delegated work: Codex, Claude
  Code, or GitHub Copilot CLI.

Optional tools:

- [Herdr](https://github.com/ogulcancelik/herdr) for observable external sessions.
- GitHub CLI (`gh`) for GitHub issue and pull-request workflows.
- `tmux` as a portable fallback for external sessions.

Provider subscriptions, credentials, usage allowances, and terms are managed
by their respective providers. Agentflow does not supply or authenticate them.

## Install

The cleanest installation uses an isolated Python tool environment:

```sh
uv tool install "git+https://github.com/saintdle/agentflow.git@v0.0.1"
# or
pipx install "git+https://github.com/saintdle/agentflow.git@v0.0.1"
```

Until the repository is public, an authenticated GitHub checkout or Git
credential helper is required. To install from a downloaded release wheel:

```sh
pipx install ./saintdle_agentflow-0.0.1-py3-none-any.whl
```

Verify the installation and prerequisites without exposing credentials:

```sh
agentflow --version
agentflow doctor
```

The wheel contains Agentflow's seven generic workflow skills and the controller,
explorer, reviewer, and pull-request gatekeeper profiles for each supported
provider. Preview and then install those bundled assets separately:

```sh
agentflow install --dry-run
agentflow install
```

Existing provider files are preserved for manual review.

For development and clean-build instructions, see
[Installation](docs/INSTALLATION.md).

## Quick start

Initialize Agentflow and Beads in an existing Git repository:

```sh
cd /path/to/project
agentflow init . --beads
agentflow beads status .
agentflow doctor
```

For a working directory that will not use Git:

```sh
mkdir my-work
cd my-work
agentflow init . --beads
```

Initialization creates only missing workflow files, preserves existing agent
instructions and custom hooks, and keeps runtime state out of version control.
The default Beads setup is local/stealth. Choose tracked or shared-server state
only when that collaboration model is intentional.

Continue with the executable [first workflow tutorial](docs/FIRST_WORKFLOW.md)
to create and approve a root/task graph, run the controller, handle a halt, and
record completion evidence.

## Bring your own skills

Bundled Agentflow workflow skills are installed by `agentflow install`. The
commands below manage additional project or user domain skills without
repackaging Agentflow.

Register a local skill source in project configuration, synchronize it, then
check that Agentflow can resolve it:

```sh
agentflow skills add ./skills/my-domain-skill
agentflow skills sync
agentflow skills list
agentflow skills doctor
```

Skill sources may live inside the project or at an explicit external path.
Project configuration is shareable when it uses repository-relative paths;
machine-specific sources are stored in the ignored
`.agentflow/config.local.json` layer. Synchronization never silently overwrites
an unrelated installed skill. See
[Skill configuration](docs/SKILLS.md) for config examples, provider discovery
locations, and team-safe setup patterns.

## Platform support

| Platform | Core CLI | Hardened isolation |
| --- | --- | --- |
| macOS | Supported | Available through `sandbox-exec`; probes fail closed |
| Linux | Supported | Not available in `0.0.1`; requests fail closed |
| Windows | Not supported in `0.0.1` | Not available |

Core coordination can run on macOS and Linux. Hardened isolation is a distinct,
macOS-only security control; ordinary execution on Linux is not equivalent
confinement. In `0.0.1`, `agentflow isolation launch` provides synchronous
hardened execution. Direct handoff and persistent Herdr/controller launches
reject hardened profiles rather than treating a successful probe as confinement.

## Documentation

- [Installation and upgrades](docs/INSTALLATION.md)
- [First workflow tutorial](docs/FIRST_WORKFLOW.md)
- [Configuration](docs/CONFIGURATION.md)
- [Bring your own skills](docs/SKILLS.md)
- [Workflow guide](docs/WORKFLOW.md)
- [Security model](docs/SECURITY.md)
- [Security reporting](SECURITY.md)
- [Contributing](CONTRIBUTING.md)
- [Support](SUPPORT.md)
- [Governance](GOVERNANCE.md)

## Project status and releases

`0.0.1` is intended for evaluation and feedback. Pull requests run validation
and package-build checks. Merges to `main` build the CLI distribution artifacts;
tagged releases are the versioned distribution boundary. See
[the changelog](CHANGELOG.md) and [release process](CONTRIBUTING.md#releases).

## Licence and trademarks

Agentflow is licensed under the [Apache License 2.0](LICENSE). Third-party
attributions are recorded in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Agentflow is an independent project. It is not affiliated with or endorsed by
OpenAI, Anthropic, GitHub, Microsoft, Beads, Herdr, or their owners. All product
and company names are trademarks of their respective owners.
