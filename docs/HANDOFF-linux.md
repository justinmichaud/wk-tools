# Handoff: the Linux portion

You are picking up `wk-tools` to finish the Linux workstation support. The macOS
side is built and working; Linux is scaffolded but has never been run.

Read `SETUP.md` first for what the system does, and `README.md` for why the
design is shaped the way it is. This document covers only what is specific to
finishing Linux.

---

## What already exists

The whole `wk` CLI, the storage model, the firewall, the build pipeline and the
Claude integration are done and verified **on macOS**, where they run inside a
podman VM. On Linux there is no VM: everything runs directly on the workstation,
which removes a layer but changes several assumptions.

Working and tested end to end (on macOS):

- `./setup` — idempotent, seven stages, second run reports no changes
- `wk sync` / `wk new` / `wk build` / `wk run` / `wk test` / `wk rm` / `wk gc`
- `wk status` / `wk logs` — build state with a machine-readable exit code
- `wk skills` — the shared mutable skills directory
- The overlay snapshot model, the nftables egress policy, the SDK patches

Written but **never executed**: `host/linux/*`, `targets/remote.sh`,
`host/linux/session.sh`, `cmd/pi`, `cmd/vm`.

---

## The Linux-specific work

### 1. `./setup` on Ubuntu 26.04

`host/linux/tools.sh`, `settings.sh` and `apt.txt` exist and are plausible but
unrun. Expect small breakage. The rule to preserve: **stock apt only**, plus
exactly one third-party repository (Tailscale). Zed and Claude Code install via
their own scripts into `~/.local`; do not add repositories for them, and do not
install anything to a path the upstream tool does not choose itself — Claude
Code self-updates into `~/.local/share/claude/versions/` and a binary parked
elsewhere can never update.

`host/linux/config.dconf` is a raw dump from the old setup and still contains
machine-specific junk: a weather location, four `nm-applet` 802.1X WiFi UUIDs, a
GTK last-folder path, Ptyxis profile UUIDs, timestamps. `cmd/backup` has filters
for these but they have not been exercised. Verify a `wk backup` → `./setup`
round trip is clean before trusting it.

### 2. There is no VM, so several things move

On macOS the podman VM is the isolation boundary and `wk` forwards into it over
`podman machine ssh`. On Linux `wk` runs directly. Check every place that
assumes the VM:

- `lib/store.sh` defaults `WK_STORE=/var/lib/wk`. On Linux that is fine but the
  ownership assumptions differ — the container uid/gid are derived from
  `stat` on `$WK_STORE`, so whoever owns it determines the workspace user.
- `lib/resources.sh` keys off `/var/lib/wk/.headless`. **Do not create that
  marker on the workstation.** It selects a 2 GB reserve instead of 12 GB, which
  is right for a headless VM and wrong for a machine with a desktop — the whole
  point is that the GUI stays interactive.
- `wk` forwards non-host commands into the VM on Darwin only, so this should
  already be correct, but verify `is_host_command` behaves on Linux.
- `host/macos/vmtools.sh` has no Linux equivalent. On Linux the SDK still needs
  patching — write `host/linux/sdk.sh` doing the same clone + `git reset --hard`
  + `container/sdk-patches/apply.sh`, and load the nftables policy.

### 3. Rootful podman is mandatory

Not a preference. Rootless podman uses pasta, which terminates container traffic
and re-emits it as ordinary sockets from the init namespace; that traffic
reaches `output`/`postrouting` and **never `forward`**, so the egress policy has
nothing to filter and would silently enforce nothing. `targets/container.sh`
already calls podman through `sudo`.

Consequence: `--userns keep-id` is unavailable, so the container user is created
inside the image by the patched `.wkdev-init`, with uid/gid taken from the owner
of the mounted home. Do not hardcode 1000 — on the macOS VM the user is uid 501.

### 4. The firewall must allowlist the Pis by tailnet address

`container/nftables/wk-egress.nft` drops all RFC1918 and allows only Anthropic,
GitHub, PyPI, `resolved_hosts`, and the specific Pi tailnet addresses in
`pi_hosts`.

On Linux this matters more than on macOS: **the workstation is itself an
unrestricted tailnet node**. If you allow the whole `100.64.0.0/10` CGNAT range
instead of individual addresses, a workspace reaches every machine the
workstation can, and the boundary is gone. Keep it to individual addresses.

`/var/lib/wk/pi-hosts` holds them and is replayed after every policy rebuild.

### 5. GPU and the graphical session — the genuinely new work

This is the part with no macOS equivalent and the most room for surprise.

Benchmarks need real GPU acceleration; llvmpipe results are meaningless. The
machine has a monitor attached. `wk enter` keeps the SDK's existing desktop
integration (`--device /dev/dri`, the Wayland socket from `$XDG_RUNTIME_DIR`,
`WAYLAND_DISPLAY`) while dropping only the network and capability flags —
display and network are orthogonal, which is what makes this possible.

`host/linux/session.sh` is written but unrun. It handles two cases: someone
logged in at the machine (nothing to do), and nobody logged in (start `cage` on
seat0 so there is a real DRM session). Verify:

- `/dev/dri/renderD128` is passed through and usable from inside a workspace
- `glxinfo`/`eglinfo` inside the container reports the real GPU, not llvmpipe
- A workspace can run MiniBrowser/WPE against the real compositor
- The unattended path actually works with no user logged in

Note `XDG_RUNTIME_DIR` was a live bug on macOS: `wkdev-create` derived it from
the invoking uid, which is 0 under sudo, so containers got `/run/user/0` which
does not exist. Patch 10 in `container/sdk-patches/apply.sh` fixes it. On Linux
with a real session this matters more, because the Wayland socket lives there.

### 6. 32-bit containers come back

Dead on Apple Silicon (no AArch32 at EL0, no published armhf image), so the
macOS path refuses. On Linux `--arch arm` with the `24.04_arm32` tag should
work as it did in the old wiki setup. `wk new --arch 32` is not implemented —
add it to `targets/container.sh`.

### 7. `remote.sh` — shared build machines

Written, never run. No containers on those boxes: a plain checkout in your home
directory. The properties that matter are politeness, because they are other
people's machines: job count from live load average rather than `nproc`,
`nice 19`, `ionice -c3`, and a `flock` so two of your own builds cannot stack.
Per-target config goes in `~/.config/wk/targets/<name>.conf`.

`wk claude` deliberately refuses on remote targets — there is no sandbox there.

---

## Traps that cost time on macOS

Each of these was found the hard way. They are fixed, but the same class of
problem will recur.

**Build state must never be inferred from a process list.** `pgrep -f "ninja"`
matches its own command line, and a build between phases has no compiler running
while being perfectly healthy. Both mistakes were made here. Use `wk status`
(exit 0 ok, 1 failed, 2 running, 3 stalled) and `wk logs`. `wk build` has a
watchdog: heartbeat every 60 s, warning after 300 s of silence with diagnostics,
kill after 1800 s.

**WebKit needs clang.** GCC fails on aarch64 in
`JSObject::crashDueToEmptyValueAtValidOffset` with
`-Werror=volatile-register-var`. `build/configs.sh` sets it and records why.

**Size builds from the container's cgroup limit, not the machine's free
memory.** Inside a container the kernel still reports the whole host's
`MemAvailable`; deriving `-j` from it picks a number the cgroup OOM-kills
mid-link. That killed the first JSC build here.

**Mutating a live overlay lower layer is undefined behaviour.** `wk sync` never
touches a tree in use; it publishes a new snapshot and existing workspaces stay
pinned. Do not "optimise" this into a shared checkout.

**`podman` does not delete a user-managed overlay upperdir.** `wk rm` must, or
workspaces leak.

**The workspace cannot reach the Ubuntu archive.** `.wkdev-init`'s apt tasks are
skipped via `WKDEV_OFFLINE=1`. If something genuinely needs a package, put it in
the image rather than widening the firewall to Ubuntu's CDNs.

---

## Verification

```sh
./setup && ./setup            # second run: no changes
wk sync
wk new smoke && wk build smoke jsc-release && wk run smoke -- -e 'print(1+1)'
wk status smoke               # exit 0
wk rm smoke
```

Then the Linux-specific checks:

```sh
# GPU reaches the workspace
wk enter smoke -- sh -c 'ls /dev/dri && eglinfo | grep -i renderer'

# the firewall holds
wk enter smoke -- sh -c 'curl -sS -m5 https://github.com >/dev/null && echo github ok'
wk enter smoke -- sh -c 'curl -sS -m5 http://192.168.1.1 || echo LAN correctly blocked'
wk enter smoke -- sh -c 'ssh rpi5 uname -m'     # after wk pi setup

# the desktop survives a full build
wk build smoke gtk-release    # keep using the machine while this runs
```

Measured on macOS (M4, VM at 8 cores / 20 GB), for comparison: `wk new` ~30 s,
JSC release ~5 min cold / ~3 min warm ccache. Native Linux should beat this.
