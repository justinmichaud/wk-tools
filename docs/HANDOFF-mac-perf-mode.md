# HANDOFF — the Mac's bench mode

- [ ] verify a real measured run staged from a guest [needs the macOS guest]
- [ ] verify the full `wk boot mbp` lifecycle against the real benchmark install, not a disposable volume [needs the Mac bench volume]
- [ ] verify `wk bench stage <ws> --to mbp` from a macOS guest onto the volume [needs the macOS guest]
- [ ] verify `wk bench compare` between a bench-mode result and a container run [needs the Mac bench volume]
- [ ] check `wk quiesce status` on the benchmark install before a run [needs the Mac bench volume]
- [ ] move the A/B lane to the `bench/mac-lane.sh` shape (state on the driver, reach in per phase), replacing the planted-agent architecture [needs the Mac bench volume]
- [ ] verify software-update scanning is denied at provision/first boot: run `wk bench mac-volume --provision` (or a first boot) on `WK Bench`, confirm `runs.tsv`'s scan-evidence column shows none across every arm [needs the Mac bench volume]
- [ ] verify `wk quiesce`'s MiniBrowser raiser: App Nap is off and read back, but the rehearsal guest's /usr/bin/python3 has no AppKit, so the raiser has never started anywhere [needs the macOS guest with the Command Line Tools]
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
- [ ] pin the three PGO collection payloads. `build/mac-pgo.sh` lets
      run-benchmark fetch Speedometer 3, JetStream 3 and MotionMark itself, so
      two arms built hours apart can be profiled against different revisions of
      a benchmark. `seed_payload` (lib/bench.sh) is the one implementation, and
      it needs a store the guest can write [needs the macOS guest]
- [ ] upstream `pgo_profile_output_directories` onto `OSXMiniDriver`
      (Tools/Scripts/webkitpy/benchmark_runner/browser_driver/osx_minibrowser_driver.py):
      five lines, the same constant `OSXSafariDriver` already returns. Until
      then `build/pgo-run-benchmark.py` monkey-patches it, and that file exists
      only to be deleted [upstream]
- [ ] verify a real `mac-release-pgo` build end to end: the phases, the
      collection's GPU path, and that the measured build's dSYMs symbolicate a
      samply capture taken on the volume [needs the Mac bench volume]
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
- [ ] decide what a PGO workspace does with the golden base's prebuilt
      `WebKitBuild/Release`. A profile-guided build shares no flags with it, so
      it is ~40 GB of dead weight in a guest that then wants ~40 GB for the
      instrumented tree and ~40 GB for the measured one. Measured on tolken
      2026-09-05: 127 GB free with the base build alone still running
      [needs the macOS guest]
- [ ] move the PGO profile collection off the build guest and onto the
      benchmark install. Measured 2026-09-06 on a guest freshly cloned from a
      freshly rebuilt golden base: Setup Assistant is frontmost at every boot
      (two boots checked), `~/.skipbuddy` does not stop it, killing it takes
      the console session with it (`/dev/console` goes admin -> root), and the
      guest's `/usr/bin/python3` has no pyobjc so no raiser can displace it.
      MiniBrowser launches and stays up; it is the focus that cannot be won.
      `build/mac-pgo.sh` refuses rather than collecting a throttled profile,
      so `wk build <ws> mac-release-pgo` stops at phase 2 in a guest. The
      screen half of that may be fixed (docs/defects 4); the missing pyobjc,
      and so the missing raiser, is not.
      The shape that works is two bench-mode visits per experiment -- one
      collecting both arms' profiles, one measuring -- which the planted-job
      architecture already supports [needs the Mac bench volume]
