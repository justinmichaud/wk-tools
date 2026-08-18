# Handoff: move the macOS VM to the proxy boundary

The Linux workstation no longer uses nftables. Its workspaces run with
`--network none` and reach the outside through a unix socket to
`container/proxy/wk-proxy.py`, an ordinary `systemd --user` service. The macOS
VM still runs the original design: rootful podman, a bridge network, and the
egress policy in nftables' forward chain.

Both work. This document is about collapsing them into one, because a security
boundary that is implemented twice is a boundary that is understood once and
verified never.

## Why Linux changed, and why it applies here too

Rootless podman has no filterable forward path: its network helper terminates
container traffic and re-emits it from the init namespace, in a cgroup scope
named `rootless-netns-<random>.scope`. There is no stable selector -- not an
interface, not a uid, not a cgroup path -- so nftables cannot express the
policy at all. Keeping nftables therefore meant keeping rootful podman, and
under rootful podman a container escape is a root escape.

None of that reasoning is macOS-specific. The VM is rootful for exactly the
same reason, and pays exactly the same price: everything inside it that talks
to podman does so as root.

The proxy also turns out to be a better policy engine than the packet filter:

| | nftables | proxy |
|---|---|---|
| granularity | IP ranges | hostnames, per port |
| GitHub | 4 CIDRs from `api.github.com/meta`, refreshed by hand | `github.com` |
| PyPI | 19 Fastly CIDRs | `pypi.org`, `pythonhosted.org` |
| `downloads.claude.ai` | `resolved_hosts`, refreshed by `wk gc --refresh-net` | covered by `claude.ai` |
| a name that resolves into RFC1918 | allowed if the address is | refused after resolution |
| verification | read the ruleset | connect and see what happens |

`wk gc --refresh-net`, the `github_v4`/`pypi_v4`/`resolved_hosts` sets and the
comment apologising for the last of them all disappear with it.

## What to do

1. Run the proxy on the VM. It is stdlib-only Python and already runs as an
   unprivileged service; the VM's `core` user has a systemd user instance, so
   the unit from `host/linux/sdk.sh` transfers almost verbatim. The one real
   decision is where the socket lives, since `/run/user/1000` in the VM is what
   `wk` would have to mount.

2. Flip `targets/container.sh`. The mode switch is already there --
   `WK_SANDBOX` is `vm-nftables` inside the VM and `rootless-proxy` outside --
   so this is deleting a branch, not adding one. `_sandbox_flags` and
   `_wrap_cmd` are the only two places that differ.

3. Decide about rootful. The VM can stay rootful (fewer moving parts, since
   the VM is already a boundary) or go rootless (`--userns keep-id` works, and
   the SDK patches for creating the container user become unnecessary). Going
   rootless is the bigger win and the bigger change; the proxy does not require
   it either way.

4. Delete `container/nftables/wk-egress.nft` and the firewall half of
   `host/macos/vmtools.sh` only once `wk verify` passes in the VM. That command
   tests the properties from inside a workspace rather than reading
   configuration, so it is the thing to trust here -- and it is what
   `wk claude` already gates on.

## Traps

**The VM has no `--network none` equivalent problem, but it does have a DNS
one.** With no interface there is no resolver, and anything that resolves
names itself rather than handing them to the proxy will fail. On Linux this
surfaced only in ssh, which is why `container/proxy/ssh-proxy.py` exists.

**`podman machine` boots CoreOS.** The proxy is Python 3 stdlib and CoreOS
ships Python 3, but the unit needs `systemd --user` and lingering for the
`core` user, which is not enabled by default there.

**Do not run both.** A workspace with an interface *and* a proxy socket has
the union of two policies, and the failure mode is silent: the nftables path
allows a raw connection the proxy would have refused, and nothing logs a
decision that was never asked for.
