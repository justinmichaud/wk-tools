On podman macos + linux targets, support building with yocto https://github.com/justinmichaud/justinmichaud.github.io/wiki/Building-WPEWebKit-for-32%E2%80%90bit-Raspberry-Pi-3-(Yocto-Wayland)

Yocto cache should be preserved even if target is destroyed

Also should support uploading new image to target.

Flashing the built image onto a physical SD card from the host is tracked
generically in `docs/HANDOFF-sdcard.md` (not yocto-specific) — consume that
rather than building a separate copy-to-host path here.

Tailscale shoud be able to be installed on the rpi target

**Target and test loop, decided 2026-08-19:** the target is the **rpi4**, and
each image gets tested over **netboot** (the Pi 4 bootloader has the same
network boot mode) rather than by writing a card — `docs/HANDOFF-netboot.md`,
which lands first and serves the TFTP root from moose. Flashing
(`docs/HANDOFF-sdcard.md`) then applies to the image that is kept, not to every
image that is tried. `wk pi setup rpi4` against the resulting image is the step
after this one (`docs/HANDOFF-linux-pi.md`).
