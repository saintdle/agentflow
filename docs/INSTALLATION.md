# Installation

The published Python distribution name is `saintdle-agentflow`. It installs
the `agentflow` command and the `agentflow` Python import package. Do not install
the unrelated `agentflow` or `agentflow-cli` distributions from a package
index.

To have a ChatGPT/Codex or Claude coding agent—or GitHub Copilot Chat in an IDE
with Agent mode and terminal access—perform these steps, send it the copy/paste
request in the repository's
[agent-led setup contract](../AGENT_SETUP.md). That contract adds preservation,
legacy-migration, redaction, verification, and halt requirements around the
commands below.

## Prerequisites

Agentflow supports Python 3.10 or later on macOS and Linux. Install these tools
before Agentflow:

1. Git for Git-backed projects.
2. Beads 1.1 or later, with `bd` available on `PATH`.
3. One or more coding-agent CLIs you intend to use.

The [Herdr runtime](https://github.com/herdrdev/herdr) is required for
persistent external worker sessions launched by an Agentflow controller, but
is optional for planning, local Beads tracking, and native-subagent workflows.
GitHub CLI and `tmux` are optional; `tmux` is for manually managed terminal
sessions, not a controller-managed persistent worker transport. Agentflow
reports missing optional integrations without reading their credential stores.

## Install an isolated CLI

Install a tagged release with `uv`:

```sh
uv tool install "git+https://github.com/saintdle/agentflow.git@v0.0.7"
```

Or with `pipx`:

```sh
pipx install "git+https://github.com/saintdle/agentflow.git@v0.0.7"
```

For a local checkout:

```sh
git clone https://github.com/saintdle/agentflow.git
cd agentflow
pipx install .
```

The source repository is public, so direct GitHub installs do not require an
authenticated checkout. A release wheel built by GitHub Actions can also be
installed without a source checkout:

```sh
pipx install ./saintdle_agentflow-0.0.7-py3-none-any.whl
```

## Verify

```sh
agentflow --version
agentflow doctor
bd --version
```

`doctor` reports executable discovery and workflow health with redacted output.
It does not authenticate provider tools or guarantee provider allowance.

## Install bundled workflow assets

The wheel ships seven generic Agentflow workflow skills plus the controller,
worker, explorer, reviewer, and pull-request gatekeeper profiles for Codex,
Claude Code, and GitHub Copilot CLI. Inspect the planned destinations, then
install them:

```sh
agentflow install --dry-run
agentflow install
```

This is a separate, explicit step because it writes to provider discovery
directories and installs the Agentflow Codex lifecycle hook. Existing files are
preserved for manual review. User-provided skills are registered and managed
with `agentflow skills add`, `sync`, `list`, and `doctor`; they are not bundled
into the distribution.

An ordinary install does not replace existing assets. `--refresh-bundled`
refreshes only assets whose ownership is established by the private install
manifest; on a manifest-less older install, a file or tree is considered owned
only when it exactly matches the bundled resource. Edited or otherwise unknown
assets are preserved and the refresh fails closed rather than claiming them.
Codex hook refresh merges exact Agentflow handler leaves, retaining other event
handlers, rule matchers, and top-level metadata. Changed files receive private
backups before atomic replacement.

## Initialize a project

```sh
cd /path/to/project
agentflow init . --beads
agentflow beads status .
```

Review every proposed hook and generated instruction file before enabling it in
a sensitive repository. Re-running initialization is idempotent for files
managed by Agentflow. Existing custom files are preserved for manual merging.

## Upgrade

Choose an explicit tag from the [GitHub releases](https://github.com/saintdle/agentflow/releases)
page, then reinstall from that tag. The commands below show the current `v0.0.7`
tag; replace it with the exact reviewed release you intend to install. Plain
`uv tool upgrade saintdle-agentflow` or `pipx upgrade saintdle-agentflow` does
not specify a new tag; use the install command with the selected tag to advance.

```sh
uv tool install --reinstall "git+https://github.com/saintdle/agentflow.git@v0.0.7"
# or: pipx install --force "git+https://github.com/saintdle/agentflow.git@v0.0.7"
agentflow --version
agentflow install --dry-run
# Review each proposed refresh; edited/unknown assets remain untouched.
agentflow install --refresh-bundled
agentflow config hooks merge --provider codex --root . --dry-run
agentflow config hooks merge --provider claude --root . --dry-run
agentflow doctor
agentflow skills doctor
```

Apply a hook merge only after reviewing its preview. Codex targets the
user-level `~/.codex/hooks.json`; Claude targets the selected project's
`.claude/settings.json`. The command preserves custom handlers and metadata,
creates a private ownership receipt for future exact-leaf upgrades, and makes
a private backup of a changed target. Invalid JSON or unowned custom data is
not treated as Agentflow-owned. Memory settings have explicit commands too:

```sh
agentflow config memory enable --root . --dry-run
agentflow config memory enable --root .
agentflow config memory disable --root . --local
agentflow config show .
agentflow memory status --root .
agentflow memory maintain --root .
```

The memory toggle materializes strict defaults when needed and preserves the
rest of the selected config. It validates both shared and local layers, refuses
to change a shared value shadowed by local memory settings, and never silently
migrates schemas or drops unknown fields. See [Memory](MEMORY.md) and
[Configuration](CONFIGURATION.md) for the layer and trust contracts.

When an upgrade introduces a new model-policy file, Agentflow preserves the
project's existing policy. Review the versioned policy diff, copy the new file
beside the old one, update `model_policy` in `.agentflow/config.json`, and run
`agentflow policy migrate --root . --dry-run` before applying managed-profile
changes. See [Configuration](CONFIGURATION.md#model-policy).

Read `CHANGELOG.md` before every `0.x` upgrade. Back up shared configuration and
Beads state before applying a documented migration.

If the command and provider integrations currently point into an older source
checkout, do not overwrite them with a generic installer. Follow the
[transactional legacy migration](MIGRATION.md), which previews exact ownership,
keeps a private rollback manifest, and leaves project state outside its write
boundary.

## Uninstall

Remove the Python tool with the installer that created it:

```sh
uv tool uninstall saintdle-agentflow
# or: pipx uninstall saintdle-agentflow
```

Project configuration, Beads state, and Agentflow runtime data are not deleted
automatically. Remove them only after reviewing the exact project-local paths
and retaining any evidence you need.

## Build from source

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]' build twine
python -m build
python -m twine check dist/*
```

Test the resulting wheel in a fresh environment rather than relying only on an
editable checkout.
