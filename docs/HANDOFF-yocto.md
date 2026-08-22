# HANDOFF — Yocto systems

The task, as set: on the container targets (Linux, and the podman VM on
macOS), support building WPE WebKit Yocto images —
https://github.com/justinmichaud/justinmichaud.github.io/wiki/Building-WPEWebKit-for-32%E2%80%90bit-Raspberry-Pi-3-(Yocto-Wayland)
— with the Yocto cache preserved even when the workspace is destroyed,
support for getting a new build onto the target, and tailscale installable on
the rpi target. Writing a built system onto a physical disk is tracked
generically in `docs/HANDOFF-sdcard.md` (not yocto-specific) — consume that
rather than building a separate copy-to-host path here.

**The test loop.** Settled 2026-08-20 on the hardware's own evidence
(`docs/HANDOFF-benchmarking.md`, "rpi4"). The rpi4 is a **bench-device** booted
from its USB stick with the SD card as the rescue role (`boot/pi-usb.sh`):
`wk sysimage write <id> --disk rpi4:/dev/sda`, then `wk boot rpi4`, one-shot,
back to host mode by itself. `wk pi setup rpi4` against the resulting image is
the step after this one (`docs/HANDOFF-linux-pi.md`).

---

## State — the command exists, and the build has run to completion

`wk sysimage build downstream-yocto-wpe-2.48-rpi4` builds the WPE WebKit 2.48
Yocto system for the rpi4. (It was `wk image build rpi4-wpe-2.48` until the
2026-08-20 rename — `docs/HANDOFF-vocabulary.md`; the old spellings below are
kept where they record what was actually run.) It is a **builder** inside the
existing `wk sysimage`, not a command of its own, and that is the load-bearing
design decision here — see the header of `image/yocto.sh` for the argument in
full. In short: what comes out is a partitioned disk image for one machine,
which is exactly what the store, the manifest, `wk sysimage ls/show/write` and
the disk path already handle. A second command would have meant a second copy
of all of it, which this handoff explicitly asks not to build.

```
wk sysimage build downstream-yocto-wpe-2.48-rpi4
    [--dry-run] [--detach] [--stop] [--stage <s>] [--workspace <name>]
    [--keep-work] [--no-import] [--chromium]
```

Stages: `layers fetch image toolchain webkit`.

| file | what it is |
|---|---|
| `image/profiles.sh` | the profile. `IMG_BUILDER=yocto` is what dispatches; `YOC_BRANCH`/`YOC_TARGET`/`YOC_IMAGE`/`YOC_RM_WORK`/`YOC_CHROMIUM` are its own fields |
| `image/yocto.sh` | the host half: workspace, stages, detach, wait, stop, import, manifest |
| `image/yocto-build.sh` | the workspace half: the SDK-environment unset, the preflight, the `local.conf` additions, one call to `cross-toolchain-helper` per stage |
| `cmd/sysimage` | a few lines of dispatch; the distro path is untouched |
| `container/yocto/Containerfile` | the workspace image: `ubuntu:24.04` plus Yocto's host tooling — a *supported* build host |
| `container/proxy/wk-proxy.py` | the seven source hosts, with the audit note |
| `lib/target.sh`, `targets/container.sh` | `t_home`, `t_spawn`, `t_pull` — detach and file-copy primitives, added to the driver contract because `podman exec` needed its own of each |

### The four decisions worth knowing

**The version pin is a branch, not a list.** `YOC_BRANCH=webkitglib/2.48` —
that branch's `Tools/yocto/rpi/manifest.xml` pins poky, meta-openembedded,
meta-raspberrypi, meta-webkit, meta-clang and meta-browser by commit, and
`targets.conf` carries the `rpi4-64bits-mesa` section and its `local.conf`. So
this repo restates none of it. The pin is also *the same pin* as the WebKit
that will run on the board, which is the property that makes the pair coherent
— `cross-toolchain-helper` hashes all of it into a `WEBKIT_CROSS_VERSION` that
is installed in the image at `/usr/share/cross-target-info-version`, so "is
this board running this system" is a string comparison and not a belief. That
hash is carried into the manifest as `cross_version=`.

(`webkitglib/2.48` is the release branch for both GLib ports. There is no
`wpe-2.48` in WebKit/WebKit — the `wpe-2.4x` branches the wiki page uses are in
WebPlatformForEmbedded/WPEWebKit, a different repository.)

**The build runs in a workspace and the host stays boring.** The distro
builder runs wholly on the workstation because it is two minutes of mtools and
debugfs. This is hours of compilation, a toolchain and ~100 GB of scratch, so
it drives a workspace (`yocto-rpi4-wpe-2.48`, created on demand) and imports
the result. `--stage` exists because the halves fail differently: `layers` is
the network-bound `repo sync`, `image` is the bitbake run, and mixing them
makes every egress failure look like a build failure.

**Detach needed a driver primitive, because the obvious spelling does not
work.** `setsid nohup … &` through `podman exec` is *not* detached: when the
exec client exits, podman tears the session down and takes the process with
it. Measured, not assumed — a detached `sleep 3; echo` started that way never
wrote its file. So the driver contract gained `t_spawn`: the generic
implementation is the `setsid nohup` that genuinely works over ssh, and
`targets/container.sh` overrides it with podman's own `exec -d`. That override
has to respell two things `wkdev-enter` would otherwise do — `--user` (plain
`podman exec` runs as **root**) and a login shell (or the SDK's `PATH` is
missing) — and without either the build fails for reasons that have nothing to
do with the build.

Output goes to `$HOME/yocto-<stage>.log`, and because a container's `$HOME`
*is* `$ws/home` on the host, following it is a plain `tail -f` with no podman
in the way (`t_home` is the other half of the primitive). The log is truncated
rather than unlinked on a restart, so an existing `tail -f` keeps working
instead of silently following a deleted inode. `yocto_wait` reports stalls and
deliberately does **not** kill on one: a bitbake task can legitimately be
silent for a long time, unlike a stalled compile, and killing one costs hours
that sstate cannot always give back. `--stop` is how a detached build is
ended.

**The cache requirement needed no new mechanism, only a fix.**
`targets/container.sh` already mounts `$WK_STORE/cache/yocto` at `/cache/yocto`
and exports `DL_DIR`/`SSTATE_DIR` into it, and `cmd/rm` already keeps it. What
was missing is that **bitbake filters its environment**: without naming them in
`BB_ENV_PASSTHROUGH_ADDITIONS` those two variables are dropped, the build
quietly uses `TOPDIR/downloads` instead, and the cache that is supposed to
survive `wk rm` never gets written. `image/yocto-build.sh` both exports the
passthrough *and* writes both paths into `local.conf`, because a hand-entered
`--bitbake-dev-shell` reads the environment and a bitbake run reads
`local.conf`, and the requirement has to hold for both.

### The host was too new — and the fix was the host, not the build

Yocto scarthgap is from 2024 and its supported build hosts stop at Ubuntu
24.04. The wkdev SDK image is **Ubuntu 26.04**: GCC 15, Python 3.14, glibc
2.43. Five distinct failures came out of that gap, each found by running the
build, in this order:

| # | what broke | why |
|---|---|---|
| 1 | four host packages missing | `makeinfo`, `socat`, `python3-git`, `python3-pexpect` — `makeinfo` is in bitbake's `HOSTTOOLS`, so mandatory |
| 2 | bitbake would not start | Python 3.14 defaults multiprocessing to `forkserver`, which pickles its target; bitbake's is a closure |
| 3 | `unzip-native`, then `m4-native`, would not compile | GCC 15 on 2009-era K&R declarations: `conflicting types for 'gmtime'` |
| 4 | `cross-toolchain-helper` would not import | the buildtools Python added to fix 3 has no `readline`, and webkitcorepy imports it |
| 5 | `gtk+3-native` died on a header mismatch | meson's CMake fallback found the SDK image's **host cups 2.4.16**, added `-I/usr/include`, and mixed glibc 2.43 headers into a build using buildtools' 2.39: `unknown type name '__time64_t'` |

Numbers 2–5 were each worked around in turn — a `sitecustomize.py` forcing
`fork`, the Yocto Project's prebuilt `buildtools-extended` tarball, a split
between "the host's Python for WebKit's scripts, buildtools' for bitbake".
Number 5 is what settled it, because it showed the shape of the remaining
work: **a WebKit SDK image is made of development headers, and buildtools
makes every one of those leaks fatal rather than harmless.** How many more
there are is not knowable in advance, and each would have been found by a
multi-hour build.

**So the base image changed instead** (decided with the user, 2026-08-19).
`container/yocto/Containerfile` is `FROM ubuntu:24.04` plus Yocto's own
required-host-packages list and the few things WebKit's driving scripts need,
tagged `localhost/wk-yocto-host:24.04`. All five failures are gone by
construction: GCC 13, Python 3.12 and glibc 2.39 are what scarthgap was
written against, so there is no buildtools tarball, no `sitecustomize`, no
interpreter split, and no foreign headers to leak. It also fixes something
quieter — with glibc 2.39, bitbake's **uninative is enabled**, so
`NATIVELSBSTRING` is `universal` and the sstate cache is portable instead of
being namespaced to one host release.

Getting a workspace to *exist* on a non-SDK image took three fixes, each found
by a container that exited on startup, and they are the price of this choice:

- **`.wkdev-init` refuses to run outside a wkdev-sdk container.** Its test is
  whether `/usr/bin/podman-host` exists. Our image writes `/etc/wk-container`
  and `sdk-patches/apply.sh` section 13 accepts that too — a stub
  `podman-host` would have been shorter and a lie, since that file is a
  working podman wrapper in the SDK image and something would eventually call
  it.
- **`utilities/podman.sh` then demanded `systemctl`.** Section 13 had made our
  container claim podman-host integration it does not have, and that file
  requires podman and systemctl unconditionally. Its own comment says what it
  really depends on — *"Requires the presence of /usr/bin/podman-host in the
  container image"* — so section 14 asks that narrower question in the place
  that means it. Host and real-SDK behaviour are unchanged, and it is
  upstreamable on its own.
- **Ubuntu base images ship an `ubuntu` user at uid 1000.** wkdev-create maps
  the host uid in unchanged, which is what makes the bind-mounted home and the
  overlay checkout writable, so `useradd --uid 1000` failed — quietly, because
  wk's own SDK patch ends that line with `|| true`. One `userdel -r ubuntu` in
  the Containerfile.

The cost is real and worth stating: the build workspace is no longer a WebKit
SDK. It carries what a Yocto build and `cross-toolchain-helper`/`build-webkit`
need and nothing else, so `wk new` on that image exists to build systems, not
to develop WebKit in.

Two things worth keeping from the detour. `clear_hosttools` survives: bitbake
gives tasks `tmp/hosttools` rather than `PATH` and only creates a *missing*
symlink, so a directory from an earlier run pins that run's toolchain — the
log reported the new compiler while the build used the old one, which is the
worst kind of wrong. And an `ERR` trap in the wrapper, because `set -euo
pipefail` exits with no message at all and this script runs detached, where
its log is the only channel there is.

### Chromium is out by default

The branch's own `local-rpi4-64bits-mesa.conf` adds it — *"Add chromium to
image to be able to compare WPE/Chromium performance"* — which is a real
reason on a fleet built for comparative benchmarking. It is also, by a wide
margin, the most expensive thing in the build: measured here,
`chromium-ozone-wayland` and `gn-native` were **21 GB of TMPDIR each**, with
`rust-native`, `cargo-native`, `rust-llvm-native` and `mozjs-115` behind them,
and roughly half of the 13,379 tasks. This profile exists to get a WPE runtime
onto the rpi4, so `YOC_CHROMIUM=0` and `--chromium` puts it back for the day
the comparison is the point.

### OPEN: is the pseudo patch needed at all? (read this before trusting it)

**Status: the patch works, and the reason given for it is refuted.** Resolve
this before building on it.

What is solid, by measurement rather than theory:

- `do_package` fails in this container with scarthgap's pinned pseudo 1.9.0
  (`got *at() syscall for unknown directory`, `tar: Cannot mkdir: Bad
  address`).
- Bumping to 1.9.11 in `image/yocto/meta-wk` fixes it. Verified by the
  three-line reproducer and by the exact recipe that failed, and the full
  13,130-task build then completed with 0 errors.
- It is not wk's doing: the reproducer is `pseudo bash -c 'tar -cf - . | tar
  -xf -'`, no bitbake, and it fails identically in a plain `podman run` with
  default seccomp and no sandbox. The overlay checkout, mixed sstate and the
  host's tar were each tested and refuted too.
- pseudo 1.9.0's wrapper list genuinely lacks `__open_2`/`__open64_2` while
  1.9.11 has them, and Ubuntu 24.04's `tar` genuinely references `__open_2`.
  Both by inspection.

**But the explanation those last two facts suggest cannot be right.** Ubuntu
24.04 with `wpe-2.46` is a known-good configuration that needs no patch — and
the Yocto spec is *byte-identical* between the branches: `wpe-2.46` and
`webkitglib/2.48` pin the same poky (`6879650b`), the same layers, and the
same `rpi/local-rpi4-64bits-mesa.conf`. So the same pseudo, built the same
way, from the same recipe. The variable is **not the branch**, and it cannot
be: the reproducer does not involve WebKit at all. It is pseudo plus that
container's `tar`. So one of these is true, and the next session should find
out which:

1. **The known-good 2.46 build ran in a different container** — most likely an
   older wkdev SDK that was 24.04-based, rather than the plain `ubuntu:24.04`
   this builder now uses. Then the patch is needed here for *any* branch, and
   the honest fix is to match whatever `tar` that container had rather than to
   carry a pseudo patch. **Cheapest check:** run the three-line reproducer in
   the known-good container. It answers this in seconds and needs no build.
2. **Something about this container differs in a way not yet looked at** — the
   `tar` build in particular. Compare `objdump -T $(command -v tar)` between
   the two containers; if the working one calls `open`/`open64` where this one
   calls `__open_2`, that is the whole difference and the patch is a
   workaround for a container choice, not for scarthgap.

**If either turns out that way, delete `image/yocto/meta-wk`.** It is one
bbappend in a layer whose only rule is build-time recipes, so removing it
changes no image content — and a local patch kept for a reason that has been
disproved is worse than no patch. What must not happen is the patch quietly
becoming load-bearing folklore because a build once succeeded with it in
place.

### scarthgap's pseudo did not work on this host (fixed)

`do_package` failed for every recipe that packages a directory tree:

```
got *at() syscall for unknown directory, fd 4
unknown base path for fd 4, path sbin
tar: ./usr/sbin: Cannot mkdir: Bad address
```

`package.bbclass` copies a tree with `tar -cf - | tar -xf -` under **pseudo**,
Yocto's `LD_PRELOAD` fakeroot, and pseudo cannot resolve the directory fd the
extracting tar hands to `mkdirat`. Reduced to three lines with no bitbake
involved at all —

```sh
pseudo /bin/bash -c '( cd $src && tar -cf - . ) | ( cd $dst && tar -xf - )'
```

— which fails, while the same pipeline without pseudo succeeds, and fails
identically in a plain `podman run` with none of wk's sandbox flags. Four
hypotheses were tested and refuted before that, each cheaply, and they are
listed because each is the obvious guess and each is wrong:

| hypothesis | how it was refuted |
|---|---|
| sstate mixed across the uninative boundary (bitbake's own warning says the two are not interchangeable) | reproduces on a completely clean cache — *Local 0 Missed 6369* |
| the *host's* `tar`, running under a pseudo built against uninative's libc | reproduces with `tar-native` and no host tar in `hosttools` at all |
| the checkout being an **overlayfs** mount, so `/proc/self/fd` would resolve outside pseudo's known root | `readlink /proc/self/fd/N` returns the overlay path correctly |
| wk's sandbox (seccomp, dropped caps, no network) | reproduces in a plain container with none of it |

**Fixed by bumping pseudo, in `image/yocto/meta-wk`.** pseudo is a
*build-time* tool and never appears in the image, so a newer one changes how
the image is built, not what it contains — unlike moving poky, which would
change the distribution itself. `image/yocto/meta-wk` is a wk-owned layer
whose `conf/layer.conf` writes that rule down: **build-time recipes only**. It
is registered by appending to the generated `bblayers.conf`, the same pattern
the wiki's custom-kernel flow uses with a local meta-webkit.

scarthgap pins `e11ae91` ("1.9.0+git", 2024); the bbappend takes `ba8887e`
(1.9.11). Two of the commits in between name the symptom exactly: `c63f439`
adds the `__open64_2` wrapper (a **fortified** glibc open variant pseudo did
not wrap — an fd opened through it is invisible to pseudo, which is precisely
"unknown directory, fd N"), and `b3958b0` avoids an EFAULT workaround — and
EFAULT is the "Bad address". Verified in minutes, not by a rebuild: pseudo's
own 197 tasks, the reproducer, and the exact recipe that had failed.

All three of scarthgap's local pseudo patches had to be dropped, which is
itself a sign of how far behind the pin is — the first two are upstream by
name. The third, `older-glibc-symbols.patch`, is not simply merged: it makes
pseudo-native link against older glibc symbol versions so native sstate can
travel between hosts, and it no longer applies. Dropping it is safe *here*
only because `SSTATE_DIR` is namespaced per build-host image, so native sstate
is never handed to another host — a dependency recorded in the bbappend, so
that removing the namespacing later trips over it.

### sstate is namespaced by the build-host image, and that is a correctness fix

bitbake said it outright on the 26.04 host: *"Disabling uninative so that
sstate is not corrupted."* A build with uninative off and a build with it on
do not produce interchangeable sstate — and **target sstate paths carry no
host marker**, so bitbake will happily hand one to the other and has no way to
refuse.

It did. The first compile on the 24.04 image reused **3007 packages** written
under the old host (*47% match*), and five recipes then failed in `do_package`
with pseudo unable to intercept `*at()` syscalls. Worth recording that this
reuse was reported as a *success* first — it was the long-outstanding "does
sstate read-reuse work" check finally passing — and only turned out to be the
cause of the next failure. A cache hit is not evidence that the cache should
have hit.

So `SSTATE_DIR` is `/cache/yocto/sstate/<the build-host image tag>`, which
makes the mixing impossible instead of documented. `DL_DIR` stays shared: a
source tarball is a source tarball whatever built it, and it is the 24 GB that
is actually expensive to refill. The old cache is kept as
`sstate/pre-namespacing-26.04/` rather than deleted — 1.8 GB, inspectable, and
`wk gc` reports the tree as kept so it stays visible.

### rm_work is on by default, and that is a real trade

`INHERIT += "rm_work"` is the difference between ~90 GB of `TMPDIR` and
~30 GB, on a workstation with ~200 GB free that also holds the base snapshots,
the ccache and every workspace. What it costs is each recipe's unpacked source
and build tree after that recipe is built — which is exactly what `bitbake -c
menuconfig virtual/kernel` and a devshell need. So the wiki's custom-kernel
flow (16 KB pages, 36-bit VA) wants `--keep-work`, and the profile knob is
`YOC_RM_WORK`. sstate is untouched either way, so rebuilds stay fast.

### The egress widening — for the sandbox audit

A Yocto build fetches sources, in principle from every upstream that every
recipe in six layers names. That is not a list anyone can write down and is
the wrong shape for an allowlist. So the build is pointed at the Yocto
Project's own source mirror first (`INHERIT += "own-mirrors"` with
`SOURCE_MIRROR_URL`), so the overwhelming majority of fetches resolve to
**one** host, and the allowlist addition is the remainder:

- `yoctoproject.org` — `git.` (poky, meta-raspberrypi) and `downloads.` (the
  mirror)
- `openembedded.org` — `git.` (named in `manifest.xml`) and `sources.` (a
  MIRROR)
- `googlesource.com` — the `repo` tool clones its own bootstrap from there
- `freedesktop.org` — `gitlab.` — polkit, and the wayland/mesa/libinput family
- `kernel.org` — `mirrors.` is one of poky's default PREMIRRORS
- `videolan.org` — `code.` — dav1d
- `metacpan.org` — `cpan.` — Archive-Zip

**Seven hostnames, and that number is measured rather than estimated.** A full
`--runall=fetch` pass over **1492 fetch tasks** produced exactly four refusals
beyond the mirror's own port-80 problem. `github.com` and
`githubusercontent.com` were already allowed and carry the other four layers,
the Pi firmware and kernel, and the `repo` launcher itself.

**The mirror covers less than it looks like it does.**
`downloads.yoctoproject.org/mirror/sources` carries oe-core's sources; it does
**not** carry meta-openembedded's, meta-raspberrypi's, meta-clang's or
meta-webkit's. Every recipe from those layers falls through to its upstream —
the first was `polkit`, refused at `gitlab.freedesktop.org` 2900 tasks in.
`--stage fetch` exists because of that: `bitbake --runall=fetch -k` fetches
every source and builds nothing, and `-k` is the load-bearing flag — without
it bitbake halts on the first unreachable host, so growing the allowlist from
evidence would cost one full run per host. One pass names all of them and the
proxy's `DENY` log is the list. With the seven hosts allowed it comes back
clean and leaves 24 GB in `DL_DIR`, so the compile after it needs no network
at all — worth running on its own before a long build for that reason alone.
(The Raspberry Pi kernel is the long pole: a `git clone --mirror` of
`raspberrypi/linux` is ~6 GB, and it is what makes a first fetch look stalled;
`yocto_wait` reporting rather than killing on silence is what lets it finish.)

**Port 80 is not optional, and getting it wrong inverts the design.** poky's
built-in `PREMIRRORS`/`MIRRORS` lists are written with `http://` URLs, so
allowing 443 only sends every fetch to its upstream instead — the exact
opposite of what pointing the build at a mirror was for. The four yocto hosts
allow 80 and 443.

**The fork, decided: keep the shared allowlist.** Seven source-code hosts is
in keeping with what is already there — the browsing block is broader — so the
proportionate answer is `ALLOWED_HOSTS`. Written down because the other branch
is the one to take if that list ever grows: a **second egress policy** on its
own socket, mounted only into workspaces that are building a system, because
`wk-proxy.py` serves one socket with one allowlist to every workspace, so
widening it for a Yocto build widens it for every `wk claude` session too.
That is real work — a second `systemd --user` service and a per-workspace
mount decision — and should be done rather than avoided if the list gets long.

Deliberately **not** `BB_FETCH_PREMIRRORONLY = "1"`: the mirror does not carry
the non-poky layers' fetches, so forbidding the upstream fallback outright
would fail meta-webkit, meta-clang and meta-raspberrypi. A recipe whose source
the mirror lacks *will* be refused by the proxy; the refusal is logged with
the hostname, so the list grows from evidence. `BLOCKED_NETS` is unchanged —
none of these names can become a route onto the LAN or the tailnet.

### What is done, and what is not

Done: the command, both builders behind one verb, the profile, the stages, the
detach model with `--stop`, the import into the store with a manifest (written
last, so an interrupted import is rubble the next one destroys), the cache
fix, the egress group, the preflight that reports missing Yocto host tooling
in the first second rather than after the layer sync — and, **2026-08-20, the
full build itself**: 13,130 tasks, 0 errors, imported as
`rpi4-wpe-2.48-20260820T124927Z` (disk image, `rootfs.tar.xz`, wic + bmap,
`cross_version` in the manifest). The import half had been verified
independently first, against a hand-made 8 MB image — which found that
`t_exec <ws> cat <file>` silently corrupts binary (1396 bytes out as 1399),
because `wkdev-enter` is an interactive-shell wrapper and not a byte pipe;
hence `t_pull` in the driver contract, alongside `t_spawn`.

Two 2026-08-20 fixes that started here and landed in the locking layer,
recorded because their reasons outlive them:

- **`hold_lock` could not reclaim a lock directory with no pid file** — the
  next taker could not tell it from a live holder, and `wk rm` sat out its
  full timeout on one. Taken to its conclusion, the lock became a **symlink**
  whose target string names the holder: `ln -s` is atomic *and* carries the
  payload, so the window does not exist rather than being made small — and the
  descriptor-inheritance problem that ruled `flock` out cannot come back,
  because there is no descriptor. See `lib/common.sh`, "locks", and the lock
  lines in `docs/TESTING.md` §6.
- **A detached build held no lock** — this process exits and the lock goes
  with it, so a concurrent `wk build` in the same workspace was not refused,
  and two builds in one checkout corrupt both. Closed the other way round,
  because a lock on this machine cannot be held by a process that is not on
  it: the workspace is asked for **evidence** instead. `ws_busy_reason`
  (`lib/target.sh`) reads the pid a detached job leaves in
  `$(t_home)/<job>.pid` and tests it *inside* the workspace, the only
  namespace the number means anything in; `wk build` treats it as a barrier
  (`--force` for a reused pid). And `yocto_spawn` refuses when **any** stage
  is live, not just the one asked for — found the hard way, by a
  `--stage fetch` starting on top of a running `--stage image` and getting two
  bitbake cookers into one build directory.

Not done, in the order it matters:

1. **Booted on the board, 2026-08-21.** The image runs on the rpi4's USB
   stick, in bench mode, and hands itself back: `wk boot rpi4 --status` reads
   it from the board's own evidence, and the self-disarm has already flipped
   the stick so the next reboot lands on the SD rescue. What follows in this
   item is how the blocker that stood here was removed. As built, the wic image baked
   `root=/dev/mmcblk0p2` — an SD-card root — so `wk sysimage write … --disk
   rpi4:/dev/sda` was refused by `image_check_root`, correctly: written to the
   stick, the firmware would load the kernel and the kernel would find no root.
   The fix was not on the writing side. `wk sysimage retarget <id>` rewrites the
   two places that name a device by path — `root=` on the kernel command line
   and `/boot` in `/etc/fstab` — to `PARTUUID=`, which the kernel resolves with
   no initramfs, so one image boots from the card or the stick and the check has
   nothing left to catch. The import now does it on the way in, so a rebuild
   needs no second step; `retarget` exists because a Yocto build is hours and
   this is seconds. It also installs the driving ssh key and, if absent, the
   identity marker — see `docs/TESTING.md`.

   **One trap found on the way, on hardware.** The rpi4's SD rescue was written
   from the same Yocto wic as the stick, so both disks carried MBR signature
   `0x076c4a2a` — and `PARTUUID=` is that signature plus a partition number.
   The board loaded the stick's kernel and mounted the *card's* root
   filesystem: a system that was neither of the two, reporting the right
   distribution, answering ssh, and looking for all the world like a
   successful boot with the fleet integration mysteriously absent. Identity is
   now stamped per disk at write time (`disk_unique_identity`, boot/disk.sh);
   `docs/TESTING.md` has the whole reading.

   What is left is the boot itself: `wk sysimage write
   rpi4-wpe-2.48-20260820T124927Z --disk rpi4:/dev/sda` (an erase of the stick,
   so it asks), then `wk boot rpi4 --system rpi4-wpe-2.48-20260820T124927Z` —
   named explicitly, because `wk boot rpi4` with no `--system` picks the newest
   system for the machine's default profile (`MACH_PROFILE=perf-linux-rpi4`).

2. **The rootfs tarball has no consumer.** The import pulls `rootfs.tar.xz`
   into the store alongside `disk.img` — a tarball is the honest archival form
   of a rootfs and it costs ~600 MB to keep — but nothing reads it today.
3. **The image is the runtime, and WebKit is sent to it separately — by
   design, not as a gap.** Confirmed against the wiki 2026-08-21 (`WebKit JSC
   Container Development Setup`, and `Building WPEWebKit for 32-bit Raspberry
   Pi 3 (Yocto Wayland)` for the device half). `webkit-dev-ci-tools` says what
   it is in its own name: the image carries weston, cmake, perl, gdb and
   **gdbserver** and no browser at all — no `cog`, no `run-minibrowser`, no
   `/WebKit` (checked in the built image). The browser is cross-built on the
   workstation and deployed on every cycle, which is what makes the cycle
   minutes instead of hours.

   The loop the wiki does by hand, and `docs/HANDOFF-pi-deploy.md` is where it
   becomes `wk pi deploy`:

       # in the workspace -- the env scrub is marked VERY IMPORTANT there,
       # because a poisoned env produces a broken archive with no error
       unset CC CXX LD_LIBRARY_PATH
       Tools/Scripts/build-webkit --wpe --release \
           --cross-target=rpi4-64bits-mesa \
           --cmakeargs="-DENABLE_MINIBROWSER=ON -DDEVELOPER_MODE=ON"
       Tools/CISupport/built-product-archive --platform=wpe --release \
           --cross-target=rpi4-64bits-mesa archive
       scp WebKitBuild/release.zip root@<device>:/WebKit/WebKit/WebKitBuild/

       # on the device
       cd /WebKit/WebKit
       Tools/CISupport/built-product-archive --platform=wpe --release extract
       systemctl start weston
       source <(strings /proc/$(pidof weston-desktop-shell)/environ \
           | grep -P '(XDG_RUNTIME_DIR|WAYLAND_DISPLAY)') \
           && export XDG_RUNTIME_DIR WAYLAND_DISPLAY
       Tools/Scripts/run-minibrowser --wpe -P wl \
           "https://browserbench.org/Speedometer3.1/?startAutomatically=true"

   **The requirement that follows from this, and that nothing here provisions
   yet: the device needs a WebKit checkout at `/WebKit/WebKit`.** Both
   `built-product-archive extract` and `run-minibrowser` are WebKit's own
   scripts, run *on the board*, from that tree — so "deploy a build" is not
   only a file transfer, and the image does not contain the tree. That belongs
   with `wk pi setup`/`wk pi deploy`, not with the image.

   So `--stage toolchain` and `--stage webkit` are the build half of this loop
   (`populate_sdk`, then `build-webkit --cross-target=`), not repairs to the
   image. Neither has run since the base image changed.

4. **"Does it build unmodified?" -- asked and answered, 2026-08-21.** The
   known-good pairing is Ubuntu 24.04 plus **`wpe-2.46` from the downstream
   `WebPlatformForEmbedded/WPEWebKit` repo** (the `wpe` remote, lib/store.sh) --
   a different repository from the `webkitglib/2.48` this handoff was written
   against, and the branch the `rpi3` skill already clones. It has its own
   profile now, `downstream-wpe-2.46-rpi4`, and reaching a branch outside
   `origin` needed `YOC_REMOTE` before the profile could exist at all.

   Built with **no local layer** it fails, and it fails in the one place that
   matters: `update-rc.d` and `base-files` die in `do_package` with

       got *at() syscall for unknown directory, fd 4
       unknown base path for fd 4, path sbin
       tar: ./usr/sbin: Cannot mkdir: Bad address

   byte for byte the signature in `image/yocto/meta-wk/recipes-devtools/pseudo/
   pseudo_%.bbappend`. Re-run with the layer and the same two recipes succeed,
   zero errors. So **the pseudo bug is a property of pseudo plus this host's
   kernel (7.0.11 aarch64), not of the release branch**: 2.46 pins poky
   6879650b, whose pseudo is the same 1.9.0-era fakeroot, and it fails
   identically. A branch cannot be known-good against a kernel that postdates
   its fakeroot.

   Which resolves the question this was asked to settle: the **image** carries
   no local changes -- pseudo is a build-time fakeroot and is never installed
   into it -- while the **build** needs the bump on this machine.
   `--no-local-layer` re-runs the experiment anywhere it might pass, and that
   is the flag to try first on a host with an older kernel.

5. **`zip` belongs in `container/yocto/Containerfile`, and is not there yet.**
   `built-product-archive archive` shells out to `zip`; the image has `unzip`
   only, so the documented deploy path cannot run and `wk pi deploy` falls back
   to a plain tar of `bin/` and `lib/` (cmd/pi, `pi_deploy_tar`). It cannot be
   installed at runtime -- `ports.ubuntu.com` is not in the egress allowlist,
   and widening that to fetch a zip is the wrong trade.
   Adding the package is a one-line change, and the reason it is deferred is
   the workspace guard: editing the Containerfile changes `WK_SDK_IMAGE`'s tag,
   and an existing workspace is pinned to the image it was made from, so the
   edit forces `wk rm` -- which would discard a Yocto **toolchain** that took
   hours (`populate_sdk` lives in the workspace, unlike sstate and DL_DIR,
   which are in the store and survive). So: add `zip` the next time this
   workspace is recreated for another reason, not before.

6. **Tailscale on the rpi target** — untouched. A Yocto image has no apt, so
   this is a recipe (or the static-binary route `SETUP.md` already describes
   for the rpi4's buildroot image), not a package install.
7. **The macOS half.** The builder refuses any target that is not a container,
   and the reasons are in the refusal: a remote target is a shared machine and
   this is 100 GB and days of CPU, and a macOS VM workspace has no
   store-backed Yocto cache at all (`targets/vm.sh` says so). The podman VM on
   macOS *is* a container target, so it should work there — with the caveat
   that the VM's disk has to be big enough, which is a `wk vm` question.
