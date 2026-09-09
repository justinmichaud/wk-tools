# HANDOFF — the macOS A/B lane

## Owed, no hardware needed

- [ ] **one path to the measured system, and it is that system's own tailnet
      node.** The benchmark install joins as `NODE_BENCH_SSH` (`tolken-bench`)
      at boot and needs no password; the Mac's *host* install stops for one at
      boot. Yet `bench_root`, `phase_plant`, `phase_status` and `phase_collect`
      reach the volume only through host mode, and `bench_root` refuses outright
      when the machine answers in bench mode ("read it back once the machine
      returns"). Measured 2026-09-09: a finished run's results are unreadable
      from the moment `leave_bench` reboots until the host install is back,
      while the install holding them answers on the tailnet throughout.
      The abstraction is already in boot/machines.sh -- `MODE_CHANNEL`,
      `r_ssh` over `m_ssh`/`i_ssh`, and `image_addr` already prefers
      `NODE_BENCH_SSH`. What is owed:
        - mac-volume's `b_probe` sets `MODE_CHANNEL=bench` when that node
          answers and carries `/etc/wk-image`; today it only tries `m_ssh`
        - the bench channel reaches a macOS install as `dotfiles/ssh/config`
          declares it -- user `bench`, host key pinned under the
          `tolken-bench` alias -- where `i_ssh_opts` forces `-l root` and an
          unpinned key, which is a Pi bench system's shape and not this one's
        - `b_bench_root` answers `/var/wk` on that channel, the volume being
          `/` there and under no `/Volumes` path at all
        - `bench/mac-ab.sh` reaches the measured system through the
          channel-aware reader, and the host-mode-only refusal goes with it
      One route, not two: the host-mode path is deleted rather than kept beside
      it (CLAUDE.md, "One path, not two")

- [ ] **every gate is askable before anything reboots.** A gate asked only after
      the transition costs a boot per defect. The measured install is reachable
      over its own node whenever it is up, and each of these reads the running
      system and nothing else: the quiet-desktop probe and findings,
      `wk quiesce on` and its readback, brightness, ambient-light compensation,
      the display topology and mode, `bench/mac-browser-check.py`, and
      `wk bench staged --dry-run`. `wk bench mac-ab --preflight` asks host-mode
      proxies for some of these and nothing at all for the rest -- so it reports
      clean and a boot then refuses. What is owed is a preflight that asks the
      real ones over that channel when the install is up, and a reboot that is
      only the transition into bench mode. The planted tree at
      `/var/wk/wk-tools` is what the launch agent runs, so pushing it over that
      channel replaces the lane in place and an iteration costs seconds

- [ ] **`--rounds` names something run-benchmark already has a word for.** In
      `bench/mac-ab.sh` a round is one interleaved pass over every plan and
      every arm, so `--rounds 1` with three plans is three A/B tests; upstream's
      round is one A/B test, and `--count` is the one term here that does match
      it (iterations). Rename what `--rounds`, `--max-rounds`, `rounds_done` and
      the job's `rounds` field count, in the driver, the autorun, the job and
      README.md, so the word means what run-benchmark means by it

- [ ] the guest rehearsal has never been run, and with it `--rehearse` -- the
      only way past a leg's own quiet-machine gate, which no `--force` reaches.
      `wk bench mac-ab --machine benchvm` resolves the whole plan through the
      driver now (`b_manage`, `b_bench_put`, `b_bench_home`, `b_display`,
      `b_restart_ready`), but a run needs `tart` and so must be driven on the
      Mac itself, with a `wk-bench` guest running and two arms staged onto it
      (`wk bench stage <ws> --to benchvm`). Nothing past `bench_home` is
      exercised until then: a dry run stops at the first reading that needs the
      guest

- [ ] `./setup`'s machine-mutating stages have no convergence test. The
      home-scoped two (dotfiles, claude) are driven for real, killed with
      SIGKILL at five points and re-run (`tests/test_crash_only.py`); the other
      ten write sudoers rules, launchd/systemd units, packages and machine
      defaults, and a test suite may not take those on the machine it runs on.
      `NEEDS_THE_MACHINE` in that file names them and the audit fails if a stage
      lands in neither list. Each wants the same question asked, on a machine
      that can be rebuilt: killed at any point, does a re-run reach the declared
      final state

## Owed, needs the Mac

- [ ] **the confirmation run of the patch, with statistics.** One A/B test of
      speedometer3 at `--count 1` compares clean (task
      `20260909T154515Z-mbp-mac-ab`: A 60.0993, B 60.1678, +0.11%), and one
      iteration per leg carries no within-leg variance, so no p-value exists and
      `wk bench precision` resolves nothing. jetstream3, speedometer3 and
      motionmark at `--count 2` or more is the run that says something about
      `7d6c5149` (PR 70886) against its base `b8e586fe`

- [ ] whether the plant's Do Not Disturb record turns DND *on* in the running
      install is unproven, and it cannot be asked there:
      `~/Library/DoNotDisturb/DB/Assertions.json` answers `Operation not
      permitted` to a read as the measured account and under `sudo` alike, so
      the probe leaves the row out and the findings call it unknown. The plant
      writes and reads it back from host mode, which verifies a file and not a
      behaviour -- and a notification is drawn on the measured desktop with
      that record in place (2026-09-09). What holds a banner off a leg
      meanwhile is `NotificationCenter` and `usernoted` held stopped, and
      neither is what the row claims. Either find a mechanism `donotdisturbd`
      honours, or delete the row and say that the stopped agents are all of it

- [ ] the warmup round's samply capture attaches and saves nothing. Its
      `profile.log` holds `Profiling <pid>, press Ctrl-C to stop...` and `All
      tasks terminated.`, and no `.json.gz` reaches `ab/<stamp>/warmup/` -- so
      a leg passes with a warning and the profile the warmup round exists to
      carry is absent

- [ ] the prose still says the benchmark install "joins nothing" and "has no
      network", in six places (bench/mac-ab.sh:21,387,834,
      bench/mac-bench-autorun.sh:2,9,140). It joins: that install brings up the
      darwin `tailscaled` cross-built from Linux and answers as `tolken-bench`
      while it measures. One sweep, and `NODE_BENCH_SSH` is what tells
      "measuring" from "finished" in place of the driver calling both silence
