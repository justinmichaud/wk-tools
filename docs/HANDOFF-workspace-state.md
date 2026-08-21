# HANDOFF — workspace lifecycle state, readiness gating, and a status command that cannot lie

Written 2026-08-19; the plan below landed the same day. Grew out of three
features — `wk new --zed`, `wk build --babysit`, `wk build --branch` — and the
question they all forced: *when is a workspace ready to be acted on, and who
is allowed to say so?* The honest answer then was "nobody, reliably": zed
opened on whatever existed, a build started on whatever existed, and an ssh
cut in the middle of `wk new` left a half-workspace that every command treated
as whole. The rules at the top are why this file stays: other docs defer to
them.

---

## The rules — read these first; nothing may contradict them

Set by the user, 2026-08-19. Every doc, comment and command in this repo
defers to these. Where an older statement disagrees, these win and the
statement is a bug to fix, not a second opinion.

1. **Smallest possible state, no caching of facts.** Every fact is
   recomputed from evidence at read time, and lives in exactly one place
   per machine; a second copy is a bug even while it is still equal.
   (Stores of *artifacts* keyed by content — ccache, base snapshots, seeded
   benchmark payloads — are not caches of facts and are fine. The test:
   could a read recompute this value, or only re-download/rebuild it?)
2. **Crash-only, guaranteed final state.** Every mutating command can be
   killed at any point and re-run, and the re-run converges to the declared
   final state. "Already exists" is never the answer to a half-made thing.
3. **Wipe over repair.** It is better to delete a workspace or re-provision
   a box entirely from scratch than to find — or patch around — an
   unexpected state. Resume-in-place is reserved for expensive stages whose
   evidence is unambiguous (a mirror fetch, the vm base build); anything
   half-made or unrecognized is destroyed and remade, and the engineering
   effort goes into making destroy-and-recreate cheap and *total*, never
   into clever in-place repair. This is [[no-in-place-upgrades]] applied to
   workspaces and targets.
4. **One lock per mutated resource.** Concurrent runs and builds serialize
   or refuse by name. A lock is not state: it dies with its holder.
5. **Detect un-managed clobbering.** When the record and the machine
   disagree (a by-hand `podman rm`, `tart delete`, a fetch into a
   snapshot), status/doctor say "the record says X, the machine says Y" and
   name the repair — they never trust either side silently.
6. **Read-only commands are read-only absolutely.** `status`, `ls`,
   `logs`, `doctor` never start a machine, never write a file, never repair.

`docs/TESTING.md` §6 is the executable form of these rules; its per-command
interruption matrix is the acceptance test for rule 2.

## The core requirement (read this before the plan)

**This is the requirement the rest of the document serves, and it is not
negotiable down to an implementation detail: `wk status` must be reliable and
clear under every failure the system can actually have.** Everything else here
— markers, staged creation, detached drivers — exists so that this command can
keep that promise. If a proposed mechanism makes status less trustworthy, the
mechanism is wrong, not the requirement.

Concretely, `wk status` (and everything that gates on the same state) must
stay correct, or degrade *visibly and honestly*, across:

- **Updates to this repo.** A status file written by last month's wk-tools is
  read by today's. So: unknown keys are ignored, missing keys mean "unknown"
  rather than a parse error, and no reader ever assumes a file's schema is the
  one it would write itself. A reader that cannot make sense of a file says
  so, names the file, and continues with the rest of the report — one strange
  workspace must not take down the whole listing.
- **OS updates and reinstalls.** Nothing load-bearing lives only in a process
  table, an environment variable, or a tmpfs. After a reboot or a reinstall,
  state is recomputed from what is on disk next to the artifacts. (This is the
  same rule [[no-in-place-upgrades]] already applies to machines: the record
  must survive the runtime.)
- **Network drops and ssh cuts.** A target that cannot be reached is reported
  as *unreachable, with the timeout that decided it* — never a hang, never
  `absent`, never a stale answer presented as current. Every probe is
  bounded (`ConnectTimeout` is already house style; it becomes a requirement).
  And an operation whose *driver* died with an ssh session must be
  distinguishable from one that is running: liveness is the process table plus
  the evidence's freshness, never the status file's own claim.
- **Random device corruption.** A truncated, half-written, or garbage status
  file is an expected input, not an exception: writes are atomic
  (tmp + `mv` on the same filesystem), reads are tolerant, and — the real
  defense — **a status file is a claim, never the record.** The record is
  evidence next to the artifact (a marker written as creation's last act, a
  git object store that either passes `rev-parse` or does not, a log whose
  mtime is its heartbeat). When cache and evidence disagree, evidence wins and
  status says the file was stale.
- **Clock trouble.** Timestamps are absolute UTC and used for display and
  staleness heuristics only; no correctness decision hinges on two machines
  agreeing what time it is.

- **Read-only, absolutely.** `wk status` makes no changes: it never starts
  the podman machine, never boots a guest, never writes a status file, never
  repairs anything — it only checks, and reports what it could not check.
  The same guarantee applies to `wk ls`, `wk logs` and `wk doctor`.
  **Done 2026-08-19**, with one honest correction: the observed
  "`wk status` starts podman" could not be reproduced on the tree as it then
  stood — every read-only command was measured, with the machine stopped and
  with a container, vm and remote workspace in the picture, and none of them
  started it. What was real is that the guarantee lived at the *call sites*,
  which is where a later path walks past it. It now lives inside
  `forward_to_vm`, the only thing in the tree that can start the machine: a
  read-only command reaching it is answered and refused rather than served by
  a boot, which also closes the window between a caller's own
  `machine_running` check and the forward. `wk selftest --section state` runs
  all four with the machine stopped and asserts it is still stopped, that no
  guest appeared, and that nothing under the host state directory was
  written.

The contract in one line: **`wk status` never hangs, never crashes, never
changes anything, and never asserts something it did not just verify; in the
worst case it says "unknown", why, and the one command that repairs it.**
Exit codes stay machine-readable: the 0/1/2/3 contract extended, and a
workspace that needs a person exits 4 (`cmd/status`), so scripts and agents
can branch.

---

## What exists today (landed 2026-08-19)

- **`wk build --babysit[=model]`** — a detached loop (`build/babysit.sh`,
  nohup, no tty, survives ssh cuts) that rebuilds after every fix a sandboxed
  Claude makes: build → on failure, headless `wk claude -p` (default `haiku`,
  `WK_BABYSIT_MODEL`/`--babysit=<m>` override) with the classified errors and
  log tail in the prompt → rebuild, up to `WK_BABYSIT_ATTEMPTS` (default 5).
  Writes `babysit.status` / `babysit.report` / `babysit.log` in the workspace
  state dir; `wk status` renders them with pid-liveness (a "fixing" claim with
  a dead pid is reported as a crash, not believed). Refuses remote targets
  with the same words as `wk claude` (no sandbox on a shared machine) and
  refuses from inside a workspace. A second `--babysit` while one is alive is
  refused by pid. **Not yet run through a real fix cycle** — that E2E (plant a
  build error, watch it get fixed) is still open in `docs/TESTING.md`
  ("Babysit").
- **`wk claude` headless** — no tty on stdin/stdout → `t_exec` instead of
  `t_exec_tty`, same sandbox verification either way. This is why the
  babysitter goes through `wk claude` rather than running claude itself.
- **`wk build --branch <b>`** — checkout before build, fetch only when the
  name is not already local; in the babysit path the checkout happens once,
  up front, never under a fix the model just made.
- **`wk new --zed` / `wk enter --zed`** — via `zed_cli()` (PATH entry or the
  app bundle's own cli). It opened as soon as creation returned — exactly the
  gap this handoff was written to close, and `wait_ready` (phase 2) closed it.
- The prior art the state model generalizes: `build.status` (whose `running`
  is already treated as a hint, with log mtime deciding liveness),
  `.wk-firstrun-complete` (a completion marker written as provisioning's last
  act, waited on by the container driver's `t_ready`), and `remote/provision.sh`
  (idempotent stages that check evidence before acting).

## The gaps — all closed 2026-08-19

0. **`wk ls` and `wk status` listed different sets.** Both now enumerate
   through `walk_targets` (which targets) and `target_workspaces` (which
   workspaces on one), and `wk ls` grew a TARGET column, because a bare name
   says nothing once more than one machine is in the listing. On macOS the
   dispatcher assembles one listing from two machines for both commands
   (`bare_report`): the host-side targets are walked out here in one pass, the
   container half is forwarded and told it is a continuation, so there is one
   header and no false "no workspaces" note. The name sets are checked by
   `wk selftest --section state`.
1. **Readiness was only half a concept.** Every driver writes `.wk-ready`
   last, `t_info` reports `creating` while it is missing, and one gate
   (`wait_ready`) is what zed and build go through.
2. **Remote creation was one long synchronous ssh.** Creation is detached on
   the driving machine, the far-side marker is written last, and a cut
   mid-clone leaves a workspace that reads `creating` from either end — which
   `wk new` converges by remaking.
3. **Creation state did not exist at all.** Now `absent | creating | present |
   broken | unreachable`, derived per call, with the driver's liveness read
   from `ws.status` and nothing else taken from it.

## The plan — landed in two phases, both 2026-08-19

Order was chosen so every step shipped alone and the fragile target (remote)
was fixed first: phase 1 (macOS host, verified against the podman VM) was
convergence-by-remaking on the near side, the snapshot completion marker, the
locks and the read-only guarantee from the audit further down; phase 2 was
everything evidence-shaped — the marker, the state, the gate, the detach
primitive, the lifecycle line. What that means in the tree:

- `.wk-ready`, one name (`WK_READY_MARKER`, lib/target.sh), written last by
  every driver. The container's is written from *inside*, by
  `container/firstrun.sh`, because that hook genuinely is the last act of
  creating one -- the driver's part ended when `wkdev-create` returned, minutes
  before the workspace was usable. The remote's is written on the far side, so
  the box itself and any other workstation derive the same answer.
- `t_info` answers the lifecycle rather than the environment: `absent`,
  `creating`, or the driver's word for something that exists. The remote driver
  answers all of it in **one** ssh round trip, because `wk status` asks per
  workspace.
- `wait_ready` in lib/target.sh, and its consumers: `wk build` (bounded,
  `WK_BUILD_READY_WAIT`, before anything is written -- `--babysit` inherits it
  by re-running `wk build`) and `wk enter --zed` / `wk new --zed`, whose waiter
  stays **foreground** deliberately: if ssh dies, only the waiter dies,
  creation continues detached, and re-running the command is the resume -- a
  detached GUI-opener would open windows into a session that no longer exists.
  Four more consumers than the plan listed -- `wk run`, `wk test`, `wk claude`,
  `wk gui` -- because
  they all had the same line (`t_info != absent`, "no such workspace") and it
  said that about four different situations, one of which was launching an
  agent into a workspace whose provisioning had not finished writing its
  CLAUDE.md or its keys. `wk test --dry-run` deliberately does not wait: a dry
  run resolves and prints, and reports the state it saw.
- `lib/detach.sh`: `detach_run`, `detach_wait`, `detach_alive`, `status_write`,
  `status_field`. Extracted from the babysitter, whose details were the ones
  worth keeping (nohup, stdin closed, both streams to the log, pid for
  liveness). `wk new` is its second user: creation is detached by default and
  the command waits in the foreground, `--no-wait` returns immediately.
- `wk status` renders the lifecycle line first, with the stage and the driver's
  liveness, and exits 4 when a workspace needs a person.

Six decisions that differ from the plan as it was written, all of them made
while implementing it:

1. **Two more states than the one step 2 asked for: `broken` and
   `unreachable`.** `broken` is creation finished plus an environment that is
   gone -- a by-hand `podman rm`, `tart delete`, `rm -rf` over there -- which
   is rule 5's case and needs a different answer (`wk rm`, because the overlay
   layer may still hold work) from a creation that never finished (`wk new`,
   which wipes). `unreachable` is the core requirement's: a machine that did
   not answer is never reported as `absent`, and the timeout that decided it is
   named. The remote driver's capacity probe therefore grew a soft form
   (`_remote_probe_try`) so a reporting path can survive what a build cannot.
2. **`broken` is the one place a record is read** to decide state. Everything
   else in `ws_state` is evidence next to the artifact, but a record-vs-machine
   disagreement cannot be detected without the record, so the marker *or* a
   `ws.status` that says `present` is what separates it from rubble.
3. **`ws.status` and `create.log` live beside the workspace directory**
   (`$WK_STORE/create/<name>.{status,log}`), not in it. They cannot be in it:
   a re-run of `wk new` over a half-made workspace destroys that directory as
   its first act, and the log the driver is writing to must not be the file it
   deletes -- the fd survives, pointing at nothing, and the words explaining
   what happened are lost exactly when they are wanted. `wk rm` removes them.
4. **The vm marker is host-side.** Creating a vm workspace clones a guest that
   is not running, so there is nothing inside it to write to, and a guest is
   visible from this host and nowhere else -- there is no second machine that
   would need to read it. Everywhere else the marker is next to the checkout.
5. **The container's old `.wk-firstrun-complete` counts as the same
   evidence.** It means exactly what `.wk-ready` means, and every container
   workspace made before today has it; read as absent they would all have been
   reported half-made and offered for destruction. New ones write only the new
   name, and the clause can go when no pre-marker workspace is left.
6. **A workspace that never finished initialising is a failed creation, not a
   warning.** `wk new` used to say "it is usable, but the push keys and the
   Claude CLI may be missing"; under rule 3 that is rubble, so the driver dies,
   the status file says `failed`, and a re-run remakes it from scratch.

The bugs that came out of it, every one found by running it rather than by
reading it:

- **A waiter must not believe the previous attempt's status file.** Re-running
  `wk new` over half-made rubble read the killed driver's `state=creating` and
  dead pid from the file the *last* run left, and reported the run it had just
  started as crashed -- while the new driver went on to finish the workspace
  correctly. `detach_run` now clears a status file no live process owns, and
  `detach_wait` trusts the pid it forked over the one in the file. A stale
  `state=failed` would have done the same thing faster.
- **A listing must not ask an unreachable machine anything.** `t_branch` and
  `t_has_wk` both resolved a remote path, which reaches for the capacity probe,
  which dies -- so `wk status` against a machine that was off printed two
  connection errors before the line that said `unreachable` perfectly clearly.
  `t_branch` now answers `-` for any state that cannot be asked, and
  `t_has_wk` checks reachability before resolving anything.
- **"no such workspace" was the answer to four different situations**, one of
  them a workspace whose container somebody had removed by hand. That is what
  the extra `wait_ready` consumers above fix.
- **The readiness refusal was absolute, and its advice was unrunnable where it
  printed.** Reported from `wk claude db --force` in a shell *on*
  devbox-arm64-2: the workspace's clone was complete and only its marker was
  missing, `--force` could not get past a `die`, and the "remake it: wk new …"
  it named is a command that machine refuses (workspaces are created from the
  workstation). It is a `barrier` now -- the distinction lib/common.sh draws:
  the command *can* proceed, and a clone that finished one step before its
  marker looks exactly like one cut in the middle, so the person looking at it
  gets to decide. `ws_remake_hint` prints "from the workstation: wk new …
  --target <t>" on a machine that only hosts workspaces.
- **A workspace with no registry entry was unreachable by name.** Reported
  from `wk pr db …`: every command resolved the target as
  `target_of || default_target`, so a `wk new` that died before registering
  left a complete checkout on a build box while every command asked podman and
  said "no such workspace: db". Two halves to the fix, and both are the audit's
  own prescription that the target view is calculated rather than carried:
  `ws_target` (lib/target.sh) now falls back to whichever target's own store on
  this machine holds that name -- a file test per configured target, no ssh,
  nothing started -- and every command plus the macOS dispatcher goes through
  it, so they cannot reach three different machines for one name. And `wk new`
  registers the target *before* it creates anything: the record is what lets a
  later command find a half-made workspace, which is the mirror image of
  `wk rm`, where the record is what outlives the artifacts.

Still open from this pass:

- ~~`wk build --detach`'s own nohup~~ **done 2026-08-19**: both remaining
  hand-rolled nohups (`--detach`'s local fallback and `--babysit`) go through
  `detach_run`. One thing came out of doing it that the primitive did not have:
  `--detach` passes an *empty* status file, because a build's status file
  carries no pid deliberately -- a build can be driven from either end of an
  ssh and a pid written by one machine is not a fact on the other, so liveness
  there is the age of the build log (cmd/status). `detach_alive` can therefore
  never answer yes about it, and the "remove last run's file" step would have
  deleted the record of a build that was running. `detach_run "" <log>` means
  "the child's record is not mine to touch".
- A creation's bookkeeping can outlive everything it describes and then be
  invisible: `wk rm` removes it, and a `state=present` file is what tells a
  hand-emptied far side apart from rubble, but a `$WK_STORE/create/<n>.*` pair
  whose workspace, environment and registry entry are all gone is listed by
  nothing and pruned by nothing. `wk gc` is where that belongs.
- ~~The `--zed` gate is exercised through `wk build`~~ **done 2026-08-19**,
  and it found the thing a `wk build` proxy could not. The gate itself is
  right: `wk new zedgate --no-wait` then `wk enter zedgate --zed` printed
  "waiting for 'zedgate' to finish being created (at: init)", opened nothing,
  and released on `present`. What is *behind* the gate does not work for a
  container workspace on a macOS host, for two reasons that have nothing to do
  with readiness: the command is forwarded into the podman VM, so it looked for
  `/Applications/Zed.app` in a Linux VM and reported "zed is not installed"
  about a Mac that has it -- and the generated `wk-<name>` alias is written by
  whichever side ran `wk new`, which for a container on macOS is the VM, so
  even a Zed launched on the host would have nothing to resolve and no route
  into the VM's container network. Refused on the host now, naming a macOS
  guest or a remote target. Whether a container in the podman VM *should* be
  reachable from the host (a generated alias with a ProxyJump through the
  podman machine) is a real question and is not answered here.
- The remote and vm markers are code-verified and name-checked by selftest, but
  no remote or guest workspace was created end to end in this pass -- the
  container was, including a driver killed mid-provisioning and remade.
- **A delegated `wk status` is answered by the far machine's own wk-tools**, so
  a build box still running an older copy answers by the older rules: measured
  2026-08-19 on devbox-arm64-2, a workspace whose creation had died was
  reported `present` from over there and `creating` by this machine's code. The
  fleet block already prints "wk-tools@<machine> ... DIFFERS from the
  workstation -- push it there", which is the honest half; what is missing is
  that the drift changes *answers* and not just versions. A `wk build` (or
  `wk remote setup`) to that machine rsyncs the tooling and the answers agree
  again.

Follow-up, explicitly out of this pass: remote *builds* still hold an ssh
open end-to-end (the trap: ssh without a pty carries no signal, so ctrl-c —
and the watchdog's stall abort — ends the local half while the compiler keeps
going at the far end, and the build lock makes the next `wk build` wait behind
it, which reads as a hang; the fix is a remote-side pid file and a
`wk build --abort`, not a shorter lock timeout). Moving the build
itself under the machine-side `wk` that `wk remote setup` installs closes
that last ssh-cut hole. The build *state* half is already the machine's — the
canonical `build.{status,log}` lives beside the checkout and both ends agree
on it — but the container half of the "build state recorded twice" leftover
is still open (moved here 2026-08-21 from the removed wk-in-workspace
handoff): a container workspace writes `build.status` and `build.log` into
its own `~/.local/state/wk/ws/<name>/`, because the host's store is not
mounted in — so `wk status <ws>` on the host says `build=none` while
`wk status` inside the same workspace says `build=ok`. The obvious fix —
bind-mounting the host's `$ws` in — is wrong as stated: `$ws` also holds
`changes/` and `overlay-work/`, and a second write path into the upperdir of
a live overlay is exactly what `lib/store.sh` warns is undefined. A dedicated
`$ws/state:/wk-ws` mount would work, but the host reads `$ws/build.status`
directly through `wk_ws_dir`, so it means moving those files for every
target. Worth doing deliberately, not as a side effect of another change —
the one-copy-per-machine rule at the top of this file governs.

Two smaller leftovers from the same (removed) handoff:

- **`wk` auto-starts the podman machine for any mutating container-target
  command even when a macOS VM is running** — on a 32 GB host the two cannot
  coexist; it should report and let the user choose. (Read-only commands no
  longer boot it: that guarantee lives in `forward_to_vm`.)
- **`wk ls` inside a workspace** prints `?` for BASE and `-` for CHANGES.
  Honest — both are overlay concepts that do not exist for this target — but
  it reads as missing data rather than as not-applicable.

## The status-file schema (shared by ws/build/test/babysit)

Plain `key=value`, one file per concern, single writer, atomic tmp+`mv`,
absolute UTC timestamps, unknown keys ignored by every reader:

    state=…            # a hint; evidence decides
    pid=…              # the driving process, for liveness — this host only
    log=… report=…     # where the words went
    updated=…          # staleness display, never correctness
    (+ concern-specific: config, model, attempt/max, branch, stage)

Written and read through `status_write` / `status_field` / `detach_alive` in
`lib/detach.sh` as of 2026-08-19 — one atomic writer and one tolerant reader,
so "a file written by last month's wk-tools still renders" is a property of two
functions rather than of every caller. The single-writer rule is enforced by
the lock: whoever holds the resource's lock writes its status file, which is
why `wk new`'s waiting half writes nothing at all. `ws.status` sits beside the
workspace directory rather than inside it (decision 3 under "The plan").

## Branch semantics

`wk new --branch <b>` clones directly onto `<b>` and records it as the
workspace default; `wk build --branch <b>` overrides per build (landed). A
bare `wk build` never touches the checkout — the user's working tree is
sacred.

## Verification

All in `docs/TESTING.md` as of 2026-08-19 — the "Babysit" subsection of §1,
and §6 "State, concurrency and clobbering" (the per-command interruption
matrix, the status-files-are-claims checks, the lock checks, and the
clobber-detection checks). TESTING.md is the single authority for test
items; nothing is listed here so the two cannot drift.

## State audit — 2026-08-19

A full pass over every command, driver and provisioning script, against
**The rules** at the top of this file — that pass and the user's direction
are where the rules came from, and everything below is findings, not a
second statement of them.

### Caches to eliminate (each is a fact recomputable at read time)

- **Container ssh aliases** (`cmd/new` writing `HostName localhost`):
  fictional — containers have no sshd. Delete outright.
- ~~**Deploy-key copies** in workspace `~/.ssh/id_*`~~ **Done** —
  `container/firstrun.sh` symlinks into the read-only `/secrets`, exactly as
  the Claude credentials already were, so a rotated key rotates everywhere
  (verified in `docs/TESTING.md`).
- ~~**`WK_FORKS` frozen into container env** at creation~~ **Done** —
  `wk_push_forks` (lib/store.sh) is read from the mounted `/opt/wk-tools` at
  use time; nothing bakes it any more.
- **Guest proxy address baked into `~/.zprofile`** by `vm/provision-base.sh`
  behind a comment-matching guard: the host already recomputes the address
  per boot for the *system* proxy; rewrite the env block per start too, or
  point the profile at a file the host refreshes. Today a changed bridge
  address hangs `git`/`pip`/`curl` in every clone, indistinguishable from
  Softnet denying traffic.
- **Machine-side conf duplicates** (`remote/provision.sh` baking
  `WK_REMOTE_ROOT` and `WK_REMOTE_REFERENCE` — `WK_REMOTE_MAX_JOBS` has since
  been removed altogether): both are probed per process anyway, and the baked
  `WK_REMOTE_REFERENCE` skips the `rev-parse` verification that exists
  precisely because the value goes stale. Keep only `WK_REMOTE_LOCAL=1`… and
  see "shared homes" below, which removes the machine-side marker entirely.
- **Near-side remote `build.status`/`build.log`**: largely closed — the
  canonical copy now lives beside the checkout on the build machine and
  `wk status`/`wk logs` ask the machine (`t_has_wk` / `t_wk`).
  What remains is that the fallback read against a machine with no `wk`
  should be labelled stale with its timestamp, never presented as live.
- **`base/<id>/sha` + `branch`**: pure caches of the tree today, read by
  nobody. Repurpose: written last, they become the snapshot's completion
  marker (closing the interrupted-`wk sync` hole — `current_base` is
  `ls | tail -1` and will pin rubble) *and* the tamper evidence (a by-hand
  fetch into a snapshot makes `rev-parse HEAD` disagree with the record).
- **Three copies of `arch`** (ws dir, container env, workspace marker): one
  authority per side — the ws file on the host, the marker in the guest —
  and nothing else.
- **`~/.wk-provisioned`** in the guest base: written, never read. Delete.
- **`pi-hosts`**: append-only cache of tailnet addresses with no pruning and
  a second, divergent copy in `dotfiles/ssh/config`. Rewrite per setup run;
  one authority.
- **Quiesce/session `off` values**: `off` currently *invents* restore values
  (schedutil, ASLR 2…) instead of restoring what was there. Record the real
  prior values at `on` time under `/run` (state that correctly dies with the
  boot) so `off` is an inverse, not a guess.
- **ccache max_size**: four writers (env default, store conf, gc, remote
  conf) for one number. One derivation, applied idempotently.

### Worst desync risks found (fix order)

1. ~~**`wk sync` has no completion marker on a snapshot**~~ **Fixed
   2026-08-19.** `sha` is written last and atomically, and is the publication
   gate: `current_base` returns the newest *marked* snapshot, `wk new`
   refuses an unmarked one by name, `wk gc` prunes it, and a snapshot whose
   tree no longer matches its recorded sha is refused by name as well
   (`base_verify`, `lib/store.sh`).
2. ~~**`wk new` re-run after a failed create re-pins `base-id`**~~ **Fixed
   2026-08-19.** The container driver writes `base-id` as its last act, so
   its absence is the evidence that creation never finished (`ws_state`,
   `lib/target.sh`, reported as `creating` by both listings); a re-run
   destroys the rubble and remakes from scratch rather than re-pinning
   anything over a surviving layer. `wk gc` prunes no snapshot at all while
   any workspace is unpinned, since that is a reference it cannot count.
3. ~~**firstrun failure has no clean recovery path**~~ **Fixed 2026-08-19**
   by the readiness marker plus decision 6 above: a failed firstrun is a
   failed creation — the driver dies, the status file says `failed`, the
   workspace reads `creating` (its `.wk-ready` was never written), and a
   re-run of `wk new` remakes it from scratch.
4. ~~**`wk rm` returns success on partial destroy**~~ **Fixed 2026-08-19.**
   Artifacts first, then a check of what is actually gone, and the registry
   entry last — so a partial destroy exits nonzero, names what is left and
   keeps the record that can still resolve it, and a re-run finishes the job.
   A workspace with nothing left but a registry entry is forgotten rather
   than refused. `unreferenced_bases` no longer answers at all while a
   workspace is unpinned, so gc cannot delete a live overlay's lower layer.
5. ~~**Unreachable ≠ absent**~~ **Fixed 2026-08-19** — `unreachable` is a
   state of its own with the timeout that decided it named, and `t_has_wk`
   checks reachability before resolving anything (phase 2, decision 1 and the
   listing bug above).
6. **`cmd/bench` reads `state=running` as gospel** while `cmd/status` has
   the mtime liveness heuristic — one shared reader for status files, with
   the evidence check built in.
7. Verified small bugs. **Fixed 2026-08-19:** `WK_TARGET=vm wk gc` died
   (`cmd/gc` sourced a driver without `lib/target.sh`; it goes through
   `load_target` now, and the snapshot and mirror work is skipped on a target
   that has neither); `wk selftest --section <typo>` exited 0 having run
   nothing (the section list is validated before anything runs). Still
   open: `wk vm rm` leaves `<name>.unfiltered` behind (false refusal
   on a recreated guest); `wk quiesce`'s state dir is `$TMPDIR`, which
   differs between a terminal and ssh, leaking caffeinate and stranding
   SIGSTOPped daemons; `is_headless` reads `/var/lib/wk/.headless` while the
   Linux cleanup checks `$WK_STORE/.headless`.

### Locks (rule 4, the design)

**Landed 2026-08-19, with the mechanism changed twice by measurement.** One
helper in `lib/common.sh` — `hold_lock <resource> [-w timeout]` for the life
of a command, `with_lock <resource> -- cmd…` for a smaller critical section —
and one implementation on both hosts.

The plan said `flock` where it exists. flock was implemented first and is
*wrong here*, for a reason worth keeping: a flock is held by the open file
description, so every process that inherits the descriptor holds it — and
`wk new` starts a container, after which podman's `conmon` supervises it
holding the inherited lock fd for as long as the workspace exists. Measured:
after one `wk new`, conmon held `ws-<name>.lock` and the next command on that
workspace waited on it forever. bash cannot mark a redirection close-on-exec,
and `flock --close` applies only to flock's own `-c` command form, which
would mean re-exec'ing every command under it.

An atomic mkdir with the holder's pid inside came next, and had a hole of its
own, found 2026-08-20 by `wk rm` sitting out its full timeout: a lock
directory whose pid file was never written is indistinguishable from a live
holder, so it can never be reclaimed. **The mechanism is now a symlink whose
target string names the holder** (`lib/common.sh`, "locks"): `ln -s` is
atomic *and* carries the payload with it, so the window does not exist rather
than being made small — and there is still no descriptor to inherit. Writing
the contention test found two more defects the mkdir form shared: several
takers breaking one dead lock could all conclude they had won (the break is a
compare-and-swap under a breaker lock now), and re-entrancy compared `$$`,
which every subshell of one command shares. A lock whose holder is gone is
broken by the next taker, which is what makes it die with its holder even on
kill -9 (verified). The cost is that there is no shared mode, so `wk new` and
`wk sync` serialise rather than overlap — `-s` is accepted and waits, which
is always more than was asked for and never less. The same mechanism serves a
build started over ssh through `lib/lockrun.sh`, run on the machine that
builds, because a lock has to die with the *build* and not with the
connection.

Lock files are keyed by the machine's hostname as well as by the resource,
because a home directory can be shared by several build machines and a pid is
only meaningful on the machine it came from.

Lock points taken: the store (`wk sync`, `wk gc`, and `wk new`/`wk rm` where
the target pins a base) and per-workspace (`wk new`, `wk rm`, `wk build`).
Not yet: `wk test`, the alias file rewrite, the vm base tree, and the
babysitter's pid guard, which should become a lock too — pid files can lie
after recycling. The per-machine build lock on a shared box is a separate
thing and already exists on the far side (`targets/remote.sh`).

### Shared-home remotes (devbox-arm64-2 / devbox-armhf-2), no special case

Several build machines can share one NFS home. The current `~/.wk-remote`
marker cannot survive that: the second `wk remote setup` overwrites the
first's `target=`, and a path-keyed build lock would serialize two
*different machines'* builds. The clean fix is rule 1 applied to identity:
**delete the machine-side marker and derive the machine's identity every
invocation** — each target's conf gains the machine's hostname (recorded at
setup from `hostname`, which is a genuine creation-time fact), and `wk` on a
box finds "which conf names me" by matching. Shared homes then need nothing
special: all the confs coexist, each names its machine, and `wk remote rm`
of one leaves the others whole. Everything per-machine under a shared root
is keyed by derived machine identity, never by path alone — the build
directories arch- or hostname-keyed, so an aarch64 and an armhf box never
collide in one checkout. Status: the lock half exists (every lock file is
keyed by hostname, `lib/common.sh`); the marker is still written and the
conf-names-me derivation is not built.

### Changing workstations: the target view is calculated, not carried

Today each workstation has a private view of the world —
`~/.config/wk/targets/*.conf` says which machines exist and
`~/.local/state/wk/targets/<ws>` says where each workspace lives — so
sitting down at a different workstation means a different, wrong answer to
"what do I have". Rule 1 splits the fix in two:

- **The workspace→target registry is a cache, and goes.** Where a workspace
  lives is evidenced by the workspace *existing on that target*, so it is
  calculated: resolve a name by asking the targets — the configured remote
  machines (parallel, `ConnectTimeout`-bounded probes), the local vm dir,
  the container store. A name found on two targets refuses and names both
  (`--target` disambiguates); a target that cannot be reached is reported
  unreachable, never silently skipped. This reclassifies the registry,
  which the audit below had accepted as a record: the workstation-change
  requirement is exactly the case that shows it was a cache all along. The
  macOS dispatcher keeps its shape — host-side targets answer first, and
  container remains the default claim, answered honestly when the podman
  machine is stopped. Status: not yet removed — `target_of` still reads the
  registry, and `ws_target` (lib/target.sh) already computes the fallback
  when the entry is missing, which is the first half of retiring it.
- **The target confs are the one genuine record** — how to reach a machine
  cannot be derived — and they are *config*, not state. **Landed 2026-08-19**
  as `targets/hosts/<name>.conf` in this repository, with
  `~/.config/wk/targets/` kept as the local override layer
  (`target_registry_dir`, lib/target.sh; the shared half travels with the
  tree `t_sync_tools` pushes). The privacy decision of 2026-08-19 already
  accepts these hostnames as published. A new workstation is `git clone &&
  ./setup` and the view is identical — nothing to migrate, nothing to drift.

Host-bound targets stay host-bound by nature: this Mac's podman VM and
tart guests are drivable only from this Mac. But they are not invisible
from elsewhere — see the fleet status below.

### `wk status` walks the fleet: every workstation that is up

Decided 2026-08-19, superseding the "future work" note this section first
carried, and the peer half **landed the same day**: workstations are peers,
listed in the same committed `targets/hosts/` as the build machines
(`WK_REMOTE_PEER=1`, an ssh destination each — see "peers" in
`targets/remote.sh` and `targets/hosts/moose.conf`), and a bare `wk status`
asks every one that is up what it is up to — by running that machine's own
read-only `wk status` (`t_has_wk` / `t_wk`) and merging the answer, since
only the machine itself can see its containers and guests. The same walk is
where out-of-sync gets noticed, because it is the one moment every machine's
view is side by side:

- **wk-tools version skew** — **done 2026-08-19**, with a correction to the
  mechanism: a sha is not available where it matters. Every machine but the
  workstation runs an rsynced *copy* with no `.git`, so `wk version` reports
  two things — the git sha and dirty flag where there is a checkout, and a
  hash of the tree's contents (relative paths, `.git` excluded) computed
  identically everywhere. The tree hash is the comparable one, and it also
  catches uncommitted work that was pushed, which a sha never could.
  `wk status` asks each machine for its own and flags any that differs from
  this one, by name. This was not a hypothetical: the push-key work hit
  `unknown option --quiet` from a stale copy three times in one afternoon.
- **A workspace claimed twice** — the same name alive on two machines is
  reported as the conflict it is, not listed twice as if normal.
- **A shared machine seen differently** — two workstations reaching one
  build box must see one state; a disagreement means a stale near-side
  file somewhere, and the walk names both views.

**Modes are dynamic, and the walk reports transitions.** A machine's mode
(`host` or `bench` — `docs/HANDOFF-vocabulary.md`) can be *about to change*:
`wk boot` arms a one-shot reboot that takes a machine out of host mode and
boots it into a system wk built, and later brings it back. So a machine's
status line is its role, its current mode, *plus any announced transition*:
`rpi5: workstation, host mode, armed to boot system <id> (armed by moose,
14:02 UTC)`. The mode itself stays derived per invocation — after the
reboot the machine simply *is* in bench mode and answers as such (or stops
answering ssh entirely, which the boot driver reports) — but the arming is
a genuine record of intent that no probe can derive in full, kept as a
small file next to the boot mechanism it describes, plus whatever the
firmware itself can be asked (the one-shot boot order is readable where the
platform allows, and evidence beats the file when they disagree). The walk
then notices the transition-shaped desyncs: a machine armed to reboot that
is still in host mode long past the arming, a machine that came back
without the arming record being cleared, and — the operational one — a
mutating command aimed at a machine that is armed to leave host mode, which
warns or refuses rather than starting a build on a box about to reboot out
from under it.

The rules bind the walk exactly as they bind everything else: read-only
absolutely (the remote `wk status` starts nothing, boots nothing, repairs
nothing — on either end); every probe `ConnectTimeout`-bounded; a
workstation that is down or unreachable is reported as such by name,
never hung on and never silently dropped; and the exit code aggregates
the worst state found anywhere, extending the existing contract. The walk
runs only on hosts — inside a workspace `wk status` still reports the
workspace itself, and the sandbox cannot reach a workstation anyway.

### The daemon question: no

Considered and rejected: rewriting this as a Python daemon holding a
workstation or target role. A daemon is the largest possible piece of state
— a resident world-view that must be kept in sync with disk, with other
machines, and with its own binary across updates; it is the caching this
plan exists to remove, plus a liveness problem (rule at the top of this
file: nothing load-bearing lives only in a process table). Every guarantee
wanted here — locks, resumability, honest status — is *stronger* without
one: locks outlive nothing, evidence outlives everything, and a CLI that
recomputes from evidence cannot be stale. A daemon would also break the
founding constraint that the macOS host installs nothing (bash 3.2 is the
only universal runtime; a daemon means an installer, a launchd unit, and a
version-skew axis between daemon and CLI).

What the daemon idea is actually reaching for is real and is adopted
without one: **roles as an explicit, derived concept.** A machine is a
workstation, a workspace, or a build machine, computed per invocation from
evidence (the workspace marker; hostname-vs-conf for build machines, per
the shared-home design above) — and the dispatcher's five overlapping
role/command lists (`is_host_only`, `is_lifecycle`, `is_host_command`,
`is_readonly_report`, plus the in-workspace branch) become one
role × command capability table that `wk help` and `selftest` can both
read, so a refusal is a table lookup rather than five case statements
agreeing by luck. Python stays where it already is and where it has been
decided (`cmd/mcp`; `cmd/bench` per `docs/HANDOFF-bench-python.md`) — in
leaf commands that run where Python exists. The dispatcher and drivers
stay bash 3.2.

## Open questions

- Does `creating` need sub-stages surfaced in status (`clone 4.2G…`,
  `provisioning`), or is the driver's log line enough? (Lean: show the stage
  name from `ws.status`, point at the log for detail.)
- `wait_ready` default timeout: creation of a remote workspace legitimately
  takes 30+ minutes on a first mirror clone. Probably: no timeout when the
  driver is alive, fail fast when it is dead.
- Should `wk rm` of a `creating` workspace kill the driver first? (Lean:
  yes, and say so.)
