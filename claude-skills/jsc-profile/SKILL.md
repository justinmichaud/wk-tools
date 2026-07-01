---
name: jsc-profile
description: Use to profile a JavaScriptCore run — a microbenchmark or a JetStream3 subtest — to find where time goes and root-cause a regression. Picks the tool by where the cost is: JSC's built-in sampling + bytecode profilers for generated JS code (FTL/DFG/Baseline), or samply (~/Development/samply) for C++ engine code. Covers reading the tier breakdown, dumping/inspecting the bytecode profile with display-profiler-output, and diffing baseline-vs-patched profiles.
user-invocable: true
allowed-tools:
  - Bash(Tools/Scripts/display-profiler-output:*)
  - Bash(make release:*)
  - Bash(git rev-parse:*)
  - Bash(ls:*)
  - Bash(cat:*)
  - Bash(grep:*)
  # Native profilers (C++ engine code):
  - Bash(samply:*)
  - Bash(perf:*)
  - Bash(valgrind:*)
  - Bash(callgrind_annotate:*)
  # Linux / 32-bit container path:
  - Bash(wkdev-enter:*)
  - Bash(taskset:*)
---

# Profiling a JSC run (root-causing where time goes)

Find the hot code in a `jsc` run — a microbenchmark or a JetStream3 subtest run headlessly via
`cli.js` — and, for a regression, find what *changed* between baseline and patched. Pick the tool by
where the cost is: **generated JS** (Step 1 tier breakdown says FTL/DFG/Baseline/RegExp) uses JSC's own
profilers (Steps 1-2); **C++ engine code** (says C/C++ or Host) uses samply (Step 3). The Step-1 tier
breakdown makes that call, so start there.

Always profile a **Release** build. On macOS `jsc` needs its own framework path or it links the system
JavaScriptCore:

```bash
DIR="$WEBKIT_ROOT/WebKitBuild/Release"
DYLD_FRAMEWORK_PATH="$DIR" "$DIR/jsc" …            # macOS; omitting it -> dyld: Symbol not found: __Z20WTFCrashWithInfoImpl…
LD_LIBRARY_PATH="$DIR/lib" "$DIR/bin/jsc" …        # Linux JSCOnly (in-place; DYLD_* is macOS-only)
```

Run `jsc` directly, not `Tools/Scripts/run-jsc` (it adds `--useDollarVM=1` and may wrap jsc in `lldb`,
perturbing the measurement).

To find *where* something is emitted (a store, a write barrier, a bounds check) across the DFG/FTL
backend, delegate the trace to a subagent and keep only its answer — that search touches many files you
do not need in your main context while reasoning about the profile.

Clean up after profiling: kill the `jsc` and any `samply` / profiler-server process you started — a
leftover or hung profiler slows or times out later runs. A profiled run that times out (`exit 124`)
produces no results JSON, so set a timeout and shrink the workload (fewer iterations, one subtest)
rather than waiting on a stuck run.

> ## ⚠️ Significance scale (keep in mind while attributing)
> A **0.1% statistically significant** regression in an **overall** benchmark score is **HUGE**;
> **>1%** on a non-noisy subtest/microbenchmark is significant. Profiling is sampling — its own counts
> are noisy and it perturbs timing slightly, so a profile **localizes** a regression you've already
> confirmed statistically (via `jsc-jetstream-compare` / `jsc-microbenchmark`); it does not by itself
> prove one. Confirm the size with timing first, then profile to explain it.

## What to profile

A microbenchmark: just the script (`jsc-microbenchmark` skill). A JetStream3 subtest: run one subtest
headlessly via `cli.js` so the profiler sees only that work (profile the headless jsc path, not the
MiniBrowser run):

```bash
cd "$WEBKIT_ROOT/PerformanceTests/JetStream3"
DIR="$WEBKIT_ROOT/WebKitBuild/Release"
DYLD_FRAMEWORK_PATH="$DIR" "$DIR/jsc" <profiler-flags> cli.js -- bigint-noble-ed25519
```

## Step 0 — Localize to a tier by re-timing (cheapest first cut for a regression)

Before reaching for a profiler, re-time the same workload with tiers switched off; where the ratio
moves tells you which tier owns the regression. This is coarse but free, and it saves you from
profiling the wrong layer.

```bash
"$DIR/bin/jsc" bench.js                # default top tier (DFG on 32-bit, FTL on 64-bit)
"$DIR/bin/jsc" --useDFGJIT=0 bench.js  # Baseline JIT + LLInt only
"$DIR/bin/jsc" --useJIT=0 bench.js     # LLInt only (pure interpreter / C++)
```

A regression that **persists with `--useJIT=0`** is in the C++ runtime (parser, GC, a builtin), not
codegen — go straight to Step 3 (samply/callgrind) and skip DFG disassembly. One that is **equal with
`--useDFGJIT=0` but appears at the top tier** is DFG codegen — Step 2.

## Step 1 — Sampling profiler: where, and which tier (do this first)

`jsc --sample` runs the built-in sampling profiler and at exit prints **top functions**, a **tier
breakdown**, and the **hottest bytecodes**.

```bash
DIR="$WEBKIT_ROOT/WebKitBuild/Release"
DYLD_FRAMEWORK_PATH="$DIR" "$DIR/jsc" --sample mybench.js
DYLD_FRAMEWORK_PATH="$DIR" "$DIR/jsc" --sample --samplingProfilerTopFunctionsCount=30 --samplingProfilerTopBytecodesCount=60 mybench.js   # deeper report (defaults 12 / 40)
# --sample == JSC_collectExtraSamplingProfilerData=true; underlying flag JSC_useSamplingProfiler=true.
```

Read the output:
- **Top functions** — `<numSamples  'name#hash:sourceID'>`. The hot JS functions.
- **Tier breakdown** — % of samples in `LLInt / Baseline / DFG / FTL / IPInt / BBQ / OMG / Wasm /
  Host / C/C++ / RegExp`. **This decides the next step:**
  - Mostly **FTL / DFG / Baseline** (or RegExp) → cost is in **generated JS** → Step 2 (bytecode
    profiler) pins the exact CodeBlock/bytecode and how it's compiled.
  - Mostly **C/C++** (or Host) → cost is in **engine C++** (runtime functions, GC, the compiler
    itself) → Step 3 (samply) gives native stacks.
- **Hottest bytecodes** — `'name#hash:JITType:bc#N <-- caller'`. Pinpoints the hot bytecode and the
  tier it ran in. A function showing lots of **Baseline/LLInt** where you expected FTL is a tier-up /
  OSR-exit problem — confirm with Step 2's `log`.

**For a regression:** `--sample` **both** builds and compare. Tells: a **tier shift** (more
Baseline/LLInt or more `C/C++` on patched = lost tier-up or a new slow path), a new hot function, or
more samples in a runtime helper.

## Step 2 — Bytecode profiler: exact CodeBlock & bytecode (generated code)

JSC's `Profiler::Database` records per-CodeBlock, per-bytecode execution counts split by tier, plus the
compilation/OSR-exit/jettison log. Best for "which bytecode is hot and is it reaching FTL".

```bash
DIR="$WEBKIT_ROOT/WebKitBuild/Release"
DYLD_FRAMEWORK_PATH="$DIR" JSC_useProfiler=true JSC_dumpProfilerDataAtExit=true "$DIR/jsc" mybench.js
PFILE=$(ls -t /tmp/JSCProfile-*.json | head -1)     # per-PID name JSCProfile-<pid>-<n>.json; grab the newest
Tools/Scripts/display-profiler-output "$PFILE"
printf 'summary\nquit\n' | Tools/Scripts/display-profiler-output "$PFILE"   # non-interactive: feed commands on stdin
```

`display-profiler-output` is an interactive Ruby tool (readline prompt). Commands:
- `summary` (`s`) — per-CodeBlock counts as **Base/DFG/FTL/FTLOSR**. The headline: which code blocks
  ran most and in which tier.
- `bytecode <hash>` (`b`) — bytecode listing **with per-bytecode execution counts** → the hot bytecode
  within a function (a bounds check, a `get_by_val`, an `add` with overflow check).
- `log <hash>` (`l`) — compilations, **OSR exits**, and **jettisons** for the CodeBlock. Confirms a
  tier-up regression: extra exits/jettisons on patched = the change made speculation fail or the code
  un-FTL-able.
- `source <hash>` — source for a CodeBlock. `profiling <hash>` (`p`) — value/type predictions.
- `full` (`f`) — summary with more detail (`full exits` / `full compiles` focus those). Also
  `inlines <hash>`, `events`, `display`, `counts`, `help`.

**For a regression:** dump both builds and diff — did a CodeBlock's **FTL** count drop and **Base/DFG**
rise (`summary`)? New **OSR exits / jettisons** on patched (`log`)? Did a specific bytecode gain count,
or an eliminated check reappear (`bytecode`)?

## Step 3 — samply: C++ engine code

When Step 1 says the cost is in **C/C++** (runtime functions, GC, or the JIT compiler itself — e.g. a
*compile-time* regression), use **samply** (native sampling profiler, Firefox-profiler UI). samply sees
native frames and the whole process (GC threads, compiler threads, libc) but labels JIT'd JS frames
poorly — the mirror image of JSC's own profilers, which label JS/bytecode precisely but miss C++
internals. Supported on **x86_64 and aarch64**; for **32-bit ARM** see the box below.

```bash
SAMPLY=~/Development/samply/target/release/samply
"$SAMPLY" setup     # once on macOS: codesign samply so it can attach. (May need sudo.)

DIR="$WEBKIT_ROOT/WebKitBuild/Release"
DYLD_FRAMEWORK_PATH="$DIR" "$SAMPLY" record -- "$DIR/jsc" mybench.js                                   # record + open UI (macOS; DYLD_* applies, samply launches jsc as a child)
DYLD_FRAMEWORK_PATH="$DIR" "$SAMPLY" record --save-only -o /tmp/prof-patched.json -- "$DIR/jsc" mybench.js   # save for later / for diffing
"$SAMPLY" load /tmp/prof-patched.json    # opens UI, resolves symbols
```

- A **Release** build has enough symbols for useful C++ stacks. A RelWithDebInfo/local-debug-info build
  gives better names for a deep dive into one phase; a slow Debug build never for timing attribution.
- **For a regression:** `record --save-only` on **both** builds, `load` each, compare the hot C++
  functions (inverted call tree / function list). A function that grew its share on patched is the
  cost — catches compile-time regressions (the optimizer phase shows up by name) and runtime-helper
  regressions (a slow path the change started hitting).

> ## ⚠️ 32-bit ARM (armv7/armhf): use JSC's own profilers first; samply needs a patched build (verified 2026-06)
> **JSC's own profilers run natively on armhf and need no patched samply** — reach for them first when
> you need tier/where, not a UI. `--sample` (Step 1) is time-based but labels JIT'd frames as
> `(unknown C PC)` / `Tier breakdown: C/C++ 100%` (no JIT symbolication), so read it as time-share. The
> bytecode profiler (Step 2) works fully but writes `JSCProfile-<pid>-<n>.json` **into the cwd** (not
> `/tmp`; the "could not save profiler output." line is a benign secondary-path failure — grab the
> newest/largest cwd file), read with `ruby Tools/Scripts/display-profiler-output <file>`.
>
> **samply on 32-bit ARM needs a build from the arm32-support branches.** Stock samply's unwinder
> `framehop` only implements `CacheNative`/`UnwinderNative` for `aarch64`/`x86_64` (released crate and
> upstream `main` fail with `could not find CacheNative/UnwinderNative in framehop`, E0412/E0433). The
> prebuilt aarch64 tarball won't exec in the armhf container (wrong ELF interpreter), and a host
> aarch64 samply attaching by `-p PID` has no arm32 unwinder. It works once built from Justin Michaud's
> branches (unmerged as of 2026-06):
>
> 1. **Toolchain:** the container's default Rust may be too old for samply's `edition2024`
>    (`rustc 1.84` fails). `rustup install stable` (gets ≥1.92), build with `cargo +stable`.
> 2. **Check out three sibling repos** (samply pins `framehop` and `linux-perf-data` as `../../<name>`
>    path deps — clone them as *siblings* with those exact dir names):
>    ```bash
>    git clone -b eng/a32-support https://github.com/justinmichaud/samply.git    samply
>    git clone -b eng/a32-support https://github.com/justinmichaud/framehop.git  framehop   # PR mstange/framehop#43: arm32 unwinder
>    git clone https://github.com/mstange/linux-perf-data.git                    linux-perf-data  # upstream main, unmodified
>    cd samply && cargo +stable build --release -p samply   # ~2 min on arm; binary: target/release/samply
>    ```
>    (samply PR is `mstange/samply#618`; if these branches are gone/merged, prefer merged upstream and
>    skip the fork.)
> 3. **Run every samply invocation under `linux32`** (sets the uname personality to 32-bit so samply
>    picks the arm code paths) and pin cores: `linux32 taskset -c 2-9 "$SAMPLY" record …`.
> 4. **Lower `perf_event_paranoid` to ≤1** (default 2 → samply errors out): the box-owner runs
>    `echo 1 | sudo tee /proc/sys/kernel/perf_event_paranoid` on the **host** (host-wide kernel knob;
>    the agent can't and shouldn't silently weaken it). Restore the prior value after.
> 5. **Symbolicate JIT'd JS frames** with jsc flags `--logJITCodeForPerf=1 --jitDumpDirectory=/tmp`
>    (writes a perf jitdump samply ingests) — otherwise JIT frames are unnamed; with them you get
>    `DFG: _findKeyEntry` etc.
>
> Recording + viewing (JSCOnly jsc is in-place — **no `DYLD_*`/`LD_LIBRARY_PATH` needed on Linux**):
> ```bash
> SAMPLY=…/samply/target/release/samply
> cd "$WEBKIT_ROOT/PerformanceTests/JetStream3"
> linux32 taskset -c 2-9 "$SAMPLY" record --save-only -o /tmp/prof_a.json.gz \
>   -- "$JSC" --useDFGJIT=1 --useConcurrentJIT=0 --logJITCodeForPerf=1 --jitDumpDirectory=/tmp \
>      -e 'var testList=["hash-map"];' cli.js          # one subtest; testList global, NOT argv
> # …record the other build/config to prof_b.json.gz the same way…
> linux32 "$SAMPLY" load --no-open --port 3001 /tmp/prof_a.json.gz   # prints a profiler.firefox.com URL
> ```
> Open the printed URL in a real browser. The `wkdev` container runs `--network host`, so a server on
> `127.0.0.1:3001` inside it is reachable from the host's Firefox — start one `samply load` per profile
> on different ports and open both tabs (or use Firefox Profiler's "Compare"). `samply load` serves the
> self-contained `--save-only` profile, so the host's aarch64 samply can also `load` an arm32-recorded
> profile if you'd rather serve from the host.
>
> **Diffing two profiles in the UI** (samply has no `diff` subcommand — only `record`/`load`/`import`):
> use Firefox Profiler's **Compare** view. Its inputs run each URL through `getProfileFetchUrl`, which
> **only accepts `from-url` or `public` profiler URLs** — a raw `http://127.0.0.1:PORT/<hash>/profile.json`
> throws *"Only public uploaded profiles are supported"*. **Each compare input URL must also select a
> thread**, or the merge fails with *"No thread has been selected in profile 0"* (`merge-compare.ts`):
> the merger reads `selectedThreads` from each profile's own URL state, and a bare `from-url` link
> selects none. So each entry is a full *view* URL with a tab segment and `?thread=<N>` (N = the thread
> index to diff, e.g. the `jsc` main thread — the highest-sample thread named `jsc` in the profile
> JSON). `thread` uses the profiler's uint-set scheme, but for a single index <32 that's the decimal
> number. Build the preloaded compare URL (query array is `arrayFormat:'bracket'`):
> `https://profiler.firefox.com/compare/?profiles[]=<enc(viewA)>&profiles[]=<enc(viewB)>` where each
> `viewX` = `https://profiler.firefox.com/from-url/<urlencode(http://127.0.0.1:PORT/<hash>/profile.json)>/calltree/?v=<CURRENT_URL_VERSION>&thread=<N>`,
> then that whole string url-encoded again as the `profiles[]` value. (Easiest: open each profile
> singly, click the thread, copy the address-bar URL — it already has the right `thread=`/`v=` — then
> feed those two to compare.) Both `samply load` servers must stay up (the page fetches each live).
> Then pick the synthetic **"Diff"** track.
>
> **Tells in the comparison:** the `useConcurrentJIT=1` profile shows extra **`JITWorker`** threads
> (the concurrent DFG compilers) absent under `=0`. If Step 2 shows identical tier counts + 0 extra OSR
> exits/jettisons but wall time differs, look at the **main thread's self-time in the `DFG:` frames**:
> more self-time there = worse codegen (same tier), vs time stuck in runtime/GC/contention = a
> scheduling/compile-window effect.
>
> **Note perf availability:** on Linux, `perf` may need a kernel package (`linux-tools-$(uname -r)`);
> when it's unavailable, callgrind and samply are the fallback.

## Deciding generated-vs-C++ at a glance

| Step-1 tier breakdown dominated by | Root cause is in | Use |
|---|---|---|
| FTL / DFG / Baseline / LLInt / RegExp | generated JS code | Step 2 (bytecode profiler) + Step 1 bytecodes |
| C/C++ / Host | engine C++ (runtime, GC, compiler) | Step 3 (samply) |
| split / unclear | both — or diffuse | do both; if neither localizes, suspect a **diffuse** codegen change |

A **diffuse** regression (a small same-direction shift spread across many functions) shows as: no
single hot function moving, a slight whole-program tier/`C/C++` shift, a category breakdown weighted
toward startup. That's a real finding — report "diffuse, not localizable to one loop" rather than
forcing a culprit.

## Determinism / hygiene

- Release build; one subtest or one microbenchmark at a time; AC power, quiet machine, `caffeinate`.
- Sampling profilers perturb timing — use them to **localize**, confirm magnitude with un-profiled
  timing runs (`jsc-microbenchmark` / `jsc-jetstream-compare`).
- For regressions, **always profile both builds the same way** and diff; a single profile shows hot
  code, not *changed* code.
- Quote args; pass lists literally (the Bash tool is zsh — unquoted `$list` does not word-split).
- Keep dumped profiles under `/tmp/` so the user can inspect them.
