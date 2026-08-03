# Changelog

All notable public changes to Agentflow are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Nothing yet.

## [0.0.1] - 2026-08-03

### Added

- Initial public-preview `saintdle-agentflow` distribution for the `agentflow`
  CLI.
- Beads-backed coordination, bounded handoffs, controller state, provider
  preflight, acceptance evidence, review routing, and usage/audit helpers.
- Config-driven skill registration, synchronization, discovery, and health
  checks.
- Project initialization for Git-backed and Gitless working directories.
- Optional Herdr integration and provider adapters for Codex, Claude Code, and
  GitHub Copilot CLI.
- Imported-asset provenance checks and macOS-only fail-closed hardened
  isolation.
- Apache-2.0 licensing, public documentation, community health files, and
  automated validation/package builds.

### Changed

- Direct and Herdr-backed launches enforce an exact approved model route;
  hardened persistent sessions fail closed when confinement cannot be proved.
- Projects can keep machine-local skill and model-policy overrides in an
  ignored `.agentflow/config.local.json` layer.
- Custom skills can be safely removed using Agentflow's managed-link registry
  and report a package digest during health checks.
- Bundled third-party skills carry self-contained provenance and licence
  notices through installation.
- Added a runnable first-workflow tutorial covering approval, dispatch,
  evidence, halt recovery, and completion.
- Release tags fail closed unless the commit is on `main`, all required
  workflows passed for that commit, and no release already exists.
- Public project configuration contains only runtime-enforced model-policy and
  custom-skill fields.
- Bundled installation is digest-aware and supports an explicit, backed-up
  refresh path.
- Hardened-isolation documentation accurately describes its protected-root
  read denylist rather than a global read allowlist.

[Unreleased]: https://github.com/saintdle/agentflow/compare/v0.0.1...HEAD
[0.0.1]: https://github.com/saintdle/agentflow/releases/tag/v0.0.1
