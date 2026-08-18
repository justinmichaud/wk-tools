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

## Lane A — Linux workstation

1. **32-bit containers (`wk new --arch 32`)** — `docs/HANDOFF-linux-arm32.md`
   Linux-only, permanently: Apple Silicon has no AArch32 at EL0. Plumb `--arch`
   through `cmd/new` → `targets/container.sh` → `wkdev-create`, add a 32-bit
   build config, watch the two traps already documented (nvidia CDI conflicts
   with `--arch`; check the arm32 image ships python3 for the proxy bridge).

2. **Raspberry Pi provisioning** — `docs/HANDOFF-linux-pi.md`
   Linux-only (macOS workspaces can't reach the Pis at all). Run
   `wk pi setup rpi5` / `rpi4` against the live devices, confirm `pi-hosts`
   populates and the proxy allowlist works, and confirm the *negative* (an
   address not in the file is refused).

3. **RPi5 tuning re-apply** — `host/linux/rpi5/HANDOFF.md`
   Separate machine, reached over SSH from the Linux workstation once step 2
   is done. Re-run `rpi5-setup.sh` → reboot → `rpi5-verify.sh` →
   `rpi5-stress.sh`, re-check the paths/fstab/indexer notes for the 26.04
   re-install, confirm the GPU at v3d=1200 with a sustained load. NUMA (Path B)
   is already done; Path A (upstreaming `CONFIG_NUMA_EMU`) is optional
   follow-up.

4. **Remote target** — `docs/HANDOFF-linux-remote.md`
   `targets/remote.sh` has never been run. Write a real
   `~/.config/wk/targets/<name>.conf`, run the driver contract end to end
   (`t_create`/`t_exec`/`t_info`/`t_list`/`t_destroy`), decide what `wk sync`
   means remotely, and fix the `t_exec` `$*`-interpolation quoting bug before
   it gets a second caller. `wk status`/`wk logs` need a decision about
   local-vs-remote status files.

5. **`docs/HANDOFF-other-remote.md`** (Windows/macOS remote targets, rentable
   cloud VMs without containers/virtualization, for PII-free perf testing) —
   pick up right after step 4 lands, since it's the same driver contract for a
   different OS/provider.

6. **Cross-compile targets** — `docs/HANDOFF-cross-compile.md`
   Wire up the `webkit-container-sdk` cross-build workspace + the rpi5/remote
   transfer helper; confirm debugging and perf testing both work against a
   cross-compiled binary. Natural follow-on to steps 2 and 4 (rpi5 as a
   target, and the "get a binary onto another machine" plumbing).

7. **SD-card image flashing** — `docs/HANDOFF-sdcard.md`
   Generic copy-image-out-of-workspace + flash-to-card flow (was an empty
   placeholder file; now scoped). Do this before yocto (step 8) so yocto can
   consume it instead of building its own copy-to-host path.

8. **Yocto builds — Linux half** — `docs/HANDOFF-yocto.md`
   Do the build-side work here first (yocto tooling is native to Linux); cache
   must survive workspace destruction, and image upload to a target should
   reuse step 7's flashing flow. The *macOS* half — confirming the same flow
   works from a Tart VM — is lane B step 4, after this lands. Also: get
   Tailscale installed on the rpi3 target image itself.

9. **Linux MiniBrowser: debugging + graphical run** —
   `docs/HANDOFF-linux-minibrower.md`
   Support running WPE MiniBrowser graphically (interactive) and attaching a
   debugger, including a layout test. Make sure prewarm processes, PSON, and
   site isolation don't get in the way. Reference:
   `Debugging-WPE-Linux-(desktop)` wiki page.

10. **Profiling tooling — Linux half** — `docs/HANDOFF-original-helpers.md`
    (profiling section only — the wasm section in that file is
    platform-agnostic filler; the git-tools table has moved to its own
    document, see step 13). Build samply, sysprof-cli, and heaptrack support
    from source, wired for cli jsc, GTK, and WPE builds, plus JIT-dump support
    (extra JSC flags + the sysprof patch) and the JSC sampling profiler.
    Restore `strip-addresses` and `show-profiled-functions` into
    `container/bin/`. The macOS-MiniBrowser half of profiling is lane B step 5.

11. **Memory charting** — `docs/HANDOFF-memory.md`
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
    `docs/HANDOFF-original-helpers.md`). Restore `gpr` first (the one with
    real logic — checks out a PR branch, confirms every git command, diffs
    before offering `reset --hard`), then `git-sync-fork`; `git-clean` and
    `commit-count` are trivial one-liners, lowest priority. Machine-agnostic —
    listed here only because Lane A has more slack after step 12; fine to pick
    up on macOS instead if that lane is idle first. Note the dependency on
    step 15's git-push toggle before wiring these to actually push anything.

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
    into lane B step 8, which re-runs the equivalent check against the VM
    model.

---

## Lane B — macOS host

1. **macOS MiniBrowser DerivedData + debugging** —
   `docs/HANDOFF-mac-minibrowser.md`
   Fix the DerivedData location so it's fast and doesn't collide with other
   builds — this is the correctness prerequisite for everything else on this
   lane (a slow/colliding DerivedData will look like flakiness in every later
   step). Then: graphical MiniBrowser run, attach a debugger, debug a layout
   test.

2. **Cross-compile / remote-target verification on macOS** — companion to
   lane A steps 4-6. Once the Linux side has the remote-target driver and
   cross-compile transfer path working, confirm the same flows work when
   driven from a macOS Tart VM (in particular `docs/HANDOFF-other-remote.md`'s
   macOS-remote-target case, which is macOS-flavored by definition).

3. **Yocto builds — macOS half** — `docs/HANDOFF-yocto.md`
   After lane A step 8 lands the build-side work, confirm the same yocto flow
   works from a Tart VM (podman inside the VM), and that image transfer to the
   host for SD-card flashing (`docs/HANDOFF-sdcard.md`) works from macOS too.

4. **Profiling tooling — macOS MiniBrowser half** —
   `docs/HANDOFF-original-helpers.md` (profiling section)
   Once lane A step 10 has the samply/sysprof/heaptrack/JIT-dump plumbing
   built for cli/GTK/WPE, extend it to macOS MiniBrowser specifically — that's
   the one target in that list that only exists on this machine.

5. **Fixed-core-count benchmarking, macOS confirmation** —
   companion to lane A step 11 (`docs/HANDOFF-memory.md`)
   Confirm the memory-chart collection and fixed-core benchmarking work
   correctly on the Tart VM once lane A has built the mechanism — core pinning
   in a VM is not the same primitive as a container `--cpuset`, so this needs
   an independent check, not just a re-run.

6. **MBP public-wifi safety check** — the one item from
   `docs/HANDOFF-tailscale.md` (#8) that has to run on this machine: confirm
   the MBP is safe to use on public/untrusted wifi. Run this alongside lane A
   step 16's audit (not before it) so both halves of the Tailscale review land
   together.

7. **Settings audit — macOS half** — `docs/HANDOFF-settings-audit.md`
   Run `wk backup`, diff `host/macos/defaults.conf` against what's committed,
   and separately scan for non-default, plausibly-deliberate preference
   domains that aren't in `defaults.conf` at all yet — `cmd/backup` only
   refreshes values for keys already listed there, it doesn't look for new
   ones. Same treatment for `symbolichotkeys.plist`, `softnet.sh`,
   `vmtools.sh`, `mcp.sh`, `tools.sh`, `playbook.yaml`. Ask the user about
   every candidate before writing anything, confirm the `wk backup` →
   `./setup` round trip, and end with the written summary of what's kept and
   why. Scheduled here, after steps 1-6, for the same reason as the Linux
   half: earlier steps in this lane are themselves a source of new
   non-default settings.

8. **Sandbox audit — macOS-specific escape surface, holistic pass** —
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

## Blocking, both lanes — `wk` does not work inside a workspace

**`docs/HANDOFF-wk-in-workspace.md`** — found 2026-08-18. Claude only ever runs
inside a workspace, so the in-workspace `wk build <config>` / `wk run` /
`wk test` interface that `CLAUDE.md` documents is load-bearing. It does not
exist: inside a macOS guest every `wk` command fails with "podman is required",
because `resolve_target` defaults to `container` when no workspace is named and
the entrypoint then tries to forward into a podman machine that cannot exist
there. Verified in the macOS guest; **the Linux-container half is unverified**
and should be established first.

This is not filler. Until it is fixed, `wk claude` puts Claude in a correctly
sandboxed guest that cannot build or test — which is the one thing the sandbox
exists to allow. It also gates `docs/HANDOFF-claude`'s "every skill invokes a
deterministic tool rather than freehand steps".

---

## Either machine / process items (no meaningful conflict, pick up as filler)

- **`docs/HANDOFF-testing.md`** — every task above should get a line item in
  `TESTING.md` as it's picked up. This is a standing rule for whoever does the
  work, not a separate task — apply it inline rather than scheduling it.
- **`docs/HANDOFF-claude`** — update `CLAUDE.md` and the skills to: drop
  stale detail, make every skill invoke a deterministic tool/script rather than
  freehand steps, cap tokens spent quoting benchmark/build output, forbid
  hand-rolled builds and benchmark methodology (always the script; handle
  cli/graphical; report GPU rendering; skip SIMD subtests only on ARMv7),
  never store useful data in `/tmp`, default new Claude threads to RC-enabled
  and named `<purpose>-<host-os/arch>-<target>`, never push/commit in git.
  Pure docs/config edit, no sandbox risk — whichever machine is idle first.

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

- **`docs/HANDOFF-sdcard.md` was an empty placeholder** — now scoped (see
  lane A step 7) rather than an orphan zero-byte file.
- **The macOS-proxy unification is done.** `docs/HANDOFF-linux.md` used to
  reference a `docs/HANDOFF-macos-proxy.md` that was never written; that file
  never existed and the underlying work (collapsing the Linux
  `--network none`-plus-proxy model and the macOS Softnet boundary into one
  allowlist-by-hostname design) is complete, so both the missing-file
  reference and the "what is left" bullet pointing at it have been removed
  from `docs/HANDOFF-linux.md`.
- **Git and GitHub helpers separated out** into their own document,
  `docs/HANDOFF-git-tools.md` (see lane A step 13) — they shared no tooling
  or machine constraint with the profiling/benchmarking/wasm material in
  `docs/HANDOFF-original-helpers.md`, and consolidating them into one place
  made it possible to also note that `report` needs no action (`wk report`
  already covers it) and that `git-clean`/`commit-count` aren't worth merging
  into anything — they solve unrelated problems.
