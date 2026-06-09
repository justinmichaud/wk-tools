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
---

# Microbenchmarking a JSC change

Measure the effect of a JavaScriptCore change on a small, isolated kernel by comparing two `jsc`
builds. Prefer **`Tools/Scripts/run-jsc-benchmarks`** — it runs each VM/benchmark in its own VM
invocation, in **random interleaved order**, and reports proper statistics (mean, CI, "definitely/
probably faster/slower"). Hand-rolled timing loops are a fallback when you need a kernel the harness
can't express.

Terminology: **baseline** = before build, **patched** = after build. Build both per the
`jsc-jetstream-compare` skill (Step 2); each side's shell is `<build>/jsc`.

> ## ⚠️ Significance scale (carry into the report)
> - A **0.1% statistically significant** regression on an **overall** benchmark score is **HUGE** —
>   ship-blocking, not noise.
> - On a single microbenchmark / subtest, **>1% is significant** unless it's an inherently noisy one
>   (GC-/startup-/allocation-dominated). A microbenchmark you wrote to isolate one loop should be
>   *low* noise — if it swings >1% run-to-run, tighten it (more warmup, more iterations, deterministic
>   inputs) before trusting any delta.
> `run-jsc-benchmarks`' own verdict words ("definitely faster/slower") already encode significance;
> trust those over eyeballing means.

## Where microbenchmarks go: JSTests/microbenchmarks/

**New microbenchmarks are placed permanently in `JSTests/microbenchmarks/`** (slow ones in
`JSTests/slowMicrobenchmarks/`) — that is their home, not `/tmp`. `run-jsc-benchmarks` discovers them
there by filename, and `--benchmarks <regex>` filters by name. The file is a tracked source file; add
it to the tree (do **not** commit unless asked — leave it in the working tree). No license header or
`//@` directive is required; the stress-test tooling auto-adds any `//@ skip` lines later.

**Pick the filename yourself** (no need to ask) following the directory's convention: lowercase
kebab-case, descriptive of the kernel, often type/operation-led (e.g. `Int32Array-…`,
`array-prototype-…`, `sha512-uint32-block-process`). Check for an existing same-named file first
(`ls JSTests/microbenchmarks/ | grep …`) so you don't clobber one. The base filename (without `.js`)
is exactly what `--benchmarks <regex>` matches, so make it greppable and unambiguous.

## A microbenchmark is just a script that does the work

`run-jsc-benchmarks` times **one whole execution of the file** and repeats it: for the microbenchmark
suite it calls `run(file)` — which **runs the entire file in a fresh global object each timed
iteration** — `--warmup` times (discarded) then `--inner` times (measured), across `--outer` VM
invocations in random interleaved order. No timing code goes in the file.

The fresh-global-per-iteration detail drives the design: **tier-up does NOT persist across iterations**
(each `run()` recompiles from scratch). So the file must contain **enough internal repetition to warm
itself up to FTL and amortize that compilation within a single run** — otherwise you measure compile
time, not steady state.

Write a good microbenchmark:
- **One hot kernel** in a function, called in a loop, with `noInline(kernel)` so it's measured as a
  real, separately-JITted call (the suite provides `noInline`).
- **Enough internal repetition** that one whole-file run is ~50–150 ms — long enough that FTL
  steady-state dominates the one-time compile, short enough that `--outer` rounds stay quick.
- **Deterministic inputs** — seed a PRNG / fill arrays by formula; never `Math.random()`/`Date.now()`.
- **Defeat dead-code elimination** — accumulate a data-dependent `sink` and reference it in a check the
  compiler can't fold away (e.g. `if (sink === <impossible>) throw …`). **Don't `print()`** stray
  lines — the harness parses stdout for its own `Time:` lines.
- **Match the real types/shapes** you're reproducing (same typed-array kind, same polymorphism), or
  the effect won't show. (And remember a kernel may be *inert* to a change: a `Uint32Array[i] | 0` load
  truncates immediately, so it's insensitive to an integer-range optimization — verify, don't assume.)

Example (`JSTests/microbenchmarks/my-kernel.js`):
```js
function kernel(a) {                       // the loop under test, isolated so it JITs
    let s = 0;
    for (let i = 0; i < a.length; i++) s = (s + a[i] * 3) | 0;
    return s;
}
noInline(kernel);
const arr = new Int32Array(1000);
for (let i = 0; i < arr.length; i++) arr[i] = (i * 2654435761) | 0;   // deterministic fill
let sink = 0;
for (let k = 0; k < 200000; k++) sink = (sink + kernel(arr)) | 0;     // internal warmup + steady state
if (sink === 0x7fffffff) throw "unreachable: " + sink;               // anti-DCE, no stray print
```

## Before running

If the user already asked you to run/measure it, just go — don't make them wait on a prompt. Otherwise
(e.g. you wrote the microbenchmark proactively and it's not obvious they want the full sweep now),
confirm once with the **AskUserQuestion tool**, stating the rough cost: which jsc builds, the benchmark
name, and the `--outer`/`--inner` round counts (≈ proportional to wall-clock). `--outer` is the main
cost/precision dial — start low (e.g. 6) for a quick read, raise only if the verdict is inconclusive.
Writing/editing the file and a single standalone smoke run to check it executes never need a prompt.

## Run the comparison with run-jsc-benchmarks (preferred)

Name each VM `Name:path`. The harness auto-detects jsc. It sets `DYLD_FRAMEWORK_PATH` itself when given
a build-tree jsc, so you don't have to (unlike running jsc by hand).

```bash
cd "$WEBKIT_ROOT"
BASE="/tmp/js3-builds/$(git rev-parse HEAD)/Release/jsc"   # baseline (per jsc-jetstream-compare)
PATCHED="$WEBKIT_ROOT/WebKitBuild/Release/jsc"
# Drop the file into JSTests/microbenchmarks/ first (e.g. my-kernel.js), then:
Tools/Scripts/run-jsc-benchmarks \
    Base:"$BASE" Patched:"$PATCHED" \
    --microbenchmarks --benchmarks 'my-kernel' \
    --outer 20 --inner 1 --warmup 3 \
    --output-name /tmp/microbench/mykernel
# Writes /tmp/microbench/mykernel_report.txt (the comparison table + verdict) and .json (raw).
```

Key knobs (more rounds = tighter CI, `1/√N`):
- `--outer <n>` — VM invocations per benchmark, **interleaved in random order**. The main lever for
  beating drift. Raise this (e.g. 20–40) until the verdict is stable.
- `--inner <n>` — iterations inside one invocation (amortizes process startup; keep small unless the
  kernel is tiny).
- `--warmup <n>` — warmup runs per invocation, discarded; ensures FTL tier-up before timing.
- `--benchmarks <regex>` — restrict to your file(s). Combine with `--microbenchmarks` to search only
  that suite.
- Per-VM env vars: `Name:JSC_useJIT=false:/path/to/jsc` sets `JSC_useJIT` for that VM (e.g. to compare
  JIT on/off). The harness does **not** unset it, so set the opposite on the other VM.

Read the `_report.txt`: it gives each VM's mean ± CI and a verdict like *"Patched is 1.0102x slower"*
with a confidence word. Trust the verdict word; a sub-1% move with "might be" is inconclusive — add
`--outer` rounds.

The microbenchmark file stays in `JSTests/microbenchmarks/` (that's its home). Leave it in the working
tree; **don't commit unless the user asks.**

## Fallback: self-rolled jsc-shell harness

When the kernel needs setup the suite can't express (async, loading a real library, custom timing),
time it yourself. **You must set `DYLD_FRAMEWORK_PATH`** here (the harness did it for you above).

```bash
DIR="$WEBKIT_ROOT/WebKitBuild/Release"
DYLD_FRAMEWORK_PATH="$DIR" "$DIR/jsc" mybench.js
# Do NOT use Tools/Scripts/run-jsc for measurement: it adds --useDollarVM=1 and may wrap jsc in lldb.
```

Harness file pattern (deterministic, anti-DCE, median-of-samples):
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
Then drive base/patched **interleaved** from the shell and aggregate (median across reps, paired
%-diff, a Welch t-test). Remember the **zsh word-splitting trap**: pass any list args literally and
build base/patched ordering with explicit `if/else`, never `for x in $list`.

If the kernel is async or uses promises, add `.catch(e => print("ERROR: " + (e && e.stack || e)))` to
the top-level — **jsc prints nothing for an unhandled rejection**, so a thrown error looks like a
silent no-output run.

## Extracting a microbenchmark from a real benchmark (e.g. a JS3 subtest)

To turn "subtest X regressed" into a standalone kernel:

1. **Profile first** (`jsc-profile` skill) to find the hot function/CodeBlock — extract *that*, not a
   guess. The intuitive culprit is often wrong (a typed-array loop whose loads are immediately `| 0`
   can be inert to an integer-range change, because the truncation already pins the type).
2. **Reach the code.** If the kernel is buried in a bundled module (browserify/CommonJS), expose it
   with a one-line edit to a *copy* of the bundle: `globalThis.__lib = lib;` right where the module
   requires it. Then your harness uses `globalThis.__lib`.
3. **Supply the environment the shell lacks.** Browser benchmarks assume `self.crypto`,
   `performance`, WebCrypto, etc. Inject substitutes — e.g. for noble-ed25519 the standalone shell has
   no WebCrypto sham, so `ed.utils.sha512` throws "environment doesn't have sha512 function"; fix by
   `ed.utils.sha512 = async (...m) => sha512(concat(m))` (and `sha512Sync`) using a sha512 you exposed
   the same way.
4. **Decompose by phase** before isolating one loop: time each sub-operation (e.g. for ed25519:
   getPublicKey / sign / verify / point-decompress / hash / hex) on both builds, interleaved, ~30 reps,
   with a per-phase t-test. Apply a multiple-comparison correction (BH/Bonferroni) across the k phases —
   a raw p < 0.05 among 6 phases is not significant. The phase(s) that move localize the loop.
5. **Isolate and confirm.** Extract the suspect loop into a microbenchmark and run it on both builds.
   If it reproduces a >1% move, that's the locus. **If it doesn't**, the regression is elsewhere or
   **diffuse** (a thin, broad codegen change across many functions) — say so; don't manufacture a
   culprit. A good causation check: a control variant that *shouldn't* be affected (e.g. swap
   `Uint32Array`→`Int32Array`, or JIT on→off) and show the gap appears/vanishes accordingly.

## Determinism checklist

- AC power, quit other apps, settle thermals, `caffeinate -dimsu &`, one run at a time.
- Prefer `run-jsc-benchmarks` (random-interleaved invocations) over hand loops; if hand-rolling,
  **interleave base/patched** and never batch one side then the other.
- Warm up to FTL; report **median + min** of many samples; favor **more outer invocations** over more
  inner iterations.
- Deterministic seeded inputs; anti-DCE checksum; match real types/shapes.
- Same build config both sides (Release, non-ASan). Quote everything; pass lists literally (zsh).
- Keep the report/JSON and the microbenchmark source under `/tmp/microbench/` so the user can inspect.
