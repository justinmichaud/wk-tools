On podman macos + linux targets, support building with yocto https://github.com/justinmichaud/justinmichaud.github.io/wiki/Building-WPEWebKit-for-32%E2%80%90bit-Raspberry-Pi-3-(Yocto-Wayland)

Yocto cache should be preserved even if target is destroyed

Also should support uploading new image to target.

Flashing the built image onto a physical SD card from the host is tracked
generically in `docs/HANDOFF-sdcard.md` (not yocto-specific) — consume that
rather than building a separate copy-to-host path here.

Tailscale shoud be able to be installed on the rpi target
