# The Linux port: what it is now

**Status: a completed record, not a pending task.** Kept in the HANDOFF
namespace because other handoffs cite it; nothing here is waiting to be done.
Hardware and version details below describe the machine as verified in 2026-08
(Ubuntu 24.04 at the time, podman 4.9) and will drift.

The Linux side is built and running. This document replaces the original
handoff: it records what changed, what was verified, and what is left.

Read `SETUP.md` for how to use it and `README.md` for why the design is shaped
the way it is. Everything below is Linux-specific.

---

## The one big change: no root, and no firewall

The original design made rootful podman mandatory, because the egress policy is
nftables in the forward chain and rootless podman's traffic never reaches it.
That reasoning is correct, and it was the wrong trade: it puts root in the daily
path, so a container escape becomes a root escape, and every `wk` command needs
a password or a NOPASSWD rule that is root-equivalent in practice.

Rootless podman turned out to be able to do everything the design needs. All
three of the things believed to require root were tested and do not:

| claimed to need root | measured |
|---|---|
| overlay `:O,upperdir=` | works rootless; files land owned by the invoking user |
| GPU passthrough | `--device nvidia.com/gpu=all` works rootless |
| egress filtering | genuinely impossible rootless -- see below |

Only the firewall was real. Rootless podman's network helper terminates
container traffic and re-emits it from the init namespace, in a cgroup scope
named `rootless-netns-<random-hash>.scope`. There is no stable selector for
nftables to match on: not an interface, not a uid, not a cgroup path.

So the boundary changed instead of the privilege model. A workspace now runs
with **`--network none`** -- its namespace has loopback and nothing else -- and
reaches the outside through a single unix socket to `container/proxy/
wk-proxy.py`, an ordinary `systemd --user` service that allowlists by hostname.
There is nothing to filter because there is no interface, and nothing to bypass
because there is no second path.

What this bought:

- `wk` never calls `sudo` on Linux. Builds, benchmarks and `wk claude` run
  unattended with no privileged component at all.
- The allowlist is by hostname and port, so the GitHub, Fastly/PyPI and
  Anthropic CIDR lists, `resolved_hosts`, and `wk gc --refresh-net` are all
  gone. `github.com` replaces four ranges that had to be refreshed by hand.
- A name that resolves into RFC1918 or the tailnet range is refused *after*
  resolution, which the address-based policy could not express.
- It is testable: `wk verify` measures the properties from inside a workspace.

The macOS VM has since moved to the same allowlist-by-hostname model.

## The other thing that was open, and worse

wkdev's whole premise is host integration, and `wkdev-create` mounts the
session D-Bus socket, the system bus, the keyring, dconf, X11, PulseAudio, the
journal, `$HOME` at `/host/home`, and all of `$XDG_RUNTIME_DIR` at `/host/run`.

The session bus alone is a complete host escape: `StartTransientUnit` on it runs
any command outside the container as the user. It is not a network problem, so
no firewall of any kind would have caught it, and on the macOS VM it was
invisible because the VM's session has nothing in it.

SDK patch 11 adds `--isolated`, which turns that whole group off, and `wk`
passes it. Display integration is deliberately *not* in the group: benchmarks
need the GPU and a compositor socket, and `wk` passes those itself -- one
socket, in one directory -- rather than by sharing the runtime directory.

`wk verify` checks for `/host/home`, `/host/run` and both D-Bus sockets on
every run.

---

## What was verified on this machine

Ampere Altra (80x Neoverse-N1), 125 GB, NVIDIA RTX A400, Ubuntu 24.04 with
podman 4.9. The target is 26.04 with podman 5; the design deliberately does not
depend on the network backend, which is most of why `--network none` was chosen
over trying to make nftables work rootless.

| | |
|---|---|
| `./setup --stage machine\|sdk\|claude` | unprivileged; second run reports no changes |
| `wk sync` | 13 GB mirror, snapshot published |
| `wk new` | 2.4 s (macOS: ~30 s) |
| `wk build jsc-release` | 4m0s cold, 3m45s after the ccache fix, **1m0s in a fresh workspace** |
| `wk build wpe-release` | 20m17s |
| `wk build gtk-release` | 20m32s |
| `wk verify` | every check green, including a hardware renderer |
| `wk bench speedometer3` | ran end to end on WPE MiniBrowser |
| `wk bench motionmark1.3.1` | ran end to end on WPE MiniBrowser |
| `wk bench compare` | compare-results breakdown, with provenance warnings |

The GPU, from inside a rootless workspace with no network interface:
`renderer=NVIDIA RTX A400/PCIe | version=OpenGL ES 3.2 NVIDIA 580.173.02`.

The shared ccache is what makes the third build number possible: a fresh
workspace building jsc-release took **752/752 direct hits** from the cache the
previous workspace filled -- one minute against four.

## The GPU benchmarks, confirmed

With `wk quiesce on` and `wk session on` (cage on seat0, gdm stopped), every
preflight gate passes and the workspace renders on the real card:

| | |
|---|---|
| Speedometer 3 | 6.419pt (count 4, 40 iterations) |
| MotionMark 1.3.1, Multiply + CanvasArcs | 1163.4pt geometric |
| MotionMark, Triangles (WebGL) | **3119.7pt on the GPU vs 1665.2pt software** |

The EGL probe reports the card on all three platforms from inside the
workspace -- wayland, gbm and device -- and the direct evidence is `nvidia-smi`
during a run:

```
|    0   N/A  N/A   298912   G   /usr/bin/cage                      1MiB |
|    0   N/A  N/A   315789   G   ...WPE/Release/bin/WPEWebProcess    1MiB |
|    0   N/A  N/A   315825   G   ...WPE/Release/bin/WPEWebProcess   61MiB |
```

A workspace with no network interface, running rootless, rendering through the
host's NVIDIA card on the attached monitor.

Two things worth knowing about these numbers. The panel's preferred mode is
**1024x600 @ 59.8 Hz**, and MotionMark scores scale with surface size, so they
are not comparable with a 1080p run -- `wk session status` reports the mode and
`wk bench` records it. And `wk session on` is enough for GPU access even
without a re-login: logind grants an ACL on `/dev/dri/*` to the seat's active
user, and the workspace runs as that same uid under `--userns keep-id`.

## NVIDIA, and 26.04

The container needs userspace libraries matching the host driver exactly.
`nvidia-container-toolkit` generates a CDI spec that does this and works
rootless -- but it is **not in Ubuntu 26.04**. Verified against Launchpad:
`nvidia-container-toolkit 1.19.0+dfsg-0ubuntu1` is published in universe for
26.10 (stonking) only; resolute (26.04), questing and noble have nothing.

So `host/linux/gpu.sh` does both. It uses `/etc/cdi/nvidia.yaml` when present,
and otherwise derives the same set from `ldconfig` -- the versioned libraries
and their SONAME symlinks, the GBM backend, the glvnd and EGL vendor JSONs, and
the `/dev/nvidia*` nodes -- and bind-mounts each at its own path. A stock 26.04
install benchmarks without adding NVIDIA's repository; adding it just makes the
first branch win.

---

## What is left

Each with its own document:

- `docs/HANDOFF-linux-arm32.md` -- `wk new --arch 32`, which only this machine
  can run (the Ampere supports AArch32 at EL0; Apple Silicon does not)
- `docs/HANDOFF-linux-remote.md` -- the never-run remote target
- `docs/HANDOFF-linux-pi.md` -- provisioning the test devices, now that the
  proxy rather than nftables consumes `pi-hosts`

Also unfinished: `host/linux/config.dconf` is still a raw dump with
machine-specific junk in it (a weather location, four nm-applet WiFi UUIDs, a
GTK last-folder path, Ptyxis profile UUIDs, timestamps). `cmd/backup` has
filters for these and they have not been exercised. This is now tracked as
the Linux half of `docs/HANDOFF-settings-audit.md`, which covers verifying
that round trip and auditing what else has drifted.

---

## Traps

The originals still apply: build state must never be inferred from a process
list (`wk status`, exit 0/1/2/3), WebKit needs clang on aarch64, size builds
from the cgroup limit rather than free memory, never mutate a live overlay
lower layer, `wk rm` must delete the upperdir, and the workspace cannot reach
the Ubuntu archive (`WKDEV_OFFLINE=1`).

These are new, and each cost real time here:

**`set -o pipefail` plus `grep -q` silently inverts a test.** `lsmod | grep -q
'^nvidia'` fails: grep exits at the first match, lsmod dies of SIGPIPE, and
pipefail reports the pipeline as failed. The GPU branch was never taken and
workspaces got llvmpipe with no error anywhere. Capture into a variable and
test that.

**The same pipefail trap bites command substitution.**
`busy=$(for f in ...; do grep -q ... && echo x; done | wc -l)` aborts the whole
script when nothing matches: the loop's status is grep's, pipefail promotes it
to the pipeline's, and `set -e` takes it from there -- with no message, because
nothing failed loudly. Count in a loop instead of piping into `wc -l`.

**And a trailing `x && y` at the end of a function.** The function returns
non-zero when the test is false, and the caller -- a plain command under
`set -e` -- dies. Fine mid-function; fatal as the last statement.

**wkdev-enter prints its own chatter on stdout.** Its banner and host
integration notes land in front of the command's output, so anything that
captures the result of `t_exec` gets the SDK's text mixed into its data.
`--quiet` exists upstream for exactly this; `t_exec` passes it.

**`--userns keep-id` makes container-root a subordinate uid.** Everything
`.wkdev-init` writes is owned by 100000 on the host, and plain `rm -rf` cannot
remove it -- `wk rm` reported success and left the workspace behind. `t_destroy`
uses `podman unshare rm -rf` when rootless.

**`wkdev-create --shell` defaults to the host's `$SHELL`.** That path need not
exist in the image; with zsh on the host, `.wkdev-init` fails with `su: failed
to execute /usr/bin/zsh` and the workspace comes up with no git identity, no
push key and no Claude CLI. Pass `--shell` explicitly.

**A CONNECT proxy must drain the client's request headers.** Leaving them
buffered forwards them into the tunnel as the first bytes of the TLS stream,
and the error is `SSL routines::wrong version number` -- which looks like a TLS
version problem and is not.

**`wk sync` trusted the previous snapshot to be a git checkout.** A directory
that was not one made it fail after all the copying, leaving a second broken
snapshot for the next run to trip over. It now checks, and refuses to publish a
snapshot that did not complete.

**WebKit's own bwrap sandbox is off inside a workspace.** SDK patch 3 gates
`--security-opt unmask=ALL` and `seccomp=unconfined` behind `--unsafe-caps`,
and bubblewrap needs both, so MiniBrowser prints "Bubblewrap does not work
inside of this container, sandboxing will be disabled". That is the intended
trade -- the workspace is the sandbox, and re-enabling those two options would
let a workspace reach the host's `/proc` and `/sys` and drop the syscall filter
-- but it means a WebKit sandbox bug cannot be reproduced in here. Use
`--unsafe-caps` deliberately for that, in a workspace you then throw away.

**`compare-results` cannot be executed directly on Linux.** Its shebang is
`#!/usr/bin/env python3 -u`, and Linux `env` does not split arguments in a
shebang line, so running it gives `env: 'python3 -u': No such file or
directory`. It works on macOS, which is presumably why it is still there.
`wk bench compare` invokes it as `python3 -u Tools/Scripts/compare-results`.
It also needs scipy, which the SDK image does not carry; `wk bench` installs it
into the workspace on demand through the proxy.

**Software rendering is not what `LIBGL_ALWAYS_SOFTWARE=1` makes it.** That
variable, and `GALLIUM_DRIVER=llvmpipe`, only steer Mesa. With the NVIDIA
vendor still listed in `/usr/share/glvnd/egl_vendor.d`, glvnd loads it for the
GBM and device platforms and the run is hardware-accelerated while calling
itself software -- which is worse than no software mode at all, because the
result carries a label saying otherwise. `__EGL_VENDOR_LIBRARY_FILENAMES`
pinned to the Mesa vendor is the one that decides it. Measured: the WebGL
subtest scored 2592pt with the incomplete environment and 1665pt with the
complete one, against 3120pt on the GPU.

**compare-results needs at least two iterations.** With `--count 1` every
p-value is NaN and it dies inside its own significance sort with a bare
`AssertionError`. `wk bench compare` warns first.

**`stat -f` does not fail on Linux, it means something else.** `admin/install.sh`
tried the BSD form first -- `stat -f '%Su' FILE || stat -c '%U' FILE` -- and on
Linux `-f` is "file system status", so it printed a block of filesystem
statistics instead of failing over. Two consequences: the owner never equalled
"root" so the helper was reinstalled on every run, and the mode check that is
the entire argument for the NOPASSWD rule being safe was comparing its patterns
against that blob, so **it had never actually run on Linux**. GNU form first,
BSD as the fallback, and an unreadable mode is now a refusal rather than a pass.

**The dconf stage could never report "no changes".** It compared the whole live
tree against a dump holding only the captured subtrees, so they never matched
and every run reloaded and reported a change -- defeating the one signal
`./setup` exists to give. It now applies and then compares before and after.

**`grep -v` exits 1 when it prints nothing.** `host/dotfiles.sh` filtered stale
`source` lines out of `~/.zshrc`; on a `.zshrc` that contained *only* a stale
line, the filter emptied the file, returned 1, and `set -e` killed the stage --
and, before stages were independent, the four stages after it.

**iproute2 is not in the SDK image.** A check written with `ip` reports nothing
and looks like a pass. `wk verify` reads `/proc/net/dev`.

---

## Verification

```sh
./setup && ./setup                  # second run: no changes
wk sync
wk new smoke && wk build smoke jsc-release && wk run smoke -- -e 'print(1+1)'
wk verify smoke                     # the sandbox, measured from inside
wk rm smoke
```

Then the parts that need the monitor:

```sh
wk quiesce on
wk session on                       # cage on seat0; stops the display manager
wk verify smoke --gpu               # refuses on a software renderer
wk bench smoke speedometer3
wk bench smoke motionmark1.3.1
wk bench compare <run-a> <run-b>
```
