# HANDOFF — writing a system onto a card in a local reader

Writing a system onto a disk is done: `wk sysimage disks <machine>` and
`wk sysimage write <id> --disk <machine>:<device>` (mechanics in
`boot/disk.sh`), used on the rpi4's stick and the rpi5's card.

One gap remains, and it is the one that blocks fully-automatic first
provisioning of a from-nothing board.

## Remaining

**`wk sysimage flash --reader /dev/sdX`.** Every write path resolves its target
through the fleet and goes over ssh, so a card reader attached to *this*
workstation cannot be named — the first medium for a new board still needs
another provisioned machine or a hand flash.

The constraint that shapes it: this workstation deliberately has no privileged
component, so a local write means **interactive sudo, and no NOPASSWD**. That
decision is recorded with the device lifecycle in
`docs/HANDOFF-vocabulary.md` (item 1) and listed as a gap in
`docs/HANDOFF-cattle.md`.

No machine constraint beyond "wherever the SD card reader physically is."

## Constraints that bind any change to the write path

- **A machine's own system disk can never be written**, and **writing a disk
  does not make anything boot it** — that is `wk boot`, and it is one-shot.
  Both are stated in the help text because both surprise people.
- **The read-back sha256 check is only valid after a `dd` write.** bmaptool
  deliberately does not write unmapped blocks, so those regions still hold
  whatever was there before while the image has zeroes; comparing the whole span
  compares bytes nobody wrote and always fails. bmaptool's per-block checksums
  are the stronger check, not a weaker one.
- **bmaptool needs to seek**, which is why the yocto import keeps the compressed
  original (`disk.wic.xz`) and its `disk.bmap` rather than only `disk.img` — it
  sends 547 MB instead of 4 GB for the 2.48 image. When an image has a map and
  the machine lacks the tool, say so and give the install command rather than
  falling back silently.
- **Identity is stamped per disk at write time** (`disk_unique_identity`). Two
  disks written from one wic image carried the same MBR signature, and since
  `PARTUUID=` is that signature plus a partition number, a board loaded one
  disk's kernel onto the other disk's root — a system that was neither, and
  looked like a successful boot.
- **The image's `root=` must match the kind of device being written**
  (`image_check_root`, `lib/image.sh`), compared by *kind* rather than path,
  because a card written in one machine's reader is routinely booted in another.
- The FEATURE_C12 `tune2fs` workaround is deliberately not implemented — it is
  for old e2fsprogs on the *target*, and nothing has hit it.
