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
- **A node is reached by its tailnet name, and how to reach it is not written
  down.** Every node this repo owns is on the tailnet, always — so there is no
  address, no `.local` name, no MAC to look up in an ARP table and no
  `ProxyJump` to store, in `dotfiles/ssh/config` or in code. The name is the
  whole address, and Tailscale SSH makes the host key a non-question, which is
  the entire class of failure `wk status` hung on (2026-08-24: a jump hop
  asking about a bridge's key on the terminal, from a probe whose output was a
  file). Stored reachability is the same bug as cached state — it is a second
  copy of a fact, and it goes stale in the way that reads as broken hardware.

  Two things cannot hold a tailnet identity and are the only exceptions: moose's
  BMC (reached through its bridge phone) and Igalia's shared build boxes
  (through the company gateway). Both are jumps, and a jump host carries its own
  bounds, because ssh passes a jump child none of the options on its command
  line.

  Open violations, and they are the reason the machinery above still exists:
  rpi3 (an mDNS name) and rpi4 (a bridge-segment address behind a `ProxyJump`)
  are the only fleet machines not on the tailnet, and the bench images
  deliberately carry no tailscale (`image/profiles.sh`: "the image has no
  tailscale and never will"). docs/TESTING.md, section 7, carries the item.

`docs/HANDOFF-reprovision.md` is the punch-list for the day this is tested by
an actual rebuild.

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
- **A handoff doc under `docs/` states what is still owed, and nothing else.**
  It is a work list, not a record: when the work lands, the doc shrinks, and
  when the last item lands the doc is deleted. What was learned on the way
  belongs in the code that embodies it, in `docs/TESTING.md`, or in
  `docs/help/`; git history holds the rest.
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
