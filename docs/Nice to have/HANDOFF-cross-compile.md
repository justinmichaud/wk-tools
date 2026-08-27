# HANDOFF — cross-compile targets (`--sysroot`)

Build WebKit on a fast machine (an arm64 or x86_64 workstation, or a Mac)
against another distribution's sysroot, and run, test and debug the result on
a real board running that distribution -- an Ubuntu rpi3, a riscv64 board --
in minutes rather than the hours a yocto image costs, with clangd working
against the same build tree. The same mechanism is the basis for running
JSTests under QEMU (aarch64, x86_64) for EWS.

Not yocto, not buildroot: those build a whole *system*; this builds one
WebKit for a system that already exists. If the sysroot can come out of a
yocto SDK, that route is fine too.

Reference implementation (sysroot generation works; a WebKit build against
it has not been run):
https://github.com/justinmichaud/webkit-container-sdk/tree/dev/cross-builds-oc
-- `wkdev-sdk-bakery --mode=build --name=wkdev-sysroot --arch=<arch>`, then
`wkdev-cross-sysroot --arch=<arch> --release=<release>` unpacks under
`~/.cache/wkdev-cross` and writes the CMake toolchain file (armhf, arm64,
riscv64, ppc64el). Upstreaming is two commits, `60bf63e60` (cross-compile
against a sysroot) and `3d8e55c92` (drop the armhf multiarch toolchain),
cherry-picked onto `Igalia/webkit-container-sdk@main`.

## What this is *not*

`wk new --arch armhf` is the other mechanism, and keeping them apart is why
`--sysroot` is a separate word:

|  | `--arch armhf` | `--sysroot <name>` |
|---|---|---|
| the workspace | *is* armhf | stays the build machine's arch |
| the compiler | the image's native armhf clang | the build machine's clang |
| the libraries | the image's own | another rootfs, mounted in |
| the flags | `linux32`, `-march=armv7-a+fp` | `-m32`, `--sysroot`, `CMAKE_LIBRARY_ARCHITECTURE`, `CMAKE_PREFIX_PATH` |
| what it can target | only what this CPU executes | anything with a sysroot, riscv64 included |
| chosen | at `wk new`, once, per workspace | per build — one workspace can cross several ways |

## Remaining

- [ ] the first target: an Ubuntu rpi3, built from x86_64 or a Mac, GTK or
      WPE MiniBrowser as the payload; the sysroot comes from the *oldest*
      release the binary has to run on (glibc is backwards compatible only)
- [ ] `wk build <ws> <config> --sysroot <name>`: the build directory gets a
      component (`WebKitBuild/cross-<sysroot>/…`, decided in `config_build_dir`,
      build/configs.sh) since it shares a checkout with the native builds;
      the flags sit beside `arch_cflags`/`arch_ldflags` (lib/arch.sh), keyed
      by sysroot -- a sysroot is not an `arch` and `arch_canon` keeps refusing
      `riscv64` as one
- [ ] `wk new --sysroot` and `wk build --sysroot` stop refusing (`cmd/new`,
      `cmd/build`) once the above exists
- [ ] transfer and run: `wk pi deploy` onto a board running the target
      distribution; `wk run`/`wk gui --lldb` against the deployed copy
- [ ] clangd against the cross build tree (docs/Urgent/HUMAN-clangd.md)
- [ ] riscv64: build on the arm64 workstation, run and debug on the board
- [ ] JSTests under QEMU user-mode for aarch64 and x86_64, as an EWS lane
