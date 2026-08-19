# Handoff: 32-bit containers on Linux

**Implemented 2026-08-18.** `wk new <name> --arch armhf` creates a native
armhf workspace, `wk build` in it produces 32-bit binaries, and `wk bench` runs
CPU-class benchmarks there.

Two findings from doing it are below, and neither is a blocker. On **trunk**
there is no 32-bit ARM JIT left, so an armhf JSC is a CLoop build by
construction; and trunk's bytecode decoding SIGBUSes on anything non-trivial.
Both are properties of the branch, not of the workspace: **2.48 still has the
ARMv7 backend and still works**, and the trunk crash is fully debuggable in
here -- lldb and gdb both drive the 32-bit process, and `wk run <ws> --lldb`
lands at the prompt on it. What is left is in "Remaining" at the bottom.

## Why it is worth doing here

32-bit is dead on Apple Silicon -- no AArch32 at EL0, and no published armhf
image -- so the macOS path refuses outright and always will. The Linux
workstation is the only machine in this setup that can run it: an Ampere
Neoverse-N1 does support AArch32 at EL0, and `lscpu` reports
`CPU op-mode(s): 32-bit, 64-bit`.

That matters because JSC's 32-bit paths get very little local coverage, and the
alternative is testing them on a Raspberry Pi over the network. What those
paths *are* has changed since the wiki pages were written -- see "What a 32-bit
workspace actually covers" below.

## The vocabulary, which is the load-bearing part

Three things on this machine could all be described as "building 32-bit", and
they are three different mechanisms. They get three different words, defined in
`lib/arch.sh` and used everywhere:

| word | what it is | where it is said |
|---|---|---|
| `--arch` | the workspace's own userland, executed natively | `wk new` |
| `--sysroot` | a cross build from a native workspace: aarch64 clang, `-m32`, another rootfs to link against | `wk build`, reserved and refused with a pointer |
| `--target` | another machine entirely | `wk new` |

The images make the distinction concrete, and their names actively invite the
mistake:

- `ghcr.io/igalia/wkdev-sdk:24.04_arm32` -- **native armhf**. `dpkg
  --print-architecture` is armhf and `/usr/local/bin/clang` targets
  `arm-unknown-linux-gnueabihf`. This is what `--arch armhf` uses, pinned in
  `WK_IMAGE_ARMHF`.
- `ghcr.io/igalia/wkdev-sdk:24.04_arm32_arm64` -- **not that**. It is an arm64
  image with armhf as a foreign multiarch architecture: aarch64 clang, armhf
  libraries alongside. That is the sysroot mechanism, and it belongs to
  `--sysroot` when that lands, together with `wkdev-sysroot:*_arm` and
  `wkdev-sysroot:*_riscv64`.

Because an armhf workspace is native, the configs are unchanged: `wk build
arm-bug jsc-release` in one is a 32-bit JSC. There is no `-32` config suffix and
deliberately so -- inside an armhf container nothing else can be built, so the
suffix would carry no information, while the same suffix on a cross build would
carry all of it.

## What was implemented

1. `wk new --arch armhf`, canonicalised through `arch_canon` (which accepts
   `32`, `arm32`, `armv7`, `arm` and stores only `armhf`), refused on any
   target but `container`.
2. `targets/container.sh` passes `--arch arm` and an explicit `--image`,
   records the architecture in `ws/<name>/arch`, and skips GPU injection.
   `container/firstrun.sh` writes `arch=` into the workspace marker, so an
   in-workspace `wk build` knows too -- it cannot be re-derived in there,
   because the kernel is the host's and `uname -m` answers aarch64.
3. `lib/arch.sh` holds the flags: `linux32`, `-mthumb -march=armv7-a+fp
   -Wno-pass-failed`, the gold low-memory link block, and `-DUSE_LD_LLD=OFF`.
   They reach the build as `WK_ARCH_*` and are applied by
   `build/build-in-target.sh`. Nothing about a config changes.
4. `wk ls` gains an ARCH column, `wk build` names the architecture in its
   banner and dry run, and `wk status` records it per build.
5. `wk bench` learned the class/runner/host axes (below), so an armhf
   workspace is benchmarkable rather than a dead end.

### The traps, and what they turned out to be

**`--arch` alone does not choose an image.** `wkdev-create` falls back to a
`_${arch}` tag only when the unsuffixed tag is *missing locally*, and on any
machine that has ever created a workspace it is present -- so `--arch arm`
silently handed podman the aarch64 image. Fixed by adding `--image` to the SDK
in `container/sdk-patches/apply.sh` (section 12, with a verify token).

**`uname -m` lies, and CMake believes it.** In an armhf container the kernel is
still the host's, so `uname -m` is aarch64 and `WebKitCommon.cmake:167` sets
`WTF_CPU_ARM64`. `linux32` is what makes it `armv8l`, which the arm branch
matches and forces to `armv7l`. Verified in the configured tree:
`CMAKE_SYSTEM_PROCESSOR:INTERNAL=armv7l`.

**`FORCE_32BIT` is not wanted here.** `webkitdirs.pm:2925-2941` adds it, `-m32`
and `CMAKE_LIBRARY_ARCHITECTURE=armv7-a+fp` when `--32-bit` is passed on an
arm64 host -- that is the *cross* case, on a 64-bit toolchain. A native armhf
clang needs none of it, and the multiarch paths it sets up are wrong for an
armhf rootfs.

**The wiki's known-good invocation no longer builds.** WTF asks clang to
vectorize loops it cannot vectorize on ARMv7 -- `SHA1::addUTF8Bytes`,
`StringImpl`, `URLParser`, `WTFString`, the simdutf paths -- and
`-Wpass-failed=transform-warning` under `-Werror` fails nine translation units
in WTF alone. Measured: `-mfpu=neon` and `-mfpu=neon-vfpv4` do not help, so it
is not a missing NEON, and clang's ARM `-march=` does not accept `+simd` or
`+neon` at all. `-Wno-pass-failed` silences exactly that diagnostic;
`--no-fatal-warnings` would have dropped `-Werror` for the whole build.

**The nvidia CDI spec and `--arch` do not mix**, as documented before, and it is
now enforced on our side too (`arch_has_gpu`): a 32-bit workspace gets no
`/dev/dri` and no derived NVIDIA mounts, because the NVIDIA userspace is
published for aarch64 only.

**The egress proxy bridge needs a python3 in the image.** It has one:
`Python 3.12.3`, and the workspace came up with its egress bridge working.

**`binfmt_misc` is not needed**, confirmed: this is native execution. The SDK's
`wkdev-cross-emulate` is for the case where it is not.

## What a 32-bit workspace covers, per branch

**There is no 32-bit ARM JIT on trunk any more.** This was found by trying to
turn it on, and it is worth writing down because every instinct says otherwise:

- `Source/JavaScriptCore/assembler/` holds ARM64, ARM64E, X86_64 and RISCV64
  assemblers. There is no `MacroAssemblerARMv7.h` and no `ARMv7Assembler.h`.
- offlineasm's `BACKENDS` (`offlineasm/backends.rb:36`) is `X86_64, ARM64,
  ARM64E, RISCV64, C_LOOP`. `Source/JavaScriptCore/CMakeLists.txt:304-320` sets
  `OFFLINE_ASM_BACKEND` for exactly those, so with `WTF_CPU_ARM` it is unset —
  forcing `-DENABLE_JIT=ON` makes the LLInt generator die with `undefined
  method 'split' for nil`.
- `PlatformEnable.h:724-727` settles it whatever CMake says: `#if
  !CPU(ADDRESS64)` undefines `ENABLE_JIT` and defines it to 0.

So an armhf `jsc-release` off trunk is a CLoop build: no JIT, no Wasm, no
sampling profiler, system malloc. That is not a defaulting mistake to work
around, it is the only 32-bit JSC trunk has. What it covers is the **CLoop
interpreter, the 32-bit value representation and the 32-bit address space**.

**2.48 is the other half, and it still works.** That branch predates the
removal, so an armhf workspace on it builds a real ARMv7 JIT — which is the
"test the 32-bit JIT tiers" story the wiki pages were written for, and the
reason this lane is worth more than trunk alone suggests. Two practical notes:

- A base snapshot carries exactly one ref (`refs/remotes/origin/main`), so the
  branch is one fetch away from inside the workspace, not already there:
  `git fetch origin webkitglib/2.48 && git checkout -b 2.48 FETCH_HEAD`.
  GitHub is on the egress allowlist, so this works from in there.
- `lib/arch.sh` deliberately forces neither `ENABLE_JIT` nor `ENABLE_C_LOOP`.
  Whatever the checked-out tree supports is what gets built, and
  `wk build <ws> jsc-release --dry-run` prints the flags it resolved. If a 2.48
  build needs the pair set explicitly, that belongs in `arch_cmake` keyed on
  nothing but the architecture -- so check what 2.48's own CMake defaults do
  before adding it.

## The first thing it found: a SIGBUS in bytecode decoding

A three-line reproducer, on the very first benchmark attempt:

```js
function f(o) { for (var k in o) o[k] = 3; return o; }
print(JSON.stringify(f({ a: 1, b: 2 })));
```

```
Program received signal SIGBUS, Bus error.
0x... in JSC::OpEnumeratorPutByVal::decode(unsigned char const*)
=> ldmia r5, {r2, r3, r5}      ; r5 = 0x0986beab  -- 3 mod 4
#1  JSC::CodeBlock::finishCreation(...)
#2  JSC::ProgramCodeBlock::create(...)
```

The same loop at top level is fine; inside a function it dies. Anything real
hits it — JetStream3's driver crashes before printing a line.

What it is: bytecode operands are packed, `decode()` reads them through a type
that promises 4-byte alignment, and clang merges three of those loads into one
`ldmia`. A multi-word load requires alignment even on a target where single
unaligned loads are free (`__ARM_FEATURE_UNALIGNED` is 1 here), so the merge is
only valid if the promise is true, and it is not.

`-mno-unaligned-access` does **not** fix it — verified by building both ways.
The flag tells clang unaligned access is unavailable; it does not stop clang
believing the pointer is aligned, which is what forms the `ldmia`. So the flag
is deliberately not in `lib/arch.sh`.

One environmental caveat before concluding anything about devices: a 32-bit ARM
kernel has an alignment-fault fixup handler (`/proc/cpu/alignment`, usually set
to fix-up-and-warn), and an arm64 kernel running an AArch32 process does not —
there is no such file in the workspace. The same binary on a Pi would likely
limp along with kernel fixups rather than dying. The bug is real either way;
its visibility is a property of this machine.

**It is debuggable in here, which is what matters more than fixing it.**
Verified in the armhf workspace: `lldb -b -o run -o bt` catches it as
`stop reason = signal SIGBUS: illegal alignment`, disassembles at the fault
(`-> ldm r5, {r2, r3, r5}`) and reads the registers; gdb gives the same
backtrace through `CodeBlock::finishCreation`; and the supported entry point,
`wk run <ws> --lldb`, launches the 32-bit jsc, stops at entry and hands over
the prompt. Both debuggers ship in the arm32 image (lldb 18.1.3, gdb 15.0.50).

## MiniBrowser on armhf: it builds and starts; on trunk it cannot run JS

Answered by building it, 2026-08-19.

- **`wk build <ws> wpe-release` succeeds** — 9,608 targets in 22 minutes,
  producing an *ELF 32-bit ARM* MiniBrowser. The link never came near the
  address-space wall the gold low-memory flags exist for, so that part of
  `lib/arch.sh` is doing its job silently.
- **It needed three features turned off**, each found by hitting it, and all
  three are the same problem: the only published arm32 image is eight months
  old and trunk has moved past its dependency set. There is no newer one — the
  registry has `24.04_arm32`, `_arm32_amd64` and `_arm32_arm64` and nothing
  else.

  | flag | why |
  |---|---|
  | `-DUSE_VULKAN=OFF` | volk is required for it (`OptionsWPE.cmake:270`) and the image has no volk |
  | `-DENABLE_WEB_RTC=OFF` | libwebrtc's CMake wants a `vpx` target the image does not build |
  | `-DENABLE_WPE_QT_API=OFF` | the image's Qt6 is *broken*, not absent: `Qt::QuickPrivate` points at a `qt6/QtQuick/6.4.2` include dir that does not exist, and it fails at the generate step |

  They live in `arch_cmake` and apply only to `--wpe`/`--gtk`, so a JSCOnly
  build is not told about options it has no use for. Delete them when a current
  arm32 image exists, or when building a branch contemporary with this one.
- **MiniBrowser launches** headless under software rendering and stays up.
- **A benchmark does not complete on trunk.** `wk bench <ws> jetstream3
  --config wpe-release` reaches "Executing: run-minibrowser ... --headless" and
  then goes silent: no browser process survives, and run-benchmark waits for a
  report that never arrives. The cause is the SIGBUS below, not the tooling —
  `WebKitBuild/WPE/Release/bin/jsc` from the same tree reproduces it in one
  line. The `wk bench` half of the path (preflight, the headless decision, the
  launch) all works.

So: on trunk, armhf MiniBrowser is a browser that starts and cannot run a real
page. 2.48 is the interesting combination — it is roughly contemporary with the
image, so the three feature disables above are likely unnecessary there too.

## Benchmarking from an armhf workspace

`wk bench` refuses gpu-class plans in here and says why (no NVIDIA userspace for
armhf, so the number would be llvmpipe's), and runs cpu-class plans -- JetStream,
Octane, Kraken, SunSpider, ARES-6 -- either in MiniBrowser or, for a JSCOnly
config, in the jsc shell via the benchmark's own `cli.js`. Results carry
`arch`, `class`, `runner` and `bench_host`, and `wk bench compare` warns across
any of them. See the header of `cmd/bench`.

## Remaining

- **An armhf workspace on 2.48** has not been tried, and it is the one that
  gets a working ARMv7 JIT. The fetch-and-checkout above is the whole setup;
  what is unknown is whether 2.48's CMake needs `ENABLE_JIT`/`ENABLE_C_LOOP`
  set by hand for a 32-bit target.
- **The trunk SIGBUS is left alone deliberately.** It is reproducible in three
  lines, it is debuggable in the workspace (above), and fixing it is WebKit
  work rather than tooling work. Anything that needs a running 32-bit JSC today
  uses 2.48.
- **`wk test` on armhf** has not been run. On trunk it will be a sea of these
  crashes, which is itself worth measuring once; on 2.48 it is the real test.
- **A browser port is built and starts** (above); what it cannot do is run JS
  on trunk. `gtk-release` is still untried.
- **A cpu-class benchmark run on armhf** has not completed, in either runner:
  the jsc shell dies in JetStream3's driver and the browser's web process dies
  the same way. The runner itself is verified on aarch64 in both modes, so this
  is a branch away rather than a tooling gap.
- **`--sysroot`** is reserved and refused, not implemented:
  `docs/HANDOFF-cross-compile.md`.
