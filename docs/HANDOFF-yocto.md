On podman macos + linux targets, support building with yocto https://github.com/justinmichaud/justinmichaud.github.io/wiki/Building-WPEWebKit-for-32%E2%80%90bit-Raspberry-Pi-3-(Yocto-Wayland)

Yocto cache should be preserved even if target is destroyed

Also should support uploading new image to target.

Flashing the built image onto a physical SD card from the host is tracked
generically in `docs/HANDOFF-sdcard.md` (not yocto-specific) — consume that
rather than building a separate copy-to-host path here.

Tailscale shoud be able to be installed on the rpi target

**Target and test loop, decided 2026-08-19:** the target is the **rpi4**, and
each image gets tested over **netboot** (the Pi 4 bootloader has the same
network boot mode) rather than by writing a card — `docs/HANDOFF-netboot.md`,
which lands first and serves the TFTP root from moose. Flashing
(`docs/HANDOFF-sdcard.md`) then applies to the image that is kept, not to every
image that is tried. `wk pi setup rpi4` against the resulting image is the step
after this one (`docs/HANDOFF-linux-pi.md`).

---

## State as of 2026-08-19 — the command exists

`wk image build rpi4-wpe-2.48` builds the WPE WebKit 2.48 Yocto image for the
rpi4. It is a **builder** of the existing `wk image`, not a command of its own,
and that is the load-bearing design decision here — see the header of
`image/yocto.sh` for the argument in full. In short: what comes out is a
partitioned disk image for one machine, which is exactly what the store, the
manifest, `wk image ls/show/flash` and the SD-card path already handle. A
second command would have meant a second copy of all of it, which this handoff
explicitly asks not to build.

```
wk image build rpi4-wpe-2.48 [--dry-run] [--detach] [--stage <s>]
                             [--workspace <name>] [--keep-work] [--no-import]
```

| file | what it is |
|---|---|
| `image/profiles.sh` | the profile. `IMG_BUILDER=yocto` is what dispatches; `YOC_BRANCH`/`YOC_TARGET`/`YOC_IMAGE`/`YOC_RM_WORK` are its own fields |
| `image/yocto.sh` | the host half: workspace, stages, detach, wait, import, manifest |
| `image/yocto-build.sh` | the workspace half: the SDK-environment unset, the preflight, the `local.conf` additions, one call to `cross-toolchain-helper` per stage |
| `cmd/image` | three added lines of dispatch; the distro path is untouched |
| `container/yocto/Containerfile` | the workspace image: `ubuntu:24.04` plus Yocto's host tooling — a *supported* build host |
| `container/proxy/wk-proxy.py` | three source hosts, with the audit note |
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
this board running this image" is a string comparison and not a belief. That
hash is carried into the manifest as `cross_version=`.

(`webkitglib/2.48` is the release branch for both GLib ports. There is no
`wpe-2.48` in WebKit/WebKit — the `wpe-2.4x` branches the wiki page uses are in
WebPlatformForEmbedded/WPEWebKit, a different repository.)

**The build runs in a workspace and the host stays boring.** The distro builder
runs wholly on the workstation because it is two minutes of mtools and
debugfs. This is hours of compilation, a toolchain and ~100 GB of scratch, so
it drives a workspace (`yocto-rpi4-wpe-2.48`, created on demand) and imports
the result. `--stage` exists because the halves fail differently: `layers` is
the network-bound `repo sync`, `image` is the bitbake run, and mixing them
makes every egress failure look like a build failure.

**Detach needed a driver primitive, because the obvious spelling does not
work.** `setsid nohup … &` through `podman exec` is *not* detached: when the
exec client exits, podman tears the session down and takes the process with it.
Measured, not assumed — a detached `sleep 3; echo` started that way never wrote
its file, and the first run of `--stage layers` failed with an empty log for
exactly that reason. So the driver contract gained `t_spawn`: the generic
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
that sstate cannot always give back.

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

Yocto scarthgap is from 2024 and its supported build hosts stop at Ubuntu 24.04.
The wkdev SDK image is **Ubuntu 26.04**: GCC 15, Python 3.14, glibc 2.43. Five
distinct failures came out of that gap, each found by running the build, in this
order:

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
Number 5 is what settled it, because it showed the shape of the remaining work:
**a WebKit SDK image is made of development headers, and buildtools makes every
one of those leaks fatal rather than harmless.** How many more there are is not
knowable in advance, and each would have been found by a multi-hour build.

**So the base image changed instead** (decided with the user, 2026-08-19).
`container/yocto/Containerfile` is now `FROM ubuntu:24.04` plus Yocto's own
required-host-packages list and the few things WebKit's driving scripts need,
tagged `localhost/wk-yocto-host:24.04`. All five failures are gone by
construction: GCC 13, Python 3.12 and glibc 2.39 are what scarthgap was written
against, so there is no buildtools tarball, no `sitecustomize`, no interpreter
split, no stale-`hosttools` trap worth forty lines, and no foreign headers to
leak. It also fixes something quieter — with glibc 2.39, bitbake's **uninative
is enabled**, so `NATIVELSBSTRING` is `universal` and the sstate cache is
portable instead of being namespaced to one host release.

Getting a workspace to *exist* on a non-SDK image took three fixes, each found
by a container that exited on startup, and they are worth naming because they
are the price of this choice:

- **`.wkdev-init` refuses to run outside a wkdev-sdk container.** Its test is
  whether `/usr/bin/podman-host` exists. Our image writes `/etc/wk-container`
  and `sdk-patches/apply.sh` section 13 accepts that too — a stub `podman-host`
  would have been shorter and a lie, since that file is a working podman wrapper
  in the SDK image and something would eventually call it.
- **`utilities/podman.sh` then demanded `systemctl`.** Section 13 had made our
  container claim podman-host integration it does not have, and that file
  requires podman and systemctl unconditionally. Its own comment says what it
  really depends on — *"Requires the presence of /usr/bin/podman-host in the
  container image"* — so section 14 asks that narrower question in the place
  that means it. Host and real-SDK behaviour are unchanged; it makes the code
  agree with its comment, and is upstreamable on its own.
- **Ubuntu base images ship an `ubuntu` user at uid 1000.** wkdev-create maps
  the host uid in unchanged, which is what makes the bind-mounted home and the
  overlay checkout writable, so `useradd --uid 1000` failed — quietly, because
  wk's own SDK patch ends that line with `|| true`. The visible symptom was the
  confusing half: "Creating user jmichaud (1000:1000)" followed by "usermod:
  user 'jmichaud' does not exist". One `userdel -r ubuntu` in the Containerfile.

The cost is real and worth stating: the build workspace is no longer a WebKit
SDK. It carries what a Yocto build and `cross-toolchain-helper`/`build-webkit`
need and nothing else, so `wk new` on that image exists to build images, not to
develop WebKit in.

Two things worth keeping from the detour. `clear_hosttools` survives: bitbake
gives tasks `tmp/hosttools` rather than `PATH` and only creates a *missing*
symlink, so a directory from an earlier run pins that run's toolchain — the log
reported the new compiler while the build used the old one, which is the worst
kind of wrong. And an `ERR` trap in the wrapper, because `set -euo pipefail`
exits with no message at all and this script runs detached, where its log is the
only channel there is.

### Chromium is out by default

The branch's own `local-rpi4-64bits-mesa.conf` adds it — *"Add chromium to image
to be able to compare WPE/Chromium performance"* — which is a real reason on a
fleet built for comparative benchmarking. It is also, by a wide margin, the most
expensive thing in the build: measured here, `chromium-ozone-wayland` and
`gn-native` were **21 GB of TMPDIR each**, with `rust-native`, `cargo-native`,
`rust-llvm-native` and `mozjs-115` behind them, and roughly half of the 13,379
tasks. This profile exists to get a WPE runtime onto the rpi4, so `YOC_CHROMIUM=0`
and `--chromium` puts it back for the day the comparison is the point.

### OPEN: is the pseudo patch needed at all? (read this before trusting it)

**Status: the patch works, and the reason given for it is refuted.** Resolve
this before building on it.

What is solid, by measurement rather than theory:

- `do_package` fails in this container with scarthgap's pinned pseudo 1.9.0
  (`got *at() syscall for unknown directory`, `tar: Cannot mkdir: Bad address`).
- Bumping to 1.9.11 in `image/yocto/meta-wk` fixes it. Verified by the
  three-line reproducer and by the exact recipe that failed, and the full 13130
  task build then completed with 0 errors.
- It is not wk's doing: the reproducer is `pseudo bash -c 'tar -cf - . | tar -xf
  -'`, no bitbake, and it fails identically in a plain `podman run` with default
  seccomp and no sandbox. The overlay checkout, mixed sstate and the host's tar
  were each tested and refuted too.
- pseudo 1.9.0's wrapper list genuinely lacks `__open_2`/`__open64_2` while
  1.9.11 has them, and Ubuntu 24.04's `tar` genuinely references `__open_2`.
  Both by inspection.

**But the explanation those last two facts suggest cannot be right.** Ubuntu
24.04 with `wpe-2.46` is a known-good configuration that needs no patch — and
the Yocto spec is *byte-identical* between the branches: `wpe-2.46` and
`webkitglib/2.48` pin the same poky (`6879650b`), the same layers, and the same
`rpi/local-rpi4-64bits-mesa.conf`. `diff` on both files is empty. So the same
pseudo, built the same way, from the same recipe.

Which means the variable is **not the branch**, and it cannot be: the reproducer
does not involve WebKit at all. It is pseudo plus that container's `tar`. So one
of these is true, and the next session should find out which:

1. **The known-good 2.46 build ran in a different container** — most likely an
   older wkdev SDK that was 24.04-based, rather than the plain `ubuntu:24.04`
   this builder now uses. Then the patch is needed here for *any* branch, and
   the honest fix is to match whatever `tar` that container had rather than to
   carry a pseudo patch. **Cheapest check:** run the three-line reproducer in
   the known-good container. It answers this in seconds and needs no build.
2. **Something about this container differs in a way not yet looked at** — the
   `tar` build in particular. Compare `objdump -T $(command -v tar)` between the
   two containers; if the working one calls `open`/`open64` where this one calls
   `__open_2`, that is the whole difference and the patch is a workaround for a
   container choice, not for scarthgap.

**If either turns out that way, delete `image/yocto/meta-wk`.** It is one
bbappend in a layer whose only rule is build-time recipes, so removing it
changes no image content — and a local patch kept for a reason that has been
disproved is worse than no patch. What must not happen is the patch quietly
becoming load-bearing folklore because a build once succeeded with it in place.

### scarthgap's pseudo did not work on this host (fixed)

`do_package` fails for every recipe that packages a directory tree:

```
got *at() syscall for unknown directory, fd 4
unknown base path for fd 4, path sbin
tar: ./usr/sbin: Cannot mkdir: Bad address
```

`package.bbclass` copies a tree with `tar -cf - | tar -xf -` under **pseudo**,
Yocto's `LD_PRELOAD` fakeroot, and pseudo cannot resolve the directory fd the
extracting tar hands to `mkdirat`.

**It is not this repo's doing, and that is established rather than assumed.**
Reduced to three lines with no bitbake involved at all —

```sh
pseudo /bin/bash -c '( cd $src && tar -cf - . ) | ( cd $dst && tar -xf - )'
```

— which fails, while the same pipeline without pseudo succeeds. Then the same
reproducer was run in a **plain `podman run`**: default seccomp, default
capabilities, network up, none of wk's sandbox flags. It fails identically. So
the workspace, the overlay, `--network none`, `--isolated` and the egress proxy
are all exonerated.

Four hypotheses were tested and refuted before that, each cheaply, and they are
listed because each is the obvious guess and each is wrong:

| hypothesis | how it was refuted |
|---|---|
| sstate mixed across the uninative boundary (bitbake's own warning says the two are not interchangeable) | reproduces on a completely clean cache — *Local 0 Missed 6369* |
| the *host's* `tar`, running under a pseudo built against uninative's libc | reproduces with `tar-native` and no host tar in `hosttools` at all |
| the checkout being an **overlayfs** mount, so `/proc/self/fd` would resolve outside pseudo's known root | `readlink /proc/self/fd/N` returns the overlay path correctly |
| wk's sandbox (seccomp, dropped caps, no network) | reproduces in a plain container with none of it |

What is left is pseudo 1.9.0 (scarthgap, 2024) against this host: **kernel
7.0.11**, aarch64, this podman. The wiki's Yocto builds ran on this same machine
in the wkdev SDK container, so this worked once — under an older kernel.

**Fixed by bumping pseudo, in `image/yocto/meta-wk`.** pseudo is a *build-time*
tool and never appears in the image, so a newer one changes how the image is
built, not what it contains — unlike moving poky, which would change the
distribution itself. `image/yocto/meta-wk` is a wk-owned layer whose
`conf/layer.conf` writes that rule down: **build-time recipes only**. It is
registered by appending to the generated `bblayers.conf`, the same way the
`local.conf` additions work and the same pattern the wiki's custom-kernel flow
uses with a local meta-webkit.

scarthgap pins `e11ae91` ("1.9.0+git", 2024); the bbappend takes `ba8887e`
(1.9.11). Two of the commits in between name the symptom exactly:

- `c63f439 ports/linux/guts: Add __open64_2 wrapper` — a **fortified** glibc
  open variant pseudo did not wrap. An fd opened through it is invisible to
  pseudo, which is precisely "unknown directory, fd N".
- `b3958b0 makewrappers: Avoid efault workaround if using AT_EMPTY_PATH` — and
  EFAULT is the "Bad address".

**Verified in minutes, not by a rebuild:**

```
pseudo-native:  Attempted 197 tasks ... all succeeded
REPRODUCER PASSES
recipe update-rc.d-0.8+git-r0: task do_package: Succeeded
```

All three of scarthgap's local pseudo patches had to be dropped, which is itself
a sign of how far behind the pin is — the first two are upstream by name
(`6831273` PIE flags, `865ca5b` glibc 2.38 wrappers). The third,
`older-glibc-symbols.patch`, is not simply merged: it makes pseudo-native link
against older glibc symbol versions so native sstate can travel between hosts,
and it no longer applies (upstream keeps it as an unapplied reference,
`137d7be`). Dropping it is safe *here* only because `SSTATE_DIR` is namespaced
per build-host image, so native sstate is never handed to another host — a
dependency recorded in the bbappend, so that removing the namespacing later
trips over it.

### sstate is namespaced by the build-host image, and that is a correctness fix

bitbake said it outright on the 26.04 host: *"Disabling uninative so that sstate
is not corrupted."* A build with uninative off and a build with it on do not
produce interchangeable sstate — and **target sstate paths carry no host
marker**, so bitbake will happily hand one to the other and has no way to
refuse.

It did. The first compile on the 24.04 image reused **3007 packages** written
under the old host (*Sstate summary: Wanted 6369 Local 3007 Missed 3362, 47%
match*), and five recipes then failed in `do_package` with `pseudo` unable to
intercept `*at()` syscalls:

```
got *at() syscall for unknown directory, fd 4
couldn't allocate absolute path for 'local'.
tar: ./usr/local: Cannot mkdir: Bad address
```

Worth recording that this reuse was reported as a *success* first — it was the
long-outstanding "does sstate read-reuse work" check finally passing — and only
turned out to be the cause of the next failure. A cache hit is not evidence that
the cache should have hit.

So `SSTATE_DIR` becomes `/cache/yocto/sstate/<the build-host image tag>`, which
makes the mixing impossible instead of documented. `DL_DIR` stays shared: a
source tarball is a source tarball whatever built it, and it is the 24 GB that
is actually expensive to refill. The old cache is kept as
`sstate/pre-namespacing-26.04/` rather than deleted — 1.8 GB, inspectable, and
`wk gc` reports the tree as kept so it stays visible.

### rm_work is on by default, and that is a real trade

`INHERIT += "rm_work"` is the difference between ~90 GB of `TMPDIR` and ~30 GB,
on a workstation with ~200 GB free that also holds the base snapshots, the
ccache and every workspace. What it costs is each recipe's unpacked source and
build tree after that recipe is built — which is exactly what `bitbake -c
menuconfig virtual/kernel` and a devshell need. So the wiki's custom-kernel
flow (16 KB pages, 36-bit VA) wants `--keep-work`, and the profile knob is
`YOC_RM_WORK`. sstate is untouched either way, so rebuilds stay fast.

### The egress widening — for the sandbox audit

A Yocto build fetches sources, and in principle from every upstream that every
recipe in six layers names. That is not a list anyone can write down and is the
wrong shape for an allowlist. So the build is pointed at the Yocto Project's
own source mirror first (`INHERIT += "own-mirrors"` with `SOURCE_MIRROR_URL`),
which carries every source of every release branch — so the overwhelming
majority of fetches resolve to **one** host, and the allowlist addition is the
remainder:

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
beyond the mirror's own port-80 problem. Pointing the build at the mirror is
what makes the list this short.

`github.com` and `githubusercontent.com` were already allowed and carry the
other four layers, the Pi firmware and kernel, and the `repo` launcher itself.

**The mirror covers less than it looks like it does**, and this is the open
design question of the whole step. `downloads.yoctoproject.org/mirror/sources`
carries oe-core's sources; it does **not** carry meta-openembedded's,
meta-raspberrypi's, meta-clang's or meta-webkit's. So every recipe from those
four layers falls through to its upstream, and the first one to do so was
`polkit`, refused at `gitlab.freedesktop.org` 2900 tasks in.

`--stage fetch` exists because of that: `bitbake --runall=fetch -k` fetches
every source and builds nothing, and `-k` is the load-bearing flag — without it
bitbake halts on the first unreachable host, so growing the allowlist from
evidence would cost one full run per host. One pass names all of them and the
proxy's `DENY` log is the list. With the seven hosts allowed it comes back
clean — *Attempted 1492 tasks … all succeeded* — and leaves 24 GB in `DL_DIR`,
so the compile after it needs no network at all. Worth running on its own before
a long build for that reason alone.

The Raspberry Pi kernel is the long pole in that pass by a wide margin: a
`git clone --mirror` of `raspberrypi/linux` is ~6 GB, and it is what makes a
first fetch look stalled. `yocto_wait` reporting rather than killing on silence
is what lets it finish.

**Port 80 is not optional, and getting it wrong inverts the design.** The first
`--stage fetch` pass produced only four refusals, and two of them were
`downloads.yoctoproject.org:80` and `sources.openembedded.org:80` — the mirror
itself. poky's built-in `PREMIRRORS`/`MIRRORS` lists are written with `http://`
URLs, so allowing 443 only sends every fetch to its upstream instead, which is
the exact opposite of what pointing the build at a mirror was for. The four
yocto hosts allow 80 and 443.

**The fork, decided: keep the shared allowlist.** Seven source-code hosts is in
keeping with what is already there — the browsing block is broader — so the
proportionate answer is `ALLOWED_HOSTS`. Written down because the other branch
is the one to take if that list ever grows: a **second egress policy** rather
than a bigger shared one, because `wk-proxy.py` serves one socket with one
allowlist to every workspace, so widening it for a Yocto build widens it for
every `wk claude` session too. A build-sources policy on its own socket,
mounted only into workspaces that are building an image, keeps the ordinary
workspace exactly as tight as it is today. That is real work — a second
`systemd --user` service and a per-workspace mount decision — and it should be
done rather than avoided if the list is long.

Deliberately **not** `BB_FETCH_PREMIRRORONLY = "1"`: the mirror does not carry
the non-poky layers' git fetches, so forbidding the upstream fallback outright
would fail meta-webkit, meta-clang and meta-raspberrypi. That means a recipe
whose source the mirror lacks *will* be refused by the proxy. The refusal is
logged with the hostname, so the list grows from evidence — add the name with a
reason rather than pre-emptively allowing a hundred hosts.

This is a widening of the same kind as, and smaller than, the browsing block
already in `wk-proxy.py`. It adds source-code hosts only, it is still by
hostname, and `BLOCKED_NETS` is unchanged — none of these names can become a
route onto the LAN or the tailnet.

### What is done, and what is not

Done: the command, both builders behind one verb, the profile, the stages, the
detach model, the import into the store with a manifest (written last, so an
interrupted import is rubble the next one destroys), the cache fix, the egress
group, and the preflight that reports missing Yocto host tooling in the first
second rather than after the layer sync.

Not done, in the order it matters:

0. **The compile has not been run to completion.** The *import* half is
   verified independently — against a hand-made 8 MB image in a throwaway
   cross-target directory, so a bug there did not have to wait six hours to
   surface. That found one: `t_exec <ws> cat <file>` **silently corrupts
   binary** (1396 bytes out as 1399; `xz` said "Compressed data is corrupt"),
   because `wkdev-enter` is an interactive-shell wrapper and not a byte pipe.
   Hence `t_pull` in the driver contract, alongside `t_spawn`.

0b. **The compile has not been run to completion on the new base.** On the old
   one it reached 8347 of 13,379 tasks before `gtk+3-native` hit the header
   mismatch that changed the base image. Everything either side of the compile
   is verified: `--stage layers` end to end, `--stage fetch` clean over 1492
   tasks with 24 GB in `DL_DIR`, bitbake parsing 3105 recipes and 5137 targets
   with 0 errors, and the import tested on its own. The compile itself is the
   one claim not yet evidenced.

1. **Netboot cannot serve this image yet, and `wk serve` is right to refuse
   it.** `check_root_is_reachable` in `cmd/serve` refuses any image whose
   `cmdline.txt` names a local root, because a netboot client would fetch the
   kernel and then have nowhere to mount `/` from — and loop, headless. A wic
   image names a local root. So the test loop this handoff decided on (netboot
   every image, flash only the one you keep) is still blocked on the network
   root, not on the image. The import already pulls `rootfs.tar.xz` into the
   store alongside `disk.img` for exactly that reason: a tarball is what fills
   an NFS root, and the day that lands the images already in the store are
   complete. `webkit-dev-ci-tools` also does its own boot-file handling "for
   NFS boot capability", which is worth reading before building it.
2. **`wk image flash rpi4 --image <id>` is untested against hardware** — the
   board is powered off (`docs/HANDOFF-netboot.md`). Note that `flash` with no
   `--image` picks `image_latest rpi4-perf`, the machine's *default* profile,
   so a yocto image must be named explicitly. That is the right default and
   worth keeping.
3. **The image carries no WebKit.** meta-webkit's `webkit-dev-ci-tools` is the
   runtime and the test tooling — it says so in its own recipe. The matching
   WebKit is a cross build against the image's toolchain:
   `wk image build rpi4-wpe-2.48 --stage webkit`, which runs `populate_sdk`
   first (hours) and then `build-webkit --wpe --release --cross-target=...`.
   What is *not* built yet is the step after it:
   `Tools/CISupport/built-product-archive
   --platform=wpe --release --cross-target=... archive` and getting the zip
   onto the board. That is "uploading new image to target" in this handoff's
   words, and it belongs next to `wk pi` rather than here.
4. **Tailscale on the rpi target** — untouched. A Yocto image has no apt, so
   this is a recipe (or the static binary route `SETUP.md` already describes
   for the rpi4's buildroot image), not a package install.
5. **The macOS half.** The builder refuses any target that is not a container,
   and the reasons are in the refusal: a remote target is a shared machine and
   this is 100 GB and days of CPU, and a macOS VM workspace has no
   store-backed Yocto cache at all (`targets/vm.sh` says so). The podman VM on
   macOS *is* a container target, so it should work there — with the caveat
   that the VM's disk has to be big enough, which is a `wk vm` question.
6. **The build workspace is not a WebKit SDK any more**, and `--stage webkit`
   has not been run since the base changed. It needs `cross-toolchain-helper`
   and `build-webkit` to work on the 24.04 image — perl, cmake, ninja and
   `python3-requests` are installed for exactly that, but a cross build of
   WebKit is the test and it has not been done. If it turns out to want more of
   the SDK than that, the honest options are to add what it names to
   `container/yocto/Containerfile` or to split the stages across two
   workspaces: bitbake on the supported host, `build-webkit` in a wkdev one.
7. **FIXED 2026-08-20. `hold_lock` cannot reclaim a lock directory with no
   `pid` file** — found
   here, and it is in `lib/common.sh`, which the other lane owns this week, so
   it is reported rather than edited. The atomic-mkdir lock records the holder's
   pid *inside* the directory it just created, and the next taker reclaims the
   lock only when it can read that pid and find the process gone. A directory
   with no pid file is therefore indistinguishable from a live holder, and
   `wk rm` sat waiting out its full timeout on one. Two candidate fixes: treat a
   pid-less lock older than a few seconds as stale, or create the directory with
   the pid already in it (`mkdir` a temp dir, write the pid, then `mv` it into
   place — the rename is the atomic step).

   The second one, taken to its conclusion: the lock is a **symlink** whose
   target string names the holder. `ln -s` is atomic *and* carries the payload
   with it, so the window does not exist rather than being made small — and the
   descriptor-inheritance problem that ruled `flock` out in the first place
   cannot come back, because there is no descriptor. Two further defects fell
   out of writing the contention test for it, both of which the mkdir form had
   as well: several takers breaking one dead lock could all conclude they had
   won (the break is a compare-and-swap under a breaker lock now), and
   re-entrancy compared `$$`, which every subshell of one command shares. See
   lib/common.sh, "locks", and the lock lines in docs/TESTING.md §6.

8. **FIXED 2026-08-20. A detached build holds no lock.** `hold_lock "ws-<ws>"` is taken for the
   foreground build, and `flock` dies with its holder — so with `--detach` this
   process exits and the lock goes with it. A second `wk image build` of the
   same profile is still refused, from evidence (a live pid in the workspace),
   but a concurrent `wk build` in the same workspace is not, and two builds in
   one checkout corrupt both.

   Closed the other way round, because a lock on this machine cannot be held by
   a process that is not on it: the workspace is asked for **evidence**
   instead. `ws_busy_reason` (lib/target.sh) reads the pid a detached job
   leaves in `$(t_home)/<job>.pid` — the convention `yocto_spawn` already
   followed — and tests it *inside* the workspace, which is the only namespace
   the number means anything in. `wk build` goes through it right after taking
   the workspace lock, as a barrier: the evidence is a pid this end cannot
   inspect further, so `--force` exists for the case where the number has been
   reused. Anything else detached into a workspace gets the same serialisation
   by writing the same file. The *same-workspace* half of this is closed: `yocto_spawn` refuses
   when any stage is live, not just the one being asked for — which was found
   the hard way, by a `--stage fetch` starting on top of a running
   `--stage image` and getting two bitbake cookers into one build directory.
