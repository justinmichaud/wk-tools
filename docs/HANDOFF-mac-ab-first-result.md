# HANDOFF — the macOS A/B lane

## Owed, no hardware needed

- [ ] **every gate is askable before anything reboots.** A gate asked only after
      the transition costs a boot per defect. The lane now reads the measured
      install over its own node whenever it is up (`b_probe`, `r_ssh`), and each
      of these reads the running system and nothing else: the quiet-desktop
      probe and findings, `wk quiesce on` and its readback, brightness,
      ambient-light compensation, the display topology and mode,
      `bench/mac-browser-check.py`, and `wk bench staged --dry-run`.
      `wk bench mac-ab --preflight` asks host-mode proxies for some of these and
      nothing at all for the rest -- so it reports clean and a boot then
      refuses. What is owed is a preflight that asks the real ones over that
      channel when the install is up, and a reboot that is only the transition
      into bench mode. The planted tree at `/var/wk/wk-tools` is what the launch
      agent runs, so pushing it over that channel replaces the lane in place and
      an iteration costs seconds

- [ ] bench mode is readable from here and nothing more. `wk boot mbp --diag`
      refuses in bench mode though `/var/log/wk-diag.txt` is right there on the
      install that answers, and `--back` refuses too: `wk-boot-priv` is on the
      host install only, so nothing from this side can hand the machine back and
      the run's own `leave_bench` is the whole of it. Each refusal names why;
      what is owed is deciding whether either verb should reach that side at all

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
