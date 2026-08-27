# HANDOFF — an A/B number off the two Pi boards

What is owed to get from "both boards run a rescue" to "two arms compared".

- [ ] get `tailnet-bridge-generic` back up and confirm `lan0` carries `10.99.1.0/24`, then `wk find rpi4` [needs moose's bridge phone]
- [ ] write the rpi3's SD as rescue+bench: write the rescue to the USB stick, flip the SD's MBR type to `0x83`, let the USB rescue repartition and rewrite the SD, flip the type back [needs the rpi3]
- [ ] write a bench system with tailscale onto the rpi4's SD [needs the rpi4's SD card in a reader]
- [ ] fix `b_media`/`b_system_kind` (boot/pi-usb.sh, boot/machines.sh): they still classify `MACH_DEVICE` as bench and `MACH_ROOT` as base, backwards from rpi4's stick-is-rescue/SD-is-bench arrangement [decision]
- [ ] run a real buildroot build through `buildroot_build` on an arm64 host [needs this Mac's podman VM or moose]
- [ ] validate `wpewebkit-2.38-buildroot-rpi4-32` once a build succeeds [needs the rpi4]
- [ ] write and validate an rpi5-64 buildroot 2.38 defconfig — none exists yet [needs the rpi5]
- [ ] run `wk pi bench --ab`, and confirm both arms are recorded, `wk bench ls` lists them, and `wk bench compare` reports the kernel/system delta [needs a board with two slots]
