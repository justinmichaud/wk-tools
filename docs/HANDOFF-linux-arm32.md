# Handoff: 32-bit containers on Linux

Not started. `wk new --arch 32` is not implemented and nothing in
`targets/container.sh` passes `--arch` through.

## Why it is worth doing here

32-bit is dead on Apple Silicon -- no AArch32 at EL0, and no published armhf
image -- so the macOS path refuses outright and always will. The Linux
workstation is the only machine in this setup that can run it: an Ampere
Neoverse-N1 does support AArch32 at EL0, and `lscpu` reports
`CPU op-mode(s): 32-bit, 64-bit`.

That matters because JSC's 32-bit paths get very little local coverage, and the
alternative is testing them on a Raspberry Pi over the network.

## What to do

`wkdev-create` already takes `--arch`, and the SDK publishes an `_arm32`
image-tag suffix (see `process_command_line_arguments` in `wkdev-create`: with
`--arch` set and `--no-pull`, it falls back to `${container_tag}_${arch}`). So
the work is:

1. `wk new --arch 32` in `cmd/new`, plumbed to `t_create` as an optional
   architecture, defaulting to native.
2. `targets/container.sh` passes `--arch arm` to `wkdev-create` and records the
   architecture in the workspace directory, so `wk build` and `wk run` do not
   have to be told again.
3. 32-bit build configs in `build/configs.sh` (e.g. `jsc-release-32`,
   `wpe-release-32`). The known-good invocation is copy-pasted across five
   wiki pages today and should be baked in once:
   - run under `linux32` (so `uname -m` lies correctly to build scripts),
   - `-mthumb -march=armv7-a+fp` in C/CXX flags,
   - the gold-linker low-memory block: `-fuse-ld=gold
     -Wl,--no-map-whole-files -Wl,--no-keep-memory
     -Wl,--no-keep-files-mapped -Wl,--no-mmap-output-file`,
   - `-DUSE_LD_LLD=OFF -DFORCE_32BIT=ON`.
   The compiler question still needs answering: the aarch64 clang in the image
   can target armv7 with a sysroot, but the image's own libraries are the ones
   that matter, and the arm32 image carries its own toolchain. Related
   provisioning (qemu-user-static/binfmt, the armv7 sdk-image bakery) is on
   the `WebKit-ARM32-ARM64-workstation-setup` wiki page and belongs in
   `./setup`/`container/` if it is needed at all — see item 4.
4. `binfmt_misc` is not needed -- this is native execution, not emulation --
   but confirm that, because the SDK's `wkdev-cross-emulate` exists for the
   case where it is.

## Traps

**The nvidia CDI spec and `--arch` do not mix.** `wkdev-create` already skips
`try_process_nvidia_gpu` when `--arch` is set, with a comment saying the nvidia
packages are unavailable there. A 32-bit workspace is therefore a software
rendering workspace, which is fine for JSC and useless for benchmarks.

**The egress proxy is architecture-independent** -- it runs on the host -- but
`container/proxy/bridge.py` runs *inside* the container and needs a python3
there. Check the arm32 image has one before assuming egress works.

**Do not let `--arch 32` reach `wk bench`.** A benchmark on a software renderer
is a different measurement; `wk bench` already refuses without a hardware
renderer, and that refusal should stay.
