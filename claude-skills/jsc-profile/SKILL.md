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
---

# Profiling a JSC run (root-causing where time goes)

Find the hot code in a `jsc` run — a **microbenchmark** or a **JetStream3 subtest run headlessly via
`cli.js`** — and, for a regression, find what *changed* between baseline and patched. The right tool
depends on whether the cost is in **generated JS code** or **C++ engine code**; the **tier breakdown**
from the sampling profiler tells you which, so start there.

Always profile a **Release** build (profiling Debug is misleading). `jsc` needs its own framework:
`DYLD_FRAMEWORK_PATH="$DIR" "$DIR/jsc" …`. Without it you get `dyld: Symbol not found:
__Z20WTFCrashWithInfoImpl…` (it linked the system JavaScriptCore). **Don't use `Tools/Scripts/run-jsc`
here** — it adds `--useDollarVM=1` and may wrap jsc in `lldb`, perturbing what you measure.

> ## ⚠️ Significance scale (keep in mind while attributing)
> A **0.1% statistically significant** regression in an **overall** benchmark score is **HUGE**;
> **>1%** on a non-noisy subtest/microbenchmark is significant. Profiling is sampling — its own counts
> are noisy and it perturbs timing slightly, so a profile **localizes** a regression you've already
> confirmed statistically (via `jsc-jetstream-compare` / `jsc-microbenchmark`); it does not by itself
> prove one. Confirm the size with timing first, then profile to explain it.

## What to profile

- **A microbenchmark:** just the script (`jsc-microbenchmark` skill).
- **A JetStream3 subtest, headless:** run one subtest via `cli.js` so the profiler sees only that work:
  ```bash
  cd "$WEBKIT_ROOT/PerformanceTests/JetStream3"
  DIR="$WEBKIT_ROOT/WebKitBuild/Release"
  DYLD_FRAMEWORK_PATH="$DIR" "$DIR/jsc" <profiler-flags> cli.js -- bigint-noble-ed25519
  ```
  (The browser MiniBrowser run is not the thing to profile — use the headless jsc path.)

## Step 1 — Sampling profiler: where, and which tier (do this first)

`jsc --sample` runs with the built-in sampling profiler and, at exit, prints **top functions**, a
**tier breakdown**, and the **hottest bytecodes**. The tier breakdown is the key decision.

```bash
DIR="$WEBKIT_ROOT/WebKitBuild/Release"
DYLD_FRAMEWORK_PATH="$DIR" "$DIR/jsc" --sample mybench.js
# Tune report depth (defaults: 12 functions, 40 bytecodes):
#   --samplingProfilerTopFunctionsCount=30 --samplingProfilerTopBytecodesCount=60
# (`--sample` == JSC_collectExtraSamplingProfilerData=true; underlying flag JSC_useSamplingProfiler=true.)
```

Read the output:
- **Top functions** — `<numSamples  'name#hash:sourceID'>`. The hot JS functions.
- **Tier breakdown** — % of samples in `LLInt / Baseline / DFG / FTL / IPInt / BBQ / OMG / Wasm /
  Host / C/C++ / RegExp`. **This decides the next step:**
  - Mostly **FTL / DFG / Baseline** (or RegExp) → the cost is in **generated JS** → go to Step 2
    (bytecode profiler) to pin the exact CodeBlock/bytecode and how it's compiled.
  - Mostly **C/C++** (or Host) → the cost is in **engine C++** (runtime functions, GC, the compiler
    itself) → go to Step 3 (samply) for native stacks.
- **Hottest bytecodes** — `'name#hash:JITType:bc#N <-- caller'`. Pinpoints the hot bytecode and the
  tier it ran in. A function showing lots of **Baseline/LLInt** samples that you expected to be FTL
  is a tier-up / OSR-exit problem — confirm with Step 2's `log`.

**For a regression:** run `--sample` on **both** builds and compare. Tells: a **tier shift** (more
Baseline/LLInt or more `C/C++` on patched = lost tier-up or a new slow path), a new hot function, or
more samples in a runtime helper.

## Step 2 — Bytecode profiler: exact CodeBlock & bytecode (generated code)

JSC's `Profiler::Database` records per-CodeBlock, per-bytecode execution counts split by tier, plus
the compilation/OSR-exit/jettison log. Best for "which bytecode is hot and is it reaching FTL".

```bash
DIR="$WEBKIT_ROOT/WebKitBuild/Release"
DYLD_FRAMEWORK_PATH="$DIR" JSC_useProfiler=true JSC_dumpProfilerDataAtExit=true "$DIR/jsc" mybench.js
# Writes a JSON to /tmp/JSCProfile-<pid>-<n>.json  (NOT a fixed name — grab the newest):
PFILE=$(ls -t /tmp/JSCProfile-*.json | head -1)
Tools/Scripts/display-profiler-output "$PFILE"
```

`display-profiler-output` is an **interactive** Ruby tool (readline prompt; feed commands on stdin for
non-interactive use, e.g. `printf 'summary\nquit\n' | display-profiler-output "$PFILE"`). Commands:
- `summary` (`s`) — per-CodeBlock execution counts as **Base/DFG/FTL/FTLOSR**. The headline view: which
  code blocks ran most and in which tier.
- `full` (`f`) — like summary with more detail; `full exits` / `full compiles` focus those.
- `source <hash>` — source for a CodeBlock (hashes come from `summary`).
- `bytecode <hash>` (`b`) — bytecode listing **with per-bytecode execution counts** → the hot bytecode
  within a function (e.g. a bounds check, a `get_by_val`, an `add` with overflow check).
- `profiling <hash>` (`p`) — internal profiling data (value/type predictions) for the CodeBlock.
- `log <hash>` (`l`) — compilations, **OSR exits**, and **jettisons** for the CodeBlock. Use this to
  confirm a tier-up regression: extra exits/jettisons on patched = the change made the speculation
  fail or the code un-FTL-able.
- `inlines <hash>`, `events`, `display`, `counts`, `help`.

**For a regression:** dump the profile on both builds and diff:
- `summary` — did a CodeBlock's **FTL** count drop and **Base/DFG** rise (lost tier-up)?
- `log` — new **OSR exits / jettisons** on patched?
- `bytecode` — did a specific bytecode gain count / a check that was eliminated reappear?

## Step 3 — samply: C++ engine code

When the cost is in **C/C++** (runtime functions, GC, or the JIT compiler itself — e.g. profiling a
*compile-time* regression), use **samply** (native sampling profiler, Firefox-profiler UI).

```bash
SAMPLY=~/Development/samply/target/release/samply
"$SAMPLY" setup     # once on macOS: codesign samply so it can attach. (May need sudo.)

DIR="$WEBKIT_ROOT/WebKitBuild/Release"
# Record + open the UI in the browser:
DYLD_FRAMEWORK_PATH="$DIR" "$SAMPLY" record -- "$DIR/jsc" mybench.js
# Or save for later / for diffing two builds without a live UI:
DYLD_FRAMEWORK_PATH="$DIR" "$SAMPLY" record --save-only -o /tmp/prof-patched.json -- "$DIR/jsc" mybench.js
"$SAMPLY" load /tmp/prof-patched.json    # opens UI, resolves symbols
```

Notes:
- A **Release** build has enough symbols for useful C++ stacks; for deep dives into a specific phase a
  RelWithDebInfo/local-debug-info build gives better names, but don't profile a slow Debug build for
  timing attribution.
- `DYLD_FRAMEWORK_PATH` still applies (samply launches `jsc` as a child).
- **For a regression:** `record --save-only` on **both** builds, `load` each, and compare the hot C++
  functions (the inverted call tree / function list). A function that grew its share on patched is the
  cost. Good for compile-time regressions (the optimizer phase shows up by name) and runtime-helper
  regressions (a slow path that the change started hitting).
- The headline trade-off vs JSC's own profilers: **samply sees native frames and the whole process**
  (GC threads, compiler threads, libc) but labels JIT'd JS frames poorly; the JSC profilers label JS/
  bytecode precisely but don't see C++ internals. Use the Step-1 tier breakdown to pick.

## Deciding generated-vs-C++ at a glance

| Step-1 tier breakdown dominated by | Root cause is in | Use |
|---|---|---|
| FTL / DFG / Baseline / LLInt / RegExp | generated JS code | Step 2 (bytecode profiler) + Step 1 bytecodes |
| C/C++ / Host | engine C++ (runtime, GC, compiler) | Step 3 (samply) |
| split / unclear | both — or diffuse | do both; if neither localizes, suspect a **diffuse** codegen change |

A **diffuse** regression (a small same-direction shift spread across many functions) often shows as: no
single hot function moving, a slight whole-program tier/`C/C++` shift, and a category breakdown weighted
toward startup. That's a real finding — report "diffuse, not localizable to one loop" rather than
forcing a culprit.

## Determinism / hygiene

- Release build; one subtest or one microbenchmark at a time; AC power, quiet machine, `caffeinate`.
- Sampling profilers perturb timing — use them to **localize**, confirm magnitude with un-profiled
  timing runs (`jsc-microbenchmark` / `jsc-jetstream-compare`).
- For regressions, **always profile both builds the same way** and diff; a single profile shows hot
  code, not *changed* code.
- Quote args; pass lists literally (the Bash tool is zsh — unquoted `$list` does not word-split).
- The bytecode profile path is `/tmp/JSCProfile-<pid>-<n>.json` (per-PID) — grab the newest, don't
  assume a fixed name. Keep dumped profiles under `/tmp/` so the user can inspect them.
