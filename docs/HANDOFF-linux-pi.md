# Handoff: the Raspberry Pi test devices

**Scope, 2026-08-19: this is rpi4 and rpi3 only.** The rpi5 is provisioned as a
regular workstation -- its own `./setup`, full tailnet privileges, podman
workspaces like moose -- so it never goes through `wk pi setup` and its
identity never enters `pi-hosts`. Benchmarking it means booting a bench system
with its own identity -- `rpi5-perf.local`, mDNS rather than a tailnet entry,
because the system carries no tailscale (`docs/HANDOFF-benchmarking.md`,
`image/profiles.sh`).

`wk pi setup` -- the tailnet half of `cmd/pi` -- has never been run against a
device. The eeprom and boot-order verbs have (`docs/HANDOFF-netboot.md`). The
macOS side never needed any of it -- workspaces there could not reach the Pis
anyway -- so this is Linux-only work.

## What changed under it

`wk pi setup` was written for the nftables design: it recorded each device's
tailnet address in `$WK_STORE/pi-hosts`, and the firewall replayed that file
into an nftables set after every policy rebuild.

The file survives, and so does its meaning, but the consumer is now the egress
proxy: `container/proxy/wk-proxy.py` re-reads `pi-hosts` on every request (it
is tiny, and `wk pi setup` must take effect without restarting the boundary).
Addresses in it are allowed on port 22 and nothing else.

The one rule to preserve: **individual addresses, never the CGNAT range.** The
workstation is itself an unrestricted tailnet node. Allowing `100.64.0.0/10`
would let a workspace reach every machine the workstation can, and the boundary
would be gone. `wk-proxy.py` blocks that range wholesale precisely so a
mistake here fails closed.

Fixed 2026-08-18, worth re-proving in step 2: that wholesale block used to
apply to the Pi addresses themselves, so an allowlisted device was refused as
"resolved to a blocked address" — the proxy now exempts exact `pi-hosts`
matches (`Policy.is_pi`) and nothing else in the range.

**Scheduling, revised 2026-08-19:** this now runs *after* the yocto image
build (`docs/HANDOFF-yocto.md`), not first — so `wk pi setup rpi4` gets a
freshly built image to run against, which tests the no-image-rebuild promise
against something other than the buildroot install it was written for. See
"Order, revised 2026-08-19" in `docs/HANDOFF.md`.

## What to do

1. `wk pi setup rpi4` against a running device (and rpi3 when it is up). It
   works over SSH and needs no image rebuild, which is the constraint that
   shaped it. Not the rpi5 -- see the scope note above.

   **The rpi4 is up as of 2026-08-20** at `raspberrypi4-64.local`, reachable as
   `rpi4-test` (`dotfiles/ssh/config`), and it runs the **WebKit Dev@CI Yocto
   image** (scarthgap 5.0.2), not the buildroot image this step was written
   against. The no-image-rebuild constraint holds either way and is now tested
   against something other than buildroot, which is what the 2026-08-19
   rescheduling wanted. It has no tailscale binary at all, so step 1 is
   genuinely unrun. See `docs/HANDOFF-netboot.md`, "State as of 2026-08-20",
   for everything else established about the board -- it is up, found, and its
   EEPROM is written.
2. Confirm the address lands in `$WK_STORE/pi-hosts` and that a workspace can
   then `ssh rpi4 uname -m` -- through `container/proxy/ssh-proxy.py`, since
   the workspace has no network interface.
3. Confirm the *negative*: a second tailnet address that is not in the file is
   refused. That is the check that proves the allowlist is an allowlist.
4. The Tailscale ACL grant in SETUP.md still applies, and still must not reuse
   `tag:server` -- that tag covers moose, nextcloud, immich, overleaf and the
   gateway.
5. `host/linux/rpi5/` needs an owner now that the rpi5 is a workstation. Its
   stability half (fan-max.service, WiFi, fstab/indexer, the NUMA kernel) is
   workstation setup and belongs in the rpi5's own `./setup` run, not in
   `wk pi setup`; its perf half belongs to the bench system
   (`perf-linux-rpi5`), which exists but does not carry it yet. Today the whole
   tree is manual state restored from backup -- the one part of a Pi wipe that
   nothing recreates.

## Traps

**ssh from a workspace has no DNS and no route.** It works only because
`~/.ssh/config` in the workspace sets a `ProxyCommand`, written by
`container/firstrun.sh`. A device added to `pi-hosts` by address will work; a
device referred to by hostname will not resolve, because the proxy resolves
names against the allowlist and a bare Pi hostname is not on it. Use addresses,
or add a name to the proxy's allowlist deliberately.

**The isolated guest network the design assumed does not exist.** Checked
2026-08-19 (`docs/HANDOFF-netboot.md`): the rpi4 is wired on the house LAN.
The design intent stands -- the tailnet plus the proxy allowlist is the only
path a *workspace* gets, whatever LAN the device sits on -- but do not treat
the boards as isolated, and do not "fix" a connectivity problem by widening
what the proxy allows.

**`host/linux/rpi5/` already contains real work** -- setup, verification,
stress, an overclock sweep and a NUMA kernel build. None of it is wired into
`cmd/pi`, and it predates this repo's conventions.
