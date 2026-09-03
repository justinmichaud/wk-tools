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
`wpewebkit-2.46-yocto-rpi5-32-64f773b84146`), and both images are built and
`ready` in their workspaces.

- [ ] rpi5's installed card helper is a loose copy: this tree's
      `admin/wk-card-priv` was scp'd into that machine's checkout and
      installed from there, so the checkout is dirty and its next `git pull`
      reverts the helper without saying so. Commit this tree and pull it
      there [no hardware needed]
- [ ] confirm the stick's `autoboot.txt` carries `[tryboot] boot_partition=3`.
      Nothing has ever read it — the write that made the second pair should
      have put it there, and an arming for pair 3 refuses without it:
      `wk boot rpi5 --system wpewebkit-2.46-yocto-rpi5-32-64f773b84146 --dry-run`
      answers [no hardware needed]
- [ ] build the two slots, one at a time, from the tip both images were built
      from (`6977eef7cd9ab01506bf0ff131dc169e8cfac601`):
      `wk sysimage webkit wpewebkit-2.46-yocto-rpi5-64 --commit 6977eef7cd9ab01506bf0ff131dc169e8cfac601 --slot base --detach`
      and the same for `wpewebkit-2.46-yocto-rpi5-32`. Each wants ~20 GB and
      `disk_admit` refuses below `WK_BUILD_DISK_GB` [no hardware needed]
- [ ] then, per system: `wk boot rpi5 --system <id>`, `wk boot rpi5 --keep`,
      `wk pi deploy wpewebkit-2.46-yocto-rpi5-<width> rpi5 --slot base`, and
      finally
      `wk pi bench rpi5 speedometer2.1 --ab-systems <64-id>,<32-id> --slot base --rounds 5`
      [needs the rpi5 free of its own workstation session]
- [ ] the 32-bit arm is a multilib image: 64-bit kernel, device tree and
      firmware exactly as the rpi5-64 profile builds them, userspace at
      `armv7vethf-neon-vfpv4`. Confirm on the booted board that the browser
      under test is 32-bit (`file` on the slot's MiniBrowser, or
      `/proc/<pid>/exe`) before believing a width comparison
      [needs the board booted into the 32-bit system]
- [ ] `b_medium_read`'s workstation half is proven by its unit tests and by
      the addressing it emits, not yet against a real medium. `wk boot rpi5
      --status` once the helper is installed is that evidence [needs the rpi5]

## Constraints that bound all of the above

- [ ] one build at a time on this host, and no build while a board is
      measuring: concurrent builds have OOM-killed podman here, and the
      benchmark driver runs on this machine [no hardware needed]
- [ ] never edit this checkout while a `wk` command runs -- a detached build
      and every `wk boot` re-read scripts from it mid-run [no hardware needed]
