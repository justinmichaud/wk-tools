# Owed: the boards' profile-guided build

README.md, "The boards' profile-guided build", is the design. This lists what
is not yet done.

## What one run on hardware settled

The cycle has run end to end once, on rpi5: moose's
`yocto-webkit-2.52-yocto-rpi5-64` holds `pgo-instr`
(`build_config=wpe-cross-pgo-collect`), `pgo` (`wpe-cross-pgo-use`) and a
`plain` slot of the same commit, built 2026-09-10, over a collection
(`build/wk-pgo/pgo/`) carrying a `.profdata` per plan, the mixed one under
`output/`, and `profile-check.json` with per-benchmark function counts.

- The cross build compiles with the SDK's clang, not its GCC: an instrumented
  configure under GCC stops at `HAVE_CLANG_PROFILE_RUNTIME`, and that build is
  what produced the profiles `llvm-profdata` then merged.

Two more, measured against the SDK this repo built for rpi5 on 2026-09-09:

- `llvm-profdata` is in the SDK's host sysroot
  (`sysroots/aarch64-pokysdk-linux/usr/bin`), so `--stage pgo-mix` has the
  toolchain that wrote the profiles.
- `libclang_rt.profile-aarch64.a` is in the SDK's target sysroot
  (`usr/lib/clang/20.1.1/lib/linux`), so `-fprofile-generate` links. Nothing
  here puts it there and nothing should: `compiler-rt-sanitizers-dev` RDEPENDS
  on `compiler-rt-sanitizers-staticdev`, and `SDKIMAGE_FEATURES` carries
  `dev-pkgs`, so the archive follows the image's own `compiler-rt-sanitizers`.
  If an image ever drops that recipe, the instrumented configure stops at
  `HAVE_CLANG_PROFILE_RUNTIME` and the fix is one
  `TOOLCHAIN_TARGET_TASK:append`.

What is left:

- [ ] **What a collection costs, per plan.** The rpi5 run's three plans
      occupied the board from 21:35 to 21:50 on 2026-09-09 (the collection
      directory's own file times: speedometer3 21:37, jetstream3 21:44,
      motionmark 21:50), and each plan's `.profdata` is about 2 MB. What is
      still unread is the wall time of each leg as the driver measured it and
      the size `/var/wk/pgo` reaches on the board, so a 2.52 `wk ab` can be
      costed the way the Mac lane's is in `wk help` [needs a board].
- [ ] **The same on rpi3, whose image is 32-bit and whose memory is 931 MB**:
      an instrumented build is several times slower and larger, and
      speedometer3 already does not complete there at 2.38 [needs the rpi3].
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

## Decisions not taken

- [ ] **No LTO.** The Mac lane's perf build is thin LTO for the instrumented
      phase and full LTO for the measured one; the board's cross configs set
      no `LTO_MODE` at all, so the two lanes do not agree on what a perf build
      is. WebKit's CMake takes `-DLTO_MODE=thin|full`
      (`WebKitCompilerFlags.cmake`, straight to `-flto=`), and upstream's PGO
      patch already handles the `__llvm_profile_filename` clash LTO causes.
      What is missing is a measurement: what full LTO costs a cross link on
      the SDK toolchain, and what it buys on a board.

## Owed upstream

- [ ] **`JSC_reportDFGCompileTimes` / `reportFTLCompileTimes` crash the web
      process.** `comm="JITWorker" ... sig=11`, twice out of two warmup legs
      on rpi5 against WebKit 64abfa28ea1c, with no such record in ~15 legs
      without them; the browser log ends mid-line, at Speedometer 3's
      metric-aggregation, and the results are never posted. Both options are
      documented as dumping a JS function signature, and that dump happens on
      the compiler thread. `wk pi bench --jit-tiers` is the only thing that
      sets them now. Worth a bug against JSC with the audit records; if it is
      fixed, the flag can go back to being the default.

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
