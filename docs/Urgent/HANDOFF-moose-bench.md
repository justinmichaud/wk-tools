# HANDOFF — a bench mode for moose

`wk help hardware` has the design (a RAM root off a USB stick, armed by `efibootmgr --bootnext`).

block if bmc is not reachable

we should generate a yocto image for moose for consistency

## Remaining, in this order

- [ ] prove `--bootnext` and `toram` work on this AMI firmware: write a stock Ubuntu Server arm64 live ISO to a spare stick, append `toram` to its kernel command line, boot it once with `--bootnext` [needs moose]
- [ ] add `boot/machines/moose.conf` and `boot/uefi-bootnext.sh` (same `b_*` driver contract as `boot/rpi5-usb.sh`), dry-run only; `wk boot moose` must report that arming needs interactive sudo rather than failing under `sudo -n` [needs moose]
- [ ] get the BMC reachable before any real reboot [needs the Librem 5 flashed]
- [ ] fix `--accept-routes` being false on the rpi5 (and check moose's own setting once it is reachable) [needs moose reachable]
- [ ] build moose's bench system as a Yocto layer: aarch64 rootfs booting through UEFI/GRUB, `toram` plus live-boot hooks, UEFI rules added to `boot/check-boot-files.py`, perf settings as kernel cmdline/sysctls instead of firmware config, and `IMG_WATCHDOG` [needs moose]
- [ ] get `wk bench` running from tolken or the rpi5 against moose as the bench device, since moose cannot drive its own bench boot [needs moose]
- [ ] add the created UEFI boot entry to `wk doctor`'s machine-local section [needs moose]
- [ ] the first reboot: armed, console attached, watchdog in the image [needs moose]
