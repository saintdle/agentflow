# Transactional migration from a legacy checkout

Agentflow `0.0.9` can replace user-level links owned by a recognized legacy
source checkout without importing that checkout's private data. The migration
is deliberately narrow, journaled, and reversible.

## What it changes

Only existing symbolic links whose resolved target exactly equals a known path
inside the supplied legacy checkout are eligible:

- the user-level `agentflow` executable link;
- bundled Agentflow skill links in supported provider discovery directories;
- Agentflow controller, worker, explorer, reviewer, and PR-gatekeeper profile links;
- the Agentflow Codex hook link.

The replacement executable must already be a packaged `0.0.9` command in an
isolated environment outside the legacy checkout. Skill, profile, and hook
links are replaced by immutable files from that same installed distribution.

An unrelated file, directory, or link is never inferred to be Agentflow-owned.
This includes user/domain skills such as company-specific authoring skills.
Those entries are reported as preserved and remain byte-for-byte unchanged.

## What it never changes

The migration does not search or write project repositories. It does not copy,
rewrite, or delete:

- Beads databases or issue graphs;
- Agentflow controller, claim, Herdr, session, handoff, history, or evidence
  state;
- project `.agentflow/config.json` or local configuration;
- Git branches, commits, worktrees, remotes, or credentials;
- provider credentials, conversations, or unrelated provider settings;
- the legacy checkout itself.

Absolute paths exist only in a private owner-readable migration manifest under
`${XDG_STATE_HOME:-~/.local/state}/agentflow/migrations/`. They are not added to
a repository or distribution.

## Prepare an isolated command

Stop Agentflow controllers and external workers first. Keep the legacy checkout
in place until the new release has been dogfooded and the rollback window has
closed.

Install the reviewed wheel into a separate environment. One portable example:

```sh
python3 -m venv ~/.local/share/agentflow/versions/0.0.9
~/.local/share/agentflow/versions/0.0.9/bin/python -m pip install \
  ./saintdle_agentflow-0.0.9-py3-none-any.whl
NEW_AGENTFLOW=~/.local/share/agentflow/versions/0.0.9/bin/agentflow
"$NEW_AGENTFLOW" --version
```

The migration also checks the process table for directly identifiable work
running from the legacy checkout. Process discovery cannot prove that every
provider-side task has stopped, so the operator must still inspect Agentflow
and Herdr status before applying.

## Preview and apply

Use a generic path to the private legacy checkout; it is never embedded in the
public package:

```sh
"$NEW_AGENTFLOW" migrate legacy \
  --from /path/to/legacy-agentflow \
  --new-command "$NEW_AGENTFLOW" \
  --dry-run

"$NEW_AGENTFLOW" migrate legacy \
  --from /path/to/legacy-agentflow \
  --new-command "$NEW_AGENTFLOW" \
  --apply
```

Dry-run performs ownership discovery without writing anything. Apply repeats
the ownership and active-process checks under a single migration lock, stages
all packaged replacements, writes a private manifest, and switches each exact
link with an immediate backup. If an operation fails, already-applied entries
are restored automatically.

The successful command returns a migration ID. Verify the new installation:

```sh
agentflow --version
agentflow doctor
agentflow install --dry-run
agentflow skills doctor
```

`agentflow install --dry-run` may offer newly bundled assets that did not exist
in the legacy installation. That is a separate explicit installation decision,
not part of the cutover.

## Roll back

```sh
agentflow migrate legacy --rollback <migration-id>
```

Rollback first verifies every installed destination against the digest stored
at apply time. If any destination has changed, it refuses before mutating any
entry. A successful rollback restores the original symbolic links. It does not
delete the isolated `0.0.9` environment or legacy checkout.

Migration manifests and backups are intentionally retained for audit and
recovery. Remove them only after reviewing their exact paths and deciding the
rollback window has ended.
