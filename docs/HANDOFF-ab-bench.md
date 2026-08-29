# HANDOFF — an A/B number off the two Pi boards

What is owed to get from "both boards run a rescue" to "two arms compared".

- [ ] rebuild both boards' rescues from a yocto profile (they predate `meta-wk-rescue`, so they carry no `wk-card-priv` and cannot write the bench medium) and re-write them once from a reader; every write after that runs from the board [needs a card reader in hand once]
- [ ] write the rpi3's SD as rescue+bench: write the rescue to the USB stick, flip the SD's MBR type to `0x83`, let the USB rescue repartition and rewrite the SD, flip the type back [needs the rpi3]
- [ ] boot `wpewebkit-2.38-buildroot-rpi4-32` from the rpi4's SD once: the image has never booted on hardware, and the card was written before `wk sysimage write` staged the BusyBox self-disarm and self-return, so it neither parks itself nor reboots unless claimed -- re-write it from a current helper first, or keep a person within reach of the board
- [ ] `wk pi boot-order rpi4` (SD first, `0xf41`): the EEPROM still says `0xf14` from the stick-first arrangement [needs the rpi4 running its rescue]
- [ ] validate the base and pr1725 slots of `wpewebkit-2.38-buildroot-rpi{3,4}-32` on a booted board: `wk ab wpe:1725 --devices rpi3,rpi4 --bits 32` end to end, and confirm both arms are recorded, `wk bench ls` lists them, and the html report renders [needs a board booted into the image]
- [ ] write and validate an rpi5-64 buildroot 2.38 defconfig — none exists yet [needs the rpi5]
- [ ] the tailnet ACL lets `tag:wk` boards reach only `tag:wk`; `wk pi bench` tunnels the runner's port over ssh instead. Decide whether boards should reach a workstation's benchmark server directly [decision]
