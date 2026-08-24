# HANDOFF — armhf workspaces: what is left to run

The tooling is done: `wk new --arch armhf` creates a native 32-bit ARM
workspace, `wk build` runs the build under `linux32` with the ARM flags, and
`lib/arch.sh` is the vocabulary. What remains is WebKit-side, or a run nobody
has made.

Note that `arch=` in `~/.wk-workspace` is the authority for a workspace's width:
the kernel is the host's, so `uname -m` says `aarch64` in a 32-bit workspace and
`lscpu` looks 64-bit too. Nothing is wrong there.

## Remaining

- **`wk test <ws>` on armhf** — never run on either branch. On trunk it will be
  a sea of the known SIGBUS crashes, which is itself worth measuring once; on
  2.48 it is the real test.
- **An armhf workspace on `webkitglib/2.48`** — never tried, and it is the
  branch that still has a working ARMv7 JIT. The fetch-and-checkout is the whole
  setup; the unknown is whether 2.48's CMake needs `ENABLE_JIT`/`ENABLE_C_LOOP`
  set by hand for a 32-bit target.
- **A cpu-class benchmark run on armhf** — never completed in either runner. The
  jsc shell dies in JetStream3's driver and the browser's web process dies the
  same way on trunk. The runner is verified on aarch64 in both modes, so this is
  a branch away rather than a tooling gap.
- **`gtk-release` on armhf** — untried. A browser port builds and starts; what
  it cannot do on trunk is run JS.
- **The trunk SIGBUS is left alone deliberately** — reproducible in three lines,
  debuggable in the workspace, and WebKit work rather than tooling work.
  Anything needing a running 32-bit JSC today uses 2.48.
- **`--sysroot` is reserved and refused**, and is a different mechanism:
  `docs/Nice to have/HANDOFF-cross-compile.md`.

There is no GPU in an armhf workspace (the NVIDIA userspace is aarch64-only), so
`wk bench` refuses gpu-class plans there and says why.
