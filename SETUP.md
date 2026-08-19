# Setting up a new machine

From a bare macOS or Ubuntu 26.04 install to a working WebKit environment.

The whole thing is one script plus three manual steps that need credentials or a
password prompt. Nothing here is order-sensitive except that `./setup` comes
first.

**`wk doctor` is the checklist.** Run it at any point — after `./setup`, after
a wipe, after an upgrade — and it prints what is provisioned and the exact
command for everything that is not, without starting or changing anything.

---

## 0. What you need beforehand

**macOS.** Four things, all signed installers or Apple tooling. `./setup`
installs nothing and will tell you which are missing:

| | Where |
|---|---|
| Xcode command line tools | `xcode-select --install` |
| podman | <https://podman.io/docs/installation#macos> — the official `.pkg`, **not** Homebrew |
| Zed | <https://zed.dev/download> |
| Tailscale | <https://tailscale.com/download/macos> |

Homebrew is deliberately not used and not required.

**Ubuntu 26.04.** Nothing. `./setup` installs stock apt packages plus Tailscale
(the one third-party repository); Zed and Claude Code install into `~/.local`
via their own scripts.

Root is needed in exactly three places on Linux, all of them during setup and
all of them interactive:

| | |
|---|---|
| `apt-get install` | the stock package list in `host/linux/apt.txt` |
| `usermod -aG render,video` | so an ssh session can open `/dev/dri` |
| the quiesce/session helper | one root-owned path plus a validated sudoers rule |

Nothing else ever needs it. Workspaces are rootless podman, the storage tree
lives under `~/.local/share/wk`, and the egress boundary is a `systemd --user`
service — so `wk new`, `wk build`, `wk run`, `wk bench` and `wk claude` never
prompt for a password and work unattended.

---

## 1. Clone and run setup

```sh
git clone https://github.com/justinmichaud/wk-tools ~/Development/wk-tools
cd ~/Development/wk-tools
./setup
```

Takes a few minutes on macOS, mostly downloading the VM image.

`./setup` is idempotent — run it whenever you sit down, and any time you change
something in this repo. A second run in a row reports **no changes**. If it
reports changes on an unmodified machine, that is a bug.

What it does:

- Verifies the host tools above.
- Applies desktop settings (keyboard shortcuts, dark mode, text substitution
  off) and links the dotfiles it owns.
- Installs the Claude config into `~/.claude`.
- Registers the `wk` MCP server with Claude Desktop.
- Creates the `wk` podman VM, sized to leave your desktop usable.
- Syncs this repo into the VM, clones and patches the container SDK, and loads
  the workspace firewall.

Individual stages:
`./setup --stage <tools|settings|dotfiles|claude|mcp|machine|vmtools|softnet|sdk|quiesce>`.

On Linux the interesting ones are `tools` (apt), `machine` (the storage tree),
`sdk` (the container SDK plus the egress proxy service) and `quiesce` (the
privileged helper).

> **It will offer to delete `podman-machine-default`.** Say yes unless you have
> containers in it you care about. applehv runs only one VM at a time, so a
> leftover machine actively prevents `wk` from starting — and it is typically
> tens of gigabytes.

### Existing files

`setup` never clobbers anything. A real file where it wants a symlink is moved
to `<name>.wk-backup` once, and it edits `~/.zshrc`, `~/.bashrc`, `~/.gitconfig`
and `~/.ssh/config` in place by appending a single guarded line each.

---

## 2. The quiesce and session helper (one sudo prompt)

```sh
./setup --stage quiesce
```

Needs a terminal, because sudo has to prompt. Installs a root-owned helper and
a `NOPASSWD` sudoers rule scoped to that one path, validated with `visudo`
before it goes in, so `wk quiesce on` never asks for a password again.

On Linux the same helper carries the benchmark session: `wk session on` stops
the display manager and starts a `cage` compositor on seat0, which is what
gives a workspace a real GPU session on the attached monitor. `wk session gdm`
starts the actual display manager and a real desktop instead, for debugging
that needs more than a kiosk's one window. The command set is a fixed
allowlist — quiesce on/off/status, and one session verb per session
configuration (`session-on`, `session-on-bmc`, `session-gdm`,
`session-gdm-bmc`, `session-off`, `session-status`) — with no argument that
becomes part of a command, and the user the compositor runs as is read from a
root-owned file rather than passed in.

Skip this if you are not benchmarking. Everything else works without it.

---

## 3a. Make root cost a password

```sh
wk sudo status          # here
wk sudo status --all    # and on every configured machine
wk sudo require         # install the drop-in (asks for your password)
```

Two defaults are worth closing on every machine you log into: sudo keeps a
five-minute timestamp, and Igalia's build machines grant `NOPASSWD: ALL`. One
drop-in of your own — `/etc/sudoers.d/zz-<user>-passwd`, which sorts after the
site's file and therefore wins — closes both. `wk doctor` reports the state on
every run.

---

## 3. The build key (one GitHub step)

Every machine that pushes gets **its own** deploy key: GitHub refuses one key
on two repositories but accepts many keys on one, so a per-machine key can be
revoked on its own and no private key is ever copied between machines.
`wk key register` generates and registers them for this workstation and for
every configured build machine; `wk status` lists them by fingerprint.

`setup` generates the local one; you register it once:

```sh
wk key register
```

Add the printed key at
<https://github.com/justinmichaud/WebKit/settings/keys/new> **with write access
enabled**. Without write access workspaces can read but not push.

It is a GitHub *deploy key*, so it is scoped to that one repository — a
workspace cannot push anywhere else with it, whatever its git config says.

Pushing is also a switch, and it is off by default whenever an agent is
running:

```sh
wk push status     # where the keys are, and therefore whether a push works
wk push on         # expose them to every workspace
wk push off        # hold them back; a push is refused at the door
```

`off` moves the private keys out of the directory workspaces have mounted, so
it takes effect immediately in workspaces that are already running and nothing
inside one can undo it. `wk claude` turns it off before it hands over control.
Fetching is never affected: every remote fetches anonymously over HTTPS.

---

## 4. First sources

```sh
wk sync
```

The first run clones WebKit and takes a while. After that it is incremental and
fast.

By default the mirror carries `main` only — WebKit has ~920 branches and
mirroring all of them costs tens of gigabytes for histories nobody checks out.
For more:

```sh
WK_MIRROR_BRANCHES="main safari-7620-branch" wk sync
wk sync --all          # every configured remote, not just origin
```

A workspace can also fetch any branch straight from GitHub on demand.

---

## 5. Use it

Measured on an M4 (10 cores / 32 GB), with the VM at 8 cores / 20 GB:

| | |
|---|---|
| `wk new` | ~30 s, including the Claude CLI install |
| `wk build jsc-release`, cold ccache | ~5 min |
| `wk build jsc-release`, warm ccache | ~3 min |
| `wk build wpe-release`, full WPE, cold | ~68 min |
| `wk test --layout`, 3 tests, software rendering | ~15 s |

> The macOS VM figures are in section 8 below; it is a separate target and
> shares none of this machinery.

> The WPE figure is a nested-virtualisation cost, not a WebKit one. The
> container gets 7 of the VM's 8 vCPUs, and the VM gets 8 of the host's 10
> cores — so a full WPE build has roughly 70% of the machine. Native Linux on
> the same core count should be substantially faster.


```sh
wk new bug-238               # instant, whatever the checkout size
wk build bug-238 jsc-release
wk run   bug-238 -- -e 'print(1+1)'
wk claude bug-238            # sandboxed agent
wk rm    bug-238             # reclaims everything it created
```

`wk build --list` shows the configs. `zed ssh://wk-bug-238/src/WebKit` opens it
in Zed.

The same commands work *inside* a workspace, with the name left out — there is
exactly one workspace in there and it is the machine:

```sh
wk build jsc-release         # from a shell in the workspace, or from an agent
wk run -- -e 'print(1+1)'
wk test
wk status                    # this workspace's own build and test state
```

### Shared build machines

A target can be a machine rather than a kind. Name it after an ssh destination
that already works, and set it up once:

```sh
wk remote setup devbox-arm64-2
```

That probes the machine, writes `~/.config/wk/targets/devbox-arm64-2.conf` if
there is none, pushes wk-tools, and configures your shell there. **It never
needs root** — a build box belongs to everyone who logs into it, so nothing is
installed, nothing outside `$HOME` is touched, and anything it finds worth
removing is a question rather than an action. Edit the conf for whatever
differs from the defaults:

```sh
# ~/.config/wk/targets/devbox-arm64-2.conf
WK_REMOTE_ROOT=/home/you/wk    # defaults to ~/wk on the box
WK_REMOTE_MAX_JOBS=16          # a ceiling, however idle the machine looks
WK_REMOTE_REFERENCE=/var/...   # a WebKit repo to clone from; usually detected
```

```sh
wk new bug-238 --target devbox-arm64-2
wk build bug-238 jsc-release   # sized from *that* machine's cores and load
```

The workspace is a plain checkout in your own home directory there — no
container, no overlay, no firewall. If the machine publishes a WebKit
repository and says so in its MOTD, workspaces are cloned from that, with the
objects hardlinked, so a checkout costs its working tree and nothing else;
otherwise the driver keeps one mirror under the root and only the first
workspace pays for the history. Builds are niced to the floor, capped by the
remote machine's live load average and by the job ceiling, and serialised
against each other with a flock, because other people are using the box too.

After setup the machine can drive itself, which is the point of provisioning
it at all — ssh in and you get zsh (where the box has one) and `wk` on PATH:

```sh
ssh devbox-arm64-2
wk ls                          # the workspaces on this machine
wk build bug-238 jsc-release   # same paths, same job policy, no ssh hop
```

The commands that act on a workstation's own store or hardware — `wk sync`,
`wk gc`, `wk vm`, `wk pi` — refuse there and say why, and so do `wk new` and
`wk rm`: a build machine holds workspaces, it does not own them. The record of
which machine each workspace lives on is the workstation's, and it is what
sends a later `wk build` to the right place.

There is no sandbox on a machine like that, so `wk claude` and `wk verify`
both refuse it outright rather than pretending otherwise.

### 32-bit workspaces

```sh
wk new arm-bug --arch armhf  # a native armhf container (Linux only)
wk build arm-bug jsc-release # a 32-bit JSC; no special config
```

The workspace itself is armhf: an armhf image, an armhf clang, armhf libraries,
executing natively — this Neoverse-N1 runs AArch32 at EL0, and Apple Silicon
does not, so this is permanently a Linux-workstation capability. Because it is
native, the configs are the ordinary ones and mean the ordinary things.

Two things follow from the architecture rather than from the config, and
`lib/arch.sh` is where they live: the build runs under `linux32` (without it
`uname -m` answers with the *host's* aarch64, and CMake configures a 64-bit
tree for a 32-bit compiler), and it links with gold and the mapping options
off (a 32-bit linker has under 4 GB of address space to link WebKit in,
regardless of the memory on the machine).

An armhf workspace gets no GPU — the NVIDIA userspace is published for aarch64
only — so it is a software-rendering workspace. `wk bench` knows: it runs
CPU-class plans there and refuses GPU-class ones.

A cross build against a sysroot is a *different* mechanism and gets a different
word, `--sysroot`, which is reserved and refused for now
(`docs/HANDOFF-cross-compile.md`).

That is the interface `wk claude` hands an agent, and the only one available to
it: a workspace has no podman and a macOS guest has no nested virtualisation, so
there is nothing to reach out to. Provisioning writes `~/.wk-workspace` naming
the checkout, and `wk` uses it to select `targets/local.sh`, where the target is
this machine. Commands that act on a host — `wk sync`, `wk gc`, `wk session`,
`wk quiesce`, `wk vm`, `wk pi` — refuse in there rather than acting on an empty
store, and a workspace can neither create nor destroy a workspace. `wk verify`
refuses too: half of what it measures is only visible from the host, so a green
result from inside would mean nothing.

Before trusting a workspace with an agent, or with a benchmark, check that the
sandbox is what it claims:

```sh
wk verify bug-238          # tests the boundary from inside the workspace
wk verify bug-238 --gpu    # and requires a hardware renderer
```

---

## 6. Benchmarking on Linux

Benchmarks need three things beyond a build, and `wk bench` refuses to run
without them rather than producing a number that cannot be defended:

```sh
wk quiesce on                          # performance governor, no indexer
wk session on                          # a real compositor on the monitor
wk bench bug-238 speedometer3          # preflight, then run-benchmark
wk bench bug-238 motionmark1.3.1
wk bench ls
wk bench compare <run-a> <run-b>       # compare-results, with provenance checks
```

What "three things" means depends on the benchmark, and `wk bench` derives that
rather than asking. Speedometer and MotionMark are gpu-class: a real compositor
on a real GPU is part of the measurement, and a run without one is refused.
JetStream and the other JS benchmarks are cpu-class: the GPU is not part of the
measurement, so the run is neither refused nor marked as degraded for lacking
one, and it runs headless where there is no display to use. The governor,
idleness and payload checks apply to both.

The config decides what executes the plan: a browser port runs it in
MiniBrowser through `run-benchmark`, and a JSCOnly port runs the benchmark's own
`cli.js` in the jsc shell — the same JSON out of both, so `wk bench compare`
does not care which. What it does care about is a mismatch: class, runner,
architecture and host all land in the result and all produce a warning when two
runs disagree.

```sh
wk bench arm-bug jetstream3 --config jsc-release   # jsc shell, no GPU needed
wk bench bug-238 jetstream3 --config wpe-release   # MiniBrowser, the official number
```

The NVIDIA driver itself comes from Ubuntu's own packages (`ubuntu-drivers
install`, or the `nvidia-driver-*` metapackage) and is not something `./setup`
manages — it is a kernel driver for the machine, not a workspace dependency.
Everything the *container* needs is derived from whatever driver is installed,
by `host/linux/gpu.sh`, at workspace creation.

`wk session on` stops the display manager and starts `cage` on seat0 —
deliberately not a desktop, so there is no compositing work in the numbers that
is not the browser's. `wk session off` stops everything, including the display
manager, and leaves the screen dark rather than restoring it; getting a
desktop back afterwards is `wk session gdm`, covered below, not `off` and a
wait.

"Dark" takes two things, and for a while it only did one of them.

Stopping the compositor and trusting the kernel's own console to blank a
now-masterless output was the original design, and this machine's NVIDIA driver
doesn't implement that -- `fb_blank` stops the console being redrawn without
reaching real hardware DPMS, so whatever was on screen when the compositor
stopped just stays there, frozen, which reads as "off" failed to anyone watching
sysfs and as "off" doing nothing to anyone looking at the monitor. So `off`
holds a placeholder compositor on the GPU instead of releasing it.

That part was right, and on its own it is not enough: a compositor that holds an
output and paints it black is still scanning out a real mode, so the monitor has
a perfectly good signal and stays lit. The screen was black and the backlight
was on, which is what "`off` doesn't turn the display off" turned out to mean.
`cage` has no DPMS option and no `wlr-output-power-management`, but it does
export `wlr-output-management`, and disabling a head through that protocol is a
real atomic modeset with the CRTC off: the connector goes `enabled` ->
`disabled`, `dpms` `On` -> `Off`, the signal stops, and the monitor sleeps for
want of one. It reaches hardware precisely because it is ordinary modesetting
rather than `fb_blank`. `wlr-randr` is the client that sends it, which is why it
is in `host/linux/apt.txt` for an 11 kB package that nothing else uses.

Neither half substitutes for the other: the modeset is what darkens the monitor,
and the held device is what stops the console being repainted over the top of
it. `wk session status` prints a `lit:` line -- the connected outputs still
being scanned out -- so "black" and "off" can be told apart from a shell instead
of from a chair in front of the monitor.

One gap is left on purpose. The placeholder covers one DRM device, not both:
wlroots takes the first entry in `WLR_DRM_DEVICES` as primary and needs a
renderer that can copy frames to any secondary, which `pixman` cannot do for a
device with no render node. Naming both got one head and no warning, so `off`
darkens the GPU's outputs and leaves the `ast` showing whatever was last latched
into it; the BMC console keeps its last frame, and the `lit:` line says so.

The compositor's DRM device is named explicitly, and that is the point. This
machine has two: the GPU, and the ASPEED BMC's display-only `ast` chip, whose
framebuffer is what the BMC serves over KVM-over-IP. Both are on seat0, so left
to enumerate, a compositor may take either — and which one it took decides
whether a number came from the GPU or from a management controller. A benchmark
session lists the GPU alone.

### Debugging a GUI remotely

```sh
wk session on --bmc          # put the session where the BMC can see it
wk gui bug-238 about:blank   # MiniBrowser in the seat, as a perf run launches it
wk session on                # back to a measurable session
```

`--bmc` is the opposite trade, and it says so every time it is used. The BMC's
chip becomes the compositor's only modesetting device — so the monitor goes
dark, the session *moves* to the BMC rather than being duplicated onto it, and
there is exactly one output, which is the one the BMC serves. The `ast` chip
has no render node of its own, so this session is software-rendered (`mode:
bmc`) — slower than a real session, and never a number to compare with
anything.

Rendering on the GPU and having wlroots copy each finished frame across to the
`ast` was tried and dropped: on an NVIDIA GPU it doesn't fail loudly the way a
refused renderer does — the compositor comes up, the socket appears, and every
frame just fails to produce something the `ast` connector can show, forever.
Software rendering is slower still, but it actually shows something.

The mode is recorded in `/run/wk-session-mode`, and `wk bench` refuses to run
against anything but `gpu`: neither the socket nor the GPU probe can tell the
difference, because the probe asks the *client* which EGL vendor it got and the
answer is still NVIDIA when the compositor behind it is llvmpipe.

### A real desktop remotely

`on` and `on --bmc` are a kiosk: cage, one window, nothing to log into.
`wk session gdm [--bmc]` starts the actual display manager and desktop instead
-- for debugging that needs more than a single browser window.

```sh
wk session gdm --bmc         # a real, loggable-into desktop, forced onto the BMC
wk session off                # outputs modeset off, nothing hidden
```

cage gets its one device from `WLR_DRM_DEVICES`; gdm's compositor (mutter) has
no equivalent, so pinning it works the other way around. `wk session gdm --bmc`
strips the GPU of the udev tags that make it a member of any seat -- the same
tags that would come off if it were physically unplugged -- so it is on
neither seat0 nor anywhere else, and mutter has nothing left to pick but the
`ast`. Retagging it onto a *different* seat instead of off every seat was the
first attempt, and the wrong one: logind creates a real seat for wherever it's
retagged to, and gdm starts a greeter on every seat it finds, so that produced
two live desktops -- one per chip -- instead of the one that was asked for.
Plain `wk session gdm` hides the `ast` instead, so the desktop can't land on
the management chip by mistake. Either way the hide is undone by
`wk session off` or by starting any other session mode; nothing here leaves a
device permanently missing.

The hide only reaches a compositor that asks logind for its devices, and that
caveat is load-bearing rather than theoretical. `wk session gdm --bmc` used to
hide the GPU successfully and put the desktop on the monitor anyway: Ubuntu's
`61-gdm.rules` sends this machine to `gdm_prefer_xorg` the moment it sees
`nvidia_drm` with `modeset=Y` — the branch ends in an unconditional `GOTO`, so
every driver version takes it — and NVIDIA's Xorg driver never opens
`/dev/dri/cardN`. It drives the card through `/dev/nvidia0`, `/dev/nvidiactl`
and `/dev/nvidia-modeset`: character devices, mode `0666`, carrying no udev
properties and no seat tags for `TAG-="seat"` to remove. The hide was real and
irrelevant, and it failed in the most convincing way available — reporting the
mode it had just been unable to enforce. (It is also why an Xorg greeter makes
the `lit:` line under-report: NVKMS modesets outside DRM, so the connector's
sysfs `enabled` never changes.)

So both gdm modes force the greeter onto Wayland, where `mutter` goes through
`libseat` and the hide is real. The override goes through gdm's own
`gdm-runtime-config` — the same tool `61-gdm.rules` uses to state its preference
— so it lands in `/run` and is gone at the next boot, the same crash-safety the
seat rule gets from living there. Afterwards the greeter's session type is
checked rather than assumed: `wk session status` prints a `greeter:` line, and
an `x11` greeter there means the `desktop:` line above it is a statement of
intent rather than of fact.

Payloads are seeded once into `~/.local/share/wk/cache/bench` and pinned by
commit, so a run does not clone Speedometer or pull MotionMark file by file
from the GitHub API every time. Results land in `~/.local/share/wk/bench/<run>`
with an `env.json` recording kernel, driver, governor, container caps, WebKit
sha and the renderer that was actually used.

---

## 7. Optional: Raspberry Pi test devices

Only if you run benchmarks on real hardware.

Put the Pis on an isolated guest network, then:

```sh
wk pi setup rpi5
wk pi setup rpi4
```

Works over SSH against a running device and needs **no image rebuild** — which
matters because the rpi4 runs a buildroot image. It installs Tailscale's static
binaries, tags the node `tag:wk`, and records its tailnet address so the
workspace firewall lets it through.

One manual step, in the Tailscale admin console — grant `tag:wk` access to
itself:

```hujson
"tagOwners": { "tag:wk": ["autogroup:admin"] },
"grants": [
  { "src": ["tag:wk"], "dst": ["tag:wk"], "ip": ["*"] },
  { "src": ["autogroup:member"], "dst": ["*"], "ip": ["*"] }
]
```

> Do not reuse the existing `tag:server`: it covers moose, nextcloud, immich,
> overleaf and the gateway, and workspaces would get all of them.

> **Caveat when editing the policy: `autogroup:member` does not cover a tagged
> node.** moose and the rpi5 both carry `tag:workstation`, and a tagged device's
> traffic is attributed to its tags rather than to the user who set it up — so a
> grant written only as `autogroup:member → *` would not authorise anything moose
> originates, and the path to a Pi from a workspace goes *through* moose. The live
> tailnet already permits this (verified 2026-08-19 with
> `tailscale ping --icmp rpi5`, which is subject to ACLs, unlike a plain
> `tailscale ping`); the note is here so a future tightening of the policy keeps
> a tag-sourced grant such as:
>
> ```hujson
> { "src": ["tag:workstation"], "dst": ["tag:wk"], "ip": ["tcp:22"] },
> ```

---

## 8. Optional: macOS VMs for Apple-port builds

Only if you build the Apple ports. Needs [Tart](https://tart.run), which
`./setup` deliberately does not install: the binary needs the
`com.apple.security.virtualization` entitlement, so it only works from inside
the signed `.app` bundle Cirrus Labs ships — not from a bare copied path — and
that is a manual decision, not something setup should do quietly. The licence
is FSL-1.1-ALv2; internal use is a Permitted Purpose.

```sh
mkdir -p ~/.local/share/tart ~/.local/bin
curl -fsSLO https://github.com/cirruslabs/tart/releases/latest/download/tart.tar.gz
tar -xzf tart.tar.gz -C ~/.local/share/tart/
ln -sfn ~/.local/share/tart/tart.app/Contents/MacOS/tart ~/.local/bin/tart
```

```sh
wk new mac-rel --target vm     # builds the golden base the first time
wk vm start mac-rel
wk build mac-rel mac-release
```

The first run pulls a prepared macOS + Xcode image (**~69 GB compressed**) and
clones WebKit inside it. Budget about three hours, once. Every workspace
afterwards is an APFS copy-on-write clone of that: measured at **1 second and
about a megabyte of real disk**.

Measured on the same M4, guest at 9 vCPU / 20 GB:

| | |
|---|---|
| `wk new --target vm` | ~1 s |
| `wk vm start`, cold boot to ssh | ~10 s |
| `wk build mac-release`, cold (there is no ccache here) | ~99 min (measured before `--export-compile-commands` was added; re-measure per HANDOFF-mac-minibrowser H3) |

Three limits to know about:

- The stock image has no room to build in, so the guest disk is grown to
  `WK_VM_DISK_GB` (320 GB) before its first boot. Left alone, a build dies
  after half an hour with `No space left on device`.

- Apple permits exactly **two running** macOS VMs per host, and
  Virtualization.framework enforces it. `wk vm start` refuses at the limit
  rather than failing opaquely with `VZErrorDomain` code 6.
- On a 32 GB machine the podman VM already holds the whole memory envelope, so
  the two cannot run at once. `wk vm start` refuses with the numbers and tells
  you what would fit.

> **A guest's egress filter needs one extra step.** Run
> `./setup --stage softnet` (it needs a terminal for sudo) to install Softnet,
> which default-denies the guest's network on the host side and leaves only the
> `wk-proxy` address reachable. Until it is installed, `wk vm start` refuses to
> boot a guest — booting with the open network takes an explicit
> `WK_VM_UNFILTERED=1`, and `wk claude` refuses such a guest the same way.

---

## Moving to another machine

Nothing is machine-specific except what `wk backup` captures and
`~/.config/wk/targets/*.conf` — the shared-build-machine confs above, which are
machine-local by design and which `./setup` neither writes nor backs up, so a
re-install loses every remote target until they are written again. On the new
machine, clone and `./setup`. To carry your current desktop settings across,
run `wk backup` on the old one first and commit the result — it writes live
settings back into `host/macos/defaults.conf`, `host/macos/symbolichotkeys.plist`
and `host/linux/config.dconf`.

`wk backup` never captures credentials. `~/.config/gh` holds an OAuth token and
is excluded explicitly.

---

## Running tests

```sh
wk test bug-238                              # JSC tests
wk test bug-238 --layout --config wpe-release -- fast/dom/Element
wk test bug-238 --layout --gpu -- <paths>    # real GPU (Linux, with a seat)
```

Layout tests default to **software rendering** (`LIBGL_ALWAYS_SOFTWARE=1`,
`GALLIUM_DRIVER=llvmpipe`, `WEBKIT_DISABLE_DMABUF_RENDERER=1`). There is no
`/dev/dri` inside a workspace on macOS — the podman VM has no GPU to pass
through — so llvmpipe is the only option there. On Linux with a real seat,
`--gpu` uses the device; anything measuring rendering performance needs it,
because llvmpipe numbers are meaningless.

`--no-retry-failures` is on by default: a retry doubles the runtime and hides
flakiness, which is the opposite of what an automated check wants.

Results land in `WebKitBuild/<port>/<config>/layout-test-results`.

## Knowing whether a build is alive

`wk build` reports its own state, so this never has to be inferred from a
process list:

```sh
wk status              # all workspaces; exit code is machine-readable
wk status bug-238      # 0 ok/idle, 1 failed, 2 running, 3 stalled
wk logs   bug-238      # errors first, then recent output
wk logs   bug-238 -f   # live
```

While a build runs it prints a heartbeat with the ninja progress counter, and
if the log goes silent it says so rather than letting you wait:

- **300 s of silence** → warning, plus diagnostics: last progress, how many
  compilers are actually running, free memory, and any cgroup OOM kills.
- **1800 s of silence** → the job is killed and reported as stalled (exit 124).

Tune with `WK_STALL_SECONDS`, `WK_ABORT_SECONDS`, `WK_HEARTBEAT_SECONDS`.

On failure the first real error is extracted for you. That matters more than it
sounds: compilers keep going past the first error, so the end of a WebKit build
log is usually unrelated to what actually broke.

## If something is wrong

```sh
WK_DEBUG=1 ./setup          # verbose
wk ls                       # workspaces, their snapshots, disk use
wk gc                       # reclaim disk, including fstrim
```

**Re-running `./setup` is the repair tool.** The VM's configuration — tooling,
SDK checkout and patches, firewall, network, packages — is regenerated from this
repo on every run, so anything changed by hand inside the VM is reverted. Your
data is not touched: the mirror, snapshots, workspaces, caches, shared skills
and the build key all survive.

To start the VM over completely:

```sh
podman machine rm wk && ./setup && wk sync
```

That loses the mirror and every workspace, so it is a last resort rather than a
first move.
