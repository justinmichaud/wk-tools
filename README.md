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
| `--target` | another machine entirely — a Pi, a remote box, a machine booted into a bench system | `wk new` |

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

Network egress is allowlisted by hostname: Anthropic, GitHub, PyPI, the
Raspberry Pi test devices, the benchmark hosts, and — a deliberate widening,
recorded in the proxy itself — a fixed set of top sites plus the CDN domains
they cannot render without, so MiniBrowser can be driven at real pages.
Everything else, including the whole local network, is refused: nothing on the
list may resolve into RFC1918 or the tailnet range. *How* that is enforced
differs by host, and the difference is the most important thing in this file.

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
installed SUID once at setup time; `wk` still never calls sudo. The boundary is
measured the same way the container's is — the guest reaches the allowlist only
through the proxy, cannot reach the LAN, and cannot bypass Softnet
(docs/TESTING.md, "Sandbox"). Until `./setup --stage softnet` has run,
`wk vm start` refuses to boot a guest at all; booting with the open network
takes an explicit `WK_VM_UNFILTERED=1`, and `wk claude` refuses a guest booted
that way.

Workstations are never sandboxed. They join the tailnet normally and keep full
access to everything.

## Resources

Builds should never make the machine unusable. Three layers:

- The VM (macOS) or container (Linux) is capped at `cores - 1` and
  `memory - 12 GB`, so the host always keeps headroom (a headless machine
  holds back less — there is no desktop to protect).
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
| `local` | n/a — it *is* the workspace | what `wk` uses inside a container or guest |

Work never runs directly on a workstation. `local` is the degenerate driver a
workspace uses on itself, which is how `wk build` works from inside one; it is
never a target you ask for.

`remote` is the one target you can have several of, so a remote target is named
after the machine — `--target devbox-arm64-2`. A target is configured in two
places, and which one a fact belongs in is the distinction:
`targets/hosts/<name>.conf` in this repository holds what is true of the
machine (its ssh destination, the CMake flags its toolchain needs, whether it
is a build box or another workstation), so every device that pulls the
repository has that target; `~/.config/wk/targets/<name>.conf` holds this
device's own view of it and overrides the shared one line by line.
`wk remote setup <machine>` provisions one, without ever needing root on it,
and leaves `wk` usable *on* the machine as well as against it.
`wk sync --target <machine>` refreshes what it keeps: its copy of wk-tools and
its WebKit mirror.

A machine in the registry can also be another *workstation* rather than a build
box (`WK_REMOTE_PEER=1`): one that is asked and not driven, so `wk status` and
`wk ls` report what is building over there while `wk new`, `wk rm` and the
tooling push are refused. That is what makes one workstation's view include the
other's.

A workspace remembers the target it was created with, so only `wk new` ever
needs `--target`. On macOS that also decides whether a command is forwarded
into the podman VM at all: only container workspaces live there.

Shared build machines are treated as someone else's machine too — job count
comes from *that* machine's live load average and free memory, builds run at
`nice 19`, and a `flock` stops two of your own builds from stacking. There is
no sandbox there, so `wk verify` refuses a remote target outright — there is
nothing to measure — and `wk claude` stops at a barrier that says exactly
that; only `--force` proceeds, loudly.

`wk help targets` is the same ground as a decision — which target a piece of
work belongs on, and what a container costs against a macOS guest — and
`wk help machines` is how a machine becomes a target here in the first place.

## Disk

```sh
wk disk                # every place wk stores something here, with the total
wk gc                  # prune by reference count; can never lose work
wk gc --purge-mirror   # erase the master git store (wk sync refetches it)
wk gc --purge-pmos     # erase a phone-image build host's chroots (~8 GB)
wk vm base --rm        # erase the golden macOS image (hours to rebuild)
```

Three things dominate, and none of them is visible from the others: the golden
macOS base VM (162 GB measured here — Xcode, a checkout and a full build, sealed
so every `wk vm new` is an instant clone), the podman VM's sparse disk image
(which is where the whole container store lives on macOS), and the master git
store — the bare mirror plus the base snapshots hardlinked from it. `wk disk`
counts all three in one read-only report and never starts anything to do it;
`wk help disk` says what is safe to erase and what it costs to get back.

It also counts what is *not* on this machine: a pmos build host keeps about 8 GB
of pmbootstrap chroots, and nothing else would ever mention them — the machine
that has them does not know they are wk's. On a macOS host `wk gc` therefore runs
in two halves, one out here and one inside the podman VM, because that is where
the two stores are.

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

## Tailnet bridges

Some things cannot join a tailnet and cannot be reached from anywhere useful: a
BMC on a dedicated management port, a test board on a cable. A **bridge** is a
phone that routes such a segment onto the tailnet — WiFi one side, USB-C
Ethernet the other, `tailscaled` advertising the subnet in between. Behind one,
moose is reachable while it is powered off or has no working OS, and a
workspace can reach the rpi3 and rpi4 without anything on the house network
reaching either.

```sh
wk sysimage build bridge-pinephone            # a pmOS system for the phone
wk sysimage write <id> --disk rpi5:/dev/mmcblk0
                             # ...card into the phone, power on...
wk bridge setup <name>       # the role: idempotent, re-run after any change
wk bridge ls                 # what is declared, and what answers
wk bridge status <name>      # the on-device health check, read-only
wk bridge rm <name>          # removes the role, leaves the OS
```

Flashing is a command too. The image is built by pmbootstrap on a Linux aarch64
machine over ssh (`rpi5` by default) because pmbootstrap is Linux-only and needs
root, and both phones are aarch64 so nothing is emulated. It bakes in the ssh
key, the bridge's hostname, the role's packages, and the WiFi credential —
copied from the build host's own connection *on that host*, so the PSK never
travels through a log or an agent's context. What comes back is an image in the
store like any other, written to a card with the same verified path everything
else uses.

A bridge is declared in `bridge/hosts/<name>.conf` — the same shared-plus-local
split targets use — and provisioned over ssh by `bridge/provision.sh`, which is
the single source of truth for what is on the phone. That is the point of it:
the predecessor of this role was one hand-built Librem 5 whose configuration
lived on the device, with a README admitting it was the only copy in existence.

Both phones run postmarketOS, which is what makes one provisioner cover both.
Installing pmOS is hands-on, once per phone; applying the role is a command,
every time. `wk help bridge` has both, and the traps — the wall charger, the
kill switches, why a bridge is never powered off.

## Layout

```
setup              host bootstrap                lib/       shared helpers
wk                 the CLI                       cmd/       one file per subcommand
host/              per-OS setup and settings     targets/   container, vm, remote, local
dotfiles/          host dotfiles (Zed only)      claude/    settings, skills, hooks, CLAUDE.md
container/         workspace-side setup          build/     configs + the in-target build
container/proxy/   the egress boundary           container/gpu/  the EGL probe
admin/             quiesce + session helper      vm/        macOS guest provisioning
image/             system profiles + builders    boot/      the fleet: machines + boot drivers
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
quiesce on/off/status, and session on/on-bmc/gdm/gdm-bmc/stop/off/status.

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
- **host** — where it ran. `wk bench <ws>` runs in a workspace and records
  `container`: cgroup limits, a shared kernel, a desktop underneath. A run on
  the bare machine records `image`: `wk sysimage build <profile>` builds a
  bootable system from a spec in this repo, `wk boot <machine>` puts a fleet
  machine into bench mode for one boot (and hands it back), and
  `wk bench stage --to <machine>` / `wk bench staged` carry the build over and
  run it there. The two are not comparable — one has a desktop underneath it
  and one is the whole machine — and `wk bench compare` warns.
