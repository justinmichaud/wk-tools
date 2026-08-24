---
name: build-webkit
description: Use when building JavaScriptCore, WebKit, or any WebKit subproject. Handles platform detection, ASan configuration checks, correct working directory, and build commands.
user-invocable: true
allowed-tools:
  - Bash(make release:*)
  - Bash(make debug:*)
  - Bash(make clean:*)
  - Bash(set-webkit-configuration:*)
  - Bash(Tools/Scripts/set-webkit-configuration:*)
  - Bash(Tools/Scripts/build-webkit:*)
  - Bash(pwd:*)
  - Bash(cat:*)
  - Bash(ls:*)
  - Bash(rm -rf:*)
  - Bash(uname:*)
  - Bash(git rev-parse:*)
  - Bash(wk build:*)
  - Bash(wk run:*)
  - Bash(wk status:*)
  - Bash(wk logs:*)
---

# Building JavaScriptCore / WebKit

**This skill owns build guidance. Nothing else should state a build command.**

## First branch: are you inside a wk workspace?

`pwd` answers it — a Linux workspace holds the checkout at `/src/WebKit`, a macOS
guest at `/Users/admin/WebKit`, and `~/.wk-workspace` exists in both.

**Inside a workspace, the build is one command and none of the tuning below is
yours to do:**

```bash
wk build <config>              # jsc-release, gtk-debug, wpe-release, mac-release, ...
wk build <config> --detach     # tens of minutes; survives the shell going away
wk build --list                # what the configs are
wk status                      # build/test state, machine-readable exit code
wk logs [-f]                   # the log, errors first
wk run -- <args>               # run jsc from the build just made
```

`wk build` already derives the job count from available memory, runs the build at
a nice level that keeps the host usable, watches for OOM (`build=oom` in
`wk status`, with the peak and the budget — rebuild with
`WK_MB_PER_JOB=3072 wk build <config>` rather than assuming the code is at
fault), and passes the right arch flags in a 32-bit workspace. Do not hand-roll
a `build-webkit` invocation in here: a raw `ninja -j$(nproc)` can hang the
machine, and every number below about `-j` is the host's problem, not yours.

**Outside a workspace — a host workstation with a checkout — the rest of this
file applies.** Every command runs from `$WEBKIT_ROOT`, the repository root;
resolve it with `git rev-parse --show-toplevel`. Default to a **release** build
and the **JavaScriptCore** target unless the user says otherwise; swap `release`
for `debug` when they ask.

## Parallelism: size `-j` to memory, and always `nice` the build

**A WebKit build is a memory hog, not just a CPU hog, and it usually shares the machine with the
user's desktop.** WebCore/WebKit unified sources routinely take **3-6 GB of RSS per `cc1plus`**, so the
core count is the wrong budget: `-j32` on an 80-core / 125 GB host was measured at 28-29 concurrent
compilers, 76 GB resident, and 3 GB of swap in use, with load pinned at 30-32 for hours. Cores were
free the whole time; memory and interactive responsiveness were not.

- **Budget `-j` from RAM at ~4-5 GB per job, then cap by cores** — the same rule the `jsc` skill applies
  to `run-jsc-stress-tests -c N`. It is the same box and the same failure mode; do not apply it only to
  test workers. Leave headroom for the desktop: on a 125 GB workstation that is roughly `-j16`, not `-j32`.
- **Always `nice -n 10` the build.** Job count protects memory; niceness is what keeps the user's editor,
  browser, and LSP responsive while 16+ compilers saturate their cores. It costs the build almost
  nothing because the build is throughput-bound, not latency-bound.
- **Ask before taking a big share of a machine you know is in use**, and say what you intend to take.
  Check first: `ps -eo pcpu,comm --sort=-pcpu | head` showing firefox/an editor/clangd means someone is
  working on it. Several multi-hour builds back to back is a much bigger ask than one.

```bash
nice -n 10 Tools/Scripts/build-webkit --gtk --release -j16   # shared workstation default
```

Pick the platform with `uname -s`: **Darwin** is macOS and uses `make`; **anything else** (Linux) uses `Tools/Scripts/build-webkit`. The `make` wrapper and ASan file checks apply to macOS only.

## macOS (Darwin) — `make`

```bash
make release SCHEME="Everything up to JavaScriptCore"   # JSC only (debug: make debug)
make release SCHEME="Everything up to WTF"              # another subproject
make release                                            # full WebKit / Safari, no SCHEME
```

`"Everything up to X"` builds X plus its dependencies (WTF, bmalloc, etc.). Pass `SCHEME=` explicitly so behavior does not depend on the current directory.

**ASan check before building** (looks for the marker file `$WEBKIT_ROOT/WebKitBuild/ASan`):
- User asked for ASan, marker absent: run `Tools/Scripts/set-webkit-configuration --asan` first.
- User did not ask for ASan, marker present: warn ASan is enabled. Offer to clean by removing `$WEBKIT_ROOT/WebKitBuild` (destructive — confirm first). If declined, build anyway and note it will be ASan-enabled.

Run binaries with `DYLD_FRAMEWORK_PATH` set to the build output:
```bash
DYLD_FRAMEWORK_PATH=$WEBKIT_ROOT/WebKitBuild/Release $WEBKIT_ROOT/WebKitBuild/Release/jsc test.js
```

Artifacts (flat framework layout):
| Artifact | Path |
|----------|------|
| JSC shell | `$WEBKIT_ROOT/WebKitBuild/Release/jsc` (or `Debug/jsc`) |
| Test binaries | `$WEBKIT_ROOT/WebKitBuild/Release/{testmasm,testb3,testair}` |
| Frameworks | `$WEBKIT_ROOT/WebKitBuild/Release/JavaScriptCore.framework/`, etc. |
| Generated headers | `$WEBKIT_ROOT/WebKitBuild/Release/DerivedSources/JavaScriptCore/` |

## Linux — `Tools/Scripts/build-webkit`

```bash
Tools/Scripts/build-webkit --jsc-only --release   # JSC only (debug: --debug)
Tools/Scripts/build-webkit --release              # full WebKit
```

Artifacts (standard `bin/`/`lib/` layout):
| Artifact | Path |
|----------|------|
| JSC shell | `$WEBKIT_ROOT/WebKitBuild/Release/bin/jsc` |
| Test binaries | `$WEBKIT_ROOT/WebKitBuild/Release/bin/{testmasm,testb3,testair}` |
| Shared libraries | `$WEBKIT_ROOT/WebKitBuild/Release/lib/` |
| Generated headers | `$WEBKIT_ROOT/WebKitBuild/Release/DerivedSources/JavaScriptCore/` |

A JSCOnly `bin/jsc` runs in place — it links its sibling `lib/` via **RPATH**, so just
`"$DIR/bin/jsc" …`, no env var. Because that is RPATH (not RUNPATH), `LD_LIBRARY_PATH` does **not**
redirect it to a different `libJavaScriptCore.so`; to run an alternate or saved lib, copy it over the
in-place file (back it up first).

### 32-bit ARMv7 (an armhf workspace)

**The container this section was written against (`wkdev32`, with host paths under
`~/Development/32`) is gone.** 32-bit work now happens in a native armhf
workspace, made on the host with `wk new <name> --arch armhf`; inside it,
`wk build jsc-release` is the whole build and it already passes `linux32` and the
ARM flags. `arch=` in `~/.wk-workspace` is the authority for the width — the
kernel is the host's, so `uname -m` answers `aarch64` in a 32-bit workspace and
`lscpu` looks 64-bit too. Nothing has gone wrong there.

Everything below is what the flags mean and what goes wrong at this width. It is
still current: the same compiler, the same linker, the same 32-bit address space.

The flags `wk build` passes for you: `-march=armv7-a -mthumb -mfpu=neon
-mfloat-abi=hard` in `CMAKE_{C,CXX}_FLAGS`, `-DCMAKE_BUILD_TYPE=RelWithDebInfo`,
`-DUSE_LD_LLD=OFF`, and `linux32` around the whole build. Output is
`WebKitBuild/JSCOnly/Release/bin/jsc`.

When a 32-bit build/link fails or the JIT comes up disabled, verify the target arch first — this is
ARM32 (ARMv7/Thumb-2), not x86 or `linux32`; assuming x86 sends the diagnosis the wrong way. A common
real cause of a disabled JIT is **Thumb-2 detection failing at CMake configure time** (check the
configure output), not a bug in the code.

`linux32` is not optional: without it `uname -m` reports the 64-bit host and CMake configures
`WTF_CPU_ARM64` for what is actually an ARM32 build.

**Debug 32-bit: put `-fuse-ld=gold` in the *linker* flags, not just `CMAKE_CXX_FLAGS`.** Otherwise the
link dies with `ld.gold: fatal error: libJavaScriptCore.so.1.0.0: pread failed: Invalid argument` — the
library exceeds the 2 GB a 32-bit gold can `pread`. Cause: developer-mode debug builds enable debug
fission, and GCC's `-gsplit-dwarf` forces `-ggnu-pubnames` unconditionally (`-gno-pubnames` cannot turn
it back off). Those `.debug_gnu_pubnames`/`.debug_gnu_pubtypes` tables stay in the objects instead of
moving into the `.dwo`, and their only consumer is a linker building `.gdb_index`.
`Source/cmake/OptionsCommon.cmake` does pass `-Wl,--gdb-index`, but it probes the linker by running
`${CMAKE_C_COMPILER} ${CMAKE_EXE_LINKER_FLAGS} -Wl,--help`, so `-fuse-ld=gold` hidden in
`CMAKE_CXX_FLAGS` makes it probe BFD ld (which has no `--gdb-index`) and disable the flag, while the
real link still uses gold. Add exactly one thing: **`-DCMAKE_EXE_LINKER_FLAGS=-fuse-ld=gold`**. CMake
then reports `Linker variant in use: GOLD`, gold discards the pubnames tables, and libJavaScriptCore
drops from ~2.6 GB to ~159 MB. Link-flag-only, so nothing recompiles.

Do **not** also pass `-DCMAKE_SHARED_LINKER_FLAGS=-fuse-ld=gold`. It is redundant (the `-fuse-ld=gold`
already in `CMAKE_CXX_FLAGS` selects the linker) and it overwrites the default shared-linker flags,
dropping `-L/jhbuild/install/lib`. Verified both ways: with it, the link line carries `-fuse-ld=gold`
twice and no jhbuild `-L`; without it, one `-fuse-ld=gold`, `--gdb-index` still present once, jhbuild
`-L` restored, same 159 MB library. Read the real link line with
`ninja -t commands lib/libJavaScriptCore.so.1.0.0 | tail -1` — a successful ninja run never echoes it.

Separately, `OptionsCommon.cmake` seeds the shared flags from the exe flags
(`set(CMAKE_SHARED_LINKER_FLAGS "${CMAKE_EXE_LINKER_FLAGS} -Wl,--gdb-index")`), discarding any real
`CMAKE_SHARED_LINKER_FLAGS`. That is a genuine bug worth fixing, but it is **not** required for the
build to work: unfixed, the shared link inherits the exe flags and lands `--gdb-index` twice, which
links fine. Counting `--gdb-index` on the link line tells you which version is in play.

**A `--debug --no-unified-builds` JSC build also trips missing `*Inlines.h` includes** that no other
configuration reaches, because the uses sit behind `ASSERT`/`ENABLE(...)`. `undefined reference` to an
inline (`JSValue::decode`, `JSCell::structure`, `Heap::vm`, `CodeBlock::wasDestructed`) means the TU
included the declaring header but not the defining `*Inlines.h`. gold names the header, not the object,
so find the culprit with
`nm -C -u <each .o in CMakeFiles/JavaScriptCore.rsp> | grep <symbol>`.

**A rebuilt object that comes back byte-identical was not rebuilt — that is a ccache hit.** CMake wires
ccache in through the ninja rule's `${LAUNCHER}` (`LAUNCHER = /usr/bin/ccache`, in
`CMakeFiles/rules.ninja`), and `compile_commands.json` does **not** record it. So a compile replayed from
`compile_commands.json` bypasses ccache and can succeed while the build keeps failing on the same file.
Grepping `build.ninja` for `ccache` finds nothing; grep for `LAUNCHER` instead. A truncated cache entry
shows up as a link error, not a compile error: gold reported
`multiple definition of ''` 52 times for one object whose 185 COMDAT group signature symbols all had
empty names (`readelf -gW obj.o | grep '^COMDAT group'` shows `` `.group' [] `` instead of a mangled
name; a healthy sibling object is the contrast to check). Confirm by compiling the file by hand without
the launcher, then fix with `CCACHE_RECACHE=1 <build command>` to overwrite just that entry. Check
`ccache -s` **as the build user** — inside the workspace, which is where the cache
is; asking on the host, or as root, reports an empty cache that reads as "ccache
is not involved." A cache over its size
limit with many cleanups and nonzero `Errors` is how entries get truncated in the first place: this one
sat at 7.7 GB against a 5 GB ceiling with 1359 cleanups, and the constant eviction also held the hit
rate to 7.6%. Raise the ceiling (`ccache -M 25G`, persisted to `~/.config/ccache/ccache.conf`) and
purge once (`ccache -C`) to clear any other truncated entries; at that hit rate the purge costs almost
nothing. `ccache -z` afterwards so the next `Errors` count is meaningful.

Corollary for the whole class: **a killed build leaves damage that survives into later builds.** Two
kinds seen here — zero-byte objects (the debug-fission `objcopy` GCC runs for `-gsplit-dwarf` then
fails with `input file is empty`) and truncated ccache entries. Neither is fixed by re-running the
build, because ninja and ccache both consider the bad artifact up to date.

**Unaligned-access SIGBUS here is usually the container, but check the faulting instruction first.**
A 32-bit ARM userland on a 64-bit kernel gets no alignment-fault fixup: `CONFIG_ALIGNMENT_TRAP` and
its `/proc/cpu/alignment` knob exist only in 32-bit ARM kernels, so a single-register access
(`ldr`/`str`/`ldrh`/`strh`) to an unaligned address raises SIGBUS instead of being emulated. A real
ARMv7 device running a 32-bit kernel fixes those up, so discount those (exit code 135) as
environmental.

**`ldrd`/`strd` are the exception and are always a real bug.** ARMv7 unaligned-access support
(SCTLR.A=0) covers single-register loads and stores only; it explicitly excludes the pair and
multiple forms (`ldrd`/`strd`, `ldm`/`stm`, `vldm`/`vstm`). Those fault on any non-word-aligned
address on real silicon, with no kernel fixup anywhere. So a SIGBUS whose faulting instruction is
`ldrd` or `strd` reproduces on hardware and must be fixed, not discounted. Disassemble before
concluding:

```
gdb -batch -ex 'handle SIGUSR1 SIGUSR2 SIGSEGV nostop noprint pass' \
    -ex 'handle SIGBUS stop print nopass' -ex run -ex 'x/1i $pc' --args <jsc> <args>
```

This matters for wasm: a linear-memory alignment immediate is only a hint, so an `i64.load` /
`f64.load` may land on any address, and lowering it to `ldrd` is wrong. Upstream removed the `strd`
fast path from `MacroAssemblerARMv7::storePair32` for this reason (`5e2002044e76`); the matching
load side is still unfixed upstream.

#### Getting a C++ backtrace out of a 32-bit crash

Both obvious routes fail here. In-container `gdb` is a 32-bit process and exhausts its address space
loading the debug build's split DWARF (`virtual memory exhausted` while reading `.dwo`s), and
`WTFReportBacktrace` prints `no stacktrace available` because `WTFGetBacktrace` cannot walk ARM32
Thumb-2 frames. Assertion output therefore arrives with no stack at all.

What works is the **host's 64-bit gdb on a core dump**, since the host is aarch64 and has the address
space the in-container debugger lacks:

1. `coredumpctl dump <pid> --output=jsc.core` (cores land in systemd-coredump, zstd-compressed).
2. Make debug-stripped copies **inside the container** so gdb reads `.symtab` and never touches the
   split DWARF: `objcopy --strip-debug bin/jsc nodebug/jsc` and the same for
   `lib/libJavaScriptCore.so.1.0.0`. Ubuntu gdb 17.1 dies with heap corruption on the
   `DW_TAG_skeleton_unit` records otherwise, so this step is required, not an optimization.
3. Add the SONAME symlink `libJavaScriptCore.so.1 -> libJavaScriptCore.so.1.0.0`; gdb looks the
   library up by SONAME and silently gives up without it.
4. Stage a sysroot with the container's `libc.so.6`, `libstdc++.so.6`, `libgcc_s.so.1`, `libm.so.6`,
   `ld-linux-armhf.so.3` and the ICU libs, and place the stripped JSC libraries at the path recorded
   in the core. Without libc symbols the unwind stops inside `abort` and you see nothing above it.
5. `gdb -q -batch -iex 'set debuginfod enabled off' -iex "set sysroot <root>" -ex 'bt 40' nodebug/jsc jsc.core`

Symbol names come from `.symtab` so frames resolve without line numbers, which is enough to identify
the failing operation. When you need line numbers or values instead, a `dataLogLn` in the one relevant
`.cpp` is far cheaper than fighting the debugger: a single-file edit plus relink is a few minutes even
in the debug build.

#### Full browser (WPE) on 32-bit ARM

For anything needing a browser rather than the jsc shell (Speedometer, MotionMark — neither has a
headless runner), build the whole WPE port: `linux32 Tools/Scripts/build-webkit --wpe --release`, same
arch flags, plus these three, none of which change codegen:

- **`-DENABLE_WPE_PLATFORM=OFF`** — it defaults ON in developer mode and CMake hard-errors
  `ENABLE_WPE_PLATFORM conflicts with ENABLE_WPE_1_1_API`. Turning it off keeps the libwpe /
  wpebackend-fdo path.
- **`-DDEVELOPER_MODE_FATAL_WARNINGS=OFF`** — WebKit enables `-Wcast-align` globally
  (`WebKitCompilerFlags.cmake`) and developer mode adds `-Werror`. On ARM32 GCC that fires on every
  cast to an over-aligned type it cannot prove is aligned, including bmalloc's `IsoPageInlines.h` /
  `IsoDirectoryPageInlines.h`, which most of WebCore includes. Do not chase these one at a time.
- **`-DENABLE_API_TESTS=OFF -DENABLE_LAYOUT_TESTS=OFF -DENABLE_WEBDRIVER=OFF`** when only the browser
  is needed — ~360 of 3451 objects.

Leave **unified builds ON** for the full port (`--no-unified-builds` is a jsc-iteration convenience;
non-unified WebCore is far too slow here). Cog is built automatically as an ExternalProject
(`ENABLE_COG` defaults ON in developer mode) into
`WebKitBuild/WPE/Release/Tools/cog-prefix/src/cog-build` — it clones from GitHub, so the build needs
network.

**A changed compiler flag invalidates every object.** Adding `-DDEVELOPER_MODE_FATAL_WARNINGS=OFF`
part-way through discards the whole ninja cache, so decide the warning/flag configuration *before*
starting a multi-hour build rather than after the first `-Werror` failure.

`g-ir-scanner` runs with `--warn-error`, so a gtk-doc comment whose `@param` name disagrees with the
header fails the build at the very end, after every link. That failure is not arch-specific; it will
also fail on 64-bit.

## Checking results

`make` and `build-webkit` return 0 on success, non-zero on failure — always check the exit code. On failure, read the output and fix the cause:
- Compilation error: fix the source and rebuild.
- Missing generated files: clean and rebuild (`make clean`, then rebuild).
- Xcode version mismatch (macOS): confirm the selected Xcode is compatible.

## Nuclear reset

Last resort, when a build is hopelessly broken. Removing `WebKitBuild` deletes all artifacts, the ASan config, and the Configuration file — confirm with the user first, then:
```bash
rm -rf $WEBKIT_ROOT/WebKitBuild
```
Then rebuild from scratch.
