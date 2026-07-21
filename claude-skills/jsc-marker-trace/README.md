# Per-section samply traces for JavaScriptCore

Profile a WebKit MiniBrowser run and split the samply trace into one pooled profile per
marked section, so a repeated section's call tree has enough samples despite a modest
sampling rate. The worked example here is the garbage collector (one fixed-interval full
GC every N seconds, split into ParallelMarking / Sweeping / Finalizers); see SKILL.md
"Extending to another kind of tracing" for other regions.

Both scripts live in this directory (the `jsc-marker-trace` skill); they are not on PATH,
so the examples call them by full path. `SKILL=~/.claude/skills/jsc-marker-trace`.

## Prerequisites

- A **Release WebKit build** with the GC text-marker patch (adds `recordGCPhaseMarker`
  driven by `JSC_useTextMarkers`; `Heap.cpp`, `Heap.h`, `HeapInlines.h`).
  macOS: `make release`. Linux/GTK: `Tools/Scripts/build-webkit --gtk --release`.
- **samply** built from `~/Development/samply` (or on `PATH`).
- The workload served somewhere (e.g. `http://localhost:8080`).
- A display (MiniBrowser renders; headless throttles timers).
- Linux only: `sudo sysctl kernel.perf_event_paranoid=1` (samply uses perf).

## 1. Capture

```sh
# <periodMS> <durationSec> <out.json.gz> [url] [rateHz]
"$SKILL"/capture.sh 30000 600 /tmp/jsc-trace/trace.json.gz http://localhost:8080
```

Sets `useFixedIntervalGCOnly` (a full GC every `periodMS`, all other GC/incremental
sweeping blocked), `useTextMarkers` (coarse per-phase marker spans), and `useJITDump`,
then records with `samply record --presymbolicate`. Env overrides: `WEBKIT_ROOT`,
`WEBKIT_BUILD`, `SAMPLY`, `TRACE_AUX`.

The marker/jitdump files land in `TRACE_AUX` (default `/tmp/jsc-trace-aux`). On macOS
the web process is XPC, so JSC options are forwarded with the `__XPC_` prefix and the
web-process sandbox must allow the samply preload (SIP/AMFI relaxed). On Linux the web
process is a normal child and `WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1` lets it
inherit the options and write the aux files.

## 2. Split

```sh
"$SKILL"/split-trace.py /tmp/jsc-trace/trace.json.gz --drop-idle --summary
```

Writes `trace-gc-{parallelmarking,sweeping,finalizers}.json.gz`, one per section,
each containing only the samples inside that section's marker spans (pooled across all
GCs). Defaults to the marker-bearing process's GC threads (main + `Heap Helper Thread`
+ `JSC Heap Collector Thread`); `--all-threads` keeps everything. `--drop-idle` strips
parked-thread samples. `--summary` prints per-section top self-time by `function:line`.

Open a section in the profiler for the full call tree:

```sh
samply load /tmp/jsc-trace/trace-gc-parallelmarking.json.gz
```

## Notes / limits

- C++ frames carry DWARF **line numbers** (`function:line` in the summary). Source
  **file paths** aren't populated by `--presymbolicate` (samply limitation); a
  framework dSYM does not fix it.
- **JS/JIT** source lines are unavailable: stock samply skips the jitdump
  `JIT_CODE_DEBUG_INFO` records. Irrelevant for GC sections (they run C++).
- The breakdown timing numbers (`[GC: ... sweeping Xms]`) are opt-in via
  `JSC_logGCTimeBreakdown=1`; that adds heavy per-block `MonotonicTime::now()` timing,
  so it is off by default to keep the sweep profile clean.
