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

Both systems are on `/dev/sda` (`sda1/2` 64-bit, `sda3/4` 32-bit), built from
`6977eef7cd9ab01506bf0ff131dc169e8cfac601`, widths confirmed from the slots.
The board is D0 silicon and needs the device tree named after its stepping;
`image/boards/rpi5/local.conf.append` states the fault and asks for it.

- [ ] rebuild both rpi5 images with the D0 tree, rewrite both pairs of
      `/dev/sda`, arm, and take the measurement. Rewriting the primary pair
      erases the whole disk, so the 32-bit system is written again after it
      (`--disk rpi5:/dev/sda@second`). Then `wk boot rpi5 --keep` inside the
      300s watchdog, `wk pi deploy wpewebkit-2.46-yocto-rpi5-<width> rpi5
      --slot base` per system, then `wk pi bench rpi5 speedometer2.1
      --ab-systems <64-id>,<32-id> --slot base --rounds 5`
- [ ] the image ships 52 of the kernel's 367 overlays and no
      `overlays/overlay_map.dtb`, which is what makes the firmware substitute
      a Pi 5 variant for an overlay that has one — and the image's config.txt
      asks for `dtoverlay=vc4-kms-v3d`, whose Pi 5 variant is
      `vc4-kms-v3d-pi5`. Unmeasured: check it when the board boots and the
      browser has to render, not before [needs the board booted]
- [ ] confirm on the booted board that the process measured is the slot's
      binary and not the image's own browser (`/proc/<pid>/exe` of the running
      WPEWebProcess). The slots' widths are settled; which one the board loads
      is not [needs the board booted]
- [ ] `NODE_DTB` (boot/machines/rpi5.conf) is a stored copy of a fact the
      board can be asked for: the firmware picks the tree from the board
      revision, which is one read of `/proc/cpuinfo` on a machine that has to
      be up for `boot-check` to run at all. Derive it in `image_dtb_for`
      instead, and the class of "boot-check passed a card that cannot boot"
      goes with it [no hardware needed]
- [ ] `cmdline_append_text` (cmd/sysimage) reads only the profile's
      `cmdline.txt.append`; it needs the board-level half `config.txt.append`
      and now `local.conf.append` have. What the rpi5 wants in it is
      `panic=10` and a bounded `rootwait=30` — this kernel takes a value there
      (`rootwait_timeout_setup`, init/do_mounts.c), so a root that never
      appears panics and the one-shot returns the board to its NVMe instead of
      costing a power cycle [no hardware for the change; a cycle to test]
- [ ] rpi5's installed card helper predates the read-a-mounted-partition fix,
      and its desktop session automounts both boot partitions, so every
      marker read fails and `wk boot rpi5 --system <id>` refuses. Clearing the
      mounts is a workaround, not the fix: `./setup --stage quiesce` from a
      terminal on rpi5, which is the only thing that can replace a helper
      [needs the rpi5]

## Constraints that bound all of the above

- [ ] one build at a time on this host, and no build while a board is
      measuring: concurrent builds have OOM-killed podman here, and the
      benchmark driver runs on this machine [no hardware needed]
- [ ] never edit this checkout while a `wk` command runs -- a detached build
      and every `wk boot` re-read scripts from it mid-run [no hardware needed]
