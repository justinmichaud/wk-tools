# HANDOFF — one A/B iteration on PR 70886, and the reporting around it

Three things are owed: an A/B that produces a number, a `wk status` that says
enough to debug one that does not, and a report generated from the result.
Everything below is on `tolken` (the `mbp` machine) and its `WK Bench` volume.

## What exists

Both arms are built, gated and staged on the volume, from the same wk-tools,
the same pinned benchmark payloads, and profiles collected with the GPU,
throttle, payload and screen gates all passing:

    A  20260906T233003Z-mac-release-pgo   b8e586fe8c30  merge-base of PR 70886
    B  20260907T021244Z-mac-release-pgo   7d6c5149ef3e  PR 70886 head

They differ by exactly the patch (one commit, 12 files, WTF spin locks and
bmalloc). `wk bench mac-ab --progress` lists every step of the experiment with
the command that does it and the command that proves it.

## The blocker

- [ ] provisioning has never completed on `WK Bench`: `grep -c "provisioning
      complete" '/Volumes/WK Bench - Data/private/var/log/wk-bench-firstboot.log'`
      is **0** across every attempt back to 2026-08-23, so the desktop quieting
      has never been applied and `wk bench staged`'s preflight refuses every leg
      with "35 setting(s) above are not a measured Mac's". Measured 2026-09-07:
      `--repair` re-armed the daemon, the volume booted at 15:01:00Z, and the
      log stops one second later at "installing Tailscale" -- the autorun
      *planted* at `/var/wk/bin` killed it, removed the daemon, then saw the
      04:22 job's `phase=done` and halted. That copy predates the fix, and only
      a plant replaces it.

      So the next attempt is three commands, in this order:

          wk bench mac-volume --repair          # on the Mac: re-arm the daemon
          wk bench mac-ab --a 20260906T233003Z-mac-release-pgo \
                          --b 20260907T021244Z-mac-release-pgo \
                          --rounds 1 --detect 0 --count 1 --shutdown --force
          # hold the power button, pick WK Bench

      `--force` is required and is the point: the preflight refuses an
      unprovisioned volume, and the plant carries the autorun that lets
      provisioning finish. That boot provisions and reboots itself to the
      firmware default (the host install), so `wk bench mac-ab --preflight`
      should then read `ok provisioned` -- and the A/B needs one more pick of
      the volume [needs two boots of the volume]

## One A/B iteration

- [ ] read the first number back, once that boot has run:

          wk bench mac-ab --progress            # where it is
          wk bench mac-ab --collect             # the numbers

      `--detect 0` above runs `--rounds` exactly. Drive it from another
      machine: the lane reboots the Mac and refuses to be driven from it
      [needs the boots above]

- [ ] then the real one: drop `--rounds 1 --detect 0 --count 1` and let it
      alternate until every plan resolves 0.3%, between 5 and 40 rounds.
      Measured off this volume's own 2026-08-24 A/B, speedometer3 needs ~29
      rounds a side for 0.3%; whether motionmark reaches it below the 40-round
      ceiling at all is unmeasured, so expect the ceiling on at least one plan
      [needs the volume]

## What a failing run has to say for itself

- [ ] `wk status <ws>` still reports `state=stalled` and exit 3 for a workspace
      whose full-LTO link has been silent past `WK_STALL_SECONDS` (300s;
      `build_live`, lib/detach.sh), which is also the state `wk status --wait`
      stops waiting through. The reading that settles it is `build_processes`
      (lib/detach.sh) and `wk status` now prints it, but it counts *this
      machine's* compilers and linkers, and a machine holding several
      workspaces cannot say which build they belong to. Measure that
      attribution before letting `build_live` read it [no hardware needed]

- [ ] `wk status` says nothing about a macOS A/B in flight. Its `bench` section
      reads this host's own store, and a mac-ab's rounds and results live on the
      volume. `wk bench mac-ab --progress` is the only view, and only from host
      mode [no hardware needed]

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

- [ ] two readers, two definitions of provisioned: the autorun and
      `wk bench mac-ab` ask the first-boot log for a completion line, while
      `wk bench staged` measures the settings themselves before every leg. A
      volume that drifts after provisioning reads as provisioned and is still
      refused leg by leg, unobservably. `refuse_unprovisioned`
      (bench/mac-bench-autorun.sh) runs on the install itself, so it could ask
      `wk_quiet_desktop_probe` and report the same finding once, up front
      [no hardware needed]

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
