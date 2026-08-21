# Handoff: moose's bench mode

The last machine in the fleet with no bench mode. `docs/HANDOFF-boot.md` lists
it as the one open item in the boot substrate and names BMC virtual media as
"the one route left"; **this file supersedes that** on the evidence below. The
BMC is the recovery console, not the boot medium, and moose's one-shot is its
own UEFI firmware.

Nothing here has been run. Nothing here reboots moose — the investigation was
read-only, and the first reboot is a decision for the day the work is picked
up.

## What the machine actually is — measured 2026-08-21, on moose

Every design question below turns on one of these, and none of them was
recorded anywhere before.

| fact | value | why it decides something |
|---|---|---|
| model | **System76 Thelio Astra** (`/sys/class/dmi/id`) | an Ampere Altra workstation, aarch64, UEFI — not a Pi, so no `config.txt`, no `start4.elf`, no EEPROM `BOOT_ORDER` |
| firmware | **AMI**, BIOS `2.01.SYS01` (`AmiEmaIndications`, `AMITSESetup` in efivars) | answers the question `docs/HANDOFF-boot.md` left open ("OpenBMC or AMI — check before designing on it"). It is AMI, so virtual media is MegaRAC's, and its scriptability is the *un*known, not the firmware family |
| Secure Boot | **disabled**, platform in Setup Mode | a bench system needs no signing, no MOK enrolment, no shim. A whole class of work does not exist |
| `efibootmgr` | installed, `/sys/firmware/efi/efivars` populated | there is a one-shot primitive **on the machine**, reachable over ssh — see below |
| disks | **one**: `nvme0n1`, 954 GB — EFI + `/boot` + one LVM PV filling the rest | nothing on this machine may hold a bench system, so the medium is external. With a RAM root that is a spare stick, not a purchase |
| RAM | **125 GB** (`/proc/meminfo`) | a RAM root is affordable by two orders of magnitude — it is what decides the medium below |
| network | `wlo1` **WiFi only**, 192.168.1.40; no wired link up | moose reaches nothing on the BMC segment (`ip route get 10.99.0.2` leaves via the house gateway) |
| BMC reachability | **none, today** | `wk bridge ls` reports both bridges `unreachable`; the only route to 10.99.0.2 is the Librem 5, which is not flashed yet (lane A step 14) |

## The headline: moose arms itself, and the BMC is not in the boot path

`efibootmgr --bootnext <n>` writes the UEFI `BootNext` variable, which firmware
consumes on the **next boot and then clears**. That is the same semantics as the
rpi5's `set_reboot_order` — the property the whole `one-shot` arming model is
built on, and the reason `wk boot --keep` can exist: a machine that is not
claimed returns to host mode by itself, with no second command and nothing to
remember.

So moose's arming model is **`one-shot`**, the driver the fleet already has a
shape for, and the BMC is not involved in it at all.

**Virtual media is the wrong boot medium here, and this is the correction to
`docs/HANDOFF-boot.md`.** A MegaRAC BMC presents an attached image to the host
as an emulated USB device, and that emulation is the machine's slowest storage
path by a wide margin. Two consequences, either one disqualifying:

- **It puts the BMC inside the measurement.** Every read the benchmark's root
  filesystem makes is served by a service processor over an emulated USB link.
  This is the same objection that keeps a network root out of every other lane
  in this fleet (`docs/HANDOFF-boot.md`, "Storage") — the delivery mechanism
  showing up in the numbers — and it is worse here, not better.
- **The image has to be somewhere the BMC can fetch it.** MegaRAC's scriptable
  path is Redfish `VirtualMedia.InsertMedia` with an image *URI* (CIFS/NFS/
  HTTPS). That is a server, holding a 4 GB image, for the BMC's benefit —
  exactly the infrastructure the fleet decided it did not want.

What the BMC **is** for: the console and the power switch when a boot fails.
That is the recovery story `docs/HANDOFF-bmc.md` already wants, and it is worth
having before the first reboot rather than after — see "The order of work".

## The medium: a RAM root off a USB stick — decided 2026-08-21

**First, a premise worth correcting, because it changes the basis for choosing.**
A second volume does *not* risk moose's GRUB. A USB medium carries its **own**
ESP and its own loader, and `efibootmgr --bootnext` points firmware at that
device for exactly one boot. moose's GRUB, moose's ESP and moose's LVM are
untouched either way — the NVMe is never written by any of this, and `wk`
refuses a machine's own system disk independently besides. So "don't touch
GRUB" is satisfied by both options and cannot pick between them.

**What does pick between them is where the root lives during the run**, and on
that the RAM root is clearly better:

- **Storage leaves the measurement entirely.** The stick is read once, at boot,
  and then nothing in the run touches it. Every other lane in this fleet records
  `root_device` and never compares a stick with an SSD precisely because storage
  variance cannot be subtracted out (`docs/HANDOFF-benchmarking.md`); a RAM root
  removes the term instead of recording it.
- **A cheap stick is then fine, and the purchase blocker disappears.** The whole
  reason a USB SSD was wanted is that cheap flash contributes variance rather
  than a subtractable bias — true while it is the root, irrelevant once it is
  read only at boot. Any stick over ~4 GB will do, and there is almost certainly
  one in a drawer.
- **moose has the RAM to spare: 125 GB** (`/proc/meminfo`, measured
  2026-08-21). A 1–2 GB squashfs plus an overlay is rounding error. This is the
  machine where the RAM-root idea that was wrong for the rpi4 (4 GB, and a
  browser benchmark is the workload) is right by a wide margin.

Two honest costs:

- **It is *more* work than a partition on the stick, not less.** Either option
  needs a new UEFI profile — every existing Linux bench profile is a Raspberry
  Pi image. On top of that a RAM root needs an initramfs that copies a squashfs
  into RAM and `switch_root`s into it. That is a solved problem with a name
  (`toram`, in Ubuntu's casper and Debian's live-boot) rather than something to
  invent, but it is a layer the plain-ext4 option does not have.
- **`root_device=ram` is a series of its own.** A RAM-rooted run is not
  comparable with a disk-rooted one, by the same rule that keeps stick and SSD
  runs apart. That is a feature — it is the most repeatable configuration
  available here — but it must be recorded, and the image runner owes that field
  anyway (`docs/HANDOFF-benchmarking.md`, "Fields the image runner has to
  record").

**So: RAM root off a USB stick, armed by `efibootmgr --bootnext`.** A second
volume stays the fallback if the mechanism turns out not to work on this
firmware, and step 1 of the order of work is a cheap experiment that finds that
out before any of the real machinery is written.

## What has to be built

Five pieces, in dependency order. None is large; the profile is the only one
with real unknowns in it.

1. **`boot/machines/moose.conf`** — moose is not in the machine registry at
   all today (the fleet has `benchvm`, `mbp`, `rpi3`, `rpi4`, `rpi5`). It
   arrives as config, not code, per **cattle, not pets**
   (`docs/HANDOFF-cattle.md`): `MACH_OS=linux`, `MACH_ROLE=workstation` (it is
   the user's machine and `wk` never writes its system disk),
   `MACH_DRIVER=uefi-bootnext`, `MACH_DEVICE` the USB stick, `MACH_ROOT` the
   NVMe's LVM root so the refusals know which disk is the machine's own, and
   the from-nothing recipe in the opening comment.

2. **`boot/uefi-bootnext.sh`** — the driver, implementing the same `b_*`
   contract as `boot/rpi5-usb.sh` (which is the closest analogue: a real
   one-shot, armed over ssh, consumed by the firmware).
   - `b_arm`: find the boot entry for the bench disk, `efibootmgr --bootnext`.
     The entry has to be *created* the first time (`efibootmgr --create --disk
     /dev/sdX --part 1 --loader '\EFI\BOOT\BOOTAA64.EFI'`) and found by label
     afterwards — never by number, which is not stable.
   - `b_disarm`: `efibootmgr --delete-bootnext`. Cheap and total, which is
     what makes this driver nicer than the rpi4's medium arming.
   - `b_probe` / `b_media` / `b_probeable`: the fleet block in `wk status`
     already wants these; the identity marker is read the same way as on any
     other machine.
   - **It needs root on moose.** `efibootmgr` writes efivars. moose's sudo is
     interactive by design and this repo does not add NOPASSWD (see the note
     on `/usr/bin/tee` in `docs/HANDOFF-boot.md` — the direction of travel is
     narrowing that grant, not adding to it). So arming moose is an
     interactive command, and `wk boot moose` must say so rather than fail
     confusingly under `sudo -n`. This is the one place moose's lane is
     genuinely less automatic than the Pis'.

3. **`perf-linux-moose`, a profile** — the piece with real work in it, now in
   its RAM-root shape: a squashfs, a kernel, and an initramfs carrying `toram`,
   on a FAT ESP the firmware can find. The root filesystem the machine runs on
   is a tmpfs copy, so the stick is out of the run. Every existing Linux bench profile is a Raspberry Pi image: the spec
   dirs hold `config.txt.append`, `image_check_boot_files` looks for
   `start4.elf`/`kernel8.img` through `image_dtb_for`, and `relabel` rewrites
   a Pi `cmdline.txt`. A UEFI aarch64 image shares none of that. What it needs:
   - an Ubuntu **aarch64 server** base (not the raspi image), booting through
     `\EFI\BOOT\BOOTAA64.EFI` and GRUB;
   - `toram` on the kernel command line, and the live hooks in the initramfs to
     honour it — casper (Ubuntu) or live-boot (Debian). Building the squashfs
     wants a rootfs tree with real ownership, which is a container workspace's
     job rather than this host's: the yocto builder already takes that shape
     (`image/yocto.sh` drives a workspace and imports the result), so it is a
     pattern to copy rather than a new problem;
   - the boot-file check taught the UEFI question. `boot/check-boot-files.py`
     models *what firmware asks of a boot tree*, and that model is currently
     the Pi's. The UEFI equivalent — does `\EFI\BOOT\BOOTAA64.EFI` resolve,
     does GRUB's config resolve the kernel and initrd it names — is the same
     kind of check and belongs in the same resolver, as a second set of rules
     rather than a second script;
   - the perf settings that live in `config.txt` on a Pi have a different home
     here: kernel command line and sysctls, not firmware config. The governor
     and swap-off units already generalise (`cmd/sysimage` installs them for
     every profile);
   - `IMG_WATCHDOG`, so an unreachable image hands the machine back by itself.
     Non-negotiable: it is the only thing standing between a failed boot and a
     trip to the machine.

   Good news that shortens this: Secure Boot is off, so no signing, and an
   Ubuntu aarch64 server image needs no firmware blobs staged the way a Pi
   image does.

4. **The runner has to live somewhere else.** Already recorded as a
   constraint in `docs/HANDOFF-boot.md` and it bites hardest here: moose is
   the machine that runs `wk bench` today, and it cannot drive its own bench
   boot. So a moose bench run needs `wk bench` working from **tolken** (the
   Mac) or the rpi5 in host mode, against moose as the bench device. The
   target registry already lets the Mac delegate to moose (`targets/hosts/
   moose.conf`, `WK_REMOTE_PEER=1`); what is missing is the reverse direction
   with moose *in bench mode*, where moose's own `wk` is not running at all.
   Worth checking early — it may be the largest piece of work here, and it is
   invisible from the boot side.

5. **`wk doctor`'s machine-local section** gains whatever moose's bench mode
   needs that a rebuild cannot recreate — the created UEFI boot entry is
   exactly that kind of state (it lives in the machine's own NVRAM, not in this
   repo). Per **cattle, not pets**, new machine-local state goes there or it is
   a bug.

## Two smaller defects found while investigating

Both are real today, independent of any of the above:

- **`dotfiles/ssh/config`'s `moosebmc` entry points at 192.168.1.41**, which
  does not answer (`ping`, 100% loss). `docs/HANDOFF-boot.md` already flags it
  as stale and names 10.99.0.2:2200 as the live address. It should be the
  bridge's address, or the entry should go — a stanza that reaches nothing is
  the kind of stale fact that reads as a live one.
- **moose has `--accept-routes` off**, and `tailscale status` says so in its
  own health check ("Some peers are advertising routes but --accept-routes is
  false"). The moment the Librem 5 is flashed and advertising 10.99.0.0/24,
  the machine that drives moose still will not reach the BMC without this. It
  is a one-line fix that will otherwise be debugged as a bridge failure. Note
  that the machine that needs it is *whichever one drives moose's boot* — so
  tolken and the rpi5, not only moose.

## The order of work

1. **Prove the mechanism for the price of an afternoon, before writing
   anything.** Write a stock **Ubuntu Server arm64 live ISO** to a spare stick,
   append `toram` to its kernel command line on the stick's own ESP, and boot it
   once with `--bootnext`. No new code, nothing of moose's written, and it
   answers the only two questions that could sink the design: does this AMI
   firmware boot removable USB when `BootNext` names it, and does `toram` work
   on this machine. `df -h /` in the booted system says whether the root is in
   RAM. A live-server ISO runs the installer on tty1 and is the wrong *base* for
   a benchmark — that is fine, it is being used as a test of the boot path, not
   as the lane.
2. **`boot/machines/moose.conf` + `boot/uefi-bootnext.sh`**, dry-run only.
   `wk boot moose --dry-run` and `wk boot moose --status` should be honest
   about a machine with no bench media yet — the fleet block already has a
   vocabulary for that.
3. **Get the BMC reachable first, before any real reboot** — lane A step 14
   (flash the Librem 5), plus the two defects above. A first UEFI boot of a new
   image on a headless-ish workstation is exactly when a console is worth
   having, and it is much less pleasant to arrange after a failed boot than
   before one.
4. **`perf-linux-moose`**, including the UEFI half of the boot-file resolver.
   Build it and check it against the image file long before booting it.
5. **The runner from another machine** (piece 4 above). Prove `wk bench` can
   drive a bench device from tolken while moose is not answering.
6. **Then, and only then, the first reboot.** With `--bootnext` armed, a
   console attached, and a watchdog in the image, the failure mode is a machine
   that comes back to its own install in a few minutes.
