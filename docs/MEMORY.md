# Optional governed memory

Agentflow memory is disabled by default. Existing `agentflow.project@1`
configuration remains valid when the `memory` section is absent; `agentflow
config show` supplies the disabled defaults. Enable it explicitly in the
shared configuration, or use the local layer for an intentional machine
override. The toggle writes the complete strict default memory object before
changing only `enabled`; it validates both layers and refuses unknown fields
or implicit schema migration. If a local `memory` object shadows shared
settings, a shared toggle is refused with guidance to use `--local`.

```sh
agentflow config memory enable --root . --dry-run
agentflow config memory enable --root .
agentflow config memory disable --root . --local
agentflow memory status --root . --json
agentflow memory maintain --root .
```

Dry-run prints a unified diff and does not create directories, backups, or
files. Applied changes use an owner-only atomic replacement and a private
backup. `--local` updates the ignored `.agentflow/config.local.json`; without
it, the shared `.agentflow/config.json` is the target.

Provider hooks normalize SessionStart, UserPromptSubmit, tool start/success/
failure, compaction, and Stop into metadata-only events. Prompt text, commands,
arguments, tool output, transcripts, paths, and credentials are discarded.
Hook storage is fail-open and owner-only under the local Agentflow state home.
`AGENTFLOW_STATE_HOME` is the explicit isolated state root and takes precedence;
`XDG_STATE_HOME` remains the fallback traditional `XDG_STATE_HOME/agentflow`
location.

Recall performs candidate lookup followed by a governed fetch. Only approved,
fresh, in-scope entries are eligible, with strict item and character budgets.
Prompt hooks use provider-native prompt fields transiently; SessionStart uses
the validated `memory.startup_query` setting (never a prompt or transcript).
Source digests are recorded per session so a digest is not injected twice;
explicit compaction/session reset is the only reset policy. `Stop` maintenance
is model-free, idempotent, lock-protected, rate-limited, retryable, and reports
durable health through `agentflow doctor` or `agentflow memory status`.

```json
{
  "schema": "agentflow.project@1",
  "version": 1,
  "model_policy": ".agentflow/models-v2.json",
  "memory": {
    "enabled": true,
    "on_prompt": true,
    "capture_failures": true,
    "max_items": 5,
    "max_chars": 2000,
    "max_age_days": 30,
    "scopes": ["project"],
    "scope_id": "",
    "max_events": 10000,
    "max_event_bytes": 5242880,
    "retention_days": 30,
    "session_retention_days": 30,
    "maintenance_interval_seconds": 300,
    "session_ledger_limit": 256,
    "startup_query": "agentflow"
  },
  "skills": []
}
```

The JSON above is a complete valid minimal schema-v1 project configuration;
the CLI is preferred to hand-editing it. Use `agentflow memory maintain
--root .` for an explicit local maintenance run. Memory never schedules work,
accesses the network, or requires Neo4j.

Approval is an advisory trust decision, not a cryptographic attestation.
Recall eligibility depends on locally stored approval metadata and current
scope/freshness checks; anyone able to alter that local state can also alter
its approval metadata. Review the source before approving an entry and do not
treat approval labels as proof that content is safe or true.

Approval IDs such as `human:` and `controller:` are same-user governance
metadata, not authenticated identities, signatures, or OS isolation. A
same-user process with database access can alter entries and approvals. Recall
is JSON-quoted untrusted reference data: approval permits recall, never command
authority, and cannot override current system, user, or project instructions.
Verify the cited source and its full digest before relying on an entry. Memory
framing is not a sandbox or a guarantee against model injection. The rendered
header and records count against `max_chars`; an empty or too-small budget
produces no injected header.
