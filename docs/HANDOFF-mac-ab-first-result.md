# HANDOFF — one A/B iteration on PR 70886, and the reporting around it

The A/B produces a number now; what is owed is a `wk status` that says enough
to debug one that does not, a report generated from the result, and the faults
that number exposed. Everything below is on `tolken` (the `mbp` machine) and
its `WK Bench` volume.

## The result

PR 70886 (WTF spin locks, bmalloc) measured on `tolken`'s `WK Bench` volume,
2026-09-07, job `20260907T214716Z` -- 16 rounds a side, interleaved, warmup
discarded, 96 of 97 legs `clean`:

    A  20260906T233003Z-mac-release-pgo   b8e586fe8c30  merge-base
    B  20260907T021244Z-mac-release-pgo   7d6c5149ef3e  PR head

    speedometer3   mean_a=58.9816  mean_b=58.9853  delta=+0.0063%  p=0.94
                   mde_pct=0.2254  target=0.30  met=yes

    taken through: accelerator AGXAcceleratorG16G, webgl 2.0, raf_hz 54.4,
    focused, screen 1470x956 dpr 2

Those rounds resolve 0.23%, so that is a null and not an absence of evidence:
a difference larger than 0.23% is not there on speedometer3. jetstream3 and
motionmark ran the same 16 rounds -- subtests are in
`/var/wk/ab/20260907T214716Z/` on the volume -- and neither has a precision
figure, for the first reason below.

## Owed by that run

- [ ] **the stopping rule cannot fire for two of three plans.**
      `arm_results` (bench/mac-bench-autorun.sh) builds
      `…/results/<dir>/result.json`, and `ab-precision` refuses exactly that:
      "a run is the directory a benchmark wrote, not the result.json inside
      it". speedometer3 survives it; jetstream3 and motionmark answer "no
      scores on side A", so `plan_resolves` always fails, `unresolved` is never
      empty, and no run can stop early on precision. Drop the `/result.json`
      [no hardware needed]

- [ ] **the run ended on its watchdog, not on itself**: `outcome=watchdog`
      after `rounds_done=16` -- nothing written for 2700s, so it rebooted
      itself, `ab-summary` never ran and there is no `summary.txt`. What hung
      after round 16 is unmeasured; the volume's `autorun.log` ends where it
      stopped [needs the volume]

- [ ] **`--count 1` gives up the within-run statistics.** Every leg warns "run
      … has count=1: no p-value can be computed", so the p-values in the report
      are across rounds only. The real run wants `--count 2` or more, which
      also shortens how many rounds a target takes [needs the volume]

- [ ] the real measurement, once those land: no `--detect 0`, `--count 2` or
      more, alternating until every plan resolves 0.3%. speedometer3 reached
      0.23% in 16 rounds at `--count 1`, so the earlier ~29-round estimate was
      pessimistic; whether motionmark resolves at all below the 40-round
      ceiling is still unmeasured [needs the volume]

## What a failing run has to say for itself

- [ ] `wk status <ws>` still reports `state=stalled` and exit 3 for a workspace
      whose full-LTO link has been silent past `WK_STALL_SECONDS` (300s;
      `build_live`, lib/detach.sh), which is also the state `wk status --wait`
      stops waiting through. The reading that settles it is `build_processes`
      (lib/detach.sh) and `wk status` now prints it, but it counts *this
      machine's* compilers and linkers, and a machine holding several
      workspaces cannot say which build they belong to. Measure that
      attribution before letting `build_live` read it [no hardware needed]

- [ ] **a run in flight is invisible in the one place that lists runs.** Both
      halves are the Mac lane sitting beside the fleet's models rather than
      inside them, and `wk status` says so itself:

          mbp    workstation   unknown from here
                 reached       tolken not a node; tolken-bench not a node
          rpi3   bench-device  bench mode
                 reached       rpi3-rescue …(down); rpi3-bench …(up)

      1. `b_probeable` for the mac-volume driver is `is_macos`, so from any
         other machine the answer is "unknown from here" -- while a board's
         driver probes over the tailnet from anywhere. The evidence is already
         in that output: `tolken not a node` means it is not in host mode, and
         with a job planted that *is* "measuring". Make the driver answer from
         elsewhere: `NODE_SSH` on the tailnet means host mode; absent, with a
         planted job, means measuring since `planted_at`.
      2. A mac-ab is a bench run that is not a bench *task*. `wk status`'s
         bench section lists `$WK_STORE/bench/*/task.json`; the plant writes a
         job onto the volume and `mac-ab-job.json` into this host's state dir
         instead, so a section built to show exactly this cannot see it. Have
         the plant record a task in that store and `wk status` lists it with no
         new display code [no hardware needed]

- [ ] **nothing checks the screen this Mac is measuring on.**
      bench/mac-browser-check.py reads `screen` and `dpr` and faults only below
      640x480, so a display mode change or an external monitor passes -- and
      neither is a detail: run-benchmark sizes its window from the screen,
      MotionMark's score is a function of the area it draws, and a second
      display changes the compositing, the refresh rate and which GPU the
      window lands on. Two runs at different resolutions are not comparable,
      and nothing would say so. Measured on the 2026-09-07 runs, for whatever a
      later one should match: `screen=[1470, 956]`, `dpr=2`, the built-in panel
      alone. The check should read the display list (one display, and the
      built-in one), pin the expected mode, and refuse a mismatch the way it
      refuses a throttled window -- with the reading in `browser-check.json`,
      where it already travels with the result [no hardware needed to write; one
      run to pin the numbers]

- [ ] the bench install has no way onto the tailnet, so a run is unobservable
      until it hands the machine back. Measured 2026-09-07: every macOS
      Tailscale build tunnels through NetworkExtension, so `tailscale up` parks
      the first boot on "would like to add VPN configurations" with nobody in
      the room, and pkgs.tailscale.com publishes tarballs for nine Linux arches
      and none for darwin -- there is no daemon to run instead. The install is
      out of the unattended path now (bench/mac-bench-firstboot.sh), and what
      would put it back is a `tailscaled` built for darwin from source, pinned
      by sha256 the way image/buildroot/tailnet-overlay.sh pins the Linux one,
      run as a root LaunchDaemon: root opens a utun with no panel. Until then
      `wk bench mac-ab --progress` answers from the volume in host mode, and
      the `tolken-bench` ssh stanza resolves to nothing on purpose
      [no hardware needed to build it; one boot to prove it]

- [ ] nothing in the lane can tell a person it wants them. `lib/wknotify.py`
      is `sd_notify` for the systemd services and no-ops elsewhere, so the only
      signal that a plant is waiting for a pick, or that a run has handed the
      machine back, is a line in a terminal nobody is watching. Both moments
      are known exactly -- `phase_go` returns when the machine goes down, and
      the driver sees ssh answer again [no hardware needed]

- [ ] two human actions per iteration, not one: the pick at the startup
      manager, and this Mac's password at the host install's login window when
      the run hands the machine back. That is what makes each failed cycle
      expensive, and it is why a refusal that arrives in bench mode has to
      carry everything a reader needs in the volume's own log

- [ ] `focused` is read by bench/mac-browser-check.py and judged by nothing.
      Measured 2026-09-07 on a run whose other readings were all clean:
      `raf_hz=58.6`, `accelerator=AGXAcceleratorG16G`, a real WebKit GPU
      client -- and `focused=False`. Either the page legitimately loses focus
      to the harness that drove it, in which case the reading should go, or
      the raiser had not taken and every leg after it measured a background
      window. One run with the raiser watched settles which [needs the volume]

- [ ] two readers, two definitions of provisioned: the autorun and
      `wk bench mac-ab` ask the first-boot log for a completion line, while
      `wk bench staged` measures the settings themselves before every leg. A
      volume that drifts after provisioning reads as provisioned and is still
      refused leg by leg, unobservably. `refuse_unprovisioned`
      (bench/mac-bench-autorun.sh) runs on the install itself, so it could ask
      `wk_quiet_desktop_probe` and report the same finding once, up front
      [no hardware needed]

## The report

- [ ] `wk bench report --html` has never been run against a mac-ab result.
      `wk bench mac-ab --collect` is proven (it produced the numbers above)
      [needs no new run -- 20260907T214716Z is on the volume]

## Standing hazards

- [ ] the trailing-`&&` audit (tests/test_owed_static_audits.py) skips any
      statement that also holds a `||`, so `{ … || true; } | while read -r n;
      do [ -n "$n" ] && printf …; done` -- a loop body whose last iteration
      decides the function's status -- passes it. Blank `{ }` and `do … done`
      bodies the way quoted spans are blanked, or the rule only sees the shape
      when it appears alone [no hardware needed]

- [ ] `wk boot mbp` refuses to arm the firmware because
      `/usr/local/share/wk-bench/owner-password` does not exist, and whether
      `bless --setBoot` needs a volume-owner credential at all is unmeasured.
      One non-destructive command settles it -- blessing the volume already
      booted changes nothing:

          sudo bless --mount / --setBoot && echo WORKED-WITHOUT-CREDENTIAL

      If root alone suffices, drop the credential from `admin/wk-boot-priv`'s
      `boot-host` and the A/B needs nobody at the keyboard. `bless --help`
      lists `--user`/`--stdinpass` under *Snapshot options*, not under Mount
      Mode, which is why this is a question and not a fact
      [needs one sudo on the Mac]

- [ ] the wk-tools tree on moose carries uncommitted files and tolken runs
      from a scratch clone at `~/Development/wk-tools-wip`, deployed with
      rsync-and-commit. `wk sync --tools` refuses an uncommitted tree by
      design, so landing this work means committing it [decision]

- [ ] `wk selftest` is red before any of this: 97 test ids fail at 2b35211,
      36 of them in test_machine_mounts and 52 across test_push_agent,
      test_key_github_pat, test_store_secrets, test_key, test_claude_login and
      test_push_switch. One contract explains most of the credential half --
      `wk push status` exits 4 where the tests expect 1 ("no deploy keys here
      at all") -- so settle that exit code first and re-count
      [no hardware needed]

- [ ] `vm/console-keys.py`, `build/pgo-run-benchmark.py` and
      `bench/mac-window-probe.sh` carry more prose than
      tests/test_comment_density.py allows (22.7%, 13.3% and 11.9% against a
      5% body ceiling), which is one of the 97 [no hardware needed]
