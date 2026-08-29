# HANDOFF — the boot substrate: what is left

- [ ] make mutating commands warn or refuse when their target machine is armed
- [ ] build moose's bench mode (docs/Urgent/HANDOFF-moose-bench.md) [needs moose]
- [ ] rpi3: a stage-2 revert for a bench kernel that panics before its self-disarm runs (today the os_prefix line stays until `wk boot rpi3 --disarm` from the rescue, which that boot never reaches): `tryboot` if the Pi 3 firmware honours it -- measure with `reboot "0 tryboot"` and a `tryboot.txt` on the board -- else an initramfs that puts config.txt back [needs the rpi3]
- [ ] rpi3: the bench system's first boot with a network -- `wk boot rpi3`, `rpi3-bench` joins, `wk pi deploy`, `wk ab` (the `@second` write from the rescue, the arming, the S11 self-disarm and the S99 self-return are measured: the board handed itself back to `rpi3-rescue` 15 min after arming, on an image with no wpa_supplicant) [needs the rebuilt `wpewebkit-2.38-buildroot-rpi3-32` image, hours]
- [ ] rpi4: verify `S11wk-self-disarm` parks the card and `S99wk-self-return` reboots an unclaimed board on a real buildroot boot (the yocto unit's `$$`-escaped ExecStart has run only in tests) [needs the card re-written by a helper that stages init.d scripts]
- [ ] rebuild and re-flash rpi3 onto the tailnet layer [needs a Pi card in hand]
- [ ] verify the rescue marker plus both self-return/self-disarm units end to end on a real board [needs a Pi card in hand]
- [ ] verify a profile's `config.txt.append` reaches the image for every builder (rpi4 clock pinning, rpi5 `os_check=0`) against a real image [needs a Pi card in hand]
- [ ] verify `kill -9` mid-`wk sysimage build` converges on re-run
- [ ] verify two `wk sysimage build` at once: the second waits rather than racing the first's cleanup
- [ ] verify with the boot device absent, arming falls through to host mode rather than hanging at firmware [needs a Pi card in hand]
- [ ] verify armed-and-not-yet-rebooted is reported ARMED, exit 2, with the "next reboot leaves this role" warning [needs a Pi card in hand]
- [ ] verify a machine armed to leave host mode shows the transition on its `wk status` line, and after reboot reports the new mode or off-ssh [needs a Pi card in hand]
- [ ] verify an armed machine still in host mode long after arming (or back in host mode with the record uncleared) is flagged as desync [needs a Pi card in hand]
- [ ] hand-check `wk help hardware` against `boot/machines.sh` and its drivers whenever either changes
