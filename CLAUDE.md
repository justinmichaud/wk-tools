# Working on wk-tools itself

This file is for an agent editing **this repository**. The other two audiences
have their own files and this one does not repeat them:

- `claude/CLAUDE.md` — deployed *inside* a workspace (container or macOS guest).
- `claude/CLAUDE-host.md` — deployed to a host machine's `~/.claude`, for
  driving `wk` rather than editing it.

The rules below were set by the user across several sessions and were previously
scattered through `docs/HANDOFF*.md`. They live here now because they bind every
change, not one task. Where an older doc or comment disagrees with them, they
win and the statement is a bug to fix rather than a second opinion.

---

## The hazard that has actually bitten

**Never edit wk-tools while a `wk` command is running.** bash reads a script
incrementally, so rewriting `cmd/build` under a 23-minute build resumed the
process mid-word and dropped it back into the lock wait. It applies with more
force to the copy on a build machine, which `t_sync_tools` replaces at the start
of every build. Nothing in the tooling prevents this today — check `wk status`
first.

## State and lifecycle

1. **Smallest possible state, no caching of facts.** Every fact is recomputed
   from evidence at read time and lives in exactly one place per machine; a
   second copy is a bug even while it is still equal. Stores of *artifacts*
   keyed by content — ccache, base snapshots, seeded benchmark payloads — are
   not caches of facts. The test: could a read recompute this value, or only
   re-download/rebuild it?
2. **Crash-only, guaranteed final state.** Every mutating command can be killed
   at any point and re-run, and the re-run converges to the declared final
   state. "Already exists" is never the answer to a half-made thing.
3. **Wipe over repair.** Deleting a workspace or re-provisioning a box from
   scratch beats patching around an unexpected state. Resume-in-place is
   reserved for expensive stages whose evidence is unambiguous (a mirror fetch,
   the vm base build). The engineering effort goes into making
   destroy-and-recreate cheap and total, never into clever in-place repair.
4. **One lock per mutated resource.** Concurrent runs serialize or refuse by
   name. A lock is not state: it dies with its holder.
5. **Detect un-managed clobbering.** When the record and the machine disagree —
   a by-hand `podman rm`, `tart delete`, a fetch into a published snapshot — the
   machine wins and the command says so.

`docs/HANDOFF-workspace-state.md` is where these came from and carries the
reasoning; this is the binding statement.

## Cattle, not pets

- Every machine is reproducible from this repo plus its declared restorables.
- **New machine-local state goes into `wk doctor`'s machine-local section, or it
  is a bug.** Each entry declares itself `regenerable`, `re-authable` or
  `backed-up`.
- **New devices arrive as config, never code** — `boot/machines/<name>.conf`,
  `targets/hosts/<name>.conf`, `bridge/hosts/<name>.conf`. A `case` statement
  naming a machine is the shape being replaced.
- Hand-applied settings are expected to vanish on a rebuild. Anything that is
  missed belongs in `./setup` or `wk backup`, not in a person's memory.

`docs/HANDOFF-cattle.md` holds the per-machine ledger and the open gaps;
`docs/HANDOFF-reprovision.md` is the punch-list for the day it is tested.

## Layering (binding for new code)

`home` / `lab` / `wk` / `field` / `stock`, with a one-way dependency rule — the
lab layer (targets, boot, image, the bench mechanics) knows nothing about
WebKit. Decided 2026-08-20 and recorded in `docs/HANDOFF-vocabulary.md`, "The
layers". Existing code catches up in `docs/HANDOFF-architecture-review.md`; new
code is expected to land on the right side of it. No CLI is minted until a layer
has a second consumer.

## Testing and documentation

- **`docs/TESTING.md` is the single authority for test items.** Every task gets
  a line item there as it is picked up, and nothing duplicates that list
  elsewhere. `[V]` is verified, `[ ]` is not, `[!]` is a known defect.
- **`wk selftest` encodes plan lines by phrase**, so rewording or deleting a
  line in `docs/TESTING.md` without touching `cmd/selftest` reports DRIFT, which
  is a failure. Change both.
- **Handoff docs carry a "Remaining, checked against the tree" block at the
  top.** When work lands, update that block rather than leaving the reader to
  infer state from the body. The body is the record of what was learned and why;
  the header is what is still owed.
- **Tools stranded by a workflow change are removed in the change that strands
  them**, not later.

## Refusals, secrets and privilege

- **No credential, key or token in the tree.** The repo is public and the
  internal addressing in it is accepted as published (user decision,
  2026-08-19); a credential crossing that line is a bug regardless. Machine-local
  values go in gitignored per-machine conf files because they are per-machine,
  not because they are secret.
- **`wk` never calls `sudo` on the workstation without a password prompt.** The
  privileged helper (`admin/wk-quiesce-priv`) is the one carve-out. Do not add
  NOPASSWD grants; the direction of travel is narrowing the ones that exist.
- **A refusal must say why and name the remedy**, and a barrier that can be
  crossed is crossed by an explicit `--force` that records itself. Never
  silently degrade — an unavailable profiler, an unpinnable CPU or a missing
  display is reported, not worked around.
- **Do not weaken the sandbox to make something work.** If a workspace cannot do
  something, that is usually the boundary working.

## Git

Never `git push --force` against a shared branch, and never commit unless asked.
