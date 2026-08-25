# HANDOFF — the boot substrate: what is left

The substrate is built and exercised on hardware. `wk sysimage
build|ls|show|disks|write|rm` produces and stores systems (unprivileged: mtools
and e2fsprogs edit images at byte offsets, nothing is mounted, `wk` never calls
sudo on this host); `wk boot <machine> [--system|--status|--keep|--back|--disarm|
--diag|--dry-run]` arms one boot with a per-machine driver; `wk pi boot-order`
and `boot/rpi-eeprom.sh` handle Pi firmware; `boot/check-boot-files.py` models
what firmware asks of a boot tree and runs before anything touches a disk. The
rpi5 and rpi4 have both been through the whole cycle with no hands on the board.

`docs/TESTING.md` §7 is the verification ledger.

## Remaining

- **Nothing gates on an armed machine.** The display half landed — `wk status`
  ends with the fleet block (role, mode, the media wk owns and what is on it,
  an `armed for <id>` flag), fed by each driver's `b_media`/`b_probeable` — but
  no mutating command warns or refuses when the machine it targets is armed.
  The clearest hole left here.
- **moose has no bench mode.** Its lane is a RAM root off a USB stick armed by
  UEFI `BootNext`, which supersedes the BMC-virtual-media idea: a service
  processor emulating USB storage puts itself inside every root-filesystem read.
  Entirely unbuilt — `docs/Urgent/HANDOFF-moose-bench.md`.
- **The rpi3 has no bench system**, so it cannot be measured and `wk pi bench
  rpi3` refuses: its card holds one system
  (`downstream-yocto-wpe-2.48-rpi3-32-20260822T205734Z`, built before the
  2026-08-25 rename) and that system is the
  base image. The arrangement it needs was decided on 2026-08-24 and is written
  up where design belongs — `wk help hardware`, "why the three Pis are arranged
  differently", plus the binding priority order in CLAUDE.md. What is *left* is
  the work:
    1. a rescue-role system for slot A (`IMG_ROLE=rescue`, so no self-return
       watchdog and no self-disarm) and a bench system for slot B;
    2. a slot-aware `wk sysimage write`, which today writes one whole system to
       one whole device and cannot put a system into a slot without destroying
       the other;
    3. arming for `boot/pi-sd.sh` — `root=` plus the bench kernel installed onto
       the shared boot partition — and the revert that goes with a stage-2
       arming, which is an initramfs fallback or a rescue-side pivot, because a
       kernel that cannot mount the armed root would otherwise panic-loop;
    4. the BusyBox equivalents of the self-return watchdog and self-disarm, for
       an image with no systemd.
  None of it is startable from a workstation alone: (1) needs a build, (2) and
  (3) need the card, and the first write is hands-on.
- **The rpi4's SD holds a bench-profile image acting as its rescue**, so it
  carries a 900-second self-return watchdog and reboots itself every 15 minutes
  while the board sits on it. Rewrite it with a rescue-role image; needs a build
  and a card.
- **`/usr/bin/tee` is NOPASSWD on moose** — passwordless write to any file, so
  equivalent to NOPASSWD root. Worth narrowing; an input to
  `docs/HANDOFF-sandboxing.md`.

## The shape that constrains anything built here

**Five machines, five last miles.** rpi5: USB one-shot over ssh. rpi4: local USB
boot, armed on the medium itself, over ssh. rpi3: hands-on until its card has a
second root slot -- one medium means one system, and that system is its base
image.
moose: its own UEFI `BootNext`, over ssh, but needing interactive sudo. MBP:
authenticated and hands-on, always.

**Apple Silicon's boot volume cannot be selected remotely, at all** — boot
volume selection goes through a LocalPolicy in the machine's own secure storage.
Re-tested 2026-08-23 with SIP *disabled* and passwordless root, because "SIP is
off now" is the obvious reason to expect otherwise: `nvram boot-volume` exits 0
and changes nothing, `bless --setBoot` says it is unsupported on Apple Silicon,
`systemsetup -getstartupdisk` prints `(null)`, and `bputil` sets security policy
rather than selecting a volume. The gate is firmware ownership, not SIP. What
*is* available is reading it: `nvram -p` publishes `boot-volume` as three
colon-separated UUIDs whose last field is the APFS volume group, so
`wk boot mbp --status` reports `firmware_default=` — evidence, not a promise,
since the startup manager boots a volume once without updating the variable.

**Every bench lane boots local media**, and two properties follow. A local root
keeps the network out of the measurement. And a board offered a medium it
cannot boot *falls through* to the next entry and comes up on its rescue system,
reachable — where firmware that gets partway into a boot tree and no further
**halts**, a state whose only exit is a hand on the power supply.

## Traps

- **An image that cannot be reached must return the machine by itself.** Every
  system carries a watchdog that self-disarms and reboots; without it a failed
  boot is a trip to the machine.
- **The residual hands-on case, and it is real**: a medium complete enough for
  firmware to commit to it (a `start4.elf` is there) but that then hangs takes
  the fall-through away — the board keeps choosing that medium, a power cycle
  re-enters the same hang instead of landing on the rescue system, and the
  self-disarm never runs because it lives in the rootfs. Observed on the rpi4
  with a downstream Yocto image (`docs/TESTING.md`). `boot/check-boot-files.py`
  is what keeps a *partial* tree from being written; a complete tree that hangs
  later is what nothing can catch from here.
- **A check that reads a different copy of the thing it checks is not a check** —
  verify the artifact that will actually be written or booted, not its source.
- **First contact with an unreachable board is physical.** Every arming
  mechanism here is an ssh command, so the tooling removes the *second* trip to
  a device and never the first.
- **A power cycle is not a reboot.** The Pi's one-shot register is reset-safe on
  purpose and survives a warm reboot, so after pulling the plug confirm which
  mode the board landed in rather than assuming.
- **Do not put the overclock in the EEPROM.** `SDRAM_BANKLOW` and `BOOT_ORDER`
  are firmware state shared by both modes; an overclock written there overclocks
  host mode too, which is the exact split this design preserves.
- **The rpi5 has no `cmdline.txt`** — boot args come from
  `/proc/device-tree/chosen/bootargs`, injected by firmware. Check what is
  already injected before adding any by hand.
- **`os_check=0` belongs in the *image's* `config.txt`.** Pi 5 firmware rejects
  kernels lacking Ubuntu's trailer and anything we build is "locally built" by
  that definition; the host's config.txt does nothing for the image.
- **Two disks written from one image are twins in every namespace** — same MBR
  signature, so `PARTUUID=` resolves to whichever the firmware enumerated
  first. Identity is stamped per disk at write time; keep it that way
  (`docs/HANDOFF-sdcard.md`).
- **The Mac's boot volume cannot be switched by script.** A plan that depends on
  rebooting the MBP into the benchmark install remotely is wrong; say so rather
  than building it.
