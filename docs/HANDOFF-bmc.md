# HANDOFF — the Librem 5 bridge in front of moose's BMC

- [ ] reflash the Librem 5 with pmOS and run `wk bridge setup tailnet-bridge-moose-bmc` [needs moose's bridge phone]
- [ ] make the phone auto power on after power loss, and recoverable if left off with nobody present [decision]
- [ ] stop the pmOS initramfs taking the USB port before host mode is ready, so the dock stops falling back to 12 Mbit/s OHCI [needs moose's bridge phone]
- [ ] confirm the A64 watchdog produces a working `/dev/watchdog` on hardware [needs moose's bridge phone]
- [ ] capture moose's BMC firmware config (users, bmc0 network) into a conf file [maintainer: moose BMC]
- [ ] confirm a board on `lan0` gets its reserved address and is reachable from a workspace over the tailnet [needs the USB-C Ethernet dock attached]
- [ ] re-check `autoApprovers` is still evaluated for `10.99.1.0/24` after any Tailscale policy edit [needs moose's bridge phone]
- [ ] run `wk bridge tailnet <name>` [needs a credential fetched by hand]
- [ ] test `BR_CAMERA=http` streaming on both phones [needs both phones]
- [ ] confirm the escalation ladder's reboot budget stops rather than rebooting forever [needs pulling the AP near moose's bridge phone]
- [ ] run `wk bridge provision tailnet-bridge-generic` on the eMMC route end to end [needs the phone, an rpi5, and a Jumpdrive cable]
