# HANDOFF — the Mac's bench mode

- [ ] verify a real measured run staged from a guest [needs the macOS guest]
- [ ] verify the full `wk boot mbp` lifecycle against the real benchmark install, not a disposable volume [needs the Mac bench volume]
- [ ] verify `wk bench stage <ws> --to mbp` from a macOS guest onto the volume [needs the macOS guest]
- [ ] verify `wk bench compare` between a bench-mode result and a container run [needs the Mac bench volume]
- [ ] check `wk quiesce status` on the benchmark install before a run [needs the Mac bench volume]
- [ ] move the A/B lane to the `bench/mac-lane.sh` shape (state on the driver, reach in per phase), replacing the planted-agent architecture [needs the Mac bench volume]
- [ ] verify software-update scanning is denied at provision/first boot: run `wk bench mac-volume --provision` (or a first boot) on `WK Bench`, confirm `runs.tsv`'s scan-evidence column shows none across every arm [needs the Mac bench volume]
- [ ] `wk quiesce`'s MiniBrowser raiser has never started anywhere. App Nap is off and read back, and a guest now carries the pyobjc it needs (`bench/mac-pyobjc.sh`); what is unread is the raiser actually holding MiniBrowser in front across a benchmark's relaunches [needs one collection or measured run]
- [ ] enumerate every launchd job on benchvm itself (`launchctl print system` / `gui/<uid>`) and classify what the table does not already name [needs the macOS guest]
- [ ] exercise the four daemon rows the rehearsal guest never runs -- `XProtect`, `XprotectService`, `diagnosticservicesd`, `powerdatad` -- and confirm none of them wedges a probe when stopped, the way mds and sysmond did [needs the Mac bench volume]
- [ ] give the bench volume SIP disabled and the workstation SIP enabled, and report both in `wk doctor` [needs the Mac bench volume]
- [ ] have `wk doctor` report the quieting the way `wk quiesce status` does, so a machine's readiness is one command [needs the Mac bench volume]
- [ ] `pmset highpowermode` is untested: neither the rehearsal guest nor tolken (fanless) has the key [needs a Mac with fans]
- [ ] `disablesleep` is read back as `SleepDisabled` in `pmset -g`; confirm that spelling on the bench volume too [needs the Mac bench volume]
- [ ] enumerate and classify every systemd timer/unit on the rpi3 yocto image [needs a Pi card in hand]
- [ ] enumerate and classify every systemd timer/unit on the rpi4 yocto image [needs a Pi card in hand]
- [ ] enumerate and classify every systemd timer/unit on the rpi5 yocto image [needs a Pi card in hand]
- [ ] re-run the mbp daemon/timer classification after any macOS version bump on `WK Bench` [needs the Mac bench volume]
- [ ] upstream `pgo_profile_output_directories` onto `OSXMiniDriver`
      (Tools/Scripts/webkitpy/benchmark_runner/browser_driver/osx_minibrowser_driver.py):
      five lines, the same constant `OSXSafariDriver` already returns. Until
      then `build/pgo-run-benchmark.py` monkey-patches it, and that file exists
      only to be deleted [upstream]
- [ ] the measured phase of `mac-release-pgo` has never run. Measured
      2026-09-06 in a guest, everything before it now does: the instrumented
      build (65 min), the browser gate against that build
      (`AppleParavirtGPU`, WebGL 2.0, 50 Hz with the raiser holding the window,
      the instrumented build's own GPU process on the accelerator), all three
      benchmarks collected against pinned payloads, and the merge, weighting
      and compression (JavaScriptCore 24,129 functions / 1.8 MB, WebCore
      33,017 / 2.2 MB, WebKit 15,266 / 0.87 MB). It stopped there for disk, not
      for a fault [needs room on tolken]
- [ ] that the measured build's dSYMs symbolicate a samply capture taken on the
      volume [needs the Mac bench volume]
- [ ] verify the warmup round's samply capture: `sudo samply record --pid` on
      the WebContent process, on a benchmark install where sudo is passwordless
      [needs the Mac bench volume]
- [ ] measure the per-round spread of jetstream3 and motionmark on the bench
      volume the way speedometer3 already is. Measured 2026-09-05 off the
      volume's own 2026-08-24 A/B (8 rounds a side, mac-release,
      `wk bench precision`): speedometer3.0 has a per-round spread of
      0.36-0.40% of score, so it resolves 0.57% at 8 rounds and needs ~29
      rounds a side for 0.3%, about 1.7 h of legs. Whether motionmark can
      reach 0.3% below the 40-round ceiling at all is unmeasured
      [needs the Mac bench volume]
- [ ] a benchmark install has never taken the Command Line Tools by itself.
      Without them there is no working `/usr/bin/python3` and run-benchmark
      cannot run at all; first boot now lifts the software-update denial
      (`wk_bench_hosts_remove`), asks `softwareupdate` for the Command Line
      Tools label, installs it and lets the later step put the denial back.
      Every part of that is exercised except the fetch: the volume that exists
      already carries them, installed by hand [needs a fresh bench volume]
- [ ] compare the two arms' `payload-pins`. A collection now profiles against a
      copy of each benchmark pinned by upstream commit (`seed_payload`) and
      writes what it pinned to `$WK_PGO_DIR/payload-pins`; nothing yet reads
      the two arms' files and refuses a pair that differ, which is the case
      speedometer3's and jetstream3's moving branches make possible
      [no hardware needed]
- [ ] the bench volume runs macOS 26.6.1 and this Mac's host install runs
      26.6.2. It does not affect a number taken wholly on the bench volume, and
      it does make the two installs not comparable with each other
      [decision]
- [ ] measure whether this Mac's startup volume can be selected without a person
      at the keyboard, which is the one human action left in an A/B.
      `boot/mac-volume.sh` states that selection goes through a LocalPolicy
      changed only by an authenticated user action, and `bless --setBoot` alone
      does exit 0 having done nothing -- but this macOS's bless carries `--user`
      and `--stdinpass`, which supply exactly that authentication without a
      prompt. Untested because it needs a volume owner's password:

          sudo bless --mount '/Volumes/WK Bench' --setBoot \
               --user <admin> --stdinpass <<<'<password>'
          wk boot mbp            # firmware_default says what it will boot next

      If the firmware's `boot-volume` then names the bench volume group, `b_arm`
      becomes a command and `BOOT_ARMING` stops being `hands-on`; if it does
      not, say so in the driver's own words and the claim is settled
      [needs a volume owner's password]
- [ ] a profile-guided build declares no disk figure, and it is the one config
      that needs one. Measured 2026-09-06 across a whole build in a guest: the
      instrumented products reach 56 GB, the measured ones 45 GB and
      DerivedData 71 GB, against `WK_BUILD_DISK_GB`'s default of 25. Two
      constraints, not one -- and `disk_admit` reads only the driver's
      filesystem, which is the wrong one for a guest. The guest's own free space
      is what a build hits first (`wk bench mac-ab` now reclaims a staged arm's
      products for that reason), and the host's is what the guest's image grows
      into. Measured on the second: a write into space the guest has already
      freed costs the host nothing -- 8 GB written, deleted and written again
      inside the guest moved the host's free space not at all -- so the image
      grows with the guest's high-water mark and never shrinks
      [needs a figure for both]
- [ ] nothing reclaims a staged build. `wk bench stage` writes a new
      `<stamp>-<config>` directory onto the benchmark install every time and no
      verb removes one: six `mac-release` directories from 2026-08-23/24 were
      still there on 2026-09-06, 8.9 GB, on a volume that shares its APFS
      container with the running system. `wk bench staged --ls` lists them, so
      the listing exists and the reclaim does not [no hardware needed]
- [ ] Speedometer 3 logs `NotAllowedError, Permission was denied` once per
      iteration under MiniBrowser: `navigator.wakeLock.request("screen")` needs
      a user gesture. Benign -- Speedometer catches it, and `caffeinate -dimsu`
      already holds the display -- but it is ten console errors in every
      collection log, and worth confirming Safari does not take a different
      path there [decision]
- [ ] `wk bench mac-ab --patch` has not built a pair end to end. Its two halves
      have: a `mac-release-pgo` build of the merge-base and of PR 70886's head,
      each staged onto the benchmark install, were driven by hand on 2026-09-06
      because the guest had room for one arm at a time and the lane's own path
      would have staged the first arm twice. What the lane adds over that is
      `phase_build_ab`'s checkout-build-stage-restore and `reclaim_products`,
      which tests/test_mac_gates.py pins and no run has exercised
      [needs a guest with room for two arms, or one more experiment]
- [ ] the 2026-09-06 baseline arm of PR 70886 is not a build to measure. Its
      whole collection ran behind a consent dialog: run-benchmark photographs
      the screen into `--diagnose-directory` on every leg, `screencapture` needs
      Screen Recording, and the prompt raised at the first leg (15:11:26) sat at
      layer 8 over the browser for four hours. The collection no longer takes a
      screenshot and the window rule no longer ignores a layer, but the staged
      `20260906T182227Z-mac-release-pgo` predates both and is to be replaced,
      not measured [needs one more build of the merge-base]
- [ ] whether this Mac's startup volume can be set from software is still
      unmeasured. `boot/mac-volume.sh` now asks the privileged helper
      (`wk-boot-priv boot-volume`), proves the return trip first because
      `--setBoot` is sticky on Apple Silicon, and reads the firmware's
      `boot-volume` back rather than trusting bless -- so a Mac that cannot be
      armed refuses instead of being trapped in bench mode. None of it has run.
      Whether a credential is needed at all is now the platform's answer rather
      than an assumption: `bless --help` on 26.6.2 lists `--user`/`--stdinpass`
      under Snapshot options and not under Mount Mode, and the helper blesses
      with root alone where the machine holds no credential, saying which form
      it used. So the owed work is one run: `wk boot mbp --prepare`, then
      `wk boot mbp`, then read which form it took [needs the Mac]
- [ ] the screen watch has never run on the benchmark install. `wk bench
      staged` now brackets every run with it (cmd/bench), so a window that draws
      mid-run fails the run rather than being missed by a preflight that read
      the screen minutes earlier; it is exercised against a stubbed probe and on
      a guest, never on the volume [needs the Mac bench volume]
- [ ] `networksetup -getairportnetwork` answers "You are not associated with an
      AirPort network" on macOS 26.6.2 with the interface associated, and
      `ipconfig getsummary` and `scutil` redact the SSID from a caller with no
      Location authorisation. Two Apple bugs worth a Feedback Assistant report;
      `write_wifi_conf` reads the preferred-network list instead, which is not
      redacted [upstream]
