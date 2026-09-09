# Owed: the boards' profile-guided build

README.md, "The boards' profile-guided build", is the design. This lists what
is not yet done.

## Never run on hardware

Nothing below has been exercised against a board or a Yocto SDK. The whole
cycle is owed one end-to-end run of

    wk sysimage webkit webkit-2.52-yocto-rpi5-64 --commit <sha> --slot pr --detach

and three things stand before it: no workspace here has built
`webkit-2.52-yocto-rpi5-64` (`wk sysimage ls`, 2026-09-09), the board's medium
does not carry it, and rpi5 is in host mode with a spent arming record for
`wpewebkit-2.46-yocto-rpi5-32`. So the run starts at `wk sysimage build`, then
the card, then `wk boot`.

Each of these is a distinct way the cycle itself can stop:

- [ ] **`llvm-profdata` in the cross environment.** `--stage pgo-mix` runs
      `lib/wkpgo.py` under `cross-toolchain-helper --cross-toolchain-run-cmd`
      and refuses if the SDK's PATH has no `llvm-profdata`. If it has none,
      the fix is the SDK's package list (`nativesdk-clang`), not a fallback to
      the workstation's: the workstation has clang 18/19 and meta-clang pins
      20, and a `.profraw` is read by the toolchain that wrote it.
- [ ] **The target's clang profile runtime.** `-DENABLE_LLVM_PROFILE_GENERATION=ON`
      configure-checks for `libclang_rt.profile` and stops with a FATAL_ERROR
      when the toolchain cannot link `-fprofile-generate`. Whether the SDK
      installs the aarch64 profile runtime is unverified; if it does not, that
      is a `TOOLCHAIN_TARGET_TASK` line, again in the image's own configuration.
- [ ] **`CC=clang` reaching the SDK's clang.** `image/yocto-build.sh` exports
      `CC`/`CXX` because `cross-toolchain-helper` writes an `environment-setup`
      that branches on them. Confirm the cross build actually compiles with
      clang and not with the SDK's GCC, which upstream's PGO support refuses.
- [ ] **What a collection costs on rpi5** -- wall time per plan against an
      instrumented build, and the size of `/var/wk/pgo` -- so `wk ab` on a
      2.52 profile can be costed before it is asked for (the Mac lane's costs
      are in `wk help`).
- [ ] **The coverage floor is the Mac lane's.** `lib/wkpgo.py`'s
      `MIN_COVERAGE` (25% of the combined profile's functions, per leg) was
      calibrated against the Apple ports' three separate frameworks. A GLib
      port merges the same code into one library, so every leg's share of it
      is larger, not smaller -- the Mac readings recomputed that way are 81%,
      84% and 63% -- but the floor has not been checked against a real board
      collection.
- [ ] **The SIGTERM window.** `pi_kill_cmd` gives a web process one second
      between SIGTERM and SIGKILL, and the profile is dumped in the SIGTERM
      handler (310954@main). Measure whether one second is enough for a
      `.profraw` on the board's medium; if it is not, the number belongs
      beside the kill and not in a retry.

## Owed upstream

- [ ] `webkitpy.llvm_profile_utils.locate_binary_xcrun` runs `/usr/bin/xcrun`
      with `check=False`, which on any host without it raises FileNotFoundError
      instead of returning non-zero -- so `LLVMProfDataExecutable.detect_binaries`
      cannot be called at all off macOS. `lib/wkpgo.py` blunts it for the
      duration of a mix; the fix belongs in that file, and this stops when it
      lands.
- [ ] `Tools/Scripts/collect-pgo-profiles` always ends in `pgo-profile compress`,
      which shells out to macOS's `/usr/bin/compression_tool`, so the collector
      cannot finish on Linux -- and the CMake ports read a plain `.profdata`
      anyway (`PGO_PROFILE_PATH`). The board lane therefore drives
      `run-benchmark` itself and calls `merge` and `combine` directly. If
      compression becomes optional, the two lanes can share the collector too.

## Not covered

- [ ] **No gate on what the collection rendered with.** The Mac lane drives the
      instrumented build at a page first and refuses a throttled or
      software-rendered browser (`bench/mac-browser-check.py`). The board lane
      gates the display and the CPU clock (`pi_leg_prepare`) and reads the
      profile back afterwards, but nothing checks that the collecting web
      process resolved a hardware GL driver and JITted -- which
      `bench/wk_board_driver.py` already reads for a warmup leg. What is
      missing is where a collection's copy of that evidence lives and what
      judges it, since `wkdata warmup-check` compares two arms and a collection
      has one.

- [ ] **buildroot at 2.52.** `webkit-2.52-buildroot-*` profiles build WebKit
      through `image/buildroot-webkit.sh` and get no profile, so a number from
      one is not comparable with a yocto number at the same release. Either
      the same three phases reach that builder, or the profiles say why they
      do not.
- [ ] **A board per 2.52 profile.** rpi3, rpi4 and rpi5 declare
      `webkit-2.52-yocto-rpi3-32`, `-rpi4-64` and `-rpi5-64`;
      `webkit-2.52-yocto-rpi4-32` is profile-guided and no machine declares it,
      so a slot for it refuses. Either a board declares it or the profile goes.
- [ ] **The instrumented slot stays on the board.** Nothing removes
      `<slot>-instr` after a collection; `wk pi bench` refuses to measure it,
      which is the safety, but the bytes accumulate. `wk pi` has no verb that
      removes a slot.
