# Handoff: SD-card image flashing

Was an empty placeholder file. The actual request lives in
`docs/HANDOFF-yocto.md` ("I need to be able to flash the sd card from the
host...") but the need isn't yocto-specific — any Pi target (buildroot rpi4,
yocto/Wayland rpi3, whatever rpi5 ends up running) produces an image that has
to get from a build workspace onto a physical SD card in the host machine.

## Done, 2026-08-20 — `wk image write`

```
wk image disks <machine>                        what is attached over there
wk image write <id> --disk <machine>:<device>    write the image onto that disk
```

with the mechanics in `boot/disk.sh`.

**The naming took two goes, and the second is the point.** This landed first as
`wk pi flash`, alongside the existing `wk image flash <machine>` — and both
names were wrong in the same way. `flash <machine>` reads as *reflash that
machine*: replace its OS, lose what is on it. That never happened; the machine's
own system disk is refused by several independent checks, and what gets written
is a removable disk that happens to be plugged into it. "flash" also implies
permanence, where a machine boots one of these disks *once* and returns to its
normal role by itself.

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

The checklist items above are covered: removable-only refusal, whole-disks-only,
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

## What to do

1. An easy way to copy the built image out of a workspace/container and onto
   the host filesystem — the workspace has no direct access to the host's SD
   card reader, so this is a two-step handoff, not a single `dd`.
2. A `wk pi flash <image> [device]` verb for writing that image to the card
   safely. The manual checklist it replaces (wiki:
   `Building-WPEWebKit-for-32-bit-Raspberry-Pi-3-(Yocto-Wayland)`, "Flashing
   the image") is exactly the error-prone part:
   - enumerate removable devices and **refuse non-removable ones** — the
     footgun this exists to remove is `dd` to the wrong `/dev/sd*`;
   - `umount` anything mounted from the card first;
   - write with `bmaptool` when a `.bmap` exists, else the wic/dd path;
   - `sync`, then `udisksctl power-off`;
   - the growpart/`resize2fs` fallback for images smaller than the card, and
     the `tune2fs` FEATURE_C12 workaround for old e2fsprogs on the target
     (label the workaround with the image generation that needs it).
3. Do this once, generically, rather than as a yocto-only script — the yocto
   task and the benchmark-image task (`docs/HANDOFF-benchmarking.md`) both
   consume this rather than duplicate it.

No machine constraint beyond "wherever the SD card reader physically is."
