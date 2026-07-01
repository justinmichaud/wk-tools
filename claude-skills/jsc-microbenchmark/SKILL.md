---
name: jsc-microbenchmark
description: Use to write and run a JavaScriptCore microbenchmark comparing two jsc builds (baseline vs patched) — e.g. "microbenchmark this loop", "does my change regress this kernel", "isolate the hot loop from a JS3 subtest and measure it". Uses Tools/Scripts/run-jsc-benchmarks (interleaved, random-order, statistical) as the primary runner, with a self-rolled jsc-shell harness as fallback. Covers extracting a microbenchmark from a real benchmark.
user-invocable: true
allowed-tools:
  - Bash(Tools/Scripts/run-jsc-benchmarks:*)
  - Bash(make release:*)
  - Bash(git rev-parse:*)
  - Bash(cp:*)
  - Bash(mkdir:*)
  - Bash(ls:*)
  - Bash(cat:*)
  # Linux / 32-bit container path:
  - Bash(Tools/Scripts/build-webkit:*)
  - Bash(wkdev-enter:*)
  - Bash(taskset:*)
---

# Microbenchmarking a JSC change

Measure a JavaScriptCore change on a small isolated kernel by comparing two `jsc` builds.
**baseline** = before build, **patched** = after build; build both per the `jsc-jetstream-compare`
skill (Step 2), each side's shell is `<build>/jsc`.

Prefer **`Tools/Scripts/run-jsc-benchmarks`**: it runs each VM/benchmark in its own VM invocation, in
random interleaved order, and reports statistics (mean, CI, "definitely/probably faster/slower"). Trust
its verdict word over eyeballing means. Hand-roll a timing loop only for a kernel the harness can't
express.

## Significance scale (carry into the report)

- **0.1% significant** on an **overall** score is **HUGE** — ship-blocking, not noise.
- On a single microbenchmark/subtest, **>1% is significant** unless it's inherently noisy
  (GC-/startup-/allocation-dominated). One you wrote to isolate a loop should be *low* noise — if it
  swings >1% run-to-run, tighten it (more warmup, more iterations, deterministic inputs) before
  trusting a delta.

## Where microbenchmarks go: JSTests/microbenchmarks/

New microbenchmarks live permanently in `JSTests/microbenchmarks/` (slow ones in
`JSTests/slowMicrobenchmarks/`), not `/tmp`. `run-jsc-benchmarks` discovers them by filename; the base
filename (without `.js`) is exactly what `--benchmarks <regex>` matches, so pick a greppable,
unambiguous name in the directory's convention — lowercase kebab-case, type/operation-led
(`Int32Array-…`, `array-prototype-…`, `sha512-uint32-block-process`). Add the file to the tree; leave
it in the working tree, **don't commit unless asked**. No license header or `//@` directive needed.

```bash
ls JSTests/microbenchmarks/ | grep my-kernel    # check you won't clobber an existing name
```

## A microbenchmark is just a script that does the work

`run-jsc-benchmarks` times one whole execution of the file and repeats it. For the microbenchmark suite
it calls `run(file)`, which runs the entire file in a **fresh global object each timed iteration** —
`--warmup` times (discarded) then `--inner` times (measured), across `--outer` VM invocations, random
interleaved. No timing code in the file.

Fresh-global-per-iteration means **tier-up does NOT persist across iterations** (each `run()`
recompiles from scratch), so the file must contain enough internal repetition to warm itself to FTL and
amortize that compile within one run — else you measure compile time, not steady state.

```js
// JSTests/microbenchmarks/my-kernel.js
function kernel(a) {                       // one hot kernel in a function so it JITs separately
    let s = 0;
    for (let i = 0; i < a.length; i++) s = (s + a[i] * 3) | 0;
    return s;
}
noInline(kernel);                          // measure it as a real, separately-JITted call
const arr = new Int32Array(1000);
for (let i = 0; i < arr.length; i++) arr[i] = (i * 2654435761) | 0;   // deterministic seeded fill
let sink = 0;
for (let k = 0; k < 200000; k++) sink = (sink + kernel(arr)) | 0;     // internal warmup + steady state
if (sink === 0x7fffffff) throw "unreachable: " + sink;               // anti-DCE, no stray print()
```

Design rules the example bakes in:
- **Size one whole-file run to ~50-150 ms**: long enough that FTL steady-state dominates the one-time
  compile, short enough that `--outer` rounds stay quick.
- **Deterministic inputs** — seed a PRNG / fill by formula; never `Math.random()`/`Date.now()`.
- **Defeat DCE** with a data-dependent `sink` referenced in a check the compiler can't fold. Don't
  `print()` stray lines — the harness parses stdout for its `Time:` lines.
- **Match the real types/shapes** (same typed-array kind, same polymorphism) or the effect won't show.
  A kernel can be *inert* to a change: a `Uint32Array[i] | 0` load truncates immediately, so it's
  insensitive to an integer-range optimization — verify, don't assume.

## Before running

If the user already asked you to run it, just go. Otherwise (e.g. you wrote it proactively) confirm once
with the **AskUserQuestion tool**, stating rough cost: which jsc builds, benchmark name, and
`--outer`/`--inner` counts (≈ proportional to wall-clock). `--outer` is the main cost/precision dial —
start low (e.g. 6) for a quick read, raise only if inconclusive. Writing the file and a single
standalone smoke run never need a prompt.

## Run the comparison with run-jsc-benchmarks (preferred)

Name each VM `Name:path`; the harness auto-detects jsc and sets the framework/library path itself for a
build-tree jsc (you don't, unlike running jsc by hand).

```bash
cd "$WEBKIT_ROOT"
BASE="/tmp/js3-builds/$(git rev-parse HEAD)/Release/jsc"   # baseline (per jsc-jetstream-compare)
PATCHED="$WEBKIT_ROOT/WebKitBuild/Release/jsc"
# Drop my-kernel.js into JSTests/microbenchmarks/ first, then:
Tools/Scripts/run-jsc-benchmarks \
    Base:"$BASE" Patched:"$PATCHED" \
    --microbenchmarks --benchmarks 'my-kernel' \
    --outer 20 --inner 1 --warmup 3 \
    --output-name /tmp/microbench/mykernel
# Writes /tmp/microbench/mykernel_report.txt (comparison table + verdict) and .json (raw).
```

Read `_report.txt`: each VM's mean ± CI and a verdict like *"Patched is 1.0102x slower"* with a
confidence word. A sub-1% move with "might be" is inconclusive — add `--outer` rounds.

Knobs (more rounds = tighter CI, `1/√N`):
- `--outer <n>` — VM invocations per benchmark, interleaved random order; the main lever against drift.
  Raise (e.g. 20-40) until the verdict is stable.
- `--inner <n>` — iterations inside one invocation (amortizes startup; keep small unless the kernel is
  tiny).
- `--warmup <n>` — warmup runs per invocation, discarded; ensures FTL tier-up before timing.
- `--benchmarks <regex>` — restrict to your file(s); combine with `--microbenchmarks`.
- Per-VM env var: `Name:JSC_useJIT=false:/path/to/jsc` sets `JSC_useJIT` for that VM (e.g. JIT on/off).
  The harness does **not** unset it — set the opposite on the other VM.

## Platform: launching jsc by hand

`run-jsc-benchmarks` handles the shared-library path for you. When you invoke `jsc` directly (the
fallback harness, or a smoke run) you must set it yourself, and it differs by platform:

- **macOS**: `DYLD_FRAMEWORK_PATH="$DIR"`, build dir `$WEBKIT_ROOT/WebKitBuild/Release`, jsc at `$DIR/jsc`.
- **Linux (incl. 32-bit ARMv7 JSCOnly container)**: `LD_LIBRARY_PATH="$DIR/lib"`, JSCOnly build dir
  `$WEBKIT_ROOT/WebKitBuild/JSCOnly/Release`, jsc at `$DIR/bin/jsc`.

```bash
# macOS
DIR="$WEBKIT_ROOT/WebKitBuild/Release";        DYLD_FRAMEWORK_PATH="$DIR" "$DIR/jsc" mybench.js
# Linux / JSCOnly
DIR="$WEBKIT_ROOT/WebKitBuild/JSCOnly/Release"; LD_LIBRARY_PATH="$DIR/lib" "$DIR/bin/jsc" mybench.js
```

Use `jsc` directly, not `Tools/Scripts/run-jsc`, for measurement: the wrapper adds `--useDollarVM=1` and
may wrap jsc in lldb.

## Fallback: self-rolled jsc-shell harness

For a kernel the suite can't express (async, loading a real library, custom timing), time it yourself.
Set the library path per your platform (see above).

```js
const now = () => (typeof performance !== "undefined" ? performance.now() : Date.now()); // jsc may lack performance
function once() { /* do N units of the kernel; return a checksum */ }
let sink = 0;
for (let w = 0; w < 3; w++) sink ^= once();          // warm up / tier up to FTL
const times = [];
for (let s = 0; s < 11; s++) { const t0 = now(); sink ^= once(); times.push(now() - t0); }
times.sort((a, b) => a - b);
print(`median=${times[times.length >> 1].toFixed(3)} min=${times[0].toFixed(3)} sink=${sink}`);
```

Drive base/patched **interleaved** from the shell and aggregate (median across reps, paired %-diff,
Welch t-test); never batch one side then the other. Beware the **zsh word-splitting trap**: pass list
args literally and build base/patched ordering with explicit `if/else`, never `for x in $list`.

For an async/promise kernel, add `.catch(e => print("ERROR: " + (e && e.stack || e)))` to the top level
— **jsc prints nothing for an unhandled rejection**, so a thrown error looks like a silent no-output run.

## Extracting a microbenchmark from a real benchmark (e.g. a JS3 subtest)

1. **Profile first** (`jsc-profile` skill) to find the hot function/CodeBlock — extract *that*. The
   intuitive culprit is often wrong: a typed-array loop whose loads are immediately `| 0` is inert to an
   integer-range change, the truncation already pins the type.
2. **Reach the code.** For a kernel buried in a bundled module (browserify/CommonJS), expose it with a
   one-line edit to a *copy* of the bundle: `globalThis.__lib = lib;` where the module requires it; the
   harness then uses `globalThis.__lib`.
3. **Supply the missing environment.** Browser benchmarks assume `self.crypto`, `performance`, WebCrypto.
   Inject substitutes — e.g. noble-ed25519's `ed.utils.sha512` throws "environment doesn't have sha512
   function", fix with `ed.utils.sha512 = async (...m) => sha512(concat(m))` (and `sha512Sync`) using a
   sha512 you exposed the same way.
4. **Decompose by phase** before isolating one loop: time each sub-operation (ed25519: getPublicKey /
   sign / verify / point-decompress / hash / hex) on both builds, interleaved, ~30 reps, per-phase
   t-test. Apply a multiple-comparison correction (BH/Bonferroni) across the k phases — a raw p < 0.05
   among 6 phases is not significant. The phase(s) that move localize the loop.
5. **Isolate and confirm.** Extract the suspect loop and run it on both builds. A >1% move is the locus.
   **If it doesn't reproduce**, the regression is elsewhere or **diffuse** (a thin broad codegen change
   across many functions) — say so, don't manufacture a culprit. Causation check: a control that
   *shouldn't* be affected (swap `Uint32Array`→`Int32Array`, or JIT on→off) and show the gap
   appears/vanishes accordingly.

## Determinism checklist

- One run at a time, other apps quit, thermals settled, AC power (macOS: `caffeinate -dimsu &`).
- Prefer `run-jsc-benchmarks` (random-interleaved) over hand loops; if hand-rolling, interleave
  base/patched, never batch one side.
- Warm up to FTL; report **median + min** of many samples; favor more outer invocations over more inner.
- Deterministic seeded inputs; anti-DCE checksum; match real types/shapes.
- Same build config both sides (Release, non-ASan). Quote everything; pass lists literally (zsh).
- Keep the report/JSON and microbenchmark source under `/tmp/microbench/` for inspection.
