# Handoff: cross-compile targets

Not started. The word is reserved and the refusals are in place: `wk new
--sysroot` and `wk build --sysroot` both die with a pointer here rather than
doing something plausible.

We should support cross-compile targets:
https://github.com/justinmichaud/webkit-container-sdk/tree/dev/cross-builds-oc

You have a workspace set up for cross compilation, and a helper to transfer the
binary to the target (eg, rpi5 or a remote machine). We should confirm that
debugging and perf testing supports this configuration too.

## What this is *not*

`wk new --arch armhf` (`docs/HANDOFF-linux-arm32.md`, done 2026-08-18) is the
other mechanism, and keeping the two apart is the reason `--sysroot` is a
separate word rather than a second spelling of `--arch`:

|  | `--arch armhf` | `--sysroot <name>` |
|---|---|---|
| the workspace | *is* armhf | stays aarch64 |
| the compiler | the image's native armhf clang | aarch64 clang |
| the libraries | the image's own | another rootfs, mounted in |
| the flags | `linux32`, `-march=armv7-a+fp` | `-m32`, `--sysroot`, `CMAKE_LIBRARY_ARCHITECTURE`, `CMAKE_PREFIX_PATH` (webkitdirs.pm:2925-2941) |
| what it can target | only what this CPU executes | anything with a sysroot, riscv64 included |
| where it is chosen | at `wk new`, once, per workspace | per build — one workspace can cross several ways |

The images on this machine already carry both, and their names do not
distinguish them: `wkdev-sdk:24.04_arm32` is native armhf, while
`wkdev-sdk:24.04_arm32_arm64` is an arm64 image with armhf as a foreign
multiarch architecture — that one belongs to this handoff, along with
`wkdev-sysroot:2.53-v8-8a63f74_arm` and `wkdev-sysroot:*_riscv64`.

## Which branch — checked 2026-08-19

The handoff pointed at `justinmichaud/webkit-container-sdk` `dev/cross-builds-oc`.
Verified against GitHub and against this machine, the claim is half right.

**It is the current cross implementation, and there is evidence it runs.** moose
has `~/Development/webkit-container-sdk` checked out on that branch at its tip
(`3d8e55c`), `~/.cache/wkdev-cross/ubuntu-noble-armhf/` holds a **1.9 GB unpacked
sysroot plus `toolchain.cmake`**, and `localhost/wkdev-sysroot:noble-armhf`
(1.87 GB) was built locally. So the sysroot half of the flow has been exercised
here. What is *not* evidenced anywhere in this store is a completed cross **build
of WebKit** against that toolchain — no `cross-*` build directory exists — so
treat "known-working" as proven for sysroot generation and unproven for the build
itself.

The sibling branch `dev/cross-builds` is the same work 2.5 hours earlier as a
single commit titled `wip`; the `-oc` versions of the three files are slightly
smaller, i.e. cleaned up. `-oc` is the newer content.

**It is not the most upstreamable, though.** `-oc` is **17 commits ahead of and 14
behind** `Igalia/webkit-container-sdk@main` (which moved as recently as
2026-08-18), and roughly fifteen of those commits have nothing to do with
cross-compiling — Swift toolchain work (swift-driver, swift-format, libdispatch,
swift-foundation, LLD-for-swift, an aarch64 triple fix) and "Replace custom
openh264 with Ubuntu's". The cross work is exactly **two commits**:

```
60bf63e60  Cross-compile WebKit for Ubuntu devices, against a sysroot
3d8e55c92  Drop the armhf multiarch cross toolchain, superseded by the sysroot flow
```

touching seven files: `docs/04-Cross-compilation-inside-the-SDK.md`,
`images/wkdev_sdk/Containerfile`, a new `images/wkdev_sysroot/Containerfile`, new
`scripts/host-only/wkdev-cross-sysroot` and `scripts/container-only/wkdev-cross-emulate`,
and the deletion of the armhf multiarch toolchain files. Upstreaming means
cherry-picking those two onto current `main` — cheap, because they are already
separated and named. `dev/cross-builds` is *more* focused but is one `wip` commit
of older content, so it is not the candidate.

Two consequences for this handoff:

- **The multiarch cross path is deliberately gone.** `3d8e55c` deletes
  `images/wkdev_sdk/cross/armhf/*`, so on this branch the sysroot flow is the only
  cross mechanism. The table above still lists `wkdev-sdk:24.04_arm32_arm64` (an
  arm64 image with armhf multiarch) as belonging here; on `-oc` that image is
  irrelevant and `wkdev-sysroot:*` is the mechanism. Keep `--arch armhf` as the
  native-workspace word and let `--sysroot` mean this flow, which is what the
  vocabulary already says.
- **The sysroot release is a floor, not a match.** `wkdev-cross-sysroot`'s own
  help says it plainly: "Target the oldest device you need to run on: glibc is
  backwards but not forwards compatible." That refines the netboot handoff's
  sysroot-equivalence idea — the requirement is *sysroot glibc ≤ image glibc*, not
  identical trees. Building both from one release satisfies it; if the rpi3/rpi4
  yocto targets are older, the sysroot must come from the oldest of them.

The flow itself, for reference: `wkdev-sdk-bakery --mode=build
--name=wkdev-sysroot --arch=<arch>` builds the image, then `wkdev-cross-sysroot
--arch=<arch> --release=<release>` unpacks it under `~/.cache/wkdev-cross` and
writes the CMake toolchain file. Supported arches are armhf, arm64, riscv64 and
ppc64el.

## The test target, decided 2026-08-19

The thing to run a cross-built binary on is the **rpi5's bench system** —
`perf-linux-rpi5`, which now exists and lands before this step: a slim distro
with no SDK on it, which is exactly the condition a sysroot cross build has to
satisfy, and a GTK MiniBrowser is the payload. (The decision named it "the
netbooted rpi5 image"; the mechanism has since moved to a USB one-shot —
`wk boot rpi5` — and netboot is gone entirely now; every board's bench lane
is local media. The condition is unchanged.) Build the system and the sysroot from the *same tree* and the ABI
question answers itself rather than becoming a debugging session — that is
the cheap version of this step.

## Where it plugs in

- `lib/arch.sh` is the vocabulary file; a sysroot is not an `arch` and must not
  become a value of one. `arch_canon` already refuses `riscv64` with that
  explanation.
- The build directory has to gain a component for a cross build, because unlike
  a native armhf workspace — which is a whole separate workspace with its own
  overlay — a cross build shares a checkout with the native builds beside it.
  `WebKitBuild/cross-<sysroot>/JSCOnly/Release` or similar; `config_build_dir`
  in `build/configs.sh` is the one place that decides.
- The flags belong beside `arch_cflags`/`arch_ldflags`, keyed by sysroot rather
  than by architecture.
