# HANDOFF — an A/B number off the two Pi boards

What is owed to get from "both boards run a rescue" to "two arms compared".

- [ ] rebuild the rpi4's rescue from a yocto profile (it predates `meta-wk-rescue`, so it carries no `wk-card-priv` and cannot write the bench medium) and re-write it once from a reader; every write after that runs from the board [needs a card reader in hand once]
- [ ] read the first `wk ab wpe:1725 --devices rpi3 --bits 32 --plan speedometer2.1` report (run started 2026-08-30T05:05Z, 5 rounds) and decide whether the lane's noise floor needs an A/A first
- [ ] rpi4: the stick (`/dev/sdb`, `wpewebkit-2.38-buildroot-rpi4-32-bb4ac3575fdd`, armed 0x0c, boot files complete) is not booted by the firmware: `wk boot rpi4` with BOOT_ORDER 0xf14 lands back on the SD rescue every time. An empty RTL9201 USB enclosure sits on the same bus as `/dev/sda` (0 bytes); unplug it and boot again before suspecting the image -- there is no serial console to read the bootloader's USB discovery [needs a hand at the rpi4]
- [ ] first boot of `wpewebkit-2.38-buildroot-rpi4-32` on hardware: the image has never booted; the self-disarm and self-return have run only in tests [needs the step above]
- [ ] validate the base and pr1725 slots of `wpewebkit-2.38-buildroot-rpi{3,4}-32` on a booted board: `wk ab wpe:1725 --devices rpi3,rpi4 --bits 32` end to end, and confirm both arms are recorded, `wk bench ls` lists them, and the html report renders [needs a board booted into the image]
- [ ] write and validate an rpi5-64 buildroot 2.38 defconfig — none exists yet [needs the rpi5]
- [ ] the tailnet ACL lets `tag:wk` boards reach only `tag:wk`; `wk pi bench` tunnels the runner's port over ssh instead. Decide whether boards should reach a workstation's benchmark server directly [decision]
- [ ] rpi3: Speedometer 3 does not complete on the standard image (10 min timeout, no result posted) while Speedometer 2.1 does (11.27 pt, base slot, 2026-08-30); during 2.1 the web process alone held 74% of the 623 MB, so Speedometer 3 is most likely out of memory there. Measure it (logread for the OOM killer during a run) and decide: 2.1 as this board's plan, or swap/zram for 3 [needs the rpi3 in bench mode]
