# HANDOFF — the boot substrate: what is left

- [ ] make mutating commands warn or refuse when their target machine is armed
- [ ] build moose's bench mode (docs/Urgent/HANDOFF-moose-bench.md) [needs moose]
- [ ] rpi3: a slot-aware `wk sysimage write` (today it writes one whole system to one whole device and cannot put a system into a slot without destroying the other) [needs a Pi card in hand]
- [ ] rpi3: arm `boot/pi-sd.sh` with `root=` plus the bench kernel on the shared boot partition, and its stage-2 revert (initramfs fallback or rescue-side pivot) [needs a Pi card in hand]
- [ ] rpi3: BusyBox equivalents of the self-return watchdog and self-disarm, for an image with no systemd [needs a Pi card in hand]
- [ ] rebuild and re-flash rpi3 and rpi4 onto the tailnet layer (rpi4's SD still runs a live 900s self-return watchdog rebooting it every 15 minutes) [needs a Pi card in hand]
- [ ] verify the rescue marker plus both self-return/self-disarm units end to end on a real board [needs a Pi card in hand]
- [ ] verify a `--rescue` write without `--grow` leaves the rest of a shared card alone (the rpi3 two-slot case) [needs a Pi card in hand]
- [ ] verify a profile's `config.txt.append` reaches the image for every builder (rpi4 clock pinning, rpi5 `os_check=0`) against a real image [needs a Pi card in hand]
- [ ] verify `kill -9` mid-`wk sysimage build` converges on re-run
- [ ] verify two `wk sysimage build` at once: the second waits rather than racing the first's cleanup
- [ ] verify with the boot device absent, arming falls through to host mode rather than hanging at firmware [needs a Pi card in hand]
- [ ] verify armed-and-not-yet-rebooted is reported ARMED, exit 2, with the "next reboot leaves this role" warning [needs a Pi card in hand]
- [ ] verify a machine armed to leave host mode shows the transition on its `wk status` line, and after reboot reports the new mode or off-ssh [needs a Pi card in hand]
- [ ] verify an armed machine still in host mode long after arming (or back in host mode with the record uncleared) is flagged as desync [needs a Pi card in hand]
- [ ] hand-check `wk help hardware` against `boot/machines.sh` and its drivers whenever either changes
