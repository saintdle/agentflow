# Changelog

All notable public changes to Agentflow are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Added a narrowly scoped, authenticated `controller resume --acknowledge-no-ready-halt`
  recovery path for a verified no-ready-work checkpoint after a dependency gate
  changes, while keeping other terminal and safety halts closed.
- Added explicit project memory enable/disable commands with strict layered
  validation, dry-run diffs, owner-only atomic writes, and private backups.
- Added targeted Codex and Claude hook merges that preserve custom handlers and
  metadata and retain private exact-handler ownership receipts for later
  upgrades.

### Changed

- Bundled refresh now uses ownership evidence, merges only Agentflow-owned
  Codex handlers, and preserves edited or unknown assets for review.
- History archive mutations serialize across threads and processes and recover
  interrupted Beads imports from a validated private journal.
- Documented the advisory nature of local memory approval metadata and updated
  the project Claude hook example with current provider events.

## [0.0.7] - 2026-10-05

### Fixed

- Start a stopped local Herdr server before provider launch, and fail early
  when the Codex Herdr session integration is missing.
- Surface Codex's first-use directory-trust prompt as a resumable, exact-pane
  `USER_ACTION_REQUIRED` response in serial and parallel controllers rather
  than silently waiting for provider identity or relaunching the task.
- Spell out the machine result enum and acceptance evidence shape in external
  handoffs so a worker does not submit a prose outcome the validator rejects.

## [0.0.6] - 2026-10-04

### Added

- Added bounded parallel root dispatch with an admission-phase gate that keeps
  deadline-draining controllers from claiming fresh work after a crash.
- Added a protected separate-terminal `controller supervise` runner with a
  per-root process lock and authenticated same-lease restart across chat or
  process boundaries.
- Added typed root workspace contracts for authenticated external handoffs:
  Git workspaces pin branch@revision, while Gitless directories bind the exact
  canonical root path without a synthetic Git base.
- Added a bounded pull-request CI smoke against real Beads 1.1.0 while keeping
  the broader integration run on its scheduled and manual triggers.
- Added protected provider-session bindings so controller hooks resolve the
  approved workflow workspace instead of an incidental IDE cwd.
- Added hash-inventoried sterile launch packages for external tasks that opt
  into restricted outbound context.
- Added deterministic generation checks for bundled model and skill resources.

### Changed

- Added exact GPT-6 Sol routes for Codex controller, judgment, and review work,
  and GPT-6 Luna routes for coding, exploration, and editing. Bundled Codex
  profiles now default to GPT-6 while GPT-5.6 Sol/Luna remain accepted for
  existing project configurations.
- Clarified public tagged-source installs and upgrades, the Herdr prerequisite
  for persistent external controller sessions, and GitHub Copilot Chat setup
  where IDE Agent mode and terminal access are available.
- External handoffs now require the leased controller's authenticated Herdr
  result channel; direct external launch fails before provider spawn.
- Native direct handoffs no longer advertise controller-owned result files;
  the conditional prose editor uses this bounded direct-session lane.
- Authenticated results now require matching local lifecycle model evidence
  for the bound provider session; this cooperative evidence is not
  provider-signed proof. Claude launch also rejects hook suppression in known
  project, user, and file-managed settings before spawn.
- Persistent Herdr Copilot launches remain fail-closed when actual-model
  evidence is unavailable; native direct Copilot use remains available.

### Fixed

- Controller deadlines now persist a resumable `incomplete` checkpoint and
  return a nonzero exit instead of reporting success; `--deadline 0` is an
  immediate deadline rather than the one-hour default.
- Handoff preflight and sterile packaging now accept exact, provider-approved
  external skill sources registered through project configuration while still
  rejecting unregistered, retargeted, or package-escaping skill references.
- Provider hooks remain fail-open when the protected workspace-binding
  registry is unreadable or malformed.
- Sterile packaging revalidates the exact approved skill pin before copying
  and verifies the copied package bytes before launch.
- Root preflight resolves relative output boundaries against the approved
  execution root rather than the invoking shell's directory.
- Approved memory recall no longer starves behind draft candidates.

## [0.0.5] - 2026-09-15

### Added

- Added controller-only execution admission with graph-derived launch budgets,
  parallel/depth/retry caps, expensive execution limits, and complete typed
  Beads-root overrides.
- Added dedicated Luna/Sonnet worker profiles for Codex, Claude Code, and
  GitHub Copilot, plus bounded native-context routing guidance.
- Added metadata-only model, effort, lineage, token, and context-pressure audit
  across the provider history formats that expose those fields.
- Added Beads/Herdr lifecycle reconciliation and optional transcript-free
  strategic-compaction and planned-only verification guidance.

### Changed

- Controller rotation is now required at a safe disposition boundary and the
  next authenticated resume advances the context generation without replacing
  the workflow root.
- Codex Terra is an approved selective coding/exploration route; it requires an
  explicit persisted selection. Generic, Auto, and Haiku routes still fail
  closed.
- Legacy schema-v1 project configs remain valid and receive safe runtime
  defaults for the new execution and guidance sections.

## [0.0.4] - 2026-08-12

### Added

- Added an automatic controller classification for Claude-authored
  reader-facing Markdown and deterministic `prose check`, conditional `prose
  prepare`, and preservation-aware `prose verify` commands.
- Added fail-closed `editing` routes at `medium` effort. Codex Luna is the
  default; project or machine configuration can select policy-approved Claude
  Sonnet or Copilot Claude Sonnet for users without OpenAI access, or `null`
  for deterministic-only checks. Passing prose uses no editor; failing prose
  permits one sibling-file edit with domain validation, never an automatic
  overwrite.

## [0.0.3] - 2026-08-11

### Changed

- Added task-aware controller-session budgets, repeated-approach halts, and
  transcript-free `controller rotate` handoff packets.
- Added advisory CodeBurn optimize reconciliation that preserves managed assets
  and distinguishes heuristic savings from Agentflow delivery evidence.
- Added pinned Claude Opus 5 routes for Claude Code and GitHub Copilot. Opus 5
  is selected only when explicitly requested and defaults to `medium` effort;
  managed controller and judgment profiles continue to prefer Opus 4.8.
- Added an agent-readable installation contract so a user can point a
  ChatGPT/Codex or Claude coding agent at the repository and request a safe,
  verified, non-overwriting setup.
- Added a chat-first walkthrough with copy/paste prompts for goal shaping,
  approval, persistent autonomous execution, reconnect-safe resume, read-only
  status, and bounded pull-request delivery without requiring the user to run
  Agentflow CLI commands directly.
- Added Mermaid component, controller-lifecycle, and security trust-boundary
  diagrams to the README and detailed documentation.

## [0.0.2] - 2026-08-03

### Added

- Transactional `agentflow migrate legacy` dry-run, apply, and rollback for
  exact user-level links owned by a recognized private legacy checkout.
- Owner-readable migration manifests, per-entry backups, active-process
  checks, drift-resistant rollback, and synthetic cutover tests that keep
  project, Beads, session, evidence, and Git state outside the write boundary.
- A full-history publication scanner covering every reachable branch/tag blob,
  sensitive historical paths, credential signatures, personal home paths, and
  an optional non-echoing private denylist enforced by the security workflow.
- Explicit skill-local and distribution documentation attributing the adapted
  `to-tickets`, `code-review`, `diagnosing-bugs`, and `wayfinder` skills to Matt
  Pocock's MIT-licensed `mattpocock/skills` project.
- A prominent disclosure that Agentflow is majority AI-generated, dogfoods its
  own multi-agent workflow, and is suitable only for development/testing.

### Security

- Legacy migration replaces only symbolic links with exact recognized targets,
  rejects a replacement executable inside the legacy checkout, and refuses
  rollback if any installed destination drifted.
- Release privacy checks now inspect content removed from the current checkout
  but retained anywhere in Git history.

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

[Unreleased]: https://github.com/saintdle/agentflow/compare/v0.0.7...HEAD
[0.0.7]: https://github.com/saintdle/agentflow/compare/v0.0.6...v0.0.7
[0.0.6]: https://github.com/saintdle/agentflow/compare/v0.0.5...v0.0.6
[0.0.5]: https://github.com/saintdle/agentflow/compare/v0.0.4...v0.0.5
[0.0.4]: https://github.com/saintdle/agentflow/compare/v0.0.3...v0.0.4
[0.0.3]: https://github.com/saintdle/agentflow/compare/v0.0.2...v0.0.3
[0.0.2]: https://github.com/saintdle/agentflow/compare/v0.0.1...v0.0.2
[0.0.1]: https://github.com/saintdle/agentflow/releases/tag/v0.0.1
