# HANDOFF — the 2.46 comparison set

Three Speedometer 2.1 comparisons, all yocto, all measured from slots built
from each system's own release tip. What is owed to finish them. Fleet-wide
and lane-wide items live in `HANDOFF-fleet.md` and `HANDOFF-ab-bench.md`.

Done, and nothing is owed for either: **rpi4, 64-bit, 2.46 vs 2.52** —
`$WK_STORE/bench/20260903T012124Z-rpi4-systems`, 4 usable rounds,
A=22.681+-0.528 (wpewebkit-2.46), B=22.330+-0.572 (webkit-2.52), B 1.55%
slower, p=0.0000. **rpi3, 32-bit, 2.46 vs 2.52** —
`$WK_STORE/bench/20260903T131642Z-rpi3-systems`, 10/10 runs, 5 usable rounds
of 5, A=10.788+-0.228, B=9.325+-0.114, B 13.55% slower, p=0.0000. Both carry
a `report-<board>-speedometer2.1.html` beside the runs.

- [ ] the rpi3 pair regresses nine times harder than the rpi4 pair, broadly
      rather than in one subtest, and mostly in *Sync* time (Elm
      CompletingAllItems +26%, Elm DeletingItems +22%, Angular2 Adding100Items
      +23%). Whether that is 32-bit, the board, or the release is not
      answered by these two runs. The rpi5 pair below holds the board and the
      release fixed and varies only the width, which is the measurement that
      separates them [no hardware needed to decide, then see below]

## rpi5, 2.46 32-bit userspace vs 2.46 64-bit — blocked: the bench system does not boot

Everything up to the boot is done and verified. Both systems are on the stick
(`/dev/sda`: `sda1/2` = `wpewebkit-2.46-yocto-rpi5-64-9ee1cf59c4d1`, `sda3/4` =
`wpewebkit-2.46-yocto-rpi5-32-64f773b84146`), both slots are built from
`6977eef7cd9ab01506bf0ff131dc169e8cfac601` with their widths confirmed from the
slots themselves (64-bit build-id 2f19338d, `MiniBrowser` ELF 64-bit aarch64;
32-bit build-id ae181e9f, ELF 32-bit ARM EABI5, interpreter
`/lib/ld-linux-armhf.so.3`), and `wk boot rpi5 --system <id>` now arms and
reboots the board.

- [ ] armed into the 64-bit system, the board never came back and never
      appeared on the tailnet under any name. It did not self-return either:
      the 900s watchdog never fired, so it did not reach the userspace that
      arms it, and a power cycle was needed. What the card says afterwards:
      `wk-image.id` and `autoboot.txt` are on the boot partition, the rootfs
      mounts and carries its `/etc/wk-image` marker, and both join units are
      installed (`wk-card-priv joins` and `wifi-joins` answer yes) -- but there
      is **no `wk-diag.txt`**. That file is written by a userspace unit ~75-90s
      in and exists to capture exactly a network failure, so its absence says
      the system did not get that far: this is earlier than WiFi or the
      tailnet, and re-seeding credentials will not address it. Next step is a
      console -- HDMI or serial on ttyAMA10 (`SERIAL_CONSOLES` in
      `raspberrypi5.conf`) -- to see where it stops
      [needs the board and a console]
- [ ] `boot-read` reads a partition an automounter already holds, rather than
      failing to mount it a second time. Fixed and unit-tested, but proven only
      against an unmounted card: rpi5 still runs the helper its last `./setup
      --stage quiesce` installed, and a helper cannot replace itself. Re-check
      with the card automounted after the next setup there
      [needs the rpi5]
- [ ] the 32-bit system has never been booted at all. Whatever stops the
      64-bit one may or may not affect it; do not assume the pair is dead
      until it has been tried [needs the board]

## Constraints that bound all of the above

- [ ] one build at a time on this host, and no build while a board is
      measuring: concurrent builds have OOM-killed podman here, and the
      benchmark driver runs on this machine [no hardware needed]
- [ ] never edit this checkout while a `wk` command runs -- a detached build
      and every `wk boot` re-read scripts from it mid-run [no hardware needed]
