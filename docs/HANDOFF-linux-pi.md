# HANDOFF — put the Pi test devices on the tailnet

**Scope: rpi4 and rpi3 only.** The rpi5 is a workstation with its own `./setup`
and full tailnet privileges, so it never goes through `wk pi setup` and its
identity never enters `pi-hosts`; benchmarking it means booting a bench system
with its own identity (`rpi5-perf.local`, mDNS, because the system carries no
tailscale). Linux-only work: workspaces on macOS could never reach the Pis
anyway.

`wk pi setup` works over SSH and needs no image rebuild — the constraint that
shaped it, and now tested against something other than the buildroot image it
was written for, since the rpi4 runs a Yocto system with no tailscale binary at
all.

## Remaining

1. **`wk pi setup rpi4`** against the running device (and rpi3 when it is up).
   Never run.
2. **Confirm the address lands in `$WK_STORE/pi-hosts`** and that a workspace
   can then reach the board — `ssh rpi4 uname -m` through
   `container/proxy/ssh-proxy.py`, since the workspace has no network interface.
3. **Confirm the negative**: a second tailnet address that is *not* in the file
   is refused. That is the check that proves the allowlist is an allowlist. It
   matters more now that a second route into that segment exists (below).
4. **The Tailscale ACL grant** — still owed, and it must not reuse `tag:server`,
   which covers moose, nextcloud, immich, overleaf and the gateway.
   (`docs/Security/HANDOFF-tailscale.md`.)
5. **`host/linux/rpi5/` needs an owner.** Its stability half (fan, WiFi, fstab,
   the NUMA kernel) is workstation setup and belongs in that board's own
   `./setup` run; its perf half belongs to the board's bench system,
   `webkit-2.52-yocto-rpi5-64`, which does not carry it yet. Today the whole tree is hand-restored state that nothing
   recreates (`host/linux/rpi5/HANDOFF.md`).

## What has changed under this, and does not replace it

The rpi4 is reachable from the workstation today — over the **tailnet bridge**,
pinned at `10.99.1.10` (`docs/HANDOFF-bmc.md`), which is how `wk pi deploy` and
`wk pi bench` drive it. That is a *host-side* path. A workspace still cannot
reach the board, and `pi-hosts` is exactly what would allow it.

## Traps

- **The consumer of `pi-hosts` is the egress proxy**, not nftables:
  `container/proxy/wk-proxy.py` re-reads it on every request, so `wk pi setup`
  takes effect without restarting the boundary. Addresses in it are allowed on
  port 22 and nothing else.
- **Individual addresses, never the CGNAT range.** The workstation is an
  unrestricted tailnet node; allowing `100.64.0.0/10` would give a workspace
  everything the workstation can reach. `wk-proxy.py` blocks that range
  wholesale so the mistake fails closed, and exempts exact `pi-hosts` matches
  (`Policy.is_pi`) and nothing else.
- **ssh from a workspace has no DNS and no route** — it works only through the
  `ProxyCommand` written by `container/firstrun.sh`. A device added by address
  works; one referred to by hostname does not resolve. Use addresses, or add a
  name to the allowlist deliberately.
- **The boards are not on an isolated guest network** — they are wired on the
  house LAN. The tailnet plus the proxy allowlist is the only path a *workspace*
  gets; do not "fix" a connectivity problem by widening what the proxy allows.
