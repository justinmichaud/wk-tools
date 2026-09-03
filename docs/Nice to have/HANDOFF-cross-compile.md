# HANDOFF — cross-compile targets (`--sysroot`)

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
