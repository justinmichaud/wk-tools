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
