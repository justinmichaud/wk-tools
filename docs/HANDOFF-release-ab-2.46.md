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

## rpi5, 2.46 32-bit userspace vs 2.46 64-bit — the board does not boot its stick

This is commissioning, not repair: `wk boot rpi5` has never booted a wk-written
system. Everything either side of the boot is done and verified.

Ready and checked: both systems on `/dev/sda` (`sda1/2` =
`wpewebkit-2.46-yocto-rpi5-64-9ee1cf59c4d1`, `sda3/4` =
`wpewebkit-2.46-yocto-rpi5-32-64f773b84146`), freshly written; `root=PARTUUID=`
agrees with the disk signature (`987478fd-02` / `987478fd`); `boot-check` says
every file the firmware asks for resolves; `autoboot.txt` selects pair 1 under
`[all]` and pair 3 under `[tryboot]`; `os_check=0` on both boot partitions; the
tailnet identity and WiFi credential seeded onto both; both slots built from
`6977eef7cd9ab01506bf0ff131dc169e8cfac601` with widths confirmed from the slots
(64-bit `MiniBrowser` ELF 64-bit aarch64, 32-bit ELF 32-bit ARM EABI5).

What happens: `wk boot rpi5 --system <64-bit id>` arms and reboots, and the
board never returns. Not on the tailnet under either name, and not on its NVMe.
Twice, ~30 min each, each needing a power cycle.

What that rules out, and it is most of the field. The armed order is `0xf64` --
USB, then NVMe, then restart. A firmware that *rejected* the kernel (missing
`os_check`) or *skipped* the stick (the rpi4's measured USB-MSD refusal, README)
would fall through to the NVMe and come up as the workstation, reachable. It
never does. So the firmware hands off to the USB kernel and the kernel hangs
before networking. `os_check=0` was missing and is now fixed, but it was not
this.

Neither image carries an initramfs, so the kernel must reach a USB root on
built-in drivers with `rootwait`, which waits forever if that root never
appears -- the shape of a board that hangs, never networks, never reaches the
watchdog that would hand it back, and needs a power cycle. The rpi4 mounts a
USB root successfully, but on a different kernel build (`kernel8.img` against
the Pi 5's 16K-page `Image-6.6.22-v8-16k`), so it is not proof the rpi5 kernel
can.

Every attempt below costs a power cycle to recover. They are ordered by answer
per cycle.

- [ ] read the console over HDMI, one boot. Everything else here infers what a
      console states outright, and the kernel is talking to nobody
      [needs the board and a monitor]
- [ ] failing that, make a hang distinguishable from a panic without a console:
      `image/boards/rpi5/cmdline.txt.append` with `panic=10`, and swap
      `rootwait` for `rootdelay=15` so a root that never appears panics instead
      of waiting. `cmdline_append_text` (cmd/sysimage) already reads that file
      per profile; it needs the board-level half `config.txt.append` now has.
      Re-write the card, arm, and time the return: back on the NVMe in ~30s
      means the kernel panicked on a missing root, which names the fault;
      still dark means it stops earlier than rootfs. `panic=10` is the idiom
      pi-tryboot already stages for this [no hardware for the change; a cycle
      to test]
- [ ] try pair 3 (the 32-bit system) once. It has never been armed, and a pair
      that boots when the other does not would move the fault into the image
      rather than the lane [one cycle]
- [ ] if the kernel is the fault, compare what the rpi4-64 image builds against
      what rpi5-64 does for USB storage, xHCI and ext4 -- built-in against
      module. Neither image has an initramfs, so anything modular on the root
      path cannot work. The rpi4 is the control: its bench root is on USB and
      it boots [no hardware needed]
- [ ] the lane has a documented precedent for exactly "armed, complete files,
      does not boot from USB": the rpi4 stages the bench kernel onto another
      medium and uses the firmware tryboot flag instead (README, `pi-tryboot`).
      rpi5-usb chose USB because tryboot was taken by flash-kernel's staging on
      the NVMe. If USB boot cannot be made to work, that decision is what to
      revisit [decision]

Once it boots, the rest is already staged: `wk boot rpi5 --keep` inside the
300s watchdog, `wk pi deploy wpewebkit-2.46-yocto-rpi5-<width> rpi5 --slot
base` per system, then `wk pi bench rpi5 speedometer2.1 --ab-systems
<64-id>,<32-id> --slot base --rounds 5`.

- [ ] confirm on the booted board that the process measured is the slot's
      binary and not the image's own browser (`/proc/<pid>/exe` of the running
      WPEWebProcess). The slots' widths are settled; which one the board loads
      is not [needs the board booted]
- [ ] `boot-read` cannot read `config.txt`, so whether an append landed can
      only be inferred from the write log. Adding it to the allowlist would
      have made the `os_check` question a one-line check instead of a
      hypothesis [no hardware needed]
- [ ] `boot-read` reads a partition an automounter already holds rather than
      failing to mount it twice. Fixed and unit-tested, but proven only against
      an unmounted card -- rpi5 runs the helper its last `./setup --stage
      quiesce` installed, and a helper cannot replace itself [needs the rpi5]

## Constraints that bound all of the above

- [ ] one build at a time on this host, and no build while a board is
      measuring: concurrent builds have OOM-killed podman here, and the
      benchmark driver runs on this machine [no hardware needed]
- [ ] never edit this checkout while a `wk` command runs -- a detached build
      and every `wk boot` re-read scripts from it mid-run [no hardware needed]
