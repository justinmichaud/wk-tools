# HANDOFF — a bench mode for moose

The last machine in the fleet with no bench mode. Nothing here is built: no
`boot/machines/moose.conf`, no `boot/uefi-bootnext.sh`, no `efibootmgr` in the
tree, no bench profile for it. Nothing here has ever rebooted moose. `wk help
hardware` has the design (a RAM root off a USB stick, armed by
`efibootmgr --bootnext`); this is the build list underneath it.

moose is a System76 Thelio Astra (Ampere Altra, aarch64, AMI UEFI
`2.01.SYS01`, Secure Boot off, 125 GB RAM, one NVMe). A RAM root is what keeps
storage out of the measurement (`root_device=ram` is its own series, and cheap
flash is then fine because the stick is read only at boot). Prove `--bootnext`
works (item 1 below); if it does not, choose one mechanism then — a plain
second partition is not a fallback kept alongside it.

## Remaining — in this order

1. **Prove the mechanism before writing anything.** Write a stock Ubuntu Server
   arm64 live ISO to a spare stick, append `toram` to its kernel command line on
   the stick's own ESP, and boot it once with `--bootnext`. No new code, nothing
   of moose's written. It answers the only two questions that can sink the
   design: does this AMI firmware boot removable USB when `BootNext` names it,
   and does `toram` work here (`df -h /` says so).
2. **`boot/machines/moose.conf` + `boot/uefi-bootnext.sh`**, dry-run only.
   - conf, per cattle-not-pets: `MACH_OS=linux`, `MACH_ROLE=workstation`,
     `MACH_DRIVER=uefi-bootnext`, `MACH_DEVICE` the stick, `MACH_ROOT` the
     NVMe's LVM root so the refusals know the machine's own disk.
   - driver, same `b_*` contract as `boot/rpi5-usb.sh`: `b_arm` creates the boot
     entry the first time (`efibootmgr --create --disk /dev/sdX --part 1
     --loader '\EFI\BOOT\BOOTAA64.EFI'`) and finds it **by label, never by
     number**; `b_disarm` is `efibootmgr --delete-bootnext`; plus `b_probe` /
     `b_media` / `b_probeable` for the fleet block in `wk status`.
   - **Arming moose needs interactive sudo.** `efibootmgr` writes efivars, and
     this repo does not add NOPASSWD. `wk boot moose` must say so rather than
     fail confusingly under `sudo -n`.
3. **Get the BMC reachable first, before any real reboot** — flash the Librem 5
   (`docs/HANDOFF-bmc.md`). Also still open: **`--accept-routes` is false on the
   rpi5**, confirmed live in `tailscale status`'s own health check ("Some peers
   are advertising routes but --accept-routes is false"). It is needed on
   whichever machine drives moose's boot — the rpi5 and tolken, not only moose —
   and it will otherwise be debugged as a bridge failure. Check moose's own
   setting the same way when it is reachable.
4. **moose's bench system — a Yocto layer of this repository's own.** The only
   piece with real unknowns. A yocto profile with a layer beside
   `image/yocto/meta-wk-tailnet`, built in a workspace like every other image
   here (`wk help images`).

   Every existing Linux bench profile is a Raspberry Pi image, so none of
   `config.txt.append`, `image_check_boot_files`'s `start4.elf`/`kernel8.img` or
   the `cmdline.txt` handling applies. It needs: an **aarch64** rootfs booting
   through `\EFI\BOOT\BOOTAA64.EFI` and GRUB; `toram` plus the live-boot hooks
   in the initramfs; UEFI rules added to `boot/check-boot-files.py` as a second
   rule set rather than a second script; the perf settings expressed as kernel
   command line and sysctls rather than firmware config; and **`IMG_WATCHDOG`,
   non-negotiable** — it is the only thing between a failed boot and a trip to
   the machine.
5. **The runner has to live on another machine**, and this may be the largest
   piece. moose runs `wk bench` today and cannot drive its own bench boot, so a
   run needs `wk bench` working from tolken or the rpi5 against moose as the
   bench device — the reverse of the delegation `targets/hosts/moose.conf`
   already supports, with moose's own `wk` not running at all. Check it early;
   it is invisible from the boot side.
6. **`wk doctor`'s machine-local section** gains the created UEFI boot entry —
   it lives in the machine's NVRAM, not in this repo.
7. **Then, and only then, the first reboot** — armed, console attached, watchdog
   in the image.
