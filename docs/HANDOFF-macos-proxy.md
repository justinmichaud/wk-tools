# The macOS proxy migration (completed)

This handoff has been carried out. It is kept as the record of why the macOS
side changed, because the reasoning applies to anything that tries to add a
second boundary back.

## What changed

The macOS podman VM used to run rootful podman on a bridge network, with the
egress policy in nftables' forward chain. It now runs the same model Linux
does: rootless podman, `--userns keep-id`, `--network none`, and an egress
proxy (`container/proxy/wk-proxy.py`) as a `systemd --user` service owned by
`core` inside the machine.

- `WK_SANDBOX` no longer has two values; `targets/container.sh` has one path.
- `container/nftables/` is deleted, along with `wk gc --refresh-net`, the
  `github_v4` / `pypi_v4` / `resolved_hosts` sets and the CIDR refreshing.
- Nothing in the daily path calls `sudo` on either host.
- `wk verify` passes inside the machine, which is the only claim that counts:
  it measures the properties from inside a workspace rather than reading the
  configuration that was supposed to produce them.

## Why, in one paragraph

Rootless podman has no filterable forward path — its network helper re-emits
container traffic from the init namespace in a cgroup scope with a random name,
so there is no stable selector for a packet filter. Keeping nftables therefore
meant keeping rootful podman, and under rootful podman a container escape is a
root escape. Removing the interface removes the need for the filter, and the
privilege with it. The proxy is also a better policy engine: hostnames and
ports instead of CIDR lists that had to be refreshed by hand.

## What it cost

Two bugs surfaced during the migration and are worth remembering, because both
were silent:

**The SDK's systemd mount.** `wkdev-create` bind-mounts `~/.config/systemd/user`
when that directory exists — and installing the proxy unit created it. The
patch that was supposed to gate it behind `--isolated` had a shell precedence
bug (`guard || [ -d x ] && mount` parses as `(guard || [ -d x ]) && mount`), so
the gate did the opposite of what it said. The mount made podman create
`~/.config` in the container as container-root, an unmapped subordinate uid
under keep-id, so the workspace user could not chmod its own config directory.

**A failure in firstrun was invisible.** `.wkdev-init` runs the hook and
carries on regardless of how it exited, so the above aborted firstrun part-way
and the workspace came up without its lldb config or shell rc while `wk new`
reported success. firstrun now writes a completion marker and `wk new` waits
for it — see `t_ready` in the driver contract.

## The remaining half

Guest macOS VMs are a separate boundary and are handled by Softnet plus a
host-side proxy. See `docs/macos-vm.md`; that half is implemented but not yet
verified, and `docs/TESTING.md` marks exactly which checks are outstanding.
