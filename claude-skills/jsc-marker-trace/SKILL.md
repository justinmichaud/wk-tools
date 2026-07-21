---
name: jsc-marker-trace
description: Use to profile where time goes inside a JavaScriptCore code region during a real WebKit/MiniBrowser run, by bracketing sub-sections with samply text markers and splitting the trace into one pooled profile per section -- repeating a section across many iterations makes up for a modest sampling rate. The worked example is the garbage collector (parallel marking / sweeping / finalizers), but the same mechanism extends to any JSC region. Covers the WebKit marker patch, the capture and split scripts, macOS vs Linux differences, and what debug info is (and isn't) available.
user-invocable: true
allowed-tools:
  - Bash(~/.claude/skills/jsc-marker-trace/capture.sh:*)
  - Bash(~/.claude/skills/jsc-marker-trace/split-trace.py:*)
  - Bash(samply:*)
  - Bash(~/Development/samply/target/release/samply:*)
  - Bash(make release:*)
  - Bash(Tools/Scripts/build-webkit:*)
  - Bash(git rev-parse:*)
  - Bash(curl:*)
  - Bash(ls:*)
  - Bash(cat:*)
  - Bash(grep:*)
  - Bash(python3:*)
  - Bash(pkill:*)
---

# Per-section samply traces for JavaScriptCore

Goal: see where time goes inside a chosen JavaScriptCore code region during a real WebKit
run, broken down into named sub-sections. A short section gets only a handful of samples
per occurrence at ~1 kHz, so the trick is to **repeat the section many times, mark each
occurrence's wall-clock span, then pool the samples from every occurrence into one
profile** -- trading wall-clock spread for statistical depth. The worked example
throughout is the garbage collector (a fixed-interval full GC every N seconds, split into
ParallelMarking / Sweeping / Finalizers); the mechanism is general -- see "Extending to
another kind of tracing".

Two tools live in this skill's directory (`~/.claude/skills/jsc-marker-trace/`, symlinked to
`wk-tools/claude-skills/jsc-marker-trace/`): `capture.sh` (record) and
`split-trace.py` (split). They are not on PATH, so invoke them by full path as below;
`README.md` in the same directory is a shorter quickstart. `$SKILL` below is that
directory.

## Prerequisites

1. A **Release WebKit build with the GC text-marker patch** (see "The WebKit patch").
   - macOS: `make release` (JSC-only after a marker-only change: `make release
     SCHEME="Everything up to JavaScriptCore"`).
   - Linux/GTK: `Tools/Scripts/build-webkit --gtk --release`.
2. **samply** built from `~/Development/samply` (or on PATH). On macOS run `samply setup`
   once (codesign). On Linux set `sudo sysctl kernel.perf_event_paranoid=1`.
3. The workload served somewhere (e.g. the user's app at `http://localhost:8080`). Use
   the user's real server; do not roll your own unless asked.
4. A real display -- MiniBrowser renders, and a headless/occluded window throttles the
   page's timers.

## The reusable mechanism (samply text markers)

This whole workflow is built on a general, subsystem-agnostic substrate. Understand it
first -- extending to other kinds of tracing is just "emit differently named spans."

`ProfilerSupport` (`Source/JavaScriptCore/runtime/ProfilerSupport.{h,cpp}`) is a marker
sink, gated by `JSC_useTextMarkers`, that writes to `marker-<tid>-<pid>.txt` in
`JSC_textMarkersDirectory`. Its API lets any code label a time span:

- `markInterval(idNonNull, Category, startMonotonicTime, endMonotonicTime, name.utf8())`
  -- one `[start, end]` span; the write is dispatched to a background WorkQueue, so it is
  cheap on the hot path. Use when you already have both timestamps.
- `markStart(id, Category, name)` / `markEnd(id, Category, name)` -- paired; `markStart`
  stashes the time keyed by `(id, Category)`, `markEnd` emits the span. Use when entry and
  exit are at different call sites.
- `mark(id, Category, name)` -- an instant point marker.
- Timestamps are `MonotonicTime` (mach-absolute ns on macOS, `CLOCK_MONOTONIC` ns on Linux)
  -- the same base samply samples on, so no conversion is needed. `Category` only matters
  for the `markStart`/`markEnd` pairing table; `markInterval` ignores it (pass any).

The file format is one line per span: `<startNs> <endNs> <name>`. samply ingests it and
tags each as a `SimpleMarker` whose display name is `name` (stored in the marker's
`data.name`, not the `name` column). samply learns the file path on macOS from its
preload's `open`/`fopen` interpose, on Linux from the perf mmap that `ProfilerSupport`
performs; both need `useTextMarkers` and a real `textMarkersDirectory`.

`split-trace.py` is generic on top of this: it splits **any** interval markers whose
name starts with `--prefix` (default `"GC "`), pooling samples per distinct name. So a new
kind of trace = new marker names sharing a prefix + `--prefix <that prefix>`.

## What the GC patch adds on top (working-tree diagnostic; not upstreamed)

The GC time breakdown already existed (`Options::useFixedIntervalGCOnly` -> a
`JSRunLoopTimer` doing `collectNow(Sync, Full)` every `fixedIntervalGCPeriodMS`; a
`GCTimeBreakdownPhase` enum; `GCTimeBreakdownScope`). The marker additions:

- `Heap::recordGCPhaseMarker(phase, start, end)` (public, in `Heap.cpp`) buffers a span
  into a file-static `GCPhaseMarkerAccumulator`; at the end of `collectNow(Sync)` it
  merges each phase's spans (500us tolerance) and writes only the merged intervals via
  `ProfilerSupport::markInterval` (names `"GC ParallelMarking"` / `"GC Sweeping"` /
  `"GC Finalizers"`). Merging is essential: `drain()` runs thousands of times per GC, so
  one marker per call floods the file (142 MB in 6 min); merged coarse spans give ~2/phase.
- `GCTimeBreakdownScope` (HeapInlines.h) emits a marker in its destructor when
  `useTextMarkers` is set; a marker-only `GCMarkerScope` wraps `m_objectSpace.sweepBlocks()`
  in `Heap::sweepSynchronously` (the bulk block sweep isn't inside any counting scope).
- **Markers are decoupled from the timing/logging.** `useFixedIntervalGCOnly` no longer
  auto-enables `logGCTimeBreakdown`, and `performFixedIntervalGC` early-returns (silent GC)
  unless `logGCTimeBreakdown` is set. Reason: the breakdown counter does 3 per-block
  `MonotonicTime::now()` calls in `MarkedBlock::sweep`, which shows up as ~5% of
  `mach_absolute_time` in the sweep profile. Markers run on `useTextMarkers` alone with
  zero per-block timing. Set `JSC_logGCTimeBreakdown=1` if you also want the `[GC: ...ms]`
  numbers.

## Extending to another kind of tracing

To split a trace by some other set of code regions (compiler phases, a WebCore layout
pass, IPC handling, ...), reuse the mechanism above:

1. **Name the regions with a shared prefix**, e.g. `"Compile DFG"` / `"Compile FTL"`
   (prefix `"Compile "`).
2. **Emit a span around each region**, gated on `Options::useTextMarkers()`:
   - Coarse or infrequent region: call `ProfilerSupport::markInterval(...)` directly, or
     add a small RAII like `GCMarkerScope` (start in ctor, end in dtor).
   - Hot region (fires many times per unit of work): copy the `GCPhaseMarkerAccumulator`
     pattern -- buffer spans, then sort+merge (small time tolerance) and write only the
     merged intervals at a natural boundary. Otherwise the marker file explodes.
   - `ProfilerSupport` is JSC-only; for WebCore/WebKit regions, thread the timestamps to a
     JSC `markInterval` call, or lift a small marker helper into WTF.
3. **Build.** Marker-only changes confined to `.cpp` (plus a method decl) are a JSC-only
   rebuild and keep WebCore/WebKit/MiniBrowser ABI-compatible; a `Heap`/header layout
   change forces the full rebuild.
4. **Capture** as usual (`capture.sh` already sets `useTextMarkers`); the region
   just needs to run during the recording (you may not need `useFixedIntervalGCOnly` at
   all if your regions fire on their own).
5. **Split** with `--prefix "Compile "`.
6. **Thread selection is the one GC-specific bit of the splitter**: `is_gc_thread`
   (main + `Heap Helper Thread` + `JSC Heap Collector Thread`) fits GC. For another
   subsystem, pass `--all-threads`, or generalize `GC_THREAD_NAMES` / `is_gc_thread` to the
   threads your regions run on. The `IDLE_LEAVES` park-frame set is generic and can stay.

This is a JSC-only change (no `Heap` layout change), so JSC alone needs rebuilding;
WebCore/WebKit/MiniBrowser stay ABI-compatible.

## 1. Capture

```sh
SKILL=~/.claude/skills/jsc-marker-trace
# capture.sh <periodMS> <durationSec> <out.json.gz> [url] [rateHz]
"$SKILL"/capture.sh 30000 600 /tmp/jsc-trace/trace.json.gz http://localhost:8080
```

It exports the JSC options (`useFixedIntervalGCOnly`, `fixedIntervalGCPeriodMS`,
`useTextMarkers`, `useJITDump`, and `textMarkersDirectory`/`jitDumpDirectory` pointed at a
fixed dir), then runs `samply record --save-only --presymbolicate`, sleeps the duration,
closes the browser, and lets samply finalize. Env overrides: `WEBKIT_ROOT`, `WEBKIT_BUILD`,
`SAMPLY`, `TRACE_AUX` (default `/tmp/jsc-trace-aux`).

`textMarkersDirectory`/`jitDumpDirectory` MUST be a real directory: with the default empty
dir, `createDumpFile` uses `mkostemps` (random suffix) and samply's
`parse_marker_file_path` does `.parse::<u32>().unwrap()` and panics on the non-numeric
suffix. A real dir gives the exact `marker-<tid>-<pid>.txt` name.

## 2. Split and analyze

```sh
"$SKILL"/split-trace.py /tmp/jsc-trace/trace.json.gz --drop-idle --summary
```

Writes `trace-gc-{parallelmarking,sweeping,finalizers}.json.gz`. Each keeps only the
samples inside that section's marker spans, pooled across all GCs; the shared
stack/frame/func tables (with symbols and JIT frames) are copied verbatim.

- default: only the **GC threads** of the marker-bearing process (main thread + `Heap
  Helper Thread` + `JSC Heap Collector Thread`). `--all-threads` keeps everything.
- `--drop-idle`: drop parked-thread samples (futex/psynch/mach waits) so only active GC
  work remains. Expect ~70% of raw GC-window samples to be parked helper threads -- that
  is why pooling many GCs is needed.
- `--summary [--top N]`: per-section top self-time by `function:line`.

Open a section for the full call tree: `samply load trace-gc-parallelmarking.json.gz`.

Reading it: ParallelMarking is dominated by `SlotVisitor::{visitChildren,appendUnbarriered,
appendToMarkStack,noteLiveAuxiliaryCell}` and `MarkedBlock::{noteMarked,candidateAtomNumber}`
on the helper/collector threads; Sweeping by `MarkedBlock::Handle::specializedSweep<...>`,
destructors (`~Node`, `~CallLinkInfo`) and libpas frees on the main thread; Finalizers by
`AccessCase::visitWeak`, `Structure::finalizeUnconditionally`, `finalizeCodeBlockEdge`,
`WeakBlock::reap`. Active-sample ratios track the time breakdown (marking >> sweeping >
finalizers).

## macOS vs Linux

The scripts branch on `uname -s`.

| | macOS (Apple WebKit) | Linux (GTK WebKit) |
|---|---|---|
| MiniBrowser | `WebKitBuild/Release/MiniBrowser.app/Contents/MacOS/MiniBrowser` | `WebKitBuild/GTK/Release/bin/MiniBrowser` |
| libraries | `DYLD_FRAMEWORK_PATH` (+ `__XPC_` copy) | `LD_LIBRARY_PATH=$DIR/lib` |
| JSC opts -> web process | `__XPC_JSC_*` (libxpc forwards only `__XPC_`-prefixed env to the XPC service) | plain `JSC_*` + `WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1` so the child inherits env |
| how samply finds aux files | its preload interposes `open`/`fopen`; samply also forwards the preload + bootstrap via `__XPC_`, so it reaches WebContent (needs SIP/AMFI relaxed -- there is no in-tree macOS sandbox-disable env var) | perf mmap events: `ProfilerSupport`/`PerfLog` `mmap` the marker/jitdump files on Linux, and samply reads the mmap'd paths |
| stop patterns | `com.apple.WebKit.{WebContent,GPU,Networking}` | `WebKit{Web,GPU,Network}Process` |
| GC thread names | full (`"Heap Helper Thread"`) | truncated to 15 chars by `prctl(PR_SET_NAME)` (`"Heap Helper Thr"`) -- the splitter matches by shared prefix |
| idle-wait leaves | `__psynch_cvwait`, `semaphore_wait_*`, `mach_msg2_trap` | `futex`, `__futex_abstimed_wait_*`, `poll`, `nanosleep` -- both sets are in the splitter |
| prereq | `samply setup` | `perf_event_paranoid <= 1` |

## Debug info: what you get

- **C++ line numbers: yes.** A Release build keeps DWARF in the `.o` files (macOS: via the
  binary's OSO debug map; Linux: in the `.so`/`.debug`), and `--presymbolicate` resolves
  `frameTable.line`. The splitter shows `function:line`.
- **C++ source file paths: no** (offline). samply's `--presymbolicate` leaves
  `funcTable.source` as a stale JS name-hash for C++ funcs; generating a framework dSYM
  does not fix it. Function+line is usually enough to find the source.
- **JS/JIT source lines: no.** JSC can emit them (`JSC_useSourceCodeDump=1` /
  `useIRDump=1`), but samply's `jitdump_manager.rs` deliberately skips `JIT_CODE_DEBUG_INFO`
  records. Would need a samply patch; moot for GC sections, which run C++.

## Gotchas

- Marker phase name lives in `markers.data[j]["name"]` (a stringArray index), NOT the
  `markers.name` column (which samply sets to the type `"SimpleMarker"`). The splitter
  reads `data.name`.
- samply has no per-thread sampling filter: `--main-thread-only` drops the marking helper
  threads, and `-p PID` attaches without the preload so markers/jitdump vanish. Filter in
  the splitter instead. The dominant noise thread (`RemoteAudioDestinationProxy render
  thread`, ~450k samples) lives *inside* the WebContent GC process, so name-based
  GC-thread filtering -- not process-scoping -- is what removes it.
- Build gotcha (macOS full build): `make release` can fail on the WebKitLegacy phase
  script "Work around rdar://109484516" under the Xcode user-script sandbox
  ("Sandbox: rm deny(1) file-write-unlink"). Workaround: `make release
  ARGS='ENABLE_USER_SCRIPT_SANDBOXING=NO'` (invalidates the cache -> broad recompile), or
  just build the JSC-only scheme.
- samply samples on-CPU threads, not every thread at the full rate, so a 10-min 1 kHz
  run of a mostly-idle app is ~1.2M samples (a ~7 MB gzipped profile), not tens of millions.
