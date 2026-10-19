# Beads 1.3.1 workspace upgrade

Agentflow requires Beads 1.3.1 or later. Its adapter uses the Beads CLI;
it does not start `bd serve`, use the Beads HTTP API, run `bd reclaim`, or
install a reclaimer. Reclamation in the integration suite is explicit and
confined to disposable test stores.

## Establish the maintenance boundary

Inventory each real store, its clients and any Dolt remote before opening it
with the new binary. Read `.beads/metadata.json`, `config.yaml`, redirects and
Git worktree relationships first. Worktrees that resolve to the same store
belong to one maintenance unit. A shared server's data lives outside the
project's `.beads`; preserve its actual data directory too.

Record the selected `bd` version and every client's executable. Identify the
running server executable separately from the `dolt` selected by `PATH`.
Persisted `.local_version` is a witness of a prior client, not a schema
measurement. Do not infer migration success from that file.

Pause controllers, hooks, dashboards and other writers for the affected
maintenance unit. Confirm they have stopped before taking a cold snapshot.
For a shared server, coordinate all clients, then stop the identified service
cleanly using its service manager. Do not kill an unrelated Dolt process.

## Preserve and rehearse recovery

An ordinary open of an old embedded store under 1.3.1 can migrate it. In
particular, `bd export` with the new binary is not a pre-migration backup.
See the [pinned upstream upgrade guide](https://github.com/gastownhall/beads/blob/v1.3.1/docs/getting-started/upgrading.md).

After quiescence, copy the complete physical data and configuration to a
private, timestamped destination outside the source. Include Dolt history,
working sets, ignored/clone-local state, project `.beads` metadata/config and
the shared server data/config when applicable. A Dolt commit/push or JSONL
export alone does not preserve all of those. Preserve uncommitted working
sets even when auto-commit has been disabled or batched.

Create a file inventory and SHA-256 manifest; verify a second restored copy
against the manifest. Keep the original snapshot sealed. Run a verified old
official binary only against a separate pre-migration copy if an old-format
export or baseline issue/dependency count is needed. Never point it at a
partly migrated live store.

Rehearse migration on the restored copy with 1.3.1. For server-backed copies,
use an isolated server, unique port and rewritten connection settings so the
rehearsal cannot contact the live service or remote. Compare issue IDs,
statuses, descriptions, acceptance metadata, comments, parent/dependency
edges, representative ready/blocked results and Dolt history. Restore and
check again if the rehearsal fails. A backup is accepted only after restore
has been demonstrated; record its path, manifest and validation results.

The repository's scheduled integration reproduces a cold snapshot, restore
and upgrade of a newly generated 1.1.0 store. This validates that fixture;
it does not certify a particular user's existing backup or database.

## Migrate each topology deliberately

For an embedded store without a remote, keep writers paused and run
`bd migrate schema` from the verified workspace with an adequate maintenance
timeout. Main and clone-local migrations have separate counters; let both
finish before normal orchestration with short command timeouts resumes.

For a shared server, upgrade all clients first and verify the service binary
that will run after its controlled restart. Run `bd migrate schema` explicitly
from an already configured workspace for each affected database, including
the global database if used. Do not change topology as part of the upgrade.

If a Dolt remote exists, follow upstream's designated-migrator procedure.
Only that migrator may use `bd migrate schema --force` after backup and
client coordination, then publish the migrated state. Do not set a global
migration bypass. Other clones must preserve unpublished work before the
upstream rebootstrap procedure; `bd bootstrap` is not a generic repair.

Refresh only hooks that are actually Beads-owned. Agentflow initializes with
`--skip-hooks --skip-agents`; preserve custom hooks and provider files. Review
the `.beads/dolt-server-config.yaml` ignore rule and individual health findings
instead of applying a broad `doctor --fix`.

## Validate, resume or roll back

After migration, record schema/health results and compare the baseline counts,
contracts, metadata, comments and dependency/readiness checks. Exercise an
exact root-scoped claim, actor-matched heartbeat, review block/unblock,
evidence-backed close and restart recovery in an expendable validation task.
Check the running server executable/version after restart. Resume writers
only after these checks pass.

If validation fails, keep writers paused, retain the failed state for diagnosis
and restore the entire consistent maintenance unit. Select a compatible
binary/server for the restored schema; do not downgrade a migrated store in
place. Coordinate all shared clients and any published remote before rollback
so a restored old schema cannot synchronize with a newer one.

## Native task supervision

The controller renews each active task with its recorded actor, separately
from its filesystem controller lease. The polling interval is limited to
60 seconds; native Beads task leases default to five minutes. Launch and
authenticated result acceptance also renew the exact claim. Full selected
task contracts remain authoritative even though discovery rows are brief.

Each newly issued claim is also sealed to its native claim-event identities in
protected controller storage. Actor names and second-resolution timestamps do
not identify a claim generation. Supervision and protected launch/result checks
verify the seal before and after native renewal. A reclaim and regrant to the
same actor therefore fences the old execution, even if native heartbeat succeeds.
Unrelated edits, normal heartbeats and idempotent same-owner claims preserve
the seal. Keep complete native audit history while an execution is active;
missing, unavailable or changed claim history fails closed.

Existing executions without a protected generation seal require operator
recovery. Resume and claim adoption do not infer a seal from current ownership
or rebind an existing capability to a later native claim. Stop and reconcile
workers before issuing replacement work; the seal does not establish worker
termination or make reclaim/admission atomic across independent CLI operations.

A failed, timed-out or identity-mismatched renewal persists a fence in the
protected controller state, halts admission and refuses further launches or
results for that task. A later successful heartbeat does not clear the fence.
This does not prove a provider process has stopped: inspect the exact live
worker, preserve its output and resolve ownership before operator recovery.
Do not delete fencing state or enable a periodic reclaimer as a workaround.

Automatic reclamation, HTTP transport, and a general claim-fence recovery
command require separate validation and are outside this compatibility change.
