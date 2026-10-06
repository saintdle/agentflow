# Local session-history metadata

Agentflow can maintain a local, metadata-only index of recoverable provider
sessions. This feature is optional and is not required for orchestration.

## Privacy boundary

History synchronization fingerprints provider files in place. It does not copy
prompt text, responses, reasoning, tool input or output, credentials, or full
transcripts into the archive. Records contain only bounded metadata and
provenance needed to locate or classify an original session. Where provider
formats expose it, Agentflow also records exact model, effort, role, parent
identity, delegation depth, and numeric token/context counters. Claude Code and
Copilot CLI contribute the bounded fields they expose; unavailable fields
remain empty rather than being inferred.

The archive is private local state. Keep it outside repositories with
owner-only permissions and never attach it wholesale to an issue or support
request.

Archive mutations use a private `.history.lock` file to serialize both threads
and independent processes. A Beads import is written first to the private
`.history-pending-update.json` transaction journal, then imported using stable
IDs, and finally committed to the archive manifest. If a process stops during
that sequence, the next mutating archive operation validates and replays the
journal before proceeding; replay is an upsert, not duplicate creation. The
journal is cleared only after both the import and manifest replacement finish.
Do not remove or edit a pending journal manually; use the history command again
to let recovery run, or retain the archive for investigation if recovery fails.

## Commands

```sh
agentflow history status
agentflow history sync --dry-run
agentflow history sync
agentflow history pending --limit 20
agentflow context audit --root . --days 30
```

Dry-run before the first synchronization and after provider upgrades. Provider
storage formats are not stable public APIs; unreadable or ambiguous records
should be skipped safely.

Curated documentation can be registered explicitly:

```sh
agentflow history artifact register architecture ./docs \
  --title "Architecture notes" \
  --kind documentation \
  --description "Curated architecture reference" \
  --include '**/*.md'
agentflow history artifact list
agentflow history artifact unregister architecture
```

Apply only summaries that conform to the documented structured schema:

```sh
agentflow history apply-summary summary.json
```

Review summaries before applying them. They must not reproduce private source
content, credentials, customer data, or sensitive paths.

## Scheduling

Where supported, a model-free local refresh can be managed with:

```sh
agentflow history schedule install
agentflow history schedule status
agentflow history schedule uninstall
```

Scheduling support is platform-specific. Uninstalling the schedule does not
delete the archive.

## Context and routing audit

`agentflow context audit` reads the sanitized manifest, never the raw provider
files. It reports aggregate model/role counts and stable history Bead IDs for
anomalies such as expensive execution routes, excessive child/depth budgets,
ambiguous models, and high recorded context pressure. Token counters are
provider metadata and are not billing evidence.

New Codex manifests add `usage_metadata` with source, client version,
availability, cross-check diagnostics, aggregate input/cached-input/output/total
counters, request count, and maximum request input. Cached input is a subset of
input. Context-pressure audits use maximum request input when available; older
manifests use the explicitly labeled input-plus-output proxy in
`peak_context_tokens` (`peak_context_semantics: input_plus_output_proxy` on new
manifests). Aggregate totals are not treated as request occupancy. Missing or
ambiguous usage stays explicit, with available reconciliation diagnostics
retained in the manifest.

Use `agentflow context compact` for an explicit one-time, transcript-free
compaction recommendation. It tells the controller to persist decisions and
evidence, disposition the current result, rotate only at a safe boundary, and
resume the same root from protected state.
