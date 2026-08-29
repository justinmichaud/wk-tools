# HANDOFF — an A/B number off the two Pi boards

What is owed to get from "both boards run a rescue" to "two arms compared".

- [ ] rebuild both boards' rescues from a yocto profile (they predate `meta-wk-rescue`, so they carry no `wk-card-priv` and cannot write the bench medium) and re-write them once from a reader; every write after that runs from the board [needs a card reader in hand once]
- [ ] write the rpi3's SD as rescue+bench: write the rescue to the USB stick, flip the SD's MBR type to `0x83`, let the USB rescue repartition and rewrite the SD, flip the type back [needs the rpi3]
- [ ] re-provision the rpi4 as its conf says (rescue on the SD, bench on the USB drive) through rpi5's reader: the stick rescue's card helper predates init.d staging, so `wk sysimage write` refuses every card it writes (disk_install_units), and the `webkit-2.52-yocto-rpi4-64` image on moose bakes that same helper. Remove `rpi4-rescue` in the admin console, then SD ← `wk sysimage write --from <that wic.xz> --disk rpi5:<sd> --rescue`, USB ← `wk sysimage write --from <wpewebkit-2.38-buildroot-rpi4-32 sdcard.img> --disk rpi5:<usb>`, both into the board, power on: the stick boots first (`0xf14`), parks itself, and every boot after that is the SD rescue until `wk boot rpi4` [needs both media in rpi5's reader, after the rpi3 SD]
- [ ] first boot of `wpewebkit-2.38-buildroot-rpi4-32` on hardware: the image has never booted; the self-disarm and self-return have run only in tests [needs the step above]
- [ ] validate the base and pr1725 slots of `wpewebkit-2.38-buildroot-rpi{3,4}-32` on a booted board: `wk ab wpe:1725 --devices rpi3,rpi4 --bits 32` end to end, and confirm both arms are recorded, `wk bench ls` lists them, and the html report renders [needs a board booted into the image]
- [ ] write and validate an rpi5-64 buildroot 2.38 defconfig — none exists yet [needs the rpi5]
- [ ] the tailnet ACL lets `tag:wk` boards reach only `tag:wk`; `wk pi bench` tunnels the runner's port over ssh instead. Decide whether boards should reach a workstation's benchmark server directly [decision]
