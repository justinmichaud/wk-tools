# HANDOFF — the 2.46 comparison set

Three Speedometer 2.1 comparisons, all yocto, all measured from slots built
from each system's own release tip. What is owed to finish them. Fleet-wide
and lane-wide items live in `HANDOFF-fleet.md` and `HANDOFF-ab-bench.md`.

Done, and nothing is owed for it: **rpi4, 64-bit, 2.46 vs 2.52** —
`$WK_STORE/bench/20260903T012124Z-rpi4-systems`, 4 usable rounds,
A=22.681+-0.528 (wpewebkit-2.46), B=22.330+-0.572 (webkit-2.52), B 1.55%
slower, p=0.0000, `report-rpi4-speedometer2.1.html` beside the runs.

## rpi3, 32-bit, 2.46 vs 2.52 — running

- [ ] the A/B is in flight: task `20260903T131642Z-rpi3-systems`, driver pid
      3265986, stdout at
      `/tmp/claude-1000/-home-jmichaud-Development-wk-tools/09ffac47-e525-43fa-a1f4-bfc9ed07a0ee/scratchpad/rpi3-ab3.log`.
      Started detached with `setsid nohup`, so it outlives any session. At
      5/10 runs with no lost leg. When it ends:
      `wk bench report 20260903T131642Z-rpi3-systems --html`. A round needs
      both arms to count; ~30 min per run on this board [no hardware needed]
- [ ] if it is dead and incomplete, re-run it as-is -- both systems on the
      card carry slot `base` already:
      `wk pi bench rpi3 speedometer2.1 --ab-systems wpewebkit-2.46-yocto-rpi3-32-4c2727985989,webkit-2.52-yocto-rpi3-32-ebb646f3bf67 --slot base --rounds 5 --timeout 1800`
      [no hardware needed]

## rpi5, 2.46 32-bit userspace vs 2.46 64-bit — blocked

Both systems are on the stick (`/dev/sda`: `sda1/2` =
`wpewebkit-2.46-yocto-rpi5-64-9ee1cf59c4d1`, `sda3/4` =
`wpewebkit-2.46-yocto-rpi5-32-64f773b84146`), and both images are built and
`ready` in their workspaces.

- [ ] `wk boot rpi5 --system <id>` refuses with "/dev/sda on rpi5 holds no wk
      system yet" while the medium provably holds two (`lsblk`: sda1 boot,
      sda2 root, sda3 boot, sda4 root; both writes reported the ids above).
      `b_device_image` (boot/machines.sh) reads `wk-image.id` by mounting the
      boot partition through `r_sudo`, which on a workstation is
      `ssh sudo -n` -- and rpi5 answers `sudo: interactive authentication is
      required` for `mkdir`/`mount`, so the probe reads nothing and
      `b_systems` reports an empty medium. Every board whose medium hangs off
      a `MACH_ROLE=workstation` machine has this; rpi3 and rpi4 are unaffected
      because a bench-device is driven as root. Decide where the privilege
      goes: a card-helper verb that reads the marker (the helper is already
      root-owned and installed on rpi5), or a narrow sudoers entry
      [decision, then no hardware needed]
- [ ] after that: build the two slots, one at a time, from the tip both
      images were built from (`6977eef7cd9ab01506bf0ff131dc169e8cfac601`):
      `wk sysimage webkit wpewebkit-2.46-yocto-rpi5-64 --commit 6977eef7cd9ab01506bf0ff131dc169e8cfac601 --slot base --detach`
      and the same for `wpewebkit-2.46-yocto-rpi5-32`. Each wants ~20 GB and
      `disk_admit` refuses below `WK_BUILD_DISK_GB`; 31 GB free as this is
      written, so reclaim first (`wk disk`, and `HANDOFF-ab-bench.md` on the
      seed cache) [no hardware needed]
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

## Constraints that bound all of the above

- [ ] one build at a time on this host, and no build while a board is
      measuring: concurrent builds have OOM-killed podman here, and the
      benchmark driver runs on this machine [no hardware needed]
- [ ] never edit this checkout while a `wk` command runs -- a detached build
      and every `wk boot` re-read scripts from it mid-run [no hardware needed]
