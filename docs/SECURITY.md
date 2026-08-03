# Security model

This document describes Agentflow's technical security boundary. To report a
vulnerability, use the private process in the root [SECURITY.md](../SECURITY.md).

## Trust boundary

The controller is trusted to hold workflow authority, mutate durable state,
validate results, and dispatch work. Workers and provider sessions are less
trusted. They receive task-scoped context and output locations, not controller
credentials or unrestricted authority to mark themselves complete.

Agentflow aims to protect:

- controller credentials and fencing state;
- exact task, claim, base, provider, model, policy, and handoff identity;
- acceptance evidence and reviewer dispositions;
- imported-skill provenance;
- local metadata archives and redacted diagnostics.

Agentflow does not make a coding agent trustworthy, secure a compromised user
account, replace operating-system access control, inspect provider-side data
handling, or guarantee that generated code is safe.

## Controller authority

Controller credentials live outside worker workspaces with restrictive file
permissions. Public lease identifiers are not resume credentials. Reattachment
rotates credentials and fences stale processes from mutation. Provider-visible
return contracts are bounded and authenticated; only the controller validates
and consumes a matching result.

Do not copy controller state into a repository, shared worktree, provider
prompt, or support report.

## Preflight and provider policy

External launch preflight binds the exact work root, task, base revision,
claim, provider, model, role, effort, policy version, handoff, required tools,
required skills, output boundary, and budget. Missing or ambiguous
security-relevant input blocks launch.

A model policy is local governance, not proof of provider identity or service
security. Use exact provider identifiers and review policy changes.

## Hardened isolation

Hardened subprocess isolation in `0.0.1` is available only on macOS through
`/usr/bin/sandbox-exec`. It uses an explicit read/write allowlist, denies
network by default, bounds resources and runtime, and tears down the process
group on timeout.

```sh
agentflow isolation probe --write /path/to/scratch
agentflow isolation launch --write /path/to/scratch -- python3 script.py
```

The probe validates controls against fresh state. A missing control, ambiguous
network result, unavailable `sandbox-exec`, or non-macOS host fails closed.
Agentflow does not silently fall back to ordinary execution when a handoff
requires `hardened` isolation.

Linux can run the core CLI but has no equivalent hardened isolation in `0.0.1`.
Running a command in a shell, virtual environment, worktree, or terminal
multiplexer is not an isolation boundary.

`sandbox-exec` is a platform facility with its own limitations. Use a dedicated
machine, virtual machine, or reviewed container boundary for genuinely hostile
code or high-value credentials.

## Imported assets and skills

Skills, plugins, hooks, MCP packages, and provider profiles can influence agent
behavior or execute code. Treat them as supply-chain inputs. Agentflow's asset
lock records source provenance, immutable revision, package digest, licences,
capabilities, and approval. Verification rejects unlocked, changed, or
capability-expanded material.

Review assets before locking them. A valid digest proves identity, not safety.
Do not grant a skill controller credentials or broad write access merely
because it is installed.

## Filesystem and Git safety

Initialization creates only missing files and preserves unrelated user
configuration. Managed ignore markers keep runtime authority and temporary
artifacts out of Git. Malformed markers halt for review rather than rewriting
the file.

Worktree retirement and destructive cleanup require explicit checks. Never use
unresolved home-directory variables, repository roots, or broad globs as
destructive targets.

## Data minimization

Diagnostics and history records are metadata-only and should redact sensitive
paths. Agentflow does not need credential-file contents. Never commit or share:

- access tokens, cookies, signing keys, or controller credentials;
- provider prompts, responses, reasoning, or tool logs;
- customer, employer, billing, or subscription data;
- local runtime directories or private session archives.

Before release, scan both the current tree and Git history for secrets and
private material.

## Dependencies

Beads, provider CLIs, Herdr, GitHub CLI, Git, Python, and the operating system
remain separate trust domains. Pin supported versions where practical, monitor
their advisories, and reproduce integration bugs independently before deciding
which project owns the fix.
