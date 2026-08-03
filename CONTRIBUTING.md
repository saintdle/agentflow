# Contributing to Agentflow

Thank you for helping improve Agentflow. This project welcomes focused bug
reports, documentation improvements, tests, and implementation changes.

By participating, you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
Security vulnerabilities must be reported through the private process in
[SECURITY.md](SECURITY.md), not a public issue.

## Before opening a change

1. Search existing issues and pull requests.
2. Open an issue for changes that alter public commands, configuration,
   security boundaries, or architecture.
3. Keep a pull request limited to one independently reviewable outcome.
4. Do not include credentials, provider transcripts, customer data, account
   details, personal paths, or internal-only material in code, fixtures, logs,
   commits, or screenshots.

## Development setup

Use Python 3.10 or later and an isolated virtual environment:

```sh
git clone https://github.com/saintdle/agentflow.git
cd agentflow
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]' build twine
```

Install Beads separately and ensure `bd` is on `PATH`. Optional integration
tests may also require their named provider CLI, Herdr, GitHub CLI, or macOS
isolation capability. Tests must skip clearly when an optional external tool is
absent; they must not infer success for an unavailable security control.

## Checks

Run the same core checks expected in continuous integration:

```sh
python3 scripts/validate.py
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m build
python3 -m twine check dist/*
git diff --check
```

Add or update tests for behavior changes. Documentation-only changes should
still pass link, formatting, and privacy checks where available.

## Pull requests

- Use a descriptive title and explain the user-visible outcome.
- Link the issue or approved goal when one exists.
- State the exact checks run and any checks that were not run.
- Call out compatibility, configuration migration, and security effects.
- Update `CHANGELOG.md` under `Unreleased` for user-visible changes.
- Preserve existing user configuration; migrations must be explicit,
  idempotent, and tested.

Maintainers may request changes or close pull requests that are unsafe,
out-of-scope, or cannot be maintained. Contributors do not merge their own
pull requests.

## Releases

Agentflow follows Semantic Versioning, with the understanding that `0.x`
versions may make breaking changes. A release should:

1. Move relevant `Unreleased` entries into a dated version section.
2. Set one matching version in package metadata and runtime output.
3. Pass tests, validation, package build, and clean-install checks on supported
   platforms.
4. Be merged to `main`, then tagged as `vX.Y.Z` from the reviewed commit.
5. Publish generated artifacts and release notes without rebuilding from a
   different source revision.

Deprecations should be announced in the changelog and retained for at least one
minor release when safety and feasibility allow.

## Licensing contributions

Unless you explicitly state otherwise, contributions intentionally submitted
for inclusion are licensed under Apache-2.0 as described in section 5 of the
project licence. Only submit work you have the right to contribute. Record the
licence and attribution for adapted third-party material.
