# HANDOFF — the macOS A/B lane

## Owed, no hardware needed

- [ ] `wk bench mac-ab --machine benchvm` — the guest rehearsal — cannot start:
      `error: benchvm (boot/machines/benchvm.conf) sets no NODE_SSH`, before it
      reaches any other check. A tart guest's address is in no ssh config and
      changes every boot, which is why `boot/mac-guest.sh` defines its own
      `m_ssh`; `bench/mac-ab.sh` now routes through `m_ssh` so that override
      wins, but four things still assume the measured machine, the machine that
      holds the tools, and the machine that can see the staging root are one
      ssh destination:
        - `host_tools` / `rwk` run `./wk build|bench stage|vm start` and belong
          to the machine that *manages* the target — tolken for both mbp and
          benchvm, so for a guest they are local, not `$HOST`
        - `put_file` / `put_tree` are a fourth implementation of staging;
          `wk bench stage` already goes through the driver's
          `b_bench_local` / `b_bench_put` / `b_bench_put_file`, and mac-guest
          implements all three while mac-volume deliberately implements none
        - `bench_home` derives the measured account's home from the volume's
          path (`…/private/var` → `…/Users/bench`); a guest's is its own user's
        - `[ -n "$HOST" ] || die` and the ~50 uses of `$HOST` as both label and
          ssh destination
      A driver hook for "run this on the machine that manages the target" is the
      shape that fits: `m_ssh` for mac-volume, local for mac-guest. This is why
      a rehearsal matters at all: `--dry-run` returns from `phase_go` before
      `b_reboot`, so no bench-side path is reachable from host mode — which is
      how a `reboot` verb that rebooted nothing survived every dry run there has
      ever been (see the `setsid` note below)

- [ ] the rehearsal needs a display it can be judged on, and must not gain a
      `NODE_DISPLAY` in its conf: the guest's is declared by `WK_VM_DISPLAY`
      (targets/vm.sh, `1280x800`), so the plant should read it from the target
      that sets it rather than store a second copy. Two things block that:
      a tart guest's panel reports `builtin=False`
      (tests/test_mac_gates.py `GUEST_DISPLAY`), so the topology rule —
      exactly one online display and it the built-in panel — refuses a guest
      outright; and `parse_expect_display` (bench/mac-browser-check.py) accepts
      only the literal word `builtin`. Generalising the declaration to
      `<kind> <w>x<h>`, kind being what `CGDisplayIsBuiltin` answers, makes a
      guest representable without weakening tolken. README.md's
      "The Mac lane" says the declared mode lives in the machine conf and would
      need the same edit

- [ ] the rehearsal's way past a guest's quiet-machine gate is gone. It was the
      job's `force` field, and it went because one `--force` meant both "cross
      the driver's barriers" and "pass `--force` to every leg", so crossing a
      preflight barrier silently disabled each leg's own gate. Whatever it gets
      instead must not be reachable from the flag that crosses a barrier

- [ ] `bench/mac-ab.sh` preflight auto-prepares (`machine_prepare "$HOST" || true`)
      when it finds a Mac it cannot restart and has a terminal. `machine_prepare`
      now pushes a commit rather than rsyncing, so on a dirty tree it surfaces
      `tools_committed`'s "commit first" refusal inside preflight and then
      reports `restartable no`. Correct, but it is new text in a preflight that
      nothing pins

- [ ] `lib/wkdata.py`'s `_declared_aggregate` resolves the suite root's `Score`
      only. A first-level jetstream3 child declares `Time: ["Geometric"]` over
      its First/Worst/Average and Speedometer's root declares
      `Time: ["Total","Geometric"]`; neither becomes a row. No behaviour is
      lost — it matches `_headline_score`'s scope — but it is a declaration the
      file reads and does not resolve

- [ ] `tests/test_resources.py`'s static rule derives its reader set from
      `lib/resources.sh` alone, so it does not guard a *wrapper* over a reading
      (`_vm_cpus`, `t_cores`, `_base_mem_mb`) being interpolated. Those sites
      are correct in code today; a sixth added tomorrow would fail no test.
      Extending the closure tree-wide is not the answer — measured, it flags
      248 sites

- [ ] nothing can turn auto-brightness *off*, only refuse a run under it.
      Measured 2026-09-08 on tolken (MacBook Air `Mac16,12`, M4, macOS 26.6.2):
      DisplayServices exports no auto-brightness symbol —
      `DisplayServicesGet/SetBrightnessAutoEnabled` and four other plausible
      names are all absent — and neither install has
      `com.apple.iokit.AmbientLightSensor.plist` or
      `com.apple.CoreBrightness.plist`, so there is no file to write either.
      The only reading is `system_profiler`'s runtime
      `spdisplays_ambient_brightness`, which `lib/wkmac.py displays` carries and
      the display rule refuses on. Finding the setter is what would let the lane
      hold the setting rather than decline the machine

- [ ] `./setup` is absent from crash-only coverage. `tests/test_crash_only.py`
      drives `wk new`, `wk rm`, `wk gc` and `build_live` and never mentions
      `setup` — so CLAUDE.md rule 2 has never been applied to the one command
      that provisions a machine's privileged state, its dotfiles, its
      credentials and its units. The stages — dotfiles, claude, mcp, sharing,
      machine, vmtools, softnet, sdk, broker — have no convergence test between
      them. Each wants the same question asked: killed at any point, does a
      re-run reach the declared final state, or is "already exists" the answer
      to a half-made thing

## Owed, needs the Mac

- [ ] **the 1-round jetstream3/speedometer3/motionmark confirmation run.**
      `20260908T201232Z` is planted and armed on the volume (`phase=planted`,
      `attempts=0`, arms `20260906T233003Z-mac-release-pgo` /
      `20260907T021244Z-mac-release-pgo`, `display builtin 1280x832`), the
      launch agent is installed, and `WK Bench` is the firmware default — so any
      boot of that volume runs it. It has not started because the restart is
      still one human action: see the next item

- [ ] the fixed `wk-boot-priv` is not installed on tolken. `v_reboot` detached
      with `setsid`, which **macOS does not ship** (`command -v setsid` answers
      nothing on 26.6.2), so the verb printed `rebooting in 3s`, rebooted
      nothing, and exited 0 — for as long as it existed. `bench/mac-ab.sh`
      caught it only because `phase_go` verifies against `kern.boottime` rather
      than trusting the helper. Both verbs now use `nohup`, and so does
      `boot_priv` in boot/machines.sh, but the copy at
      `/usr/local/libexec/wk-boot-priv` over there is the old one and replacing
      it needs root on that Mac:
        wk boot mbp --prepare        (one password prompt, in a terminal there)
      Until then the lane restarts nothing and the planted job waits for a
      reboot by any means

- [ ] the prose still says the benchmark install "joins nothing" and "has no
      network" in six places (bench/mac-ab.sh:17,374,803,
      bench/mac-bench-autorun.sh:2,9,106, boot/mac-volume.sh:238), which was
      the unprovisioned state and is no longer the design. One sweep, once the
      boot below has shown the join works

- [ ] one boot proves the darwin `tailscaled` (bench/mac-tailnet.sh). The
      transport is now built and on the volume: `collect` cross-built
      tailscaled 1.102.2 for darwin/arm64 from Linux (56 MB, and that Mac's own
      `file` reads both binaries as Mach-O arm64), and the plant put them,
      the auth key, `tailnet.conf` naming `tolken-bench`/`tag:wk` and both
      LaunchDaemons at `/var/wk/tailnet` with no privilege at all. The autorun's
      `converge_self` installs them into that install and joins with its own
      passwordless sudo. What the boot has to show is that `launchctl bootstrap
      system` opens the utun as root with no panel on macOS 26.6.2, that
      `tailscale up --auth-key file:` joins unattended and spends the key, that
      `ssh tolken-bench` reaches the install *while it measures*, that
      `--accept-dns=false` wrote no `/etc/resolver`, and that a restage rejoins
      on the same tailnet IP with no `-1`. A relayed rather than direct
      connection is the expected symptom of macOS local-network privacy, not a
      fault

- [ ] whether `bless --setBoot` needs a volume-owner credential is measured by
      running it rather than asserted: the helper blesses with root alone and
      each verb says what bless answered. Read on tolken 2026-09-08:
      `bless --help` lists `--user`/`--stdinpass` under *Snapshot options* only.
      The owed work is one run — `wk boot mbp --prepare`, then `wk boot mbp` —
      and reading the form it took. Do not run it while a job is planted: it
      blesses the host install first, which is the way back it proves, and that
      changes which volume the next boot enters

- [ ] the hand-back has never run. `leave_bench` now blesses the host install,
      reads the firmware back, and reboots only if it names that install --
      halting otherwise, because this volume is the firmware default and a
      reboot would land back here. Whether `bless --setBoot` succeeds for a
      volume the bench account does not own is the platform's answer and this
      boot has it; if it halts, the log says what the firmware read back and
      the power button is still the way

- [ ] superseded by the hand-back above, and kept only until one boot shows
      which way it went: collecting a result needed a finger on the power button. The bench
      install powers the Mac off; the firmware default is the bench volume, so
      the next boot measures again rather than coming back to host mode. If
      `bless --setBoot` needs no credential (above), the bench install could
      bless the host install before it halts, and then a wake — `pmset -a womp 1`
      on that install, plus a magic packet from moose — reaches host mode with
      nobody in the room. Two things are unbuilt for that: `wk-boot-priv` is
      installed only on the host install, so `stage_payload` would have to put it
      on the volume too and `bench_install`'s gate would have to be re-read from
      the other side; and whether a Mac halted by `halt` wakes on LAN at all is
      unmeasured

- [ ] the display-mode convergence has never run. The bench install comes up at
      `1470x956` and the job now declares `1280x832`, so the first boot of
      `20260908T201232Z` writes the WindowServer configuration and restarts
      once. What it has to show: that WindowServer adopts a mode written into
      `com.apple.windowserver.displays.plist` while the volume was merely
      mounted and the machine rebooted with `/sbin/reboot` (which is what makes
      the write survive — an orderly shutdown lets WindowServer save the old
      configuration back), and that the `mode_declared` guard refuses rather
      than looping if it does not

## Decisions taken

- the measured mode is `builtin 1280x832`, pixel-exact at scale 2 on the
  2560x1664 panel. The 16 rounds taken at `1470x956` — a scaled mode above the
  panel, rendered 2940x1912 and downsampled — are not comparable with anything
  taken after this
- `wk bench mac-ab --shutdown` is gone. The firmware default is the bench
  volume and the helper's reboot is the one transition; `--plant` leaves the job
  on the volume and reboots nothing, which is what the startup-manager path
  wanted
- `machine_prepare` pushes a commit rather than rsyncing an uncommitted tree, so
  a later `git pull` on the far side has nothing of anyone's to replace. An
  uncommitted tree is refused, naming `git commit -a`
