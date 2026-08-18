# Handoff: SD-card image flashing

Was an empty placeholder file. The actual request lives in
`docs/HANDOFF-yocto.md` ("I need to be able to flash the sd card from the
host...") but the need isn't yocto-specific — any Pi target (buildroot rpi4,
yocto/Wayland rpi3, whatever rpi5 ends up running) produces an image that has
to get from a build workspace onto a physical SD card in the host machine.

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
