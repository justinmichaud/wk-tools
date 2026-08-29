# HANDOFF — an A/B number off the two Pi boards

What is owed to get from "both boards run a rescue" to "two arms compared".

- [ ] rebuild the rpi4's rescue from a yocto profile (it predates `meta-wk-rescue`, so it carries no `wk-card-priv` and cannot write the bench medium) and re-write it once from a reader; every write after that runs from the board [needs a card reader in hand once]
- [ ] read the first `wk ab wpe:1725 --devices rpi3 --bits 32` report (run 2026-08-29T14:14Z, 5 rounds of speedometer3; the board reported `throttled=0x80000` -- a soft temperature limit -- during it) and decide whether the lane's noise floor needs an A/A first
- [ ] rpi4: write `wpewebkit-2.38-buildroot-rpi4-32` (rebuilt with wpa_supplicant, OpenSSH and the fleet kernel fragment) onto the stick from the rescue (`--disk rpi4:/dev/sda`), `wk boot rpi4`, and boot it once -- the image has never booted on hardware [needs the rebuilt image; the rescue `webkit-2.52-yocto-rpi4-64-fa57af516940` on the SD is up as `rpi4-rescue`, and the EEPROM already says usb-first]
- [ ] first boot of `wpewebkit-2.38-buildroot-rpi4-32` on hardware: the image has never booted; the self-disarm and self-return have run only in tests [needs the step above]
- [ ] validate the base and pr1725 slots of `wpewebkit-2.38-buildroot-rpi{3,4}-32` on a booted board: `wk ab wpe:1725 --devices rpi3,rpi4 --bits 32` end to end, and confirm both arms are recorded, `wk bench ls` lists them, and the html report renders [needs a board booted into the image]
- [ ] write and validate an rpi5-64 buildroot 2.38 defconfig — none exists yet [needs the rpi5]
- [ ] the tailnet ACL lets `tag:wk` boards reach only `tag:wk`; `wk pi bench` tunnels the runner's port over ssh instead. Decide whether boards should reach a workstation's benchmark server directly [decision]
