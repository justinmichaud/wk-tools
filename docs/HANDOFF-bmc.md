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
  captured nowhere — a named gap in `docs/HANDOFF-cattle.md` that belongs here.
- Battery care for both phones: `docs/Urgent/HUMAN-battery.md`.

## What the first bridge taught — do not re-derive

- **Which dock, and it is the phone's property, not the dock's.** The A64 has no
  SuperSpeed anywhere, so a dock whose NIC sits behind its own USB3 hub can only
  ever present its USB2 hub — role swap perfect, power perfect, downstream ports
  `not attached` forever. The PinePhone's own dock puts its NIC on the USB2 path
  and works. Ask of any dock which hub its NIC is behind.
- **A pasted tailnet policy does not take effect on its own.** `autoApprovers`
  is evaluated when a node *advertises*, and re-running setup re-asserts an
  unchanged value, which `tailscale set` treats as a no-op — so a route
  advertised before the policy existed stays unapproved indefinitely with every
  other check green. Setup now re-advertises when it finds the route advertised
  but not primary.
- **A board that moves networks breaks in DNS first.** The rpi4 kept its old
  lease alongside the new one: egress fine, DNS dead, stale nameservers at route
  metric 10 against the bridge's 1002. It looks like a broken bridge and is a
  client that has not let go.
- **The udev rule must not match a synthetic MAC.** pmOS sets
  `cloned-mac-address=stable`, so the segment interface runs on a hashed address
  while the naming rule applies at `ACTION=="add"`, when the hardware address is
  still in place. Autodetection reads `ethtool -P` and the keyfile pins
  `cloned-mac-address=permanent` on that leg.
- **dnsmasq is `after net`, not `need net`.** The board asks for DHCP on
  carrier-up, which is when the kernel enumerates the adapter; `need net` put
  dnsmasq behind that and the first request was always lost. netwatch also flaps
  the link when it sees carrier with no lease for two passes — the only way to
  make a client that has given up ask again.
- **The fleet finds a board behind a bridge by its reserved address**, jumping
  through the phone (`dotfiles/ssh/config`), with the bare address matched as a
  Host pattern so `wk boot`'s bench-mode channel gets the jump too.
