# Local session-history metadata

Agentflow can maintain a local, metadata-only index of recoverable provider
sessions. This feature is optional and is not required for orchestration.

## Privacy boundary

History synchronization fingerprints provider files in place. It does not copy
prompt text, responses, reasoning, tool input or output, credentials, or full
transcripts into the archive. Records contain only bounded metadata and
provenance needed to locate or classify an original session.

The archive is private local state. Keep it outside repositories with
owner-only permissions and never attach it wholesale to an issue or support
request.

## Commands

```sh
agentflow history status
agentflow history sync --dry-run
agentflow history sync
agentflow history pending --limit 20
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
