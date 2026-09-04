# Working on wk-tools itself

This file binds every change to **this repository**. Two other audiences have
their own files: `claude/CLAUDE.md` is deployed inside a workspace,
`claude/CLAUDE-host.md` to a host that drives `wk`. Neither repeats this one.

Where the design lives: README.md is the one document — architecture,
setup, and every supported workflow by example. Nothing else in the tree
describes the design, and where any file disagrees with it, the
disagreement is a bug to fix.

## The hazard

**Never edit wk-tools while a `wk` command is running** — here or on a machine
`t_sync_tools` copies the tree to. bash reads a script incrementally, so a
rewritten file resumes a running process mid-word. Check `wk status` first.

## The standard every change is held to

- **Anyone can use it without thinking.** Every command, flag and refusal is
  explained once, concisely, where the user meets it. A newcomer with the
  README, `wk help` and a bare machine gets to a build and a benchmark without
  guessing. A concept is defined before it is used, and has one name.
- **Nothing is ad-hoc.** A concern that applies to more than one command —
  resolving the workspace name, refusing a name inside a workspace, waiting for
  a workspace to be ready, parsing flags, printing usage, exit codes, logging —
  is implemented once, in the dispatcher or a library, and every command gets
  it. A command re-deciding one of these is a bug, even when it decides the same.
- **Every branch is testable and tested, or it does not exist.** An `if`, a
  `case` arm, a `--force`, a platform special case: each is reachable from
  `wk selftest`, or it is owed work named in a handoff.
  A branch nothing exercises is deleted, not kept in case.
- **No stored copy of a recomputable fact.** A cache is extra state that drifts
  and a second code path -- the hit and the miss -- that both need tests. A list
  of workspaces, a workspace's target, a machine's reachability, a probe's
  result: recomputed from evidence on every read. Records of a user's choice,
  locks, logs and measurements are not caches.
- **Measure before theorising.** A fault is fixed at its root after it has been
  measured; machinery built around an unmeasured fault is deleted.
- **No dead code, ever.** A function, file, package or dependency with no
  caller is removed in the change that strands it. Tombstones — a name the
  tooling still refuses, naming its replacement — are the only remnant allowed.
- **Every line of comment, help text and documentation earns its place.**
  A comment says what the code does and why it has to be that way; if the code
  already says it, the comment goes. Help text states what a flag does, not why
  the alternative was rejected. Duplicated prose is deleted, not synchronised.
  The ceiling is **15% of a file's non-blank lines**, and a file above it is
  trimmed, not defended. A `cmd/*` file's leading block is not a comment for
  this purpose -- it is what `wk <cmd> -h` prints (`explain_cmd`, the
  dispatcher) -- and it is held to the help-text rule instead: what a flag
  does, not why the alternative was rejected. What earns a line is what a reader cannot re-derive
  from the code in a minute: an external constraint, a measurement, a hardware
  or firmware fact, the one line naming why a construct has to be that way.
  What does not: a restatement of the code, the case against a rejected
  alternative, a narration of what a file used to do, a second copy of
  README.md.
- **A claim of work is backed by evidence.** Done means a test ran and passed;
  a status report derives every word from evidence taken at that moment.
  Nothing reports done, healthy or running from a record it did not just
  re-verify.
- **Structured data is not handled in bash.** JSON, tables, records and
  machine-readable tool output are read and written in Python
  (`python3` is present on every host and image). Bash orchestrates processes;
  it does not parse `sfdisk -J` with sed or build JSON with escapes.
- **Prefer the off-the-shelf component.** A tool that exists (a machine-readable
  output mode, a system service, a library on every host) is used before a
  reimplementation is written. A reimplementation states, in one line, the
  constraint that rules the standard thing out.

## State and lifecycle

1. **Smallest possible state, no caching of facts.** Every fact is recomputed
   from evidence at read time and lives in exactly one place per machine; a
   second copy is a bug even while it is still equal. Stores of *artifacts*
   keyed by content — ccache, base snapshots, seeded benchmark payloads,
   downloaded distro bases — are not caches of facts. The test: could a read
   recompute this value, or only re-download/rebuild it? A built system image is
   an artifact the workspace already names; it is not imported, catalogued or
   described by a manifest (`wk help images`).
2. **Crash-only, guaranteed final state.** Every mutating command can be killed
   at any point and re-run, and the re-run converges to the declared final
   state. "Already exists" is never the answer to a half-made thing.
3. **Wipe over repair.** Destroy-and-recreate is cheap and total; in-place
   repair is reserved for expensive stages whose evidence is unambiguous (a
   mirror fetch, the vm base build).
4. **One lock per mutated resource.** Concurrent runs serialize or refuse by
   name. A lock is not state: it dies with its holder.
5. **The machine wins.** When the record and the machine disagree — a by-hand
   `podman rm`, `tart delete`, a fetch into a published snapshot — the command
   reports it and believes the machine.
6. **Read-only is read-only.** A reporting command changes nothing, takes no
   lock, and is never blocked by the work it asks about.
7. **Prompts guard destructive actions only.** Routine paths never prompt; a
   destructive prompt defaults to No and declines without a terminal.

## Cattle, not pets

- Every machine is reproducible from this repo plus its declared restorables.
  Swapping hardware, swapping a drive, reinstalling, or losing a machine
  outright changes nothing about how the rest of the fleet works.
- **New machine-local state goes into `wk doctor`'s machine-local section, or it
  is a bug.** Each entry is `regenerable`, `re-authable` or `backed-up`.
- **New devices arrive as config, never code** — `boot/machines/<name>.conf`,
  `targets/hosts/<name>.conf`, `bridge/hosts/<name>.conf`, all in one shape.
  A `case` statement naming a machine is a bug.
- Hand-applied settings vanish on a rebuild. Anything missed belongs in
  `./setup` or `wk backup`, not in a person's memory.
- **No in-place upgrades.** A guest, golden base or image is fixed by changing
  the input that produces it and rebuilding, never by patching the running copy.
- **A node is reached by its tailnet name, and how to reach it is not written
  down.** No address, `.local` name, MAC or `ProxyJump` is stored, in
  `dotfiles/ssh/config` or in code. The two things that cannot hold a tailnet
  identity — moose's BMC (through its bridge phone) and Igalia's build boxes
  (through the company gateway) — are the only ssh-config entries. A board the
  tailnet cannot name is found by enumeration (`reach_enumerate`, lib/reach.sh),
  and what it finds is not written down. There is no mDNS.

## What a bench lane optimises for, in this order

1. **The quality of the result.** The measured system's storage and bus are part
   of the measurement; the bench system goes on the better medium.
2. **Durability and recoverability.** Every board keeps a rescue a failed bench
   system cannot take down, and a revert that does not depend on the bench
   system working. Firmware-enforced fall-through beats software; separate
   media beat separate partitions.
3. **The fewest unique configurations** — but never bought by violating 1 or 2.

So the boards have different hardware arrangements and the same code: one image
model (a base image is never measured), one write path, one arming interface
(`b_arm` / `b_disarm` / `b_self_disarm_sh`), one set of refusals. The per-board
difference is one function — how the running system is selected. `wk help
hardware` derives each board's arrangement.

## Layering

`home` / `lab` / `wk` / `field` / `stock`, one-way dependency: the lab layer
(targets, boot, image, bench mechanics) knows nothing about WebKit. `wk help
design` defines the layers. No CLI is minted until a layer has a second consumer.

## One path, not two

Every alternative is a second implementation of one behaviour: it fails
differently, it is exercised half as often, and here testing it means hardware
in hand. Two paths cost every combination of the two, forever. So:

- **No fallbacks.** "Use X, or Y if X is absent" is refused: the command
  requires X and says so. The path that only runs when the first one failed is
  the one nobody tested.
- **One implementation per behaviour** — one ssh wrapper, one detach, one
  wait-until, one logger, one card writer, one JSON reader. If the one is too
  slow or too narrow, fix it.
- **One implementation per rule.** A safety rule lives where the privilege is
  (the helper's gate) and callers ask it; a second copy can drift into
  permitting what the first refuses.

Where a second path is genuinely forced by a constraint, it is exercised by a
test or it refuses loudly; it never silently degrades.

## Testing and documentation

- **`tests/` is the test authority, and `wk selftest` runs it.** Real tests
  (stdlib `unittest`), one per behaviour: a dispatcher rule, a command's
  refusal, a lock, a status file, a workspace lifecycle against the real
  container target. A test creates what it needs and removes it in teardown;
  a test that needs a VM, a machine or a board skips by name when that is
  absent. There is no test plan document: a behaviour is tested or it is owed
  work in a handoff.
- **A handoff doc under `docs/` lists owed work and nothing else.** No design,
  no reasoning, no history, no "do not re-derive" — those belong in
  README.md or in the code that embodies them. A
  handoff shrinks as work lands and is deleted with its last item.
- **Write the present tense.** Nothing in this repo narrates its own past: not
  what a file used to do, what was removed, what an attempt got wrong, or when.
  State the rule, not the bug that motivated it. What is not yet true is a
  `TODO:` line naming what is owed. The one exception is a tombstone.

## Refusals, secrets and privilege

- **No credential, key or token in the tree.** The repo is public. Machine-local
  values live in gitignored per-machine conf files.
- **`wk` never calls `sudo` on the workstation without a password prompt.** The
  three privileged helpers — `admin/wk-quiesce-priv`, `admin/wk-card-priv`,
  `admin/wk-boot-priv` — are the carve-outs; do not add more. What earns one is shape: a fixed verb list,
  no passthrough, no argument that becomes part of a command, and a gate
  narrower than the capability sounds. The card helper touches only a usb or
  mmc **whole disk the machine is not running from**.
- **A refusal says why and names the remedy.** A barrier that can be crossed is
  crossed by an explicit `--force` that records itself. Nothing silently
  degrades: an unavailable profiler, an unpinnable CPU, a missing display is
  reported, not worked around.
- **Do not weaken the sandbox to make something work.** A workspace that cannot
  do something is usually the boundary working.

## Git

Never `git push --force` against a shared branch, and never commit unless asked.
