# Installation

The published Python distribution name is `saintdle-agentflow`. It installs
the `agentflow` command and the `agentflow` Python import package. Do not install
the unrelated `agentflow` or `agentflow-cli` distributions from a package
index.

## Prerequisites

Agentflow supports Python 3.10 or later on macOS and Linux. Install these tools
before Agentflow:

1. Git for Git-backed projects.
2. Beads 1.1 or later, with `bd` available on `PATH`.
3. One or more coding-agent CLIs you intend to use.

Herdr, GitHub CLI, and `tmux` are optional. Agentflow reports missing optional
integrations without reading their credential stores.

## Install an isolated CLI

Install a tagged release with `uv`:

```sh
uv tool install "git+https://github.com/saintdle/agentflow.git@v0.0.1"
```

Or with `pipx`:

```sh
pipx install "git+https://github.com/saintdle/agentflow.git@v0.0.1"
```

For a local checkout:

```sh
git clone https://github.com/saintdle/agentflow.git
cd agentflow
pipx install .
```

The repository requires authentication while it remains private. A release
wheel built by GitHub Actions can be installed without a source checkout:

```sh
pipx install ./saintdle_agentflow-0.0.1-py3-none-any.whl
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
explorer, reviewer, and pull-request gatekeeper profiles for Codex, Claude Code,
and GitHub Copilot CLI. Inspect the planned destinations, then install them:

```sh
agentflow install --dry-run
agentflow install
```

This is a separate, explicit step because it writes to provider discovery
directories and installs the Agentflow Codex lifecycle hook. Existing files are
preserved for manual review. User-provided skills are registered and managed
with `agentflow skills add`, `sync`, `list`, and `doctor`; they are not bundled
into the distribution.

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

Upgrade to an explicit tag, then rerun health checks:

```sh
uv tool upgrade saintdle-agentflow
# or: pipx upgrade saintdle-agentflow
agentflow --version
agentflow doctor
agentflow skills doctor
```

Read `CHANGELOG.md` before every `0.x` upgrade. Back up shared configuration and
Beads state before applying a documented migration.

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
