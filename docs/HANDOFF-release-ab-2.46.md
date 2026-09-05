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
      separates them [needs the rpi5 pair]

## rpi5, 2.46 32-bit userspace vs 2.46 64-bit

Both systems are on `/dev/sda` (`sda1/2` = `wpewebkit-2.46-yocto-rpi5-64-f3e2d8d0a46c`,
`sda3/4` = `wpewebkit-2.46-yocto-rpi5-32-9e58345f6a25`), written 2026-09-04 from
images carrying the D0 overlay, both built from
`6977eef7cd9ab01506bf0ff131dc169e8cfac601`.

**The 64-bit system is done and needs nothing.** It boots in ~42s, joins as
`rpi5-bench`, holds `wk boot --keep`, returns on `--back`, has its `base` slot
deployed (WebKit `6977eef7cd9a`, build-id `2f19338d5dadf7a5a8fa11bd150cd745e85b8672`)
and a connected DRM output (`card0-HDMI-A-2`). The board's own silicon needs
`overlays/bcm2712d0.dtbo`, which `image/boards/rpi5/local.conf.append` states
and asks for.

- [ ] **the 32-bit system does not reach userspace.** Armed at 2026-09-04
      23:53:00Z, it never appeared under either name and the 300s watchdog
      never returned the board, so it stopped before the userspace that runs
      the watchdog -- the same shape the 64-bit had before the overlay, but
      not the same cause: the two systems are on one card, share a kernel and
      a device tree, and the 64-bit one boots. What differs is the userspace
      (poky multilib, `YOC_MULTILIB=lib32`) and the pair (3, selected by
      `[tryboot]`, armed here for the first time). Read the console over HDMI
      on one boot: an init that dies says so, and separates a broken 32-bit
      rootfs from a pair-3 selection that landed somewhere unintended
      [needs the board, a monitor, and a power cycle]
- [ ] then `wk pi deploy wpewebkit-2.46-yocto-rpi5-32 rpi5 --slot base`, and
      `wk pi bench rpi5 speedometer2.1 --ab-systems
      wpewebkit-2.46-yocto-rpi5-64-f3e2d8d0a46c,wpewebkit-2.46-yocto-rpi5-32-9e58345f6a25
      --slot base --rounds 5`
- [ ] `wk boot --status` printed a boot time taken from the arming record
      while the board was mid-reboot, which reads as a machine that has been
      up since a boot it has already left. The fallback is deliberate; what is
      owed is that a fallback value says it is one [no hardware needed]
- [ ] rpi5's installed card helper predates the read-a-mounted-partition fix,
      and its desktop session automounts both boot partitions, so every marker
      read fails and each write and arm needs the mounts cleared first:
      `./setup --stage quiesce` from a terminal on rpi5, the only thing that
      can replace a helper [needs the rpi5]

## Constraints that bound all of the above

- [ ] one build at a time on this host, and no build while a board is
      measuring: concurrent builds have OOM-killed podman here, and the
      benchmark driver runs on this machine [no hardware needed]
- [ ] never edit this checkout while a `wk` command runs -- a detached build
      and every `wk boot` re-read scripts from it mid-run [no hardware needed]
