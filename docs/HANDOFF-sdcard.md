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
2. Host-side instructions (or a `wk` verb) for writing that image to the card
   safely — the usual footgun is `dd`-ing to the wrong `/dev/disk*`/`/dev/sd*`,
   so whatever this is should confirm the target device before writing.
3. Do this once, generically, rather than as a yocto-only script — the yocto
   task should consume this rather than duplicate it.

No machine constraint beyond "wherever the SD card reader physically is."
