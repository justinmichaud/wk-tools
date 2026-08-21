# Handoff: SD-card image flashing

Was an empty placeholder file. The actual request lives in
`docs/HANDOFF-yocto.md` ("I need to be able to flash the sd card from the
host...") but the need isn't yocto-specific — any Pi target (buildroot rpi4,
yocto/Wayland rpi3, whatever rpi5 ends up running) produces an image that has
to get from a build workspace onto a physical SD card in the host machine.

## Done, 2026-08-20 — `wk sysimage write`

```
wk sysimage disks <machine>                         what is attached over there
wk sysimage write <id> --disk <machine>:<device>    write the system onto that disk
```

with the mechanics in `boot/disk.sh`.

**The naming took three goes, and the last two are the point.** This landed
first as `wk pi flash`, alongside the existing `wk image flash <machine>` —
and both names were wrong in the same way. `flash <machine>` reads as *reflash
that machine*: replace its OS, lose what is on it. That never happened; the
machine's own system disk is refused by several independent checks, and what
gets written is a removable disk that happens to be plugged into it. "flash"
also implies permanence, where a machine boots one of these disks *once* and
returns to host mode by itself. (`wk image` itself became `wk sysimage` later
the same day — `docs/HANDOFF-vocabulary.md` — and every old spelling is
refused by name.)

They were also one operation pretending to be two: same store, same
verification, same unmount, same refusals, differing only in device policy — and
the stricter policy was right for both. So there is one verb, the disk is named
rather than the machine (`<machine>:<device>`, spelled like `scp`'s `host:path`
so the containment reads without explanation), and both old spellings now fail
with a message saying why the name was wrong rather than only where it went.

Two facts the help text states outright, because both surprise people:
a machine's own system disk can never be written; and **writing a disk does not
make anything boot it** — that is `wk boot`, and it is one-shot.

**Where the card is decided the shape.** It is not in the workstation — that
deliberately has no privileged component — it is in a reader attached to a fleet
machine. So the work happens on that machine over ssh, where sudo is
passwordless, and this end only decides and reports. Same division `b_flash`
already uses; this is the generalisation of it, because `b_flash` writes one
hardcoded `MACH_DEVICE` and insists it be usb, which is right for the rpi5's
boot stick and wrong for a card reader.

Verified against the rpi5's reader with the WPE 2.48 yocto image:

```
==> unmounting /dev/mmcblk0p1 on rpi5      (the desktop automounter had it)
==> writing disk.img to /dev/mmcblk0 on rpi5 (3984 MB, zstd=yes)
==> read-back matches
==> growing /dev/mmcblk0p2 to fill the device
```

and confirmed on the card afterwards: `mmcblk0p1 130M vfat boot`,
`mmcblk0p2 7.3G ext4 root` — grown from the image's 3.8 G to fill a 7.5 G card.

The manual checklist this replaces (wiki:
`Building-WPEWebKit-for-32-bit-Raspberry-Pi-3-(Yocto-Wayland)`, "Flashing the
image") is covered item by item: removable-only refusal, whole-disks-only,
never the machine's own root disk, a size check, unmount-after-confirm, sync,
`udisksctl power-off` (best-effort), and growpart/resize2fs. Two deviations
worth stating:

- **`bmaptool` is the fast path, and it needed three things.** bmaptool has to
  seek in the image, so it cannot work on a stream — which is why the yocto
  import keeps the *compressed* original (`disk.wic.xz`) and its `disk.bmap`
  rather than only the decompressed `disk.img`. The compressed file is what gets
  copied over, so writing the 2.48 image sends **547 MB instead of 4 GB** and
  writes **2.2 GiB of 3.9 GiB (55.5% mapped)**, checksumming each block against
  the map. Third: rpi5 did not have `bmap-tools` installed. The fallback to the
  zstd/dd stream is no longer silent — when an image has a map and the machine
  lacks the tool, it says so and gives the install command, because that is a
  missing package on one machine rather than a property of the image.

  One correctness note, commented where the decision lives: **the read-back
  sha256 check is only valid after a dd write.** bmaptool deliberately does not
  write unmapped blocks, so those regions still hold whatever was there before
  while the image has zeroes; comparing the whole span compares bytes nobody
  wrote and always fails. bmaptool's per-block checksums are the stronger
  check, not a weaker one.
- **The FEATURE_C12 `tune2fs` workaround is not implemented**, because nothing
  has hit it: it is for old e2fsprogs on the *target*, and this image's ext4 was
  written by scarthgap's own tooling.

A check the checklist did not ask for and should have: **the image's `root=`
must match the kind of device being written**. `image_check_root` in
`lib/image.sh` refuses when it does not, in both flashing verbs and in their dry
runs — see `docs/HANDOFF-yocto.md`. It is compared by *kind*, not by path,
because a card written in one machine's reader is routinely booted in another.

## What remains

The original task list (copy the image out of the workspace, a safe flashing
verb with the removable-only/umount/bmaptool/grow checklist, done once and
generically) is covered by the above: the store holds what `wk sysimage build`
produces, both flashing verbs collapsed into `write`, and yocto and perf
systems go through the same path.

The one gap is **`wk sysimage flash --reader`** — a card reader attached to
*this* workstation cannot be named, because every write path resolves its
target through the fleet and goes over ssh. That is what blocks fully-automatic
first provisioning, and it is recorded (with the no-NOPASSWD decision) in
`docs/HANDOFF-vocabulary.md`'s lifecycle.

No machine constraint beyond "wherever the SD card reader physically is."
