# wk-tools

WebKit/JSC development environment for macOS and Ubuntu 26.04.

The organising idea: **the host stays boring.** Toolchains, checkouts, build
trees, caches and the coding agent all live in disposable workspaces. macOS
installs nothing at all; Linux installs stock apt packages plus Tailscale.

```sh
git clone https://github.com/justinmichaud/wk-tools ~/Development/wk-tools
cd ~/Development/wk-tools && ./setup
```

`./setup` is idempotent — run it whenever you sit down. A second run in a row
reports no changes.

**Setting up a new machine from scratch: see [SETUP.md](SETUP.md).**

## Workspaces

A workspace is a named, disposable environment for one task.

```sh
wk sync                    # fetch all upstreams, publish a base snapshot
wk new bug-238             # instant, regardless of checkout size
wk build bug-238 jsc-release
wk run bug-238 -- -e '1+1'
wk claude bug-238          # sandboxed agent, permissions relaxed
wk rm bug-238              # reclaims everything it created
zed ssh://wk-bug-238/src/WebKit
```

Creating a workspace costs one overlay mount, not a clone. The base snapshot is
shared and read-only; each workspace writes only to its own copy-on-write
layer.

### Why snapshots rather than one shared checkout

`git fetch` into a tree that a workspace has mounted would corrupt it. The
kernel is explicit:

> Changes to the underlying filesystems while part of a mounted overlay
> filesystem are not allowed. If the underlying filesystem is changed, the
> behavior of the overlay is undefined.

So `wk sync` never modifies a tree in use. It fetches into a bare mirror, then
publishes a *new* snapshot hardlinked from the previous one. Existing
workspaces stay pinned to the snapshot they were created from, and keep
working. `wk gc` reclaims snapshots once nothing references them.

Hardlinking the base to the working copy — the obvious idea — cannot work:
`link(2)` returns `EXDEV` across mounts, and hardlinked files share an inode,
so a read-only base would force every working copy read-only too.

## Isolation

A workspace cannot reach the host filesystem, and this is structural rather
than a convention. On macOS the podman machine is created with no virtiofs
mounts at all, so the VM cannot see `/Users`; `setup` verifies this on every
run and refuses to continue if it is not true. On Linux there is no VM, so the
equivalent is the container's own mount set: the SDK's host-session integration
— the host home directory, the session D-Bus socket, the keyring, the whole
runtime directory — is turned off with `--isolated`, and the workspace is given
the few trees it actually needs. That matters more than it sounds: the session
bus alone is a full host escape, and it is not a network problem, so no
firewall addresses it.

Network egress is restricted to the Anthropic API, GitHub, PyPI and the
Raspberry Pi test devices. Everything else, including the whole local network,
is refused. *How* that is enforced differs by host, and the difference is the
most important thing in this file.

**Linux: there is no network interface.** A workspace runs with
`--network none`. Its namespace has loopback and nothing else — no address, no
route, no resolver — so there is nothing to filter and nothing to bypass. Its
one channel is a unix socket to an egress proxy running as an ordinary
`systemd --user` service, which allowlists by hostname and refuses anything
resolving into RFC1918 or the tailnet range. Nothing in the daily path needs a
privilege: `wk` never calls `sudo` on Linux.

**macOS: nftables in the VM.** Rootful podman on a bridge, with the policy in
the forward chain. That is the original design and what macOS is verified
against; `docs/HANDOFF-macos-proxy.md` covers moving it to the proxy model.

Linux does not copy the macOS design because it cannot. Rootless podman has no
filterable forward path — its network helper re-emits container traffic from
the init namespace, in a cgroup scope with a random name — so nftables would
have meant keeping rootful podman, and under rootful podman a container escape
is a root escape. Removing the interface removes the need for the filter.

Either way the claim is checked by testing it, never by reading the
configuration that was supposed to produce it:

```sh
wk verify bug-238    # from inside the workspace: no interface, allowlist
                     # enforced, no path with the proxy bypassed, no host files
```

`wk claude` runs that first and refuses to start if anything fails. A firewall
that failed to load and a proxy that is not running both look perfectly fine
from the host's config files, and both fail open.

Tailscale ACLs are defence in depth, never the boundary: Tailscale's own
documentation notes that ACLs "don't affect local network traffic".

Workstations are never sandboxed. They join the tailnet normally and keep full
access to everything.

## Resources

Builds should never make the machine unusable. Three layers:

- The VM (macOS) or container (Linux) is capped at `cores - 2` and
  `memory - 12 GB`, so the host always keeps headroom.
- Builds run niced and `ionice`d, inside a systemd scope on Linux.
- Build processes get a raised `oom_score_adj`, so the OOM killer takes the
  build rather than your session.

Job count is derived from memory actually available, never from core count
alone — a WebKit link step is what turns a `-j$(nproc)` build into a hung
machine. `wk build` prints the numbers it chose.

## Targets

Every command runs against a target, behind one driver contract:

| Target | Isolation | Notes |
|---|---|---|
| `container` | overlay + no network interface + no host mounts | the default |
| `vm` | macOS VM (Tart) | Apple ports; Apple permits 2 per host |
| `remote` | **none** | shared build boxes; polite, no containers |

There is no `local` target: work never runs directly on a workstation.

Shared build machines are treated as someone else's machine too — job count
comes from live load average, builds run at `nice 19`, and a `flock` stops two
of your own builds from stacking.

## Claude

`wk claude <name>` runs the agent inside a workspace with permissions relaxed,
because the workspace is the blast radius. It runs `wk verify` first and
refuses to start unless the boundary measurably holds.

Claude Desktop can create and drive workspaces itself through the MCP server
registered by `setup` (`cmd/mcp`). That server exposes only the driver
contract — no generic host exec — and caps how many workspaces can exist.

Credentials do not transfer from the host: Claude Code uses the macOS Keychain
on Darwin and `~/.claude/.credentials.json` on Linux. One `claude login` inside
the first workspace seeds a shared volume for all of them.

## Layout

```
setup              host bootstrap                lib/       shared helpers
wk                 the CLI                       cmd/       one file per subcommand
host/              per-OS setup and settings     targets/   container, vm, remote
dotfiles/          host dotfiles (Zed only)      claude/    settings, skills, hooks, CLAUDE.md
container/         workspace-side setup          build/     named build configs
container/proxy/   the egress boundary           container/gpu/  the EGL probe
admin/             quiesce + session helper      docs/      handoffs
```

## Benchmarking

```sh
wk quiesce on              # no password: setup installs a validated sudoers rule
wk session on              # a real GPU session on the attached monitor (Linux)
wk bench bug-238 speedometer3
wk bench compare <run-a> <run-b>
```

Linux cannot make a `#!` script setuid — the kernel ignores the bit — so the
privileged half is a `NOPASSWD` rule naming one root-owned path, installed only
after `visudo -c` validates it, and re-checked for writability on every setup
run. The command set behind it is a fixed allowlist with no passthrough:
quiesce on/off/status, and session on/off/status.

`wk bench` wraps `run-benchmark` and `compare-results` rather than replacing
them. What it adds is the part that decides whether a number means anything:

- **it checks the environment before the run, not after.** Hardware renderer
  (a MotionMark score from llvmpipe is not a slow result, it is a different
  measurement), a real compositor, the performance governor, no other build
  running, an idle machine. A failed check refuses the run; `--force` records
  the result as forced so it can never be quietly compared with a clean one.
- **it pins the payload.** run-benchmark otherwise clones Speedometer, or
  fetches MotionMark file by file from the GitHub API, on every run. Seeded
  once into the shared cache and keyed by commit.
- **it records provenance.** Kernel, driver version, governor, container caps,
  WebKit sha and renderer land next to the result, because two JSONs with no
  provenance are not a comparison.
