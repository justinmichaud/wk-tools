# HANDOFF — the boot substrate: what is left

The substrate is built and exercised on hardware. `wk sysimage
build|ls|show|disks|write|rm` produces and stores systems (unprivileged: mtools
and e2fsprogs edit images at byte offsets, nothing is mounted, `wk` never calls
sudo on this host); `wk boot <machine> [--system|--status|--keep|--back|--disarm|
--diag|--dry-run]` arms one boot with a per-machine driver; `wk pi boot-order`
and `boot/rpi-eeprom.sh` handle Pi firmware; `boot/check-boot-files.py` models
what firmware asks of a boot tree and runs before anything touches a disk. The
rpi5 and rpi4 have both been through the whole cycle with no hands on the board.

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
  (`webkit-2.52-yocto-rpi3-32`) and that system is the base image. The
  arrangement it needs is written up where design belongs — `wk help hardware`, "why the three Pis are arranged
  differently", plus the binding priority order in CLAUDE.md. What is *left* is
  the work:
    1. a slot-aware `wk sysimage write`, which today writes one whole system to
       one whole device and cannot put a system into a slot without destroying
       the other;
    2. arming for `boot/pi-sd.sh` — `root=` plus the bench kernel installed onto
       the shared boot partition — and the revert that goes with a stage-2
       arming, which is an initramfs fallback or a rescue-side pivot, because a
       kernel that cannot mount the armed root would otherwise panic-loop;
    3. the BusyBox equivalents of the self-return watchdog and self-disarm, for
       an image with no systemd.
  Both remaining items need the card in hand, and the first write is hands-on.
  What the role *is* no longer needs building: one image serves both, and
  `wk sysimage write --rescue` decides which by leaving a marker the units read
  (`disk_seed_role`, boot/disk.sh).
- **Both boards are still running images built before the tailnet layer**, so
  neither is on the tailnet and the rpi4's SD carries a live 900-second
  self-return watchdog that reboots it every 15 minutes while the board sits on
  it. Rewriting either is a build and a card write, not a design question.

## Owed — unverified on real hardware

- [ ] the rescue marker plus both self-return/self-disarm units, checked end to
      end on a real board: marker lands, `systemctl show wk-self-return`
      reports the condition unmet, and the board actually reboots unattended
      within the profile's watchdog period after `IMG_WATCHDOG` fires
- [ ] no `IMG_ROLE`/rescue profile exists yet; `wk sysimage ls` reports
      `unrecorded` role for pre-field images — decide what it should say
- [ ] a `--rescue` write without `--grow` leaves the rest of a shared card
      alone (the rpi3 two-slot case) — unverified
- [ ] a profile's `config.txt.append` reaches the image for every builder
      (rpi4 clock pinning, rpi5 `os_check=0`) — idempotent by marker, read
      back after write, never run against a real image
- [ ] `kill -9` mid-`wk sysimage build`, re-run converges at every point
- [ ] two `wk sysimage build` at once: the second waits rather than racing the
      first's cleanup
- [ ] with the boot device absent, arming falls through to host mode rather
      than hanging at firmware
- [ ] armed-and-not-yet-rebooted is reported ARMED, exit 2, with the
      "next reboot leaves this role" warning
- [ ] a machine armed to leave host mode shows the transition on its `wk
      status` line (system id, who armed it, when); after reboot the walk
      reports the new mode or off-ssh
- [ ] an armed machine still in host mode long after arming (or back in host
      mode with the record uncleared) is flagged as desync
- [ ] `wk help hardware` needs hand-checking against `boot/machines.sh` and
      its drivers whenever either changes — nothing enforces the two agree

## The shape that constrains anything built here

**Five machines, five last miles.** rpi5: USB one-shot over ssh. rpi4: local USB
boot, armed on the medium itself, over ssh. rpi3: hands-on until its card has a
second root slot -- one medium means one system, and that system is its base
image. moose: its own UEFI `BootNext`, over ssh, but needing interactive sudo.
MBP: authenticated and hands-on, always -- `wk help hardware` has why Apple
Silicon rules out anything else.

## Traps

- **The residual hands-on case, and it is real**: a medium complete enough for
  firmware to commit to it (a `start4.elf` is there) but that then hangs takes
  the fall-through away — the board keeps choosing that medium, a power cycle
  re-enters the same hang instead of landing on the rescue system, and the
  self-disarm never runs because it lives in the rootfs. Observed on the rpi4
  with a downstream Yocto image. `boot/check-boot-files.py`
  is what keeps a *partial* tree from being written; a complete tree that hangs
  later is what nothing can catch from here.
- **Do not put the overclock in the EEPROM.** `SDRAM_BANKLOW` and `BOOT_ORDER`
  are firmware state shared by both modes; an overclock written there overclocks
  host mode too, which is the exact split this design preserves.
