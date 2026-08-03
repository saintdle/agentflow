# Bounce-back protocol

Use a stable marker so repeated scans update or skip the existing comment:

```md
<!-- agentflow-gatekeeper -->
## Integration blocked

- Status: `BLOCKED`
- Gate: <CI | conflict | review | policy | identity>
- Evidence: <check name, conflict files, or review state>
- Reproduce: `<exact command or GitHub link>`
- Base branch status: <passes | also failing | untested>
- Owner: @<original worker or assignee>
- Next action: <one bounded fix>
```

Keep log excerpts under 20 lines. Prefer a check URL to copied output. Do not open repeated comments for the same unchanged failure.
