# HANDOFF — master task order

One reading of every `docs/HANDOFF-*` file plus `host/linux/rpi5/HANDOFF.md`,
ordered into two lanes — **Linux workstation** (podman, rootless, the mature
port) and **macOS host** (Tart VM, the Apple ports) — so both machines can work
without touching the same files or the same conceptual area at the same time.

Rule of thumb used below: if a task only makes sense on one machine (hardware,
OS-specific API, "the macOS path refuses outright"), it is assigned there. If a
task is genuinely shared code, it is assigned to Linux first (it is the
verified port) with an explicit second step to port/verify on macOS — so the
two machines are never mid-air on the same file at once.

**Security and settings auditing are scheduled last in both lanes,
deliberately.** Everything built in steps 1..N changes the attack surface (new
proxy behavior, new remote targets, new cross-compile transfer paths, restored
git helpers that can push, a yocto image-upload path, camera streaming on the
BMC) and, separately, the live machine's settings (quiesce, session, GPU,
Pi/Tailscale config). Auditing either first would mean redoing it after every
later step anyway; auditing last means one holistic pass over the system as it
will actually ship, and one settings-persistence conversation with the user
instead of several. Treat the ordering below as real — don't pull either audit
forward piecemeal. The settings audit (`docs/HANDOFF-settings-audit.md`) asks,
for every non-default setting found on each host, whether to persist it into
the repo or drop it, and ends with a written summary of what's kept and why;
it runs just before the security pass in each lane.

Each item names the source handoff so detail is not duplicated here.

---

## Done since — locking, consistency, and the macOS profiling/benchmark lane (2026-08-20, macOS host)

One session, in the order the user asked for it: fix locking and reliability,
make the commands consistent, then the macOS VM lane — debugging, profiling,
benchmarking — with the rest of the macOS work (remote, cross-compile, yocto)
explicitly deprioritised.

**Locking, rebuilt on a different primitive.** The lock is now a **symlink**
whose target string names the holder. Three defects went with the old
atomic-mkdir form, and the last two were found by writing the test rather than
by being reported:

1. *A lock naming nobody was waited out in full.* mkdir publishes the lock
   before the pid is written into it, and a process killed in that window
   leaves something indistinguishable from a live holder (`wk rm` sat out its
   whole timeout on one — docs/HANDOFF-yocto.md item 7). `ln -s` writes the
   payload *with* the lock, so the window does not exist rather than being
   made small.
2. *Several takers could break one dead lock and all conclude they had won.*
   Reading the link back after the swap does not settle it — at the instant A
   read it back, A really had won, and B overwrote it a moment later. The break
   is a compare-and-swap now: a short-lived breaker lock, and the swap only if
   the lock still holds the same dead payload the caller saw. Measured before
   the fix: twelve takers, eleven critical sections.
3. *Re-entrancy compared `$$`*, which every subshell of one command shares — so
   twelve parallel subshells each recognised their own parent and walked
   straight in (a counter that ended at 1). It is this process's own list now,
   and the payload carries four bytes of urandom so two subshells cannot write
   byte-identical claims.

**One EXIT trap, and handlers register under it** (`wk_atexit`). There was one
trap and six claimants — `hold_lock`, `barrier`, `cmd/new`'s driver, the seed
cleanup in `cmd/image`, `boot/disk.sh`'s staging cleanup, four prefetch reapers
— and bash keeps only the last one set. Every one was correct alone and
silently disabled whichever had been set before it; for a lock that turned "it
dies with its holder" from a property into a repair. `wk selftest` now checks
both the behaviour and that no file has gone back to trapping.

**One lock mechanism everywhere.** `flock` is gone: it was still the image
store's lock (and macOS ships no `flock(1)`, so on a Mac that lock was no lock)
and the remote build lock, where the descriptor is inherited by everything the
build leaves running. The remote build takes the same lock through
`lib/lockrun.sh` in the wk-tools that `t_sync_tools` has just pushed there, so
`wk remote setup` no longer has to refuse a machine for want of a package.

**And the half a lock cannot cover.** A job detached *into* a workspace
outlives the command that took the lock: a Yocto stage held nothing out here
and a `wk build` in the same checkout was let straight in. `ws_busy_reason`
asks the workspace for evidence instead — the pid a detached job leaves in
`home/<job>.pid`, tested *inside* the workspace — and `wk build` goes through
it as a barrier (docs/HANDOFF-yocto.md item 8).

**Consistency.** `image`, `boot` and `serve` were neither host-only nor host
commands, so on a macOS host they forwarded into the podman VM and were allowed
inside a workspace; they are classified now, which is the item
docs/HANDOFF-netboot.md listed as owed. `wk help`'s `image` line matches the
verbs it actually has, `--explain` no longer cuts a command's sub-flags off its
usage, and `cmd/report` joined the house style (it was the one file pinned to
`/bin/bash`, with `date -v-7d` that fails on Linux and `$USER` — the *unix*
name — deciding whose week it reported).

**`wk profile`** (docs/HANDOFF-profile.md) — the env-var walls from three wiki
pages and two skills, written down once: JSC's sampling and bytecode
profilers, samply, Instruments via xctrace, heaptrack and massif, with
`--mode native` picking xctrace on the Apple ports and samply everywhere else.
Composition is verified in `wk selftest --quick`; nothing has been *run*,
because this host has no guest and no container workspace. `t_pull` grew `vm`
and `remote` implementations and `t_pull_dir` joined the contract.

**The macOS benchmark role** (docs/HANDOFF-benchmarking.md, "The macOS shape"):
build in the guest, run on the metal. `boot/mac-volume.sh` adds a third arming
model — `hands-on` — for a transition no software can make on Apple Silicon,
and the machine drives itself because it is the only one of its kind here.
`wk bench stage <ws> --to mbp` copies the product, `Tools/`, and a `stage.json`
written last onto the benchmark volume while it is merely mounted.

**And the runner, `wk bench staged`.** The two questions it was waiting on were
answered by reading and running WebKit's own code out of a base snapshot rather
than guessing: run-benchmark drives from a *partial* tree (`Tools/Scripts`
alone — plans and patches resolve relative to webkitpy, not to a checkout), and
`--browser minibrowser --platform osx --build-directory …` launches
`MiniBrowser.app` directly with `DYLD_*` set, which is what makes the partial
tree sufficient. Two more facts fell out of the same reading, both of which
would otherwise have been found in the benchmark role with the machine already
rebooted: it needs a python with PyObjC (Apple's `/usr/bin/python3`, not the
Homebrew one on PATH — `import objc` is not autoinstalled), and webkitpy
installs everything else into the staged tree on first use.

**A benchmark runs in the benchmark role or it does not run** — the one refusal
here that `--force` does not open, at the user's direction: a run on the
workstation produces a result of exactly the same shape and nothing tells them
apart afterwards. `--dry-run` still describes it. `WK_IMAGE_MARKER` lets the
role's code path be exercised without a reboot, and a run that uses it is
recorded as `role_marker_overridden`, which `wk bench compare` warns about.

Verified end to end against a simulated role with a stub browser: preflight,
payload build, http server, driver, `prepare_env`, launch, the timeout path,
the recorded failure with the real exception surfaced, and the record on the
volume. What has *not* run is a measurement — that needs a real `mac-release`
build, which needs the golden base guest this machine does not have. Two things
found by running it: a benchmark that dies leaves the Dock's launch animation
off (put back now), and `wk bench compare` on two *files* was being forwarded
into the podman VM, where it reported "no such run" about a file sitting right
there.

**`wk quiesce` measures instead of trusting.** On macOS it now prints the
privileged half's claim and what the machine says now, labelled separately, and
covers what the helper does not touch: a configured Time Machine destination,
the sleep timer, and `CPU_Speed_Limit` — a machine thermally held back during
one half of an A/B is a difference that has nothing to do with the change.

`wk selftest --quick` is 21 checks (was 15 of 198 line items; the plan is 545
now, 31 encoded), and every claim above that could be tested without a guest,
a board or an external SSD is one of them.

## Done since — a batch of reported defects (2026-08-19, macOS host)

Driven by what broke in use rather than by a lane. Every item has a
`docs/TESTING.md` line; the two that changed a design decision are written up
in `docs/HANDOFF-workspace-state.md` and `docs/HANDOFF-linux-remote.md`.

1. **A shared target registry** — `targets/hosts/<name>.conf`, in this
   repository and therefore on every device that pulls it, with
   `~/.config/wk/targets/<name>.conf` still overriding it line by line. This
   was the answer to two separate reports: buildbox4's three clang-19 CMake
   flags had to be re-typed on every command from every device, and a
   reinstall lost every target (the gap listed under "Fixed / resolved" below).
   `wk build bb4 gtk-release-asan` now needs no `--cmake`.
2. **`moose` is visible from the Mac** — a *peer* target
   (`WK_REMOTE_PEER=1`): another workstation, which can be asked and not
   driven. `wk status` and `wk ls` delegate to its own wk and report its
   workspaces; no tooling is pushed to it and `wk new`/`wk rm` against it are
   refused. Provisioning it as a build box would have written `~/.wk-remote`
   and made it refuse `wk sync`, `wk gc` and `wk new` *on itself*.
3. **One round of probes, not N** — every target is asked concurrently before
   the walk prints anything, so two unreachable machines cost 10.0 s rather
   than one `WK_SSH_TIMEOUT` each. Only the waiting moved; the report is the
   same report. This mattered *because* of item 1: a device now knows machines
   that are not on its network.
4. **`wk sync --target <machine>`** — the far-side equivalent of a sync, which
   did not exist: it pushes wk-tools (the stale far copy that answers a
   delegated command by the old rules) and fetches that machine's own mirror.
   `wk status` named the drift and nothing but a full `wk remote setup` fixed
   it. A machine that clones from a shared repository in its MOTD is told so
   rather than silently skipped.
5. **`wk sudo` read sudoers wrongly** — it grepped the whole listing for
   `NOPASSWD: ALL` while sudoers takes the *last* match, so buildbox4 (site
   NOPASSWD, our `PASSWD: ALL` after it) was reported as wide open and
   `require` re-installed and asked for a password on every run. Now the
   verdict starts from `sudo -n true`, and `require` gates on the property
   rather than on a drop-in path that is wrong whenever the remote login name
   differs from the local one.
6. **The in-workspace interface has one spelling** — a workspace name on the
   command line inside a workspace is refused (`refuse_ws_name`), where it used
   to be silently accepted as a synonym. Two spellings meant everything written
   about the in-here interface was describing one of them.
7. **`wk build --dry-run` prints the exact commands**, including the ones only
   the target can resolve (ionice/choom, the cgroup-clamped job count, the real
   cmakeargs), and `--explain` names the flag rather than describing them. It
   refuses to ask a target whose wk-tools predates the mechanism -- an old
   `build-in-target.sh` ignores `WK_DRY_RUN` and *builds*, which is how that
   guard was found.
8. **`wk build --env NAME=VALUE`** — the environment was the one half of a
   build that could not be steered from the command line, because the build
   runs in the target and only what `config_build_env` carries gets there.
   Applied last, so it overrides the config rather than joining it.
9. **Layout tests on a remote target, against the build already there** — the
   config now defaults to what the workspace was last built with, and a
   JSCOnly config is refused for the layout suite by name. A bare
   `wk test <ws> --layout` used to resolve `--jsc-only` and point
   run-webkit-tests at a tree with no WebKitTestRunner in it.
10. **`wk claude`**: Claude's own options work on either side of the workspace
    name, and on a remote target it starts in **auto mode** rather than with
    `--dangerously-skip-permissions` -- skipping permissions is the sandbox's
    bargain, and a shared build box is the one target with no sandbox.
11. **`--detach` and `--babysit` share one detach primitive**, and the `--zed`
    gate was exercised with a real editor -- which found that `--zed` cannot
    work for a container workspace on a macOS host at all. Both are written up
    in `docs/HANDOFF-workspace-state.md`.

12. **git remotes: `origin` and `fork` are separate things, and now checked**
    — `wk remotes [<ws>] [--fix]`. The wiring had one authority
    (`wk_wiring_script`) and no verifier, so a workspace created before it
    kept whatever `git clone --shared <mirror>` left: `bb4` on buildbox4 had
    `origin` = the machine's local mirror, pushable, with `fork` pushing over
    https — which means git never consults ssh and no deploy key is ever
    offered, whatever `wk push` says. `db`, created after, was correct, and
    nothing reported the difference. A wrong origin is now flagged in
    `wk status` too, and the branch's own tracking is checked by the same rule
    -- we never push to an upstream, always to a fork -- with `--fix`
    retargeting it (bb4's branch tracked `origin/<x>` for a branch that exists
    only on the fork). Each upstream is paired with its own project's fork, so a
    `wpe/<x>` branch belongs to `forkwpe`. `--fix` takes its arguments from the
    driver (`t_wiring_args`), so creation and re-assertion cannot drift.

    Reading the user's wiki for the canonical set turned up a remote missing
    everywhere: `wpe` (WebPlatformForEmbedded/WPEWebKit) was in the *mirror's*
    upstreams and in `wk pr`'s repositories but was never wired into a
    workspace, so no workspace could fetch a WPE branch at all. The wiring now
    creates every upstream in `wk_remotes`, fetch-only. The wiki's fifth
    remote, `igalia` over gitlab ssh, is a recorded omission: port 4429 is not
    in the egress allowlist and a workspace holds no personal key.
13. **`wk sync --target container`** — the podman VM's `/opt/wk-tools` was
    refreshed only by `./setup --stage vmtools`, so a command added to this
    repo was "unknown command" inside every container until somebody
    remembered. That is how `wk remotes` first failed. On Linux there is
    nothing to sync and the command says so: containers bind-mount the
    checkout.
14. **The push switch, end to end.** Proven from a container: refused with the
    switch off, `* [new branch]` on a `--dry-run` push with it on. *Not* working
    from a remote target, and not because of the switch — buildbox4's deploy key
    exists but was never registered on GitHub, so a push is
    `Permission denied (publickey)` with everything else correct. `wk push on`
    now names `wk key check`, which is the only thing that can ask GitHub.
    Registering the three machines' keys (bb4 "not registered", devbox-arm64-2
    and moose "no key") is a GitHub-side action and is left to the user.

One hazard found the hard way and worth stating on its own: **editing wk-tools
while a `wk` command is running corrupts that run.** bash reads a script
incrementally, so rewriting `cmd/build` under a 23-minute build resumed the
process mid-word (`empt.: command not found`) and dropped it back into the lock
wait. The build itself was fine and a re-run recorded `ok` -- the crash-only
design held -- but the same applies with more force to the copy on a build
machine, which `t_sync_tools` replaces at the start of every build. Nothing in
the tooling prevents it today.

## Next, in parallel — one prompt per machine (written 2026-08-19)

The two tasks below touch disjoint files (one shared exception:
`docs/TESTING.md`, where each adds lines only under its own sections). Both
sessions start the same way: read "The rules" at the top of
`docs/HANDOFF-workspace-state.md` — nothing may contradict them.

**macOS box — implement the state rules, phase 2. DONE 2026-08-19** — the
readiness half: `.wk-ready` written last by every driver, `t_info` answering
the lifecycle (`absent | creating | present`, plus `broken` and `unreachable`,
which the implementation needed and which rule 5 and the core requirement
ask for), one `wait_ready` gate that `wk build` and the `--zed` paths go
through, `lib/detach.sh` extracted from the babysitter with `wk new` as its
second user (creation detached by default, `--no-wait` to return at once),
and `wk status` rendering the lifecycle first with a new exit code 4 for a
workspace that needs a person. Verified against the podman VM: a workspace
created and followed to `present`, its driver killed -9 at the init stage and
then reported as abandoned-with-a-dead-driver by `wk status` (exit 4),
refused by `wk build` with the repair command, and remade by a re-run of
`wk new`; a container removed by hand reported as `broken` rather than
`absent`. Six decisions that differ from the plan as written are listed in
`docs/HANDOFF-workspace-state.md` under "The plan" — the most load-bearing
being that `ws.status` and the creation log live *beside* the workspace
directory, because the wipe at the start of a re-run would otherwise delete
the log the driver is writing to. What is left there: `wk build --detach`
still has its own nohup rather than `detach_run`, and the `--zed` gate is
exercised through `wk build` rather than by opening a real editor.

**macOS box — implement the state rules, phase 1. DONE 2026-08-19** — all
four steps and both bugs, verified against the podman VM (a real workspace
created, half-broken, remade, destroyed; two `wk new` raced; `wk gc` raced
against `wk new`). Two corrections came out of it, both written up in
`docs/HANDOFF-workspace-state.md`: the "`wk status` starts podman"
observation did not reproduce (the guard moved inside `forward_to_vm`
anyway, which is the only thing that can start it), and `flock` had to be
abandoned for an atomic-mkdir lock, because podman's `conmon` inherits the
lock fd and then holds a workspace's lock for as long as the container
exists. What phase 2 is — the `.wk-ready` far-side marker, `wait_ready`,
the detach primitive, the lifecycle line in `wk status` — is listed under
"The plan" there. The prompt that was run:

> Read docs/HANDOFF-workspace-state.md in full ("The rules" govern) and
> docs/TESTING.md §6. Implement in this order, verifying each step against
> the podman VM before the next:
>
> 1. Find and fix the path by which a bare `wk status` still starts the
>    podman machine (look in `wk`'s dispatch/forwarding and at podman CLI
>    calls that auto-start the machine). Then make `wk ls` and `wk status`
>    enumerate from one shared walk so their workspace-name sets are
>    identical. Encode the §6 "read-only commands" checks in selftest:
>    status/ls/logs/doctor with the machine stopped leave it stopped.
> 2. Snapshot completion marker: `cmd/sync` writes `base/<id>/sha` and
>    `branch` last, as the publish gate; `current_base` and `wk new` ignore
>    unmarked snapshots; `wk gc` prunes them; a snapshot whose tree no
>    longer matches its recorded sha is refused by name.
> 3. Crash-only `wk new` / `wk rm` per rule 3 (wipe over repair): `base-id`
>    written last; a re-run over a half-made workspace destroys the rubble
>    and remakes it from scratch — never "already exists"; `wk rm` destroys
>    artifacts first, forgets the registry last, and exits nonzero naming
>    leftovers on partial failure.
> 4. `with_lock` in lib/common.sh (flock where a store lives, an
>    atomic-mkdir lock on the bare macOS host) and take it in sync, gc,
>    new, rm, build per §6 "Concurrency".
>
> On the way, fix the two verified bugs: `WK_TARGET=vm wk gc` dies sourcing
> a driver without lib/target.sh, and `wk selftest --section <typo>` exits 0
> having run nothing. Run `wk selftest` and tick the §6 lines you verified.
> Do NOT touch cmd/pi, docs/HANDOFF-netboot.md, or create cmd/image,
> cmd/serve, cmd/boot — the Linux box is working there.

**Linux box — netboot: the rpi5 perf image.** Paste this:

> Read docs/HANDOFF-netboot.md — "State as of 2026-08-19" and the appendix
> are ground truth; the one-shot, the revert and the self-return watchdog
> are already proven on hardware. Execute its "Next three actions":
>
> 1. `wk image build` on an Ubuntu base — WiFi is a first-class
>    requirement (the rpi5 has no wired fallback, ever), and Ubuntu is the
>    stack already proven to drive this radio onto this AP. Bake in: the
>    driving machine's ssh key, the network profile, sshd, an identity
>    marker, the self-return watchdog (TimeoutStartSec=infinity), the diag
>    dump to the FAT partition, a persistent journal, and no first-boot
>    resize-and-reboot (it spends the one-shot). The spec lives in the
>    repo; encapsulate the appendix's steps as the implementation.
> 2. Arm the one-shot with that image, confirm it comes up reachable over
>    WiFi, then set perf_event_paranoid and the JIT-dump environment — the
>    profiling half this whole step exists to unblock.
> 3. `wk serve` plus `wk pi netboot-enable rpi4`, trying proxy DHCP on the
>    LAN before any cable.
>
> `wk boot` arming must write the role-transition record and clear it on
> return, per "wk boot is a role transition" in the netboot handoff and the
> fleet-status section of docs/HANDOFF-workspace-state.md. Add TESTING.md
> lines under a new netboot section as you go. Do NOT touch `wk`,
> cmd/status, cmd/ls, cmd/sync, cmd/new, cmd/rm, lib/store.sh,
> lib/common.sh, lib/target.sh, or targets/container.sh — the macOS box is
> working there.

---

## Out of order, by request — the git-push switch (2026-08-19)

Asked for directly while the state-rules work was landing, so it jumped the
queue: `wk push on|off|status`, the toggle `docs/HANDOFF-sandboxing.md` asked
for and `docs/HANDOFF-git-tools.md` was waiting on. **This is not the security
audit being pulled forward** — that is still last in both lanes, for the
reasons at the top of this file. It is one mechanism the audit will have to
look at, built now because an agent could push and the answer could not wait.

Landed with it, from the same request: every checkout gets `origin` =
`WebKit/WebKit` and both forks wired automatically, from one place
(`wk_wiring_script`) rather than four that had drifted — the remote build
machine's driver had pointed `origin` at that box's own shared clone;
`wk build ... --cmake '<flags>'`, which adds to the config's CMake flags where
a hand-written `--cmakeargs` silently replaced them; and
`wk <command> --explain`, which prints what a command does, what it acts on
and whether it changes anything, so an agent does not have to read the
implementation to find out. Details in `docs/HANDOFF-sandboxing.md` and the
TESTING lines under §1 and §4.

---

## Was blocking, both lanes — `wk` inside a workspace

**`docs/HANDOFF-wk-in-workspace.md`** — found 2026-08-18, **both halves done and
verified the same day**: Linux from the workstation, macOS from this host. Claude only ever runs inside a workspace, so the
in-workspace `wk build <config>` / `wk run` / `wk test` interface that
`CLAUDE.md` documents is what the sandbox exists to allow, and it did not exist.
It does now on Linux: a marker written by provisioning, a `targets/local.sh`
where the target is this machine, and the workspace name made optional in
`build`/`run`/`test`/`logs`. Measured in a fresh workspace — `wk build
jsc-release` from inside, 1m16s.

The macOS guest half — lane B's first item, and where the original "podman is
required" failure was measured — is verified, along with the bash 3.2 parse the
Linux host could not run. It needed four things on top, all in the handoff: the
in-workspace build was sized from a third of the guest (the desktop reserve
subtracted twice), a bare `wk run` in a guest resolved a JSCOnly path that
cannot exist there, the marker was written only on tooling sync and so was
absent on a guest booted straight into `wk claude`, and `--dry-run` had to exist
before any of it could be checked without a 99-minute build.

One piece remains, and it blocks nothing: build state is recorded twice, once
per side, because a workspace cannot see the host's store. Under the state
rules in `docs/HANDOFF-workspace-state.md` (one copy of a fact per machine)
this is a violation to fix, not a design; the handoff explains why the
obvious mount is wrong and what to do instead.

`docs/HANDOFF-claude.md`'s "every skill invokes a deterministic tool rather than
freehand steps" is unblocked on both platforms.

---

## Lane A — Linux workstation

### Order, revised 2026-08-19 — netboot first

**The numbered items below are stable identifiers, not the running order.**
Three other files cite "lane A step N" (`docs/HANDOFF-profile.md`,
`docs/HANDOFF-claude.md`, and lane B here), so the numbers stay put and the
running order is stated here instead.

Netboot moves to the front, at the user's direction, and the four steps behind
it re-order to consume it. The reason it is worth pulling forward: netboot is
the one mechanism *all* of them need — an unsandboxed machine you can put an
image on and take back afterwards — and every one of them was otherwise going
to invent its own way to get a binary onto hardware.

  N. **Boot an image — the shared substrate** — `docs/HANDOFF-netboot.md`.
     **Started 2026-08-19:** `wk image build|flash|ls|show|rm` and
     `wk boot [--status|--keep|--back|--disarm|--diag]` exist, with the spec in
     `image/` and one driver per machine in `boot/`; the rpi5's perf image is
     built from a pinned Ubuntu 26.04 base and written to its USB stick. See
     that file's newest state section for what is proven and what is not — the
     rpi5 half is **done and measured on hardware** (arm -> image reachable in
     53 s -> claim -> hand back in ~40 s, with `perf_event_paranoid=-1` set on
     the image). `wk serve` and `wk pi netboot-enable` are written and verified
     as far as hardware allows -- boot files fetched over the real LAN by
     another machine, the EEPROM diff produced without writing. What is left is
     a board to point them at: the rpi4 is powered off. (new
     file, split out of the benchmarking handoff's open questions, which it now
     answers). Scoped 2026-08-19 to **all three machines**, at the user's
     direction: moose and the MBP, not just the rpi5. That scoping produced the
     structural finding — Apple Silicon cannot netboot at all, so the substrate
     is an image store plus one boot driver per machine, and the MBP's driver is
     hands-on by nature. Boot the rpi5 first for **profiling**: the least
     demanding consumer, so the mechanism gets proven before storage-stability
     requirements pile on. Pulls the profiling half of step 10 forward with it.
  1. Then **benchmarking on that image** — `docs/HANDOFF-benchmarking.md`,
     `bench_host=image`, driven remotely. Promoted out of the unassigned pile
     at the bottom of this file by the same decision; it is no longer waiting
     on step ⑦'s SD-card flashing, because netboot needs no media.
  2. Then **cross-compilation** — step ⑥. The netbooted image is a slim distro
     with no SDK on it, which is exactly the condition a sysroot cross build
     has to satisfy; a GTK MiniBrowser built on moose and run there is the
     test. Cheap if the image and the sysroot are the same tree — see the
     netboot handoff's "sysroot equivalence".
  3. Then **yocto for the rpi4** — step ⑧, consuming step ⑦'s flashing path,
     and testable over netboot before any SD card is written.
  4. Then **`wk pi setup` on the rpi4** — step ②, against that yocto image.
     This is why step 2 is no longer first: it now has a freshly built image to
     run against instead of the buildroot install, which also tests the
     no-image-rebuild promise on something new.

Everything else keeps its old relative order (④ ⑤ then the rest of ⑩ ⑪ ⑫ ⑬ ⑭,
then the audits ⑮ ⑯ ⑰ last, for the reasons at the top of this file). Step ③,
the rpi5 tuning re-apply, splits around the new first step: its *stability*
half is a prerequisite — the board has to be a working, reachable workstation
before anything can arm a one-shot netboot over SSH — while its perf half was
already reassigned to the image, which is now the thing being built.

**Hardware state, measured 2026-08-19:** rpi4 and rpi3 not on the tailnet at
all, moose's BMC not answering, and moose is WiFi-only (`wlo1`; all three wired
NICs DOWN, carrier=0 — no cable in any of them). The rpi5 was powered on later
the same day and **is up on the tailnet but not reachable over SSH**:
`tailscale ping` pongs (so tailscaled is running and a path exists, via a
relay), while TCP 22 times out — one attempt completed the handshake and then
produced no banner. That points at `sshd` on the board or a tailnet ACL, not at
the network. Nothing on the netboot step can start until that is cleared: the
one-shot is armed over SSH.

The serving role is also required to *float* (any idle machine serves, the Mac
included), which the netboot handoff now covers — the one non-obvious
consequence is that `TFTP_IP` lives in firmware, so the servers share a service
alias IP rather than each being named.

1. **32-bit containers (`wk new --arch armhf`)** — `docs/HANDOFF-linux-arm32.md`
   **Done 2026-08-18**, and verified: an armhf workspace builds `jsc-release`
   natively (`CMAKE_SYSTEM_PROCESSOR=armv7l`, armhf clang, `linux32`).
   Linux-only, permanently: Apple Silicon has no AArch32 at EL0.

   The lasting decision is the vocabulary, because three mechanisms here could
   all be called "32-bit" and two of them do not exist yet: `--arch` is the
   workspace's own userland executed natively, `--sysroot` (reserved, refused
   with a pointer) is a cross build from a native workspace, and `--target` is
   another machine. The images invite exactly this confusion — `24.04_arm32` is
   native armhf, `24.04_arm32_arm64` is an arm64 image with armhf multiarch —
   so `wk new --arch` pins the image explicitly rather than letting the SDK
   infer one. Configs are unchanged: an armhf workspace builds `jsc-release`,
   not `jsc-release-32`.

   Two findings came out of it, both about the *branch* rather than the
   workspace: trunk has no 32-bit ARM JIT left (an armhf JSC there is a CLoop
   build by construction), and trunk's bytecode decoding SIGBUSes on anything
   non-trivial. 2.48 still has the ARMv7 backend and still works, and the trunk
   crash is fully debuggable in the workspace — `wk run --lldb`, lldb and gdb
   all drive the 32-bit process — so neither blocks the lane.

   Left over, all small: an armhf workspace has never been put on 2.48 (one
   fetch), a browser port has never been built on armhf, `wk test` has never
   been run there, and no benchmark has completed in one. See the handoff's
   "Remaining".

2. **Raspberry Pi provisioning** — `docs/HANDOFF-linux-pi.md`
   Linux-only (macOS workspaces can't reach the Pis at all). Run
   `wk pi setup rpi5` / `rpi4` against the live devices, confirm `pi-hosts`
   populates and the proxy allowlist works, and confirm the *negative* (an
   address not in the file is refused).

   **rpi5 role, decided 2026-08-18 and confirmed 2026-08-19:** the rpi5 is
   provisioned as a **regular workstation, now** — its own `./setup`, full
   tailnet privileges, Claude sandboxed in podman workspaces exactly like
   moose. It does not go through `wk pi setup`, and its workstation identity is
   never in `pi-hosts`, because an unrestricted tailnet node reachable from
   inside a workspace would be the boundary gone. So this step is rpi4 (and
   rpi3) only.

   Benchmarking it becomes a booted image instead, and that is no longer an
   open design problem: `docs/HANDOFF-netboot.md` settles the mechanism (a
   one-shot `set_reboot_order` over SSH, moose serving), and closing that gap
   is now the *first* thing lane A does rather than the last. The gap itself
   still stands until it lands — until then the rpi5 is not a benchmark device.
   `docs/HANDOFF-benchmarking.md`'s "Booting the image" still covers the other
   two machines, including the fact that the M4 cannot netboot at all.

   **Revised 2026-08-19:** this step runs *last* of the five re-ordered items,
   against the yocto rpi4 image from step 8 rather than the buildroot install.

3. **RPi5 tuning re-apply** — `host/linux/rpi5/HANDOFF.md`
   Separate machine, reached over SSH from the Linux workstation once step 2
   is done. Re-run `rpi5-setup.sh` → reboot → `rpi5-verify.sh` →
   `rpi5-stress.sh`, re-check the paths/fstab/indexer notes for the 26.04
   re-install, confirm the GPU at v3d=1200 with a sustained load. NUMA (Path B)
   is already done; Path A (upstreaming `CONFIG_NUMA_EMU`) is optional
   follow-up.

   Under the step-2 decision this tree splits: the *stability* half (fan
   always 100%, WiFi stability, fstab/indexer, the NUMA kernel) belongs to the
   rpi5 in every role and keeps being applied to the installed OS; the *perf*
   half (overclock, v3d, perf governor, swap off) moves into the benchmark
   image definition when `docs/HANDOFF-benchmarking.md` is picked up, so the
   workstation install never depends on an overclock to be usable.

   **Revised 2026-08-19:** the stability half is now a prerequisite for the
   new first step, not a peer of it — a one-shot netboot is armed over SSH, so
   the board has to be up, reachable and provisioned as a workstation before
   any of this begins. The perf half's destination now exists as a written
   design (`docs/HANDOFF-netboot.md`): the image's own `config.txt`, served
   over TFTP.

   One part of that split is not obviously image state: the EEPROM
   (`SDRAM_BANKLOW`, `BOOT_ORDER`) and `config.txt` are *firmware* settings
   shared by both roles, so an overclock left there overclocks the workstation
   too. The image has to carry its own `config.txt` on the boot medium rather
   than write the EEPROM — see the rpi5 section of
   `docs/HANDOFF-benchmarking.md`.

4. **Remote target — DONE 2026-08-19** — `docs/HANDOFF-linux-remote.md`
   Run end to end against `devbox-arm64-2` (80 cores, Debian 12, through a
   ProxyJump) — and driven from the **macOS host**, not from Linux, because
   the driver is host-agnostic and the target is a Linux box either way. The
   contract, the conf, `wk sync`'s remote meaning, the status-file question and
   the `$*` quoting bug are all answered in the handoff.
   The shape that came out of it: a target name can now be a *machine*
   (`--target devbox-arm64-2`, resolved through
   `~/.config/wk/targets/<name>.conf`), because remote is the one target you
   can have several of — so `WK_TARGET` is the name and `WK_TARGET_KIND` is the
   driver, and commands branch on the kind.
   Two findings beyond the driver. Job sizing measured the *driving* machine
   for every target: on a macOS host `/proc/loadavg` does not exist, so a
   shared box always looked idle — `lib/resources.sh` now takes `WK_AVAIL_MB`,
   `WK_LOAD` and `WK_MAX_JOBS` from the driver. And a bare `wk status` on a
   macOS host reported containers only, because the dispatcher forwards it into
   the podman VM whose registry has never heard of a guest or a build box; it
   had been hiding the `vm` target the same way.
   Left over, and not a wk problem: Debian 12's clang 18 + libstdc++ 12 has no
   `<format>`, which trunk's WTF has required since 2026-06-16, so *trunk*
   cannot be built on that box without the container SDK (step 6). Releases up
   to 2.52.x build there normally.

   **Extended the same day** with `wk remote setup <target>`, which provisions
   a shared machine without ever needing root on it: zsh through the shared rc
   rather than `chsh`, `wk` on PATH there, and a `~/.wk-remote` marker plus
   `WK_REMOTE_LOCAL=1` that let the same driver act on the same workspaces from
   the machine itself with no ssh hop (`wk build` there, 2m30s against 8m47s
   cold from the workstation). Workspaces are cloned from a WebKit repository
   the machine advertises in its MOTD when it has one — hardlinked, 69 MB of
   new `.git` against a 13 GB source — and the advertisement is verified first,
   because buildbox4's names a path that no longer exists. What it finds lying
   around it offers to remove, once, with the size attached; it never removes
   anything unattended. The machine holds workspaces and does not own them:
   `wk new` and `wk rm` refuse there, because the registry that says which
   machine a workspace lives on -- and so where a later `wk build` goes -- is
   the workstation's. The machine holds workspaces and does not own them:
   `wk new` and `wk rm` refuse there, because the registry that says which
   machine a workspace lives on -- and so where a later `wk build` goes -- is
   the workstation's.

5. **`docs/HANDOFF-other-remote.md`** (Windows/macOS remote targets, rentable
   cloud VMs without containers/virtualization, for PII-free perf testing) —
   pick up right after step 4 lands, since it's the same driver contract for a
   different OS/provider.

6. **Cross-compile targets** — `docs/HANDOFF-cross-compile.md`
   Wire up the `webkit-container-sdk` cross-build workspace + the rpi5/remote
   transfer helper; confirm debugging and perf testing both work against a
   cross-compiled binary. Natural follow-on to steps 2 and 4 (rpi5 as a
   target, and the "get a binary onto another machine" plumbing).
   Then `docs/HANDOFF-pi-deploy.md` — `wk pi deploy` and `wk pi bench`, the
   build-archive-copy-extract loop and the per-session device prep ritual —
   which consumes this step's transfer path and step 2's provisioning.

   **Revised 2026-08-19:** this is position 2 in the running order, and its
   test target is the netbooted rpi5 image rather than the rpi5 install — a
   slim distro with no SDK on it, running a cross-built GTK MiniBrowser. If the
   image and the sysroot are the same tree, the ABI question answers itself;
   see `docs/HANDOFF-netboot.md`. The "natural follow-on to steps 2 and 4"
   note above is superseded: netboot supplies the get-a-binary-onto-hardware
   path, so neither the Pi provisioning nor the remote driver gates it.

7. **SD-card image flashing** — `docs/HANDOFF-sdcard.md`
   Generic copy-image-out-of-workspace + flash-to-card flow (was an empty
   placeholder file; now scoped). Do this before yocto (step 8) so yocto can
   consume it instead of building its own copy-to-host path.

   **Revised 2026-08-19:** yocto is still the consumer, but the benchmark image
   no longer is — it netboots, so no card is written for it. That drops this
   step from two consumers to one, and it can be done lazily, once the yocto
   image needs to survive without a netboot server.

8. **Yocto builds — Linux half** — `docs/HANDOFF-yocto.md`
   Do the build-side work here first (yocto tooling is native to Linux); cache
   must survive workspace destruction, and image upload to a target should
   reuse step 7's flashing flow. The *macOS* half — confirming the same flow
   works from a Tart VM — is lane B step 5, after this lands. Also: get
   Tailscale installed on the rpi3 target image itself.

   **Revised 2026-08-19:** position 3 in the running order, and the target is
   the **rpi4** — netboot it (the Pi 4 bootloader has the same network boot
   mode) so each image can be tested the moment it builds, without a card and
   without a trip to the machine. Flashing then becomes the last step for the
   image that is kept, not the loop that tests them.

9. **Linux MiniBrowser: debugging + graphical run** —
   `docs/HANDOFF-linux-minibrower.md`, implemented together with
   `docs/HANDOFF-debug.md` (`wk debug` and `wk run --until-crash` — the same
   attach recipes as commands rather than instructions).
   Support running WPE MiniBrowser graphically (interactive) and attaching a
   debugger, including a layout test. Make sure prewarm processes, PSON, and
   site isolation don't get in the way. Reference:
   `Debugging-WPE-Linux-(desktop)` wiki page.

10. **Profiling tooling — Linux half** — `docs/HANDOFF-profile.md` (which
    subsumes `docs/HANDOFF-original-helpers.md`'s profiling section the way
    git-tools was split out earlier). Build samply, sysprof-cli, and heaptrack
    into the workspace image, and ship `wk profile` as the interface — the
    env-var walls (JIT dump, sampling profiler, bytecode profiler, heaptrack,
    strong-ref tracking) become modes of one command instead of wiki
    copy-paste and skill recitation.
    (`strip-addresses` and `show-profiled-functions` are already restored in
    `container/bin/`.) The macOS-MiniBrowser half of profiling is lane B step 6.

    **Revised 2026-08-19:** the part of this that needs a machine of its own
    comes forward with netboot and is its first consumer — `perf_event_paranoid`
    and the JIT-dump environment are ours to set outright in an image, which is
    the whole reason profiling leads rather than follows. The workspace-side
    provisioning (samply/sysprof/heaptrack in the container image, the
    profile-viewing path out through the egress allowlist) stays here, in the
    old relative position: an image is where a run has no sandbox, not a
    replacement for profiling inside a workspace.

11. **Memory charting** — `docs/HANDOFF-memory.md` (`wk bench mem`; starts by
    rescuing `plot-memory-log.py` and the experiment patches out of the wiki).
    "All targets" — build the collection/charting mechanism here since Linux
    is the reference target, then it should apply unchanged to WPE/GTK/JSCOnly.
    Fixed-core-count running *and* benchmarking needs to exist for every
    target; macOS is the one place lane B must independently confirm it
    (Tart VM core pinning is a different mechanism than a podman `--cpuset`).
    Reference: `Memory-benchmark-charts-(2.52)` wiki page.

12. **PGO profile build support** — `docs/HANDOFF-original-helpers.md`
    ("Building" section: collect + build with a PGO profile). No stated
    machine constraint; do it on Linux since the build plumbing lives there,
    then confirm it's not blocked on macOS.

13. **Git and GitHub helpers** — `docs/HANDOFF-git-tools.md` (split out of
    `docs/HANDOFF-original-helpers.md`). `gpr` is already restored as
    `wk pr`; bring it up to house style, then add `git-sync-fork`; `git-clean`
    and `commit-count` are trivial one-liners, lowest priority.
    Machine-agnostic — listed here only because Lane A has more slack after
    step 12; fine to pick up on macOS instead if that lane is idle first. Note
    the dependency on step 17's git-push toggle before wiring these to
    actually push anything.

14. **BMC recovery/streaming** — `docs/HANDOFF-bmc.md`
    Unrelated to the container/VM work — it's about the Librem 5's BMC board.
    Auto power-on after power loss, remote recovery when the device is off and
    unattended, and camera streaming to watch the screen. Do from the Linux
    workstation (it's the always-on box). Feed the camera-streaming and
    remote-recovery design into steps 16/17 — a device you can power-cycle and
    watch remotely is also a new remote-access surface worth auditing.

15. **Settings audit — Linux half** — `docs/HANDOFF-settings-audit.md`
    Run `wk backup`, diff `host/linux/config.dconf` (and `apt.txt`) against
    what's committed, verify `cmd/backup`'s junk filters (weather location,
    WiFi UUIDs, GTK last-folder path, Ptyxis UUIDs, timestamps) actually
    strip what they claim to, then ask the user about everything left before
    writing anything back — and confirm the `wk backup` → `./setup` round
    trip actually reproduces the state. Scheduled here, not earlier, because
    steps 1-14 (quiesce, session, GPU, Pi setup, etc.) are themselves a source
    of new non-default settings worth catching in this pass. End with the
    written summary of what's kept and why — that's the actual deliverable.

16. **Tailscale ACL audit** — `docs/HANDOFF-tailscale.md`
    Run from the workstation (it's the always-on tailnet node). Confirm
    Karen's ACL is scoped to immich/nextcloud/overleaf/proxmox only, your two
    accounts have full access, `wk` containers can reach rpi4/rpi5 and nothing
    else, and do the full port scan. Item 8 (MBP safe on public wifi) is the
    one piece of this that has to be *checked from* the macOS host — that's
    lane B's final step. Deliberately scheduled after everything above:
    steps 1-14 add new Tailscale-reachable surface (Pi devices, remote
    targets, the BMC), so auditing before they exist would mean redoing it.

17. **Sandbox / escape audit — holistic pass** —
    `docs/HANDOFF-sandboxing.md`
    Last, on purpose: audit the whole container setup for escapes (session
    D-Bus, host mounts, SUID-binary-to-bypass-auto-mode, credential/ssh-key
    search) with every feature above already built, so the audit sees the
    real final shape of the system — the remote-target driver, the
    cross-compile transfer path, the restored git helpers, the yocto
    image-upload path, and the BMC's remote-recovery/streaming surface all
    included. Add the git-push toggle (push allowed only inside the
    container, never from a bare `wk claude` on the host) as part of this
    pass, and retrofit it onto step 13's restored git helpers. Feed findings
    into lane B step 9, which re-runs the equivalent check against the VM
    model.

---

## Lane B — macOS host

1. **`wk` inside a macOS guest — DONE 2026-08-18** —
   `docs/HANDOFF-wk-in-workspace.md`
   Verified in `wk-mac-rel` with no `WK_IN_VM=1` and no podman error, plus the
   bash 3.2 parse of every file the change touches. Four fixes came out of it
   (job sizing in a guest, the default config for a bare `wk run`, the marker on
   boot as well as on sync, and `--dry-run`); the one item left over — build
   state recorded once per side — is written up there and blocks nothing.

2. **macOS MiniBrowser DerivedData + debugging — DONE 2026-08-18** —
   `docs/HANDOFF-mac-minibrowser.md`
   All three lines of the original spec. DerivedData is placed explicitly and
   separated per config, so `mac-release-asan` no longer builds into
   `mac-release`'s tree. MiniBrowser runs windowed on the guest's own desktop
   with hardware Metal, launched by `wk gui`. A debugger attaches three ways --
   `wk run --lldb` for jsc, `wk gui --lldb[ web]` for the browser and its web
   process, `wk test --layout --lldb <test>` for a layout test -- and needed no
   codesigning work, because the guest has SIP disabled and developer mode on.
   `wk test` against an Apple port ran green for the first time on the way.
   Debugging covers both processes on the test side — `--lldb ui` attaches
   WebKitTestRunner, which `--wrapper` cannot do, because the driver's stdin and
   stdout are the test protocol.

   The golden base was re-provisioned in the process, and it needed to be: it
   carried no egress block, no `~/.claude` links and no warmed autoinstall, so
   every new workspace would have failed at webkitpy exactly as before the F1/F2
   fix (F4, F6, F9). The rebuild also caught a bug this lane had introduced —
   `-derivedDataPath` is a flag, so it reached `build-imagediff`, which has no
   `-scheme`; every Apple build since had ended in `BUILD SUCCEEDED` followed by
   exit 64 and **no ImageDiff**, which every pixel and reftest comparison needs
   (A8). A cold build costs 85.8 min with `--export-compile-commands` on, which
   settles H3.

   One thing found on the way is fixed rather than left: the browser could
   reach nothing, because the guest's egress was environment variables and
   WebKit's network process does not read them — every egress check had used
   curl, which does (B11). Two remain, neither blocking: the guest is one macOS
   release behind (B9 — **parked by decision**: upstream publishes no
   `macos-tahoe-xcode:26.6`, re-checked 2026-08-18, and the only symptom is an
   `open -a` nothing uses), and Swift-interop debug info does not resolve in lldb under
   the compilation cache (C5). That one now has a measured answer rather than a
   theory: `WK_NO_COMPILATION_CACHE=1` clears it completely — 103 warnings to 0
   — and the cold build is *faster* without the cache, 68.6 min against 85.8,
   because a first build pays to write 11 GB of CAS and reads none of it back
   (C6). C++ debugging was unaffected either way.

3. **Cross-compile / remote-target verification on macOS** — companion to
   lane A steps 4-6. **The remote-target half is already done**, and done from
   this machine: lane A step 4 was built and verified on the macOS *host*
   (2026-08-19), which is the case that matters most here. What is left is the
   Tart-guest case — a macOS guest driving a remote target, where ssh has to
   cross the Softnet boundary as well — plus the cross-compile transfer path
   once lane A step 6 lands, and `docs/HANDOFF-other-remote.md`'s
   macOS-*as*-remote-target case, which is macOS-flavored by definition (the
   driver's `nice`/`ionice`, `/proc/*` probe and ccache path are the three
   places that assume Linux).

4. **Yocto builds — macOS half** — `docs/HANDOFF-yocto.md`
   After lane A step 8 lands the build-side work, confirm the same yocto flow
   works from a Tart VM (podman inside the VM), and that image transfer to the
   host for SD-card flashing (`docs/HANDOFF-sdcard.md`) works from macOS too.

5. **Profiling tooling — macOS MiniBrowser half** —
   `docs/HANDOFF-original-helpers.md` (profiling section)
   Once lane A step 10 has the samply/sysprof/heaptrack/JIT-dump plumbing
   built for cli/GTK/WPE, extend it to macOS MiniBrowser specifically — that's
   the one target in that list that only exists on this machine.

6. **Fixed-core-count benchmarking, macOS confirmation** —
   companion to lane A step 11 (`docs/HANDOFF-memory.md`)
   Confirm the memory-chart collection and fixed-core benchmarking work
   correctly on the Tart VM once lane A has built the mechanism — core pinning
   in a VM is not the same primitive as a container `--cpuset`, so this needs
   an independent check, not just a re-run.

7. **MBP public-wifi safety check** — the one item from
   `docs/HANDOFF-tailscale.md` (#8) that has to run on this machine: confirm
   the MBP is safe to use on public/untrusted wifi. Run this alongside lane A
   step 16's audit (not before it) so both halves of the Tailscale review land
   together.

8. **Settings audit — macOS half** — `docs/HANDOFF-settings-audit.md`
   Run `wk backup`, diff `host/macos/defaults.conf` against what's committed,
   and separately scan for non-default, plausibly-deliberate preference
   domains that aren't in `defaults.conf` at all yet — `cmd/backup` only
   refreshes values for keys already listed there, it doesn't look for new
   ones. Same treatment for `symbolichotkeys.plist`, `softnet.sh`,
   `vmtools.sh`, `mcp.sh`, `tools.sh`, `playbook.yaml`. Ask the user about
   every candidate before writing anything, confirm the `wk backup` →
   `./setup` round trip, and end with the written summary of what's kept and
   why. Scheduled here, after steps 1-7, for the same reason as the Linux
   half: earlier steps in this lane are themselves a source of new
   non-default settings.

9. **Sandbox audit — macOS-specific escape surface, holistic pass** —
   companion to lane A step 17 (`docs/HANDOFF-sandboxing.md`), and likewise
   scheduled last. The Linux audit found host-mount and D-Bus escapes that
   were invisible on macOS "because the VM's session has nothing in it" per
   `docs/HANDOFF-linux.md` — that's a reason to double check, not a reason to
   skip. Re-run the same escape checklist (host filesystem reachability,
   credential/SSH-key search, suid-binary bypass of auto mode) against the
   Tart VM model once lane A's findings are in — the VM's isolation properties
   are different in kind (a real VM boundary vs. a container namespace) and
   may hide different bugs. Also verify the git-push toggle lane A adds in
   step 17 works the same way here.

---

## Either machine / process items (no meaningful conflict)

Standing rule, not a task: every task above gets a line item in
`docs/TESTING.md` as it is picked up — apply it inline.

- **`docs/HANDOFF-workspace-state.md` — new 2026-08-19, and not filler: pick
  it up before anything else that creates or gates on workspaces. It opens
  with "The rules" — no caching of facts, crash-only convergence, wipe over
  repair, one lock per resource, clobber detection, read-only reports — and
  every other doc, comment and command defers to them; read them first.** Workspace
  lifecycle state (`absent | creating | present`, evidence-at-the-artifact),
  readiness gating for `wk build` / `--zed` / the babysitter, staged resumable
  creation, and one detach primitive — all in service of its **core
  requirement: a `wk status` that stays reliable and clear across wk-tools
  updates, OS updates, network drops and device corruption, and never asserts
  what it did not just verify.** Grew out of the 2026-08-19 features
  (`wk build --babysit`/`--branch`, `wk new --zed`, headless `wk claude`),
  which are landed and listed there; the readiness/resumability work is the
  open half. Machine-agnostic; whichever lane is idle first.

- **`docs/HANDOFF-mac-perf-mode.md` — new 2026-08-20.** The one part of the
  macOS benchmark lane that software cannot do: making a second macOS install
  this Mac can boot, provisioning it, and booting it. Everything either side of
  it is built and exercised; this is a disk, half an hour at the keyboard, and
  a list. Machine-specific by definition -- it is this Mac.

- **`docs/HANDOFF-test-runner.md` — started 2026-08-19.** `wk selftest` exists:
  `--quick` is 13 hermetic checks (no workspace, no podman, no ssh), a bare run
  adds the remote section, and every check names the `docs/TESTING.md` line it
  implements so a reworded line reports DRIFT rather than passing quietly. It
  starts nothing and prints its own coverage — 15 of 198 line items. What is
  left is coverage rather than machinery: the container and vm sections need a
  workspace and a guest, and the plan still mixes automatable lines with ones
  that need a human. Three defects fell out of writing it, including
  `wk selftest` itself forwarding into the podman VM and being answered by a
  stale copy of wk-tools.

Genuine filler, in no order:

- **`docs/HANDOFF-claude.md`** — rewrite `CLAUDE.md` and the skills so every
  instruction is executable *from inside a workspace*. Gated on the blocking
  wk-in-workspace item above; carries the concrete defect list from the
  2026-08 audit (host-side sudo steps, retired-machine paths, contradictory
  build guidance, the missing uclamp fallback).
- **`docs/HANDOFF-benchmarking.md`** — **no longer filler as of 2026-08-19: it
  is position 1 in lane A's revised running order**, right behind netboot. See
  "Order, revised 2026-08-19" at the top of lane A. Its netboot half is split
  out into `docs/HANDOFF-netboot.md`, and it no longer waits on step 7's
  image-flashing flow — netboot needs no media. The rpi5 role conflict was
  already resolved (recorded in lane A step 2 and in the file itself).
- **`docs/HANDOFF-bench-python.md`** — rewrite `cmd/bench` in Python; it has
  outgrown bash (four inline Python heredocs, JSON via 16 env vars, a
  hand-rolled plan parser).
- **`docs/HANDOFF-code-server.md`** — remote dev via code-server plus a web
  UI for container lifecycle. Two or three separate tasks bundled; split
  before picking up.
- **`docs/HANDOFF-helix.md`** — helix config approaching zed parity
  (multi-file git review in particular), for rpi5 development where zed is
  too slow.
- **`docs/HANDOFF-claude-analysis.md`** — mine local Claude transcripts for
  automation candidates; runs per device, one machine at a time.

---

## Final step — after literally everything else above

**Architecture review and upstreaming pass** — `docs/HANDOFF-architecture-review.md`

Do not start this until both lanes (including their settings and sandbox
audits) and the filler items above are all done. With the whole system built
out, look holistically across both lanes for:

1. Places where the two platforms (or two features) grew similar-but-separate
   implementations that should become one shared abstraction, so a future fix
   lands once instead of twice — the target-driver contract
   (`targets/container.sh`/`remote.sh`/`vm.sh`), the platform-branched
   `cmd/backup`/settings-audit logic, and the various "get a file out of an
   isolated workspace" paths (proxy, cross-compile transfer, SD-card flashing)
   are the ones already visible from this pass; there will be more once
   everything above is actually built.
2. What's generically useful enough to give back rather than keep as a local
   patch — candidates already visible: the rootless-podman egress-proxy
   design, the carried `webkit-container-sdk` patches (`--isolated`,
   `--unsafe-caps` gating), the RPi5 NUMA kernel work's Path A (Launchpad
   request for `CONFIG_NUMA_EMU`), and `gpr`/the profiling wrappers as a
   WebKit contributor toolkit.

This is a review with a written decision per candidate, not a mandate to
execute all of them — see the doc for the full list and what "done" means.

## Fixed / resolved since the individual handoffs were written

- **The privacy scrub is closed, 2026-08-19, by user decision.** The repo stays
  public and the internal hostnames and RFC1918 addresses in it are accepted as
  published; no scrub of HEAD, and no history rewrite.
  `docs/HANDOFF-privacy-scrub.md` is now the record of that decision rather
  than a task. The line that still matters is unchanged: no credential, key or
  token in the tree.
- **`docs/HANDOFF-sdcard.md` was an empty placeholder** — now scoped (see
  lane A step 7) rather than an orphan zero-byte file.
- **The macOS-proxy unification is done.** `docs/HANDOFF-macos-proxy.md` was
  never written and the underlying work (collapsing the Linux
  `--network none`-plus-proxy model and the macOS Softnet boundary into one
  allowlist-by-hostname design) is complete; every reference to the missing
  file (HANDOFF-linux.md, README.md, targets/container.sh,
  host/macos/vmtools.sh) is gone.
- **2026-08-18 review pass.** Fixed in code, no task remains: the proxy's
  self-blocking of Pi tailnet addresses; `wk vm start`/`wk claude` now fail
  closed when Softnet is missing; `wk enter` with no command; `wk pi`'s
  unbound `WK_STORE` crash and its dead nftables branch; `wk verify`/`wk
  bench` ignoring the target registry; unquoted argument splices in
  claude/gui/test/remote-exec and the forwarded `WK_CONFIG`; `wk setup`
  advertised but not dispatchable; `wk gc` now prunes stale bench payload
  seeds, reports the growth dirs it keeps, and no longer password-prompts for
  fstrim; `wk skills pull` no longer silently overwrites uncommitted repo
  edits; host and workspace Claude configs are split (the host no longer gets
  `Bash(*)` or the "you are in a sandbox" briefing), and macOS guests now get
  a Claude config at all; sdk-patches verify covers every security-relevant
  section; the tart install pointer named the wrong GitHub org.
- **Git and GitHub helpers separated out** into their own document,
  `docs/HANDOFF-git-tools.md` (see lane A step 13) — they shared no tooling
  or machine constraint with the profiling/benchmarking/wasm material in
  `docs/HANDOFF-original-helpers.md`, and consolidating them into one place
  made it possible to also note that `report` needs no action (`wk report`
  already covers it) and that `git-clean`/`commit-count` aren't worth merging
  into anything — they solve unrelated problems.
