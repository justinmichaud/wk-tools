# Handoff: the Raspberry Pi test devices

`cmd/pi` is written and has never been run against a device from this machine.
The macOS side never needed it -- workspaces there could not reach the Pis
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

## What to do

1. `wk pi setup rpi5` and `wk pi setup rpi4` against running devices. It works
   over SSH and needs no image rebuild, which is the constraint that shaped it:
   the rpi4 runs a buildroot image.
2. Confirm the address lands in `$WK_STORE/pi-hosts` and that a workspace can
   then `ssh rpi5 uname -m` -- through `container/proxy/ssh-proxy.py`, since
   the workspace has no network interface.
3. Confirm the *negative*: a second tailnet address that is not in the file is
   refused. That is the check that proves the allowlist is an allowlist.
4. The Tailscale ACL grant in SETUP.md still applies, and still must not reuse
   `tag:server` -- that tag covers moose, nextcloud, immich, overleaf and the
   gateway.
5. Make `wk pi setup rpi5` also deploy `host/linux/rpi5/` to the device. Today
   that tree (overclock, fan-max.service, verify/stress scripts) is manual
   state restored from backup -- the one part of a Pi wipe that `./setup`
   cannot recreate. Mirror how the tailscale install already copies files in.

## Traps

**ssh from a workspace has no DNS and no route.** It works only because
`~/.ssh/config` in the workspace sets a `ProxyCommand`, written by
`container/firstrun.sh`. A device added to `pi-hosts` by address will work; a
device referred to by hostname will not resolve, because the proxy resolves
names against the allowlist and a bare Pi hostname is not on it. Use addresses,
or add a name to the proxy's allowlist deliberately.

**The devices are on an isolated guest network on purpose.** The tailnet is the
only path to them, which is what lets a workspace reach them without being able
to reach anything else on the LAN. Do not "fix" a connectivity problem by
putting them on the main network.

**`host/linux/rpi5/` already contains real work** -- setup, verification,
stress, an overclock sweep and a NUMA kernel build. None of it is wired into
`cmd/pi`, and it predates this repo's conventions.
