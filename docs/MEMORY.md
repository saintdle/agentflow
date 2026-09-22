# Optional governed memory

Agentflow memory is disabled by default. Existing `agentflow.project@1`
configuration remains valid when the `memory` section is absent; `agentflow
config show` supplies the disabled defaults. Enable it explicitly in the
shared configuration and use the local layer only for an intentional machine
override. Local values win only after both layers pass strict validation.

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
"memory": {
  "enabled": true,
  "on_prompt": true,
  "scopes": ["project"],
  "max_items": 5,
  "max_chars": 2000
}
```

Use `agentflow memory maintain --root .` for an explicit local maintenance
run. Memory never schedules work, accesses the network, or requires Neo4j.
