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
wk new arm-bug --arch armhf   # a native 32-bit workspace (Linux only)
wk build bug-238 jsc-release
wk run bug-238 -- -e '1+1'
wk claude bug-238          # sandboxed agent, permissions relaxed
wk rm bug-238              # reclaims everything it created
zed ssh://wk-bug-238/src/WebKit
```

Creating a workspace costs one overlay mount, not a clone. The base snapshot is
shared and read-only; each workspace writes only to its own copy-on-write
layer.

### Architecture

`--arch armhf` makes the workspace itself 32-bit: an armhf image, an armhf
clang, armhf libraries, all executing natively — this Neoverse-N1 runs AArch32
at EL0, which is why this exists on the Linux workstation and can never exist
on Apple Silicon. Everything in there is native, so the configs mean what they
always meant: `wk build arm-bug jsc-release` is a 32-bit JSC. The architecture
is fixed at creation, recorded with the workspace, and shown by `wk ls`.

Three different things could all be called "building 32-bit here", and they get
three different words so no flag is ever a guess about which was meant:

| word | what it is | where |
|---|---|---|
| `--arch` | the workspace's own userland, executed natively | `wk new` |
| `--sysroot` | a *cross* build from a native workspace — another arch's libraries, `-m32`, an aarch64 clang | `wk build`, reserved, not implemented |
| `--target` | another machine entirely — a Pi, a remote box, a device booted off a benchmark image | `wk new` |

The armhf image has no NVIDIA userspace and never will, so an armhf workspace
is a software-rendering workspace. That is fine for JSC and for CPU-class
benchmarks, and it is not a rendering measurement — see below.

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

**macOS uses the same proxy**, running inside the podman VM as a `systemd
--user` service. It used to be rootful podman plus nftables; that is gone, and
the `WK_SANDBOX` comment in `targets/container.sh` records why.

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

`wk claude` runs that first for container workspaces and refuses to start if
anything fails. A firewall
that failed to load and a proxy that is not running both look perfectly fine
from the host's config files, and both fail open.

Tailscale ACLs are defence in depth, never the boundary: Tailscale's own
documentation notes that ACLs "don't affect local network traffic".

A macOS VM workspace of the `vm` target is filtered too, by different means:
Tart runs **Softnet**, a userspace packet filter, as a subprocess on the host,
default-denying everything except the address where the same `wk-proxy.py`
listens. The filter is outside the guest on purpose — `pf` inside it would be
modifiable by whatever is being sandboxed. Softnet needs root, so it is
installed SUID once at setup time; `wk` still never calls sudo. That path is
implemented but not yet verified, and until `./setup --stage softnet` has run
a guest's egress is unfiltered and `wk vm start` says so.

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
| `vm` | VM + Softnet default-deny + the same proxy | Apple ports, Tart; 2 running guests per host |
| `remote` | **none** | shared build boxes; polite, no containers |

There is no `local` target: work never runs directly on a workstation.

A workspace remembers the target it was created with, so only `wk new` ever
needs `--target`. On macOS that also decides whether a command is forwarded
into the podman VM at all: only container workspaces live there.

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
the first container workspace seeds a shared volume for all of them. macOS VM
workspaces cannot use that volume at all — the Keychain is not a file to share —
so log in once inside the golden base and every clone inherits it.

## Layout

```
setup              host bootstrap                lib/       shared helpers
wk                 the CLI                       cmd/       one file per subcommand
host/              per-OS setup and settings     targets/   container, vm, remote, local
dotfiles/          host dotfiles (Zed only)      claude/    settings, skills, hooks, CLAUDE.md
container/         workspace-side setup          build/     configs + the in-target build
container/proxy/   the egress boundary           container/gpu/  the EGL probe
admin/             quiesce + session helper      vm/        macOS guest provisioning
docs/              handoffs and design notes

```

## Benchmarking

```sh
wk quiesce on              # no password: setup installs a validated sudoers rule
wk session on              # a real GPU session on the attached monitor (Linux)
wk bench bug-238 speedometer3
wk bench compare <run-a> <run-b>
```

A session names its DRM device explicitly, and the mode decides whether a
number means anything. `on` is a measurable kiosk on the GPU. `on --bmc` moves
the session to the BMC's display chip so it can be watched over KVM-over-IP —
software-rendered, recorded in `/run/wk-session-mode`, and never a number to
compare with anything; `wk bench` refuses it and `wk test --gpu`, `wk enter`
and `wk gui` warn. `gdm [--bmc]` starts the actual display manager and desktop
instead, for debugging that needs more than a kiosk's one window. `off`
modesets every output dark and holds a placeholder compositor so the console
cannot repaint over it; a desktop comes back with `wk session gdm`, not with
`off` and a wait. `wk session status` reports the mode, the greeter type and
whether outputs are still lit.

The mechanics — why each mode works the way it does on a machine with an
NVIDIA GPU and a BMC display chip on the same seat — are in SETUP.md
section 6.

```sh
wk session on --bmc        # watchable over KVM-over-IP; never for numbers
wk session gdm --bmc       # a real desktop, forced onto the BMC's chip
wk session off             # outputs modeset off; back with wk session gdm
wk gui bug-238 [url]       # MiniBrowser in the seat, launched as perf runs do
```

Linux cannot make a `#!` script setuid — the kernel ignores the bit — so the
privileged half is a `NOPASSWD` rule naming one root-owned path, installed only
after `visudo -c` validates it, and re-checked for writability on every setup
run. The command set behind it is a fixed allowlist with no passthrough:
quiesce on/off/status, and session on/on-bmc/gdm/gdm-bmc/off/status.

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

Three axes decide what a run needs and what it may be compared with. All three
are derived rather than passed, recorded with the result, and warned about by
`wk bench compare` when two runs disagree:

- **class** — what the benchmark measures, from the plan. Speedometer and
  MotionMark are gpu-class and need a real compositor on a real GPU. JetStream
  and the other JS benchmarks are cpu-class: the GPU is not part of the
  measurement, so the run is not refused for lacking one and is not marked as
  degraded for it. That is what makes an armhf workspace — or any machine with
  no display — somewhere a JetStream number can honestly be taken.
- **runner** — what executes it, from the config's port. A browser port runs
  the plan in MiniBrowser through `run-benchmark`, which is the official number
  for every plan. A JSCOnly port runs the benchmark's own `cli.js` in the jsc
  shell, which is the only thing a JSCOnly tree can do; it writes the same
  result JSON, so `wk bench compare` and `compare-results` work unchanged.
- **host** — where it ran. Everything `wk bench` can currently reach is
  `container`: a workspace, with its cgroup limits and a desktop underneath it.
  The bootable benchmark image will be `image`, and the two are not comparable.
