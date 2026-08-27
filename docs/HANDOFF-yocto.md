# HANDOFF — the yocto builder: what is left

## Remaining

- [ ] run `--stage toolchain` again since the base image changed [needs a yocto workspace]
- [ ] resolve the pseudo patch: confirm whether `image/yocto/meta-wk/…/pseudo_%.bbappend` is needed only on this host's kernel, by re-running the experiment with `--no-local-layer` on a host with an older kernel [needs a second build host]
- [ ] build the tailscale layer (`image/yocto/meta-wk-tailnet`) clean, write it with `disk_seed_tailnet`, boot it, and confirm the board answers to its own tailnet name [needs a yocto workspace and a Pi board]
- [ ] build a downstream Yocto image in a macOS VM workspace (the podman VM on macOS is a container target, so it should work, subject to the VM's disk being big enough) [needs a macOS VM]
- [ ] build and write `webkit-2.52-yocto-rpi3-64` to the rpi3's SD card [needs the rpi3 board]
- [ ] time a second `wk sysimage build` of the same profile to confirm sstate reuse makes it faster than the first [needs a yocto workspace]
- [ ] move whatever `packages:` installs over WiFi at first boot (~17 min) into the rootfs at build time instead, for anything that does not need a per-machine secret [needs a yocto workspace]
