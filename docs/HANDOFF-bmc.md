# HANDOFF — the Librem 5 bridge in front of moose's BMC

`wk bridge` and the generic role are built and one bridge is live:
`tailnet-bridge-generic` runs pmOS on the PinePhone's eMMC, advertises
10.99.1.0/24, and carries the rpi4 at its reserved 10.99.1.10 — verified across
a reboot of the phone. `bridge/hosts/tailnet-bridge-moose-bmc.conf` declares the
second one and is not provisioned.

## Remaining

- **Reflash the Librem 5 with pmOS, then `wk bridge setup
  tailnet-bridge-moose-bmc`.** The phone is still running the hand-built
  PureOS/systemd configuration this role replaces; setup refuses a phone that is
  not on pmOS rather than half-applying over it. The sharpest requirement is
  unchanged: **moose must be reachable through this phone when moose is powered
  off, hung, or has no working OS**, and nothing may depend on a BMC-side
  setting, because the BMC's configuration resets on every power loss.
- **The two original questions still have no answer**: how to make the phone
  auto power on after power loss, and how to recover it if it is left off with
  nobody present.
- **The USB race is worked around, not fixed.** The pmOS initramfs takes the
  port for its own gadget network at 13 s and flips the phy to peripheral with a
  hub attached; EHCI then fails for a minute and the dock lands on the companion
  OHCI controller at **12 Mbit/s instead of 480**, while host mode is *correct*
  and the link still reads 100 Mb/s — so nothing but the bus speed shows it, and
  it is intermittent. `wk bridge status` fails on bus speed and
  wk-bridge-usb-host recovers with a four-step rebind (unbind OHCI, unbind EHCI,
  bind EHCI, bind OHCI), three attempts per boot, costing a re-DHCP each time.
  Stopping the initramfs taking the port is the real fix.
- **Confirm the watchdog on hardware.** The A64's device tree declares one but
  `linux-postmarketos-allwinner` ships `CONFIG_SUNXI_WATCHDOG` unset; the
  profile now declares it (`PMO_KCONFIG`) and `wk sysimage build` patches the
  kernel aport — the one place this repo edits somebody else's tree. Until a
  `/dev/watchdog` is confirmed, the netwatch ladder is the only recovery, and it
  cannot see a kernel that has stopped scheduling.
- **moose's BMC firmware config** (users, the bmc0 network) is set by hand and
  captured nowhere — the one piece of this fleet that no conf reproduces.
- Battery care for both phones: `docs/Urgent/HUMAN-battery.md`.

## Untested — written, needs the hardware in a specific state

- **A board on `lan0` gets its reserved address and is reachable from a
  workspace over the tailnet** — needs `autoApprovers` actually evaluated
  (below) *and* the USB-C Ethernet dock physically attached; the segment itself
  (DHCP to rpi3/rpi4) has never been exercised.
- **`autoApprovers` for `10.99.1.0/24` is evaluated only when a node
  *advertises* a route** — a route advertised before the policy existed stays
  unapproved forever; re-running setup re-asserts the same value and
  `tailscale set` with an unchanged value is a no-op. Setup now withdraws and
  re-advertises whenever the route is up but not primary, but this is the
  failure that looks exactly like success and is worth re-checking after any
  policy edit.
- **`wk bridge tailnet <name>`** — untested, the one remaining step needing a
  credential fetched by hand.
- **`BR_CAMERA=http` streaming** — unproven on both phones; libcamera-era
  sensors do not always present a format ffmpeg will open.
- **The escalation ladder's reboot budget** — pull the AP, watch
  `wk-bridge-netwatch` climb, confirm it stops at the budget rather than
  rebooting forever.
- **`wk bridge provision tailnet-bridge-generic` on the eMMC route, end to
  end** — Jumpdrive to the card, phone cabled to rpi5, internal storage
  appearing as a new USB disk (found by content-diff against a baseline, since
  Jumpdrive exports the SD card too), the bridge image written there, the card
  out, and the phone coming up on its own install and answering
  `wk bridge setup` at `<hostname>.local`.

