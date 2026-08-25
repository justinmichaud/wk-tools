# HANDOFF — cross-compile targets (`--sysroot`)

Not started. The word is reserved and the refusals are in place: `wk new
--sysroot` and `wk build --sysroot` both die pointing here (`cmd/new:76`,
`cmd/build:188`), and `lib/arch.sh` is the vocabulary that keeps the two
mechanisms apart.

Reference implementation:
https://github.com/justinmichaud/webkit-container-sdk/tree/dev/cross-builds-oc

## What this is *not*

`wk new --arch armhf` is the other mechanism, and keeping them apart is why
`--sysroot` is a separate word:

|  | `--arch armhf` | `--sysroot <name>` |
|---|---|---|
| the workspace | *is* armhf | stays aarch64 |
| the compiler | the image's native armhf clang | aarch64 clang |
| the libraries | the image's own | another rootfs, mounted in |
| the flags | `linux32`, `-march=armv7-a+fp` | `-m32`, `--sysroot`, `CMAKE_LIBRARY_ARCHITECTURE`, `CMAKE_PREFIX_PATH` |
| what it can target | only what this CPU executes | anything with a sysroot, riscv64 included |
| chosen | at `wk new`, once, per workspace | per build — one workspace can cross several ways |

## Remaining

- **Decide whether this is still worth building, and for what.** WebKit is
  already cross-built for a board by a *different* route — `wk sysimage build
  <profile> --stage webkit` against the target's own yocto SDK — and `wk pi
  deploy` is the transfer helper this file asked for. The case only the sysroot
  flow can serve is a target with no SDK of its own, riscv64 above all.
- **The test target, still never attempted**: a slim system with no SDK of its
  own, which is exactly the condition a sysroot cross build has to satisfy, with
  a GTK MiniBrowser as the payload. Every system this repo builds for a Pi is a
  yocto or buildroot one that *has* an SDK, so picking the target is part of
  this work rather than a given.
  Build the system and the sysroot from the *same tree* and the ABI question
  answers itself.
- **Debugging and perf-testing a cross-built binary** are both untried, and
  `wk debug` does not exist at all (`docs/Urgent/HANDOFF-debug.md`).
- **Where it plugs in**, when it is built:
  - `lib/arch.sh` is the vocabulary file; a sysroot is not an `arch` and must
    not become a value of one (`arch_canon` already refuses `riscv64` with that
    explanation).
  - the build directory needs a component for a cross build, since unlike a
    native armhf workspace it shares a checkout with the native builds beside
    it — `WebKitBuild/cross-<sysroot>/…`, decided in `config_build_dir`
    (`build/configs.sh`).
  - the flags belong beside `arch_cflags`/`arch_ldflags`, keyed by sysroot.

## Facts worth not re-deriving

- **The mechanism**: `wkdev-sdk-bakery --mode=build --name=wkdev-sysroot
  --arch=<arch>`, then `wkdev-cross-sysroot --arch=<arch> --release=<release>`
  unpacks under `~/.cache/wkdev-cross` and writes the CMake toolchain file.
  Supported: armhf, arm64, riscv64, ppc64el.
- **Sysroot generation is exercised here; a cross *build* of WebKit against it
  is not.** moose holds the branch at `3d8e55c`, a 1.9 GB unpacked
  ubuntu-noble-armhf sysroot and a locally built `wkdev-sysroot:noble-armhf`,
  and no `cross-*` build directory anywhere.
- **The multiarch path is deliberately gone** on that branch; `wkdev-sysroot:*`
  is the only cross mechanism there, so `wkdev-sdk:24.04_arm32_arm64` is
  irrelevant to this work.
- **The sysroot release is a floor, not a match**: glibc is backwards but not
  forwards compatible, so the sysroot must come from the *oldest* target it has
  to run on.
- **Upstreaming is two named commits**, not a branch merge — `60bf63e60`
  (cross-compile against a sysroot) and `3d8e55c92` (drop the armhf multiarch
  toolchain), cherry-picked onto `Igalia/webkit-container-sdk@main`. The rest of
  `-oc` is unrelated Swift-toolchain work. That is an item in
  `docs/HANDOFF-architecture-review.md`.
