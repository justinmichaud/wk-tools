# HANDOFF — tailnet-bridge devices (the BMC's librem5, and its successors)

**Status 2026-08-20: the software half shipped** — `wk bridge` (`cmd/bridge`,
`bridge/`), one-command provision/removal from this repo, camera streaming,
the power/network watchdogs, and the tar file gone; TESTING.md §8 has the
verified lines.

**Status 2026-08-22: `tailnet-bridge-generic` is done and carrying the rpi4.**
pmOS v25.12 on the phone's internal storage (the card is out; the eMMC boots),
on the tailnet as `tag:bridge` with 10.99.1.0/24 approved and primary, `lan0`
up at 10.99.1.1, the rpi4 holding its reserved 10.99.1.10, reachable from the
workstation by ProxyJump and reaching the internet through the NAT egress.
Verified across a reboot of the phone, not just live. `wk bridge status` is
green on everything except the watchdog.

What the hardware taught, all of it now in `bridge/devices.sh`,
`docs/help/bridge.txt` and TESTING.md §8:

- **which dock, and why it is the phone's property, not the dock's.** The A64
  has no SuperSpeed anywhere, so a dock whose NIC sits behind its own USB3 hub
  can only ever present its USB2 hub — role swap perfect, power perfect, four
  downstream ports `not attached` forever. The PinePhone's own dock puts its
  NIC on the USB2 path (CoreChips `0fe6:9900`, `cdc_ether`, 100 Mb/s) and
  works. Ask of any dock which hub its NIC is behind.
- **a pasted tailnet policy does not take effect on its own.**
  `autoApprovers` is evaluated when a node *advertises*, and re-running setup
  re-asserts an unchanged value, which `tailscale set` treats as a no-op. So a
  route advertised before the policy existed stays unapproved indefinitely,
  with every other check green. `wk bridge setup` now re-advertises when it
  finds the route advertised but not primary.
- **a board that moves networks breaks in DNS first.** The rpi4 kept its old
  `192.168.1.159` lease alongside the new one: egress fine, DNS dead, because
  the stale nameservers sat at route metric 10 against the bridge's 1002. It
  looks like a broken bridge and is a client that has not let go.
- **the fleet could not find the board behind the bridge** — `MACH_SSH` was
  mDNS on the house LAN. Fixed in `dotfiles/ssh/config`, jumping through the
  phone to the reserved address, with the bare address matched as a Host
  pattern so `wk boot`'s bench-mode channel gets the jump too.

And three things that came out of finishing it, all now fixed rather than
recorded:

- **the hardware watchdog exists after all.** pmOS ships
  `linux-postmarketos-allwinner` with `CONFIG_SUNXI_WATCHDOG` unset while the
  A64's device tree declares the watchdog, so nothing bound to it. The profile
  now declares that option (`PMO_KCONFIG` in `image/profiles.sh`) and
  `wk sysimage build` patches it into the kernel aport — the one place this
  repo edits somebody else's tree, and `wk help bridge` argues the case. The
  role loads the driver by the device's own modalias, from both `wk bridge
  setup` and the watchdog service's `start_pre`, so a boot does not depend on
  udev's coldplug.
- **the udev rule was matching a synthetic MAC.** pmOS sets
  `ethernet.cloned-mac-address=stable`, so the segment interface runs on a
  hashed address while the rule that names it applies at `ACTION=="add"`, when
  the hardware address is still in place. Autodetection now reads
  `ethtool -P`, and the keyfile pins `cloned-mac-address=permanent` on that one
  leg. Left as it was, the rename would have stopped working at the next
  reflash — invisibly, because the interface was already named by the previous
  rule.
- **the NIC can enumerate and still be forty times too slow, and the root
  cause is not fixed.** The pmOS initramfs sets up its USB-gadget network at
  13 s and flips the phy to peripheral with a hub attached; EHCI fails for a
  minute, gives up, and the dock lands on the companion OHCI controller at
  12 Mbit/s instead of 480. Host mode is *correct* in that state and the link
  still reads 100 Mb/s, so nothing but the USB bus speed shows it — and it is a
  race, so it is intermittent. `wk bridge status` now prints and fails on the
  bus speed, and wk-bridge-usb-host recovers it with a four-step rebind (unbind
  OHCI, unbind EHCI, bind EHCI, bind OHCI), capped at three attempts per boot.
  **Open:** stopping the initramfs from taking the port in the first place.
  Worth doing — the recovery is a repair after the fact, and it costs the board
  a re-DHCP each time.
- **the segment came back 80 s after a reboot.** The board asks for DHCP on
  carrier-up, which is when the *kernel* enumerates the adapter; `need net` put
  dnsmasq well behind that, so the first request was always lost. It is
  `after net` now, which `bind-dynamic` makes safe, and netwatch flaps the link
  when it sees carrier with no lease for two passes — the only way to make a
  client that has given up ask again.

**Next is the librem5**, re-flashed as `tailnet-bridge-moose-bmc`. The original
asks, kept below because the hardware steps still execute against them:

How can I make my librem5 bmc auto turn on after power loss? How can I recover this device if it is left off without being physically present?

Going forward, we will name the librem5 tailnet-bridge-X and switch to pmos for portability. We will support the librem 5 and pinephone as the primary devices.

This role should:
- Support bridging the ethernet to the tailnet
- Support streaming the camera whenever the kill switch is on, so that I can watch the screen remotely.
- Be resilient to power loss and network failures.

Make the setup files run from this repo instead of the phone, and get rid of the tar file in this repo. It should be one command to re-provision a new tailnet bridge device, and it should be easy to remove one.

We will first flash my pinephone to act as a bridge for the rpi4 or rpi3 (tailnet-bridge-generic). Then, we will re-flash my librem5 to act as a bridge for the bmc (tailnet-bridge-moose-bmc).
