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

## rpi5, 2.46 32-bit userspace vs 2.46 64-bit — ready to measure

Both systems are on the stick (`/dev/sda`: `sda1/2` =
`wpewebkit-2.46-yocto-rpi5-64-9ee1cf59c4d1`, `sda3/4` =
`wpewebkit-2.46-yocto-rpi5-32-64f773b84146`), both read off the medium by
`wk boot rpi5 --system <id>`, and the stick's `autoboot.txt` carries
`[all] boot_partition=1` and `[tryboot] boot_partition=3`, so either pair
arms. Both slots are built from `6977eef7cd9ab01506bf0ff131dc169e8cfac601`
and their widths are confirmed from the slot itself, not the board:
64-bit `base` build-id 2f19338d, `MiniBrowser` ELF 64-bit aarch64; 32-bit
`base` build-id ae181e9f, `MiniBrowser` ELF 32-bit ARM EABI5 interpreter
`/lib/ld-linux-armhf.so.3`.

- [ ] per system: `wk boot rpi5 --system <id>`, `wk boot rpi5 --keep`,
      `wk pi deploy wpewebkit-2.46-yocto-rpi5-<width> rpi5 --slot base`, and
      finally
      `wk pi bench rpi5 speedometer2.1 --ab-systems <64-id>,<32-id> --slot base --rounds 5`
      [needs the rpi5 free of its own workstation session]
- [ ] confirm on the booted board that the process measured is the slot's and
      not the image's own browser: `/proc/<pid>/exe` of the running
      WPEWebProcess. The slot's width is settled; which binary the board loads
      is not [needs the board booted into each system]
- [ ] rpi5's card helper is installed from that machine's own checkout; keep
      it in step with this one, since every medium read now needs `boot-read`
      [no hardware needed]

## Constraints that bound all of the above

- [ ] one build at a time on this host, and no build while a board is
      measuring: concurrent builds have OOM-killed podman here, and the
      benchmark driver runs on this machine [no hardware needed]
- [ ] never edit this checkout while a `wk` command runs -- a detached build
      and every `wk boot` re-read scripts from it mid-run [no hardware needed]
