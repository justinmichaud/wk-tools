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

## The hazard

**Never edit wk-tools while a `wk` command is running.** bash reads a script
incrementally, so rewriting `cmd/build` under a running build resumes the
process mid-word and drops it back into the lock wait. It applies with more
force to the copy on a build machine, which `t_sync_tools` replaces at the start
of every build. Nothing in the tooling prevents this today — check `wk status`
first.

## State and lifecycle

1. **Smallest possible state, no caching of facts.** Every fact is recomputed
   from evidence at read time and lives in exactly one place per machine; a
   second copy is a bug even while it is still equal. Stores of *artifacts*
   keyed by content — ccache, base snapshots, seeded benchmark payloads,
   downloaded distro bases — are not caches of facts. The test: could a read
   recompute this value, or only re-download/rebuild it?

   **A built system image is not one of them.** A workspace
   produces an image; wk detects that it did and writes it to a card. It is not
   imported, stored, catalogued or otherwise treated specially, and no manifest
   restates what it is — the store bought a second name for something the
   workspace already names, and charged a copy, an import that can half-finish,
   and a compressed duplicate to re-derive on every edit. `wk help images` has
   the model, including what identity means without a catalogue and why every
   edit belongs on the card rather than on the image.
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
  whole address, and Tailscale SSH makes the host key a non-question -- which
  closes the class of failure where a jump hop asks about a bridge's key on the
  terminal, from a probe whose output is a file. Stored reachability is the same bug as cached state — it is a second
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

## What a bench lane optimises for, in this order (binding)

1. **The quality of the result.** The measured system's storage and bus are part
   of the measurement. Where a board's media differ in a way that touches a run,
   the bench system goes on the better one and the rest of the design bends
   around that.
2. **Durability and recoverability.** Every board keeps a rescue a failed bench
   system cannot take down, and a revert that does not depend on the bench
   system working. Firmware-enforced fall-through beats a software one; separate
   media beat separate partitions.
3. **The fewest unique configurations**, so that a change is not three device
   tests. Third, and it may not buy uniformity by moving a bench root onto a
   worse bus (1) or by putting a rescue in the same failure domain as the thing
   it rescues (2).

The consequence reads like a contradiction and is not: the three Pis end up with
**different hardware arrangements and the same code**. Each arrangement is forced
by 1 and 2 — `wk help hardware` shows the derivation per board. What 3 governs is
everything above the medium: one image model (a base image is never measured),
one write path, one arming interface (`b_arm` / `b_disarm` / `b_self_disarm_sh`),
one set of refusals. The per-board difference is one function — how the running
system is selected — and everything above it is exercised by whichever board is
in the room.

## Layering (binding for new code)

`home` / `lab` / `wk` / `field` / `stock`, with a one-way dependency rule — the
lab layer (targets, boot, image, the bench mechanics) knows nothing about
WebKit. `docs/HANDOFF-vocabulary.md`, "The layers", has the reasoning. Existing
code catches up in `docs/HANDOFF-architecture-review.md`; new
code is expected to land on the right side of it. No CLI is minted until a layer
has a second consumer.

## One path, not two (binding)

**Do not create an explosion in the possible paths that need testing.** Every
alternative is a second implementation of one behaviour: it fails differently,
it is exercised half as often, and in this repo testing it means hardware in
hand — a card in a reader, a board that has to be power-cycled by a person. Two
paths do not cost twice; they cost every combination of the two, forever.

What this rules out, with the examples that produced the rule:

- **Fallbacks.** "Use the privileged helper, or inline `sudo` if it is absent"
  was written and removed the same day. The fallback is the path that runs on
  the machine nobody tested, on the day something is already wrong.
- **Two ways to do one thing.** There were two card writers, bmaptool and dd,
  chosen by what the machine had installed. The fast one existed to consume
  store artifacts; when the store went, it went with it rather than being kept
  working. If the remaining path is too slow, make *it* faster.
- **Two implementations of one rule.** "May this disk be written" lived both in
  `disk_refuse_unless_safe` and in the helper's gate. Two copies of a safety
  rule is one that can drift into permitting what the other refuses — and the
  copy that matters is the one holding the privilege. The caller asks it now.

When a second path is genuinely unavoidable, it is not enough to write it: it
has to be exercised by something, or it must refuse rather than silently
degrade. A path that only runs when the first one fails, and is never tested, is
not a safety net. It is a second bug waiting for the worst moment.

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
- **Write the present tense. Nothing in this repo narrates its own past.**
  A comment says what the code does and why it has to be that way. It does not
  say what it used to do, what was removed, what a previous attempt got wrong,
  or on what date any of that happened. A reader who has never seen the old
  shape must not have to carry it to understand the new one, and a heading like
  "why there is no X" is unreadable to someone who never knew there was an X.
  Git history holds the story; the tree holds the result.

  So: state the rule, not the bug that motivated it. "This is read back because
  a write that did not land is invisible from here" carries everything a dated
  war story does, and stays true when nobody remembers the day. Anything not
  yet true is a `TODO:` line naming what is owed, not prose
  about what was tried. Two exceptions, both deliberate: `docs/TESTING.md`,
  which is a work list whose whole content is what is and is not yet verified,
  and a **tombstone** -- a name the tooling still refuses by name, which has to
  say what to use instead and nothing more.

## Refusals, secrets and privilege

- **No credential, key or token in the tree.** The repo is public and the
  internal addressing in it is accepted as published; a credential crossing that
  line is a bug regardless. Machine-local
  values go in gitignored per-machine conf files because they are per-machine,
  not because they are secret.
- **`wk` never calls `sudo` on the workstation without a password prompt.** Two
  privileged helpers are the carve-outs — `admin/wk-quiesce-priv` and
  `admin/wk-card-priv`, because writing a card is `dd` plus mounts on
  a machine reached by a BatchMode ssh that has no terminal to prompt on. Do not
  add more; the direction of travel is narrowing the ones that exist. What earns
  one is not need but *shape*: a fixed verb list, no passthrough, no argument
  that becomes part of a command, and a gate narrower than the capability
  sounds. The card helper may only touch a usb or mmc **whole disk the machine
  is not running from** — the boot check is the load-bearing half, since every
  board here boots from exactly the kind of device the transport check allows.
- **A refusal must say why and name the remedy**, and a barrier that can be
  crossed is crossed by an explicit `--force` that records itself. Never
  silently degrade — an unavailable profiler, an unpinnable CPU or a missing
  display is reported, not worked around.
- **Do not weaken the sandbox to make something work.** If a workspace cannot do
  something, that is usually the boundary working.

## Git

Never `git push --force` against a shared branch, and never commit unless asked.
