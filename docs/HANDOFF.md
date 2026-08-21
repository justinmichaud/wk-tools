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

One standing hazard, learned the hard way and not owned by any one step:
**editing wk-tools while a `wk` command is running corrupts that run.** bash
reads a script incrementally, so rewriting `cmd/build` under a 23-minute build
resumed the process mid-word and dropped it back into the lock wait. The same
applies with more force to the copy on a build machine, which `t_sync_tools`
replaces at the start of every build. Nothing in the tooling prevents it today.

---

## Done — the ledger

Newest first. Each entry is a pointer: the detail, the defects and the
verification live in the named handoff and in `docs/TESTING.md`, not here.

- **2026-08-21, Linux — cattle, not pets** (`docs/HANDOFF-cattle.md`, the
  rule and the per-machine ledger): the fleet registry became config —
  `boot/machines/<name>.conf`, each opening with its device's from-nothing
  recipe, with the `MACH_OS` field the vocabulary asked for (`wk boot mbp`
  from Linux now refuses by the registry's word instead of probing the wrong
  computer) — closing the vocabulary lifecycle's registry item; `wk doctor`
  gained the machine-local-state section (everything a rebuild cannot get
  from this repo, declared as regenerable / re-authable / backed-up); both
  are in `wk selftest --quick`. The remaining gaps are named in the ledger:
  `flash --reader`, `provision`/`unprovision`, the settings audits, a backup
  story for bench results, the BMC's own config, and the home layer.
- **2026-08-21, Linux — netboot removed outright, and the fleet made visible.**
  The removal is recorded under Fixed/resolved below. Built with it:
  `wk status` ends with the fleet block (per device: role, mode, the media wk
  owns and what is on it — the rpi4's stick and its system, armed/disarmed;
  the Mac's bench volume attached-or-missing — parallel read-only probes, one
  subshell per machine, honest "unknown from here" for machines only a Mac
  can probe), fed by new `b_media`/`b_probeable` driver hooks; and
  `wk help hardware`, the fleet's physical shape in prose. The rpi3 gained
  its hands-on local-SD stub driver, and `wk pi boot-order` shrank to
  `usb-first|local`.
- **2026-08-20, Linux — the vocabulary rename landed end to end**
  (`docs/HANDOFF-vocabulary.md`, now the record of what the words mean).
  `wk image` → `wk sysimage`; `wk boot`/`wk serve` take `--system`; profiles
  renamed to carry their category (`perf-linux-rpi5`,
  `downstream-yocto-wpe-2.48-rpi4`, …) with the old spellings refused by name;
  `role=bench-device`, `mode=host|bench` (`MODE`/`MODE_CHANNEL` replaced
  `ROLE`), `in_bench_mode`. Store manifests and the `image/<profile>/` spec
  dirs migrated; `wk boot rpi5 --dry-run` verified against the real board;
  selftest green. Still missing from the lifecycle: `wk sysimage flash
  --reader`, `wk provision`/`wk unprovision`, and the device registry as
  config — see the vocabulary handoff's [MISSING] items.
- **2026-08-20, Linux — every doc audited against the code.** Finished work
  collapsed to dated records, stale claims fixed (egress-allowlist wording,
  Softnet hedges, removed job ceiling, arming-model table), and the new
  vocabulary threaded through docs and tool alike. Three real defects found by
  the audit and fixed: `cmd/serve` defaulted to a renamed profile,
  `remote/provision.sh` still required the abandoned `flock`, and the renamed
  profiles had orphaned their `image/<profile>/config.txt.append` spec dirs.
- **2026-08-20, Linux — yocto build-side done** (`docs/HANDOFF-yocto.md`):
  the full `downstream-yocto-wpe-2.48-rpi4` build completed and imported
  (store id `rpi4-wpe-2.48-20260820T124927Z`). Fleet integration
  (`install_fleet_integration`) is shared by both builders. What remains is
  hardware: the wic image bakes an SD-card root, so writing it to the rpi4's
  USB stick is refused by `image_check_root` — flashing and booting it is the
  open half, plus the rpi3 targets and lane B's macOS half.
- **2026-08-20, macOS — locking, consistency, and the macOS benchmark lane.**
  The lock is a symlink whose target names the holder (three defects with the
  mkdir form, two found by writing the test); one EXIT trap with registered
  handlers (`wk_atexit`); `flock` gone everywhere (`lib/lockrun.sh` on build
  machines); `ws_busy_reason` covers the half a lock cannot. `wk profile`
  shipped (`docs/HANDOFF-profile.md`). The macOS benchmark lane —
  `wk bench stage`/`staged`, the `hands-on` and guest arming models — was run
  **end to end on real builds between two guests**: `BENCH OK`, a record
  carrying `bench_host=image`, eleven defects found and fixed on the way,
  every one in `docs/TESTING.md`. A benchmark runs in bench mode or it does
  not run, and `--force` does not open that. What is left for the real machine
  is a volume and two clicks: `docs/HANDOFF-mac-perf-mode.md`.
  `wk quiesce` on macOS measures instead of trusting. (Details:
  `docs/HANDOFF-benchmarking.md` "The macOS shape",
  `docs/HANDOFF-workspace-state.md` for the locks.)
- **2026-08-19 — a batch of reported defects**, every one recorded in
  `docs/TESTING.md` and its owning handoff: the shared target registry
  (`targets/hosts/<name>.conf`, personal overrides beside it), peer
  workstations (`WK_REMOTE_PEER`), one concurrent probe round for all targets,
  `wk sync --target <machine>` (and `--target container` for the podman VM's
  tools copy), the `wk sudo` last-match fix, one spelling for the in-workspace
  interface, `wk build --dry-run`/`--env`, layout tests against a remote
  target's own build, `wk claude` in auto mode on remote targets, one detach
  primitive, `wk remotes [--fix]` (which also found the missing `wpe`
  upstream), and the push switch proven end to end from a container —
  registering the build machines' deploy keys on GitHub is the one piece left,
  and it is the user's.
- **2026-08-19 — the git-push switch** (`wk push on|off|status`), built out of
  order by request, with the remote wiring unified (`wk_wiring_script`) and
  `wk <command> --explain`. `docs/HANDOFF-sandboxing.md` records the
  mechanism; the audit it belongs to is still scheduled last.
- **2026-08-18 — `wk` inside a workspace**, both halves
  (`docs/HANDOFF-wk-in-workspace.md`): the in-workspace `wk build`/`run`/
  `test` interface exists on Linux containers and macOS guests. One leftover
  recorded there (build state written once per side), blocking nothing.
- **2026-08-18/19 — netboot substrate and the rpi5** (`docs/HANDOFF-netboot.md`,
  its "state" section is the authority): `wk sysimage`, `wk boot` with five
  drivers, `wk serve`, `wk pi boot-order`; the rpi5 proven on hardware
  (arm → bench system reachable in 53 s → claim → hand back), the rpi4 lane
  rescoped 2026-08-20 to its USB stick. (Netboot was kept for the profiling
  lane at the time; that idea fell with the netboot root on 2026-08-21 —
  see the ledger entry above.)

---

## Lane A — Linux workstation

**The numbered items are stable identifiers, not the running order.** Netboot
(step N) ran first, at the user's direction, and is done as far as the
substrate goes; the revised order behind it was 1 (benchmarking on the bench
systems), then ⑥ cross-compile, then ⑧ yocto, then ② pi provisioning — with
⑧'s build side now landed (see the ledger) and its hardware half open.

Hardware state, updated 2026-08-20: the rpi5 is up on the tailnet and
reachable (its stick was verified over ssh by a `wk boot --dry-run` today);
the rpi4 answers on the LAN and its EEPROM has been read and written
(`docs/HANDOFF-netboot.md`); the rpi3 is unprovisioned and off; moose is the
machine this lane runs on.

  N. **Boot a system — the shared substrate** — `docs/HANDOFF-netboot.md`.
     Substrate built and proven (see the ledger). Netboot is gone outright
     (2026-08-21): every bench lane boots local media, `wk serve` and its
     daemon are removed (the boot-file resolver they carried now guards
     `wk sysimage write`), and the rpi3's lane is its SD card (hands-on stub
     driver until provisioned). Open there: moose's bench mode (BMC virtual
     media, the one route left) and provisioning the rpi3.
  1. **Benchmarking on the bench systems** — `docs/HANDOFF-benchmarking.md`.
     `bench_host=image` runs driven remotely; the image runner still has to
     record `kernel_provenance`, `kernel_arch`, `profile` (stock/oc) and
     `root_device` — none of those fields exists in `cmd/bench` yet.
  2. **Raspberry Pi provisioning** — `docs/HANDOFF-linux-pi.md`. Runs *last*
     of the re-ordered items, against the rpi4's yocto system. rpi4/rpi3 only:
     the rpi5 is a workstation (decided 2026-08-18, recorded in
     `docs/HANDOFF-benchmarking.md`), so it never goes through `wk pi setup`.
  3. **RPi5 tuning re-apply** — `host/linux/rpi5/HANDOFF.md`. The *stability*
     half (fan, WiFi, fstab, the NUMA kernel) stays on the installed OS; the
     *perf* half is baked into the bench systems (governor and swap-off
     already are; the overclock/v3d half has not moved yet — see that file's
     2026-08-20 update).
  4. **Remote target — DONE 2026-08-19** — `docs/HANDOFF-linux-remote.md`
     holds the record and the still-open leftovers (`wk gui`/`wk gc` on remote
     targets, delegated-output interleave).
  5. **Windows/macOS/cloud remotes** — `docs/HANDOFF-other-remote.md`: the
     same driver contract for a different OS/provider; the three Linux
     assumptions in `targets/remote.sh` are named there. Also the PII-free
     perf-testing case.
  6. **Cross-compile targets** — `docs/HANDOFF-cross-compile.md`. Test target:
     the rpi5's `perf-linux-rpi5` bench system (a slim distro with no SDK —
     exactly the condition a sysroot cross build must satisfy). Then
     `docs/HANDOFF-pi-deploy.md`, which consumes the transfer path.
  7. **`wk sysimage flash --reader`** — `docs/HANDOFF-sdcard.md`, aligned with
     the vocabulary lifecycle's item 1: the one write path that cannot exist
     yet, a card reader on *this* workstation. Interactive sudo, no NOPASSWD.
  8. **Yocto — hardware half** — `docs/HANDOFF-yocto.md`. Build side done (the
     ledger); open: flash and boot the built system on the rpi4, the rpi3
     targets in both widths, and the `meta-wk` pseudo question.
  9. **Linux MiniBrowser: debugging + graphical run** —
     `docs/HANDOFF-linux-minibrower.md` together with `docs/HANDOFF-debug.md`
     (`wk debug`, `wk run --until-crash` — verified still unbuilt).
  10. **Profiling — workspace half** — `docs/HANDOFF-profile.md`. `wk profile`
      is built; open: samply/sysprof-cli/heaptrack in the workspace image, the
      profile-viewing path through the egress allowlist, and the workspace
      answer to `perf_event_paranoid` (the bench systems already set `-1`).
  11. **Memory charting** — `docs/HANDOFF-memory.md` (`wk bench mem`;
      fixed-core running for every target — `wk bench` records `cores` but
      does not pin them yet).
  12. **PGO profile build support** — `docs/HANDOFF-original-helpers.md`.
  13. **Git and GitHub helpers** — `docs/HANDOFF-git-tools.md`: `wk pr` is
      restored; `git-sync-fork` and the small helpers remain.
  14. **Tailnet bridges — hardware half** — `docs/HANDOFF-bmc.md`. The
      software shipped 2026-08-20 (`wk bridge`, `bridge/`, camera streaming,
      the watchdogs; TESTING.md §8); open is flashing the PinePhone
      (`tailnet-bridge-generic`), re-flashing the librem5
      (`tailnet-bridge-moose-bmc`), and `docs/HANDOFF-bmc-battery.md`. Feed
      the new remote-access surface into steps 16/17.
  15. **Settings audit — Linux half** — `docs/HANDOFF-settings-audit.md`.
  16. **Tailscale ACL audit** — `docs/HANDOFF-tailscale.md`. Item 8 (MBP on
      public wifi) is lane B's final step, run alongside this.
  17. **Sandbox / escape audit — holistic pass** —
      `docs/HANDOFF-sandboxing.md`, last on purpose; the incident list and the
      done/open boundary are recorded there.

---

## Lane B — macOS host

1. **`wk` inside a macOS guest — DONE 2026-08-18** (the ledger).
2. **macOS MiniBrowser DerivedData + debugging — DONE 2026-08-18** —
   `docs/HANDOFF-mac-minibrowser.md` holds the decisions (DerivedData
   placement, the compilation-cache findings, the lldb attach recipes) and the
   two open items (A9, B9-parked).
3. **Cross-compile / remote-target verification on macOS** — the remote-target
   half is done (driven from this machine, 2026-08-19). Left: the Tart-guest
   case (ssh across the Softnet boundary), the cross-compile transfer once
   lane A step 6 lands, and macOS-*as*-remote-target
   (`docs/HANDOFF-other-remote.md`).
4. **Yocto — macOS half** — `docs/HANDOFF-yocto.md`: confirm the flow from a
   Tart VM and image transfer to the host for flashing. Unblocked now that
   lane A's build side landed.
5. **Profiling — macOS MiniBrowser half** — extend lane A step 10's
   provisioning to the one target that only exists on this machine.
6. **Fixed-core-count benchmarking, macOS confirmation** — companion to lane A
   step 11; core pinning in a Tart VM is a different primitive than a podman
   `--cpuset`, so it needs an independent check.
7. **MBP public-wifi safety check** — `docs/HANDOFF-tailscale.md` item 8,
   alongside lane A step 16.
8. **Settings audit — macOS half** — `docs/HANDOFF-settings-audit.md`.
9. **Sandbox audit — macOS-specific escape surface** — companion to lane A
   step 17, against the Tart VM model; also verify the push switch behaves
   the same here. One input is already recorded: the golden base provisions
   with egress unfiltered (`docs/HANDOFF-mac-minibrowser.md`, B11 note).

---

## Either machine / process items

Three standing rules, not tasks, applied inline as work lands: every task
above gets a line item in `docs/TESTING.md` as it is picked up; tools
stranded by a workflow change are removed in the change that strands them;
and **cattle, not pets** (`docs/HANDOFF-cattle.md`) — every machine is
reproducible from this repo plus declared restorables, new machine-local
state goes into `wk doctor`'s machine-local section or it is a bug, and new
devices arrive as config, never code.

- **`docs/HANDOFF-workspace-state.md`** — "The rules" at its top govern
  everything that creates or gates on workspaces; read them first. Phases 1
  and 2 are done and recorded there; its "still open" list (babysit end to
  end, creation bookkeeping in `wk gc`, the `$TMPDIR` quiesce hole, the
  headless path split, `.unfiltered`, `cmd/bench` reading `state=running`,
  ccache max_size writers) is live.
- **`docs/HANDOFF-mac-perf-mode.md`** — the one part of the macOS benchmark
  lane software cannot do: a second macOS install, a disk, half an hour at the
  keyboard. Machine-specific by definition.
- **`docs/HANDOFF-test-runner.md`** — the machinery exists and the printed
  coverage line is the authority; what is left is coverage (the container and
  vm sections need a workspace and a guest).

Genuine filler, in no order:

- **Task orientation** — separate daily webkit tasks, perf tasks, one-time
  setup and ongoing maintenance, and make each command orient the user.
  `wk boot --status` leads with role and mode; `wk status` and `wk help`
  should orient the same way, everywhere a session starts.
- **Maximum build perf as a guarantee** — thought through end to end rather
  than a habit of flags.

- **`docs/HANDOFF-claude.md`** — no longer gated (the in-workspace interface
  landed 2026-08-18): rewrite `CLAUDE.md` and the skills so every instruction
  is executable from inside a workspace; the defect list is current as of
  2026-08-20.
- **`docs/HANDOFF-bench-python.md`** — rewrite `cmd/bench` in Python; the
  counts that justify it are refreshed there (≈1650 lines, eight heredocs,
  thirty-two `WK_M_*` variables).
- **`docs/HANDOFF-code-server.md`** — split before picking up.
- **`docs/HANDOFF-helix.md`** — helix config approaching zed parity.
- **`docs/HANDOFF-claude-analysis.md`** — mine local transcripts for
  automation candidates; one machine at a time.
- **`docs/HANDOFF-git-perf.md`** — keep WebKitBuild outside the repo so git
  and zed stay fast; not started.
- **`docs/HANDOFF-router.md`**, **`docs/HANDOFF-clangd.md`** — small, scoped,
  unstarted.

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
   isolated workspace" paths (proxy, cross-compile transfer, card flashing)
   are the ones already visible; there will be more once everything above is
   actually built.
2. What's generically useful enough to give back rather than keep as a local
   patch — candidates already visible: the rootless-podman egress-proxy
   design, the carried `webkit-container-sdk` patches (`--isolated`,
   `--unsafe-caps` gating), the RPi5 NUMA kernel work's Path A (Launchpad
   request for `CONFIG_NUMA_EMU`), and `gpr`/the profiling wrappers as a
   WebKit contributor toolkit.
3. **Enforcing the layering decided in `docs/HANDOFF-generalizing.md`**
   (2026-08-20): the `home`/`lab`/`wk`/`field`/`stock` decomposition and its
   one-way dependency rule — directory moves, a grep selftest that the lab
   layer knows no WebKit, and the driver-contract conformance test item 1
   already lists. The rule binds new code from the decision date; this step
   is where existing code catches up. Two vestigial macOS-lane items are
   already marked in place for it: the podman VM playbook's bridge and
   nftables tasks (`host/macos/playbook.yaml`), removable only on the machine
   that can prove the round trip.

This is a review with a written decision per candidate, not a mandate to
execute all of them — see the doc for the full list and what "done" means.

## Fixed / resolved since the individual handoffs were written

- **Netboot is removed, 2026-08-21, by user decision, in two same-day steps**
  — first the root (no device will ever netboot for a run: every bench lane
  boots local media), then `wk serve` itself: dead code is worse than a
  re-implementation if the need ever returns. Gone: `cmd/serve`,
  `boot/wk-tftpd.py` and its root-owned port-69 helper (`./setup` removes
  the installed copy and its sudoers grant), `boot/pi-netboot.sh`, the
  netboot EEPROM orders, the NFS/RAM root phases, the serving election, and
  moose's UEFI HTTP/PXE route. Kept: the boot-file resolver, moved into
  `boot/check-boot-files.py`, where `wk sysimage write` runs it before any
  disk is touched. `docs/HANDOFF-netboot.md` carries the record.
- **The generalization design is decided, 2026-08-20, by user decision** —
  `docs/HANDOFF-generalizing.md` went from brainstorm to a researched,
  decided proposal in one pass: the three-layer decomposition
  (`home` / `lab` / `wk` / `field` / `stock`, recorded in
  `docs/HANDOFF-vocabulary.md`), the one-way dependency rule (binding for new
  code now; existing code catches up in the architecture review, item 3
  there), SandboxKit as a profile of the lab layer rather than a codebase,
  and the Galician naming idea scrapped entirely. No CLI is minted until a
  layer has a second consumer.
- **The privacy scrub is closed, 2026-08-19, by user decision.** The repo stays
  public and the internal addressing in it is accepted as published; no scrub
  of HEAD, and no history rewrite (so removing an item from HEAD later would
  not un-publish it). The inventory that was weighed, kept here so it is not
  re-discovered and re-escalated: `dotfiles/ssh/config` (internal hostnames
  and RFC1918 addresses — reaching them requires already being on the network,
  and that boundary is the Tailscale audit's); the rpi5 WiFi identity (out of
  HEAD since 2026-08-18 via the gitignored `rpi5.conf`, still in history);
  and machine/service names throughout the docs, where the names are the
  point. None of it is a credential. The line that still matters: no
  credential, key or token in the tree — a future addition that crosses it is
  a bug regardless of this decision. Machine-local values keep going in
  gitignored per-machine conf files because they are per-machine, not because
  they are secret.
- **`docs/HANDOFF-sdcard.md` was an empty placeholder** — now scoped (lane A
  step 7).
- **The macOS-proxy unification is done.** The rootful-podman + nftables model
  is gone on both hosts (one allowlist-by-hostname proxy design;
  `targets/container.sh`, `WK_SANDBOX`); no reference to the never-written
  HANDOFF-macos-proxy.md remains outside this note.
- **2026-08-18 review pass** — a batch of fixes with no remaining task; the
  durable ones are recorded in their owning handoffs and `docs/TESTING.md`.
- **Git and GitHub helpers separated out** into `docs/HANDOFF-git-tools.md`
  (lane A step 13); `report` needs no action (`wk report` covers it), and
  `git-clean`/`commit-count` solve unrelated problems.
