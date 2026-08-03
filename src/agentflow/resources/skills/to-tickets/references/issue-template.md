# Agent issue template

```md
## Outcome
<one observable behavior>

## Context
- Goal: <path or URL>
- Depends on: <issue links or none>
- Base branch/SHA: <value>

## Scope
- Owns: <files or subsystem>
- Excludes: <adjacent work>

## Acceptance
- [ ] <testable condition>
- [ ] <testable condition>

## Verification
```sh
<exact commands>
```

## Handoff
Return branch/commit, checks, risks, and evidence. Do not paste raw logs.
```

Suggested labels:

- `agent-work`
- one priority: `agent-priority:p0` through `agent-priority:p3`
- one complexity: `agent-size:s`, `agent-size:m`, or `agent-size:l`
- `blocked` only while a declared dependency or diagnostic prevents progress
