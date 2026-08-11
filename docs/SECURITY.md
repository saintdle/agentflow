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

```mermaid
flowchart TB
    subgraph Trusted["Trusted coordination boundary"]
        Controller["Root controller<br/>lease, fencing, policy, disposition"]
        Authority["Controller credentials<br/>outside worker workspaces"]
        Beads["Beads<br/>durable graph and evidence"]
        Authority --> Controller
        Controller <--> Beads
    end

    subgraph Scoped["Task-scoped worker boundary"]
        Handoff["Typed handoff<br/>exact claim, base, skills, scope, budget"]
        Worker["Provider worker session"]
        Workspace["Bounded output workspace"]
        Inbox["Authenticated return contract<br/>structured result only"]
        Handoff --> Worker
        Worker --> Workspace
        Worker --> Inbox
    end

    subgraph External["External trust domains"]
        Launcher["Herdr, tmux, or native agent runtime"]
        Provider["Codex, Claude Code, or Copilot service"]
        Git["Git and GitHub"]
        Launcher --> Provider
    end

    Controller --> Handoff
    Controller --> Launcher
    Provider --> Worker
    Inbox -->|"validate and consume"| Controller
    Workspace --> Git
    Controller -->|"reviewed integration only"| Git
```

Controller credentials and raw provider transcripts do not cross into the
task-scoped worker boundary. The return channel carries a bounded result that
the controller authenticates and validates before changing durable task state.

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

Hardened subprocess isolation in `0.0.3` is available only on macOS through
`/usr/bin/sandbox-exec`. Process execution, writes, and network access are
deny-by-default. Reads use a different boundary: the profile permits blanket
system reads so interpreters and toolchains can start, denies protected home
roots such as `/Users`, and then explicitly re-allows only caller-declared
read/write roots plus an ephemeral home. Undeclared system paths such as
`/private/tmp`, `/opt`, or mounted volumes may therefore remain readable. It
also bounds resources and runtime and tears down the process group on timeout.

```sh
agentflow isolation probe --write /path/to/scratch
agentflow isolation launch --write /path/to/scratch -- python3 script.py
```

The probe validates controls against fresh state, but a successful probe is not
confinement for a later process. `agentflow isolation launch` is the separate,
synchronous path that applies the generated hardened profile to the supplied
command. A missing control, ambiguous network result, unavailable
`sandbox-exec`, or non-macOS host fails closed.

Direct `agentflow handoff launch` requires an exact role, model, and effort and
validates them against model policy. It is not the synchronous isolation path:
a handoff declaring `isolation_profile: hardened` is rejected rather than
launched unconfined. Authenticated Herdr/controller launch also rejects hardened
handoffs in `0.0.3`, because a probe cannot confine the later persistent
provider session. Use `agentflow isolation launch` for synchronous hardened
commands; persistent-session confinement is not currently provided.

Linux can run the core CLI but has no equivalent hardened isolation in `0.0.3`.
Running a command in a shell, virtual environment, worktree, or terminal
multiplexer is not an isolation boundary.

`sandbox-exec` is a platform facility with its own limitations. Use a dedicated
machine, virtual machine, or reviewed container boundary for genuinely hostile
code or high-value credentials. Do not place secrets in globally readable
temporary, toolchain, or mounted-volume paths and rely on Agentflow to hide
them.

## Publication history gate

The security workflow fetches every branch and tag and scans every reachable
historical blob, not only the current checkout. It rejects credential
signatures, personal home paths, sensitive file names, provider transcripts,
Agentflow runtime state, and Beads database artifacts even if they were later
deleted from `HEAD`.

Maintainers can configure a newline-delimited
`AGENTFLOW_PUBLICATION_DENYLIST` repository secret for organization-specific
names or paths. The scanner reports only a redacted finding code, object ID,
and repository path; it never echoes a matched marker. The secret-backed pass
runs only on trusted non-pull-request events so unreviewed PR code never
receives the private denylist.

Before a repository becomes public, run the same scanner locally with an
owner-readable denylist file and inspect all fetched refs:

```sh
git fetch --force --prune origin '+refs/heads/*:refs/remotes/origin/*' \
  '+refs/tags/*:refs/tags/*'
python scripts/ci/scan_git_history.py . --denylist-file /private/path/denylist.txt
```

A finding in published history requires a deliberate credential response and,
when appropriate, a separately approved history rewrite. Deleting the current
file is not remediation.

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
