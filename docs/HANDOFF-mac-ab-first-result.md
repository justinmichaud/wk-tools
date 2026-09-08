# HANDOFF — the macOS A/B lane

## Owed, no hardware needed

- [ ] `wk bench report` shows no headline row for jetstream3 or motionmark.
      Those plans write `{"Score": ["Geometric"]}` on the suite root — an
      aggregator declaration carrying no values — and `_subtest_metrics`
      (lib/wkdata.py) drops the suite prefix from first-level children, so
      jetstream3's 77 subtests and motionmark's 8 are indistinguishable from
      the suite by its `"/" not in name` test. `ab-precision` computes the
      declared aggregate; `report` does not, so a report of either plan is
      subtests and no total. Changing `_subtest_metrics`' row set or naming is
      what it takes, and tests/test_bench_report.py pins both

- [ ] `cmd/build` does not record its abort deadline, so nothing can tell a
      build that is merely `silent` from one whose watchdog was killed with -9
      — a live `cmd/build` would have written `stalled` by the deadline. It is
      a record of a choice and not a cached fact: `WK_ABORT_SECONDS` is a
      per-run override and `cmd/bench` passes 5400. Wants `abort_after=` in
      `build.status` and a reader in `cmd/status`; a field with no reader is
      dead data

- [ ] between the plant and `--collect`, a mac-ab task has no runs, so
      `task-status` reads `incomplete` and `wk status` warns "stopped before
      every planned run ended". Accurate for a task, wrong for this one, and it
      clears on collect. The state a planted job is in — measuring, or powered
      off with the result on the volume — is the same one `b_probe`
      (boot/mac-volume.sh) declines to guess between, so the note should say
      that instead

- [ ] five callers interpolate a `lib/resources.sh` reading into a word instead
      of assigning it, so a refusal prints its message and the caller continues
      with an empty value: `setup:58`, `image/yocto.sh:396,405`,
      `targets/container.sh:156-157`, `targets/vm.sh:46-47`,
      `cmd/bench:563,568`. Each wants `v=$(...)` on its own line;
      `cmd/status:539-541` already does it

- [ ] `wk bench mac-ab --machine benchvm` (the guest rehearsal) has lost two
      things it needed and should be rebuilt or dropped. It refuses for want of
      a `NODE_DISPLAY`, and must not gain one: the guest's display is declared
      by `WK_VM_DISPLAY` in targets/vm.sh, so a copy in
      `boot/machines/benchvm.conf` would be a stored copy of a recomputable
      fact — the plant should read it from the target that sets it. And the
      job's `force` field is gone: it was the rehearsal's way past the quiet
      check, but one `--force` meant both "cross the driver's barriers" and
      "pass `--force` to every leg", so crossing a preflight barrier silently
      disabled each leg's own quiet-machine gate. Whatever the rehearsal gets
      instead must not be reachable from the flag that crosses a barrier

- [ ] two implementations of "run this on that machine": `m_ssh`
      (boot/machines.sh) branches on `NODE_LOCAL`, and `mv_sh`
      (boot/mac-volume.sh) branches on `is_macos`. `mv_sh`'s condition is the
      right one — `NODE_LOCAL=1` claims "the machine drives itself", which is
      only true when the caller is standing on it, so from moose `m_ssh` for
      `mbp` would run the command on moose and answer about the wrong computer.
      Folding `m_ssh` onto the same test ("am I the machine NODE_SSH names",
      which bench/mac-ab.sh already spells with `hostname -s`) would retire
      `mv_sh` and `NODE_LOCAL` together. It touches only the two machines with
      `NODE_LOCAL` set, `mbp` and `benchvm`, and neither is exercised by a test
      that could catch a mistake — hence not done here

## Owed, needs the Mac

- [ ] `wk boot mbp --prepare` has not been run, so the boot helper is not on
      tolken and `wk bench mac-ab` refuses in preflight ("restartable"). One
      command from a terminal does it — it rsyncs this tree and runs
      `./setup --stage quiesce` there, asking for a password once, and the lane
      does it itself when it has a terminal. Measured 2026-09-08: with a console
      user logged in and osascript automation working, `«event aevtrrst»` was
      declined, because a graceful restart is refusable by any application that
      will not quit; `sudo -n` there wants a password. Until it is prepared the
      job stays planted and correct and any reboot runs it

- [ ] `wk bench mac-ab --shutdown` still hand-rolls its transition through
      System Events: `admin/wk-boot-priv` has no halt verb, and loginwindow
      answers no shutdown event at all (-1708, measured 2026-09-07). So "one
      implementation of restarting this Mac" is true of the restart and not of
      the shutdown. A `halt` verb on the helper is the shape that would fix it
      — a fixed verb, no argument — and whether `--shutdown` is still wanted at
      all now that a cold start enters bench mode by itself is the prior question

- [ ] one boot proves the darwin `tailscaled` (bench/mac-tailnet.sh). Everything
      above and below it is proven: the binary cross-builds pure-Go and runs on
      tolken, `cmd/tailscaled/tailscaled.go:280-282` gates on nothing but uid 0,
      and the utun path is `com.apple.net.utun_control`. What the boot has to
      show is that `launchctl bootstrap system` opens that utun as root with no
      panel on macOS 26.6.2, that `tailscale up --auth-key file:` joins
      unattended and spends the key, that `ssh tolken-bench` reaches the install
      *while it measures*, that `--accept-dns=false` wrote no `/etc/resolver`,
      and that a `--repair` restage rejoins on the same tailnet IP with no `-1`.
      A relayed rather than direct connection is the expected symptom of macOS
      local-network privacy, not a fault

- [ ] whether `bless --setBoot` needs a volume-owner credential is now measured
      by running it rather than asserted: the helper blesses with a credential
      where the machine holds one and with root alone where it does not, and
      each verb says which form it used and what bless answered. Read on tolken
      2026-09-08: `bless --help` lists `--user`/`--stdinpass` under *Snapshot
      options* only — Mount Mode names neither — and
      `/usr/local/share/wk-bench/` does not exist there. So the owed work is one
      run: `wk boot mbp --prepare`, then `wk boot mbp`, then read the form it
      took. If root alone suffices, the credential drops out of `boot-host`
      entirely [needs the Mac]

- [ ] a `wk_quiet_desktop_probe` row reporting `tailscaled` as expected-running.
      It must never join `wk_quiet_desktop_stopped`: pausing it drops the
      tailnet mid-leg, which is the thing it exists to fix, and leaves a live
      utun with nothing draining it. Measured on moose, the fleet's busiest
      node: 1.78% of one core over 3.7 days, RSS 71 MB

- [ ] collecting a result still needs a finger on the power button, and one
      chain removes it. The bench install powers the Mac off; the firmware
      default is the bench volume, so the next boot measures again rather than
      coming back to host mode. If `bless --setBoot` turns out to need no
      credential (above), the bench install could bless the host install before
      it halts, and then a wake — `pmset -a womp 1` on that install, plus a
      magic packet from moose — reaches host mode with nobody in the room. Two
      things are unbuilt for that: `wk-boot-priv` is installed only on the host
      install, so `stage_payload` would have to put it on the volume too and
      `bench_install`'s gate would have to be re-read from the other side; and
      whether a Mac halted by `shutdown -h` wakes on LAN at all is unmeasured
      [needs the Mac, after --prepare]

## Decision

- [ ] the wk-tools tree on moose carries this work uncommitted, and tolken runs
      from a scratch clone at `~/Development/wk-tools-wip` (5f2848c), deployed
      by rsync-and-commit. `wk sync --tools` refuses an uncommitted tree by
      design, so landing this means committing it. The 2026-09-07 A/B was
      planted from a tree older than moose's HEAD — its planted copy of the
      autorun carries no `detect_off` — which is why `--rounds 1 --detect 0`
      ran seventeen rounds
