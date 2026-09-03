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

## rpi5, 2.46 32-bit userspace vs 2.46 64-bit — owed

Both systems are on the stick (`/dev/sda`: `sda1/2` =
`wpewebkit-2.46-yocto-rpi5-64-9ee1cf59c4d1`, `sda3/4` =
`wpewebkit-2.46-yocto-rpi5-32-64f773b84146`), both read off the medium by
`wk boot rpi5 --system <id>`, and the stick's `autoboot.txt` carries
`[all] boot_partition=1` and `[tryboot] boot_partition=3`, so either pair can
be armed. The 64-bit slot is built (`base`, build-id 2f19338d).

- [ ] rpi5's card helper was installed from that machine's own checkout after
      the tree was synced there; keep it in step with this one, since
      `wk sysimage write` and every medium read now need `boot-read`
      [no hardware needed]
- [ ] finish the 32-bit slot build, then confirm from the workspace -- not the
      board -- that it is actually 32-bit: `file` on
      `WebKitBuild/wk-slots/base/root/bin/MiniBrowser` must say `ELF 32-bit`
      `ARM`, and `slot.json`'s `target` must say `rpi5-32bits-mesa`. The 64-bit
      slot reads `ELF 64-bit ARM aarch64`. The slot is `build-webkit
      --cross-target` against the image's toolchain, and the image's *userspace*
      being lib32 does not by itself make the slot 32-bit: if it comes out
      aarch64 the comparison measures nothing and the multilib has to reach the
      slot build [no hardware needed]
- [ ] then, per system: `wk boot rpi5 --system <id>`, `wk boot rpi5 --keep`,
      `wk pi deploy wpewebkit-2.46-yocto-rpi5-<width> rpi5 --slot base`, and
      finally
      `wk pi bench rpi5 speedometer2.1 --ab-systems <64-id>,<32-id> --slot base --rounds 5`
      [needs the rpi5 free of its own workstation session]
- [ ] confirm on the booted board that the process under test is the 32-bit one
      (`/proc/<pid>/exe` of the running WPEWebProcess), which the file check
      above cannot answer: the board could still load a 64-bit browser from the
      image rather than the slot [needs the board booted into the 32-bit system]

## Constraints that bound all of the above

- [ ] one build at a time on this host, and no build while a board is
      measuring: concurrent builds have OOM-killed podman here, and the
      benchmark driver runs on this machine [no hardware needed]
- [ ] never edit this checkout while a `wk` command runs -- a detached build
      and every `wk boot` re-read scripts from it mid-run [no hardware needed]
