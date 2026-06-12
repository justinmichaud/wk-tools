---
name: jsc-jetstream-compare
description: Use when measuring the JetStream3 performance impact of a JavaScriptCore (or WebKit) change — e.g. "run JetStream3 to measure this PR", "is there a regression on delta-blue", "compare perf of two commits". Builds before/after, runs JetStream3 either in MiniBrowser (official, Tools/Scripts/run-benchmark) or headless in the jsc shell (PerformanceTests/JetStream3/cli.js), compares per-subtest with Tools/Scripts/compare-results (Welch + FDR), and can narrow a regression to a loop via profiling and microbenchmarks.
user-invocable: true
allowed-tools:
  - Bash(make release:*)
  - Bash(Tools/Scripts/run-benchmark:*)
  - Bash(Tools/Scripts/compare-results:*)
  - Bash(git stash:*)
  - Bash(git checkout:*)
  - Bash(git rev-parse:*)
  - Bash(git log:*)
  - Bash(git merge-base:*)
  - Bash(git diff:*)
  - Bash(cp:*)
  - Bash(mkdir:*)
  - Bash(ls:*)
  - Bash(cat:*)
---

# Measuring a JSC change on JetStream3 (statistically)

Quantify the performance effect of a JavaScriptCore/WebKit change on **JetStream3** and compare two
builds per-subtest with `Tools/Scripts/compare-results` (Welch's t-test + FDR). Two ways to run it:

- **Browser (official, representative):** drive the real benchmark in **MiniBrowser** via
  `Tools/Scripts/run-benchmark --plan jetstream3`. Real engine config, the upstream JetStream3.0 the
  plan fetches; matches how regressions are validated. Needs a **full `make release` (MiniBrowser)
  build** per side and an **awake, attached display** (see Gotchas).
- **Headless (jsc shell):** run `PerformanceTests/JetStream3/cli.js` directly in `jsc`. No display
  needed, faster to iterate, jsc-only build is enough. Slightly different harness/engine config than
  the browser but the right tool when there's no display or you're iterating on root-cause.

Terminology: **baseline** = the "before" build (ToT); **patched** = the "after" build (with the
change). `$WEBKIT_ROOT` is the repo root; all `Tools/Scripts/*` run from there.

> ## ⚠️ What counts as a regression — read this before reporting
> JetStream3 scores aggregate ~60 subtests, so the **overall geomean is extremely stable**.
> - **Overall score: a 0.1% FDR-significant regression is HUGE.** The overall noise floor is ≈ ±0.1%,
>   so a *statistically significant* 0.1% drop is a real, ship-blocking regression — not noise. 0.2%
>   overall is routinely validated and taken seriously. Do not dismiss sub-1% overall moves.
> - **Per-subtest: anything over 1% is significant** on a non-noisy subtest. Treat a >1% FDR-flagged
>   per-subtest move as a real regression worth root-causing.
> - **The noisy subtests** (json-parse-inspector, doxbee, Babylon, splay, tsf, async-fs, and other
>   GC-/startup-dominated ones) swing ±2–5% regardless of N; only believe large, FDR-flagged moves
>   there, and never gate an equivalence bound on them.
> Apply this scale in **every** report. "Not significant" ≠ "no effect" until the CI is tight enough
> to rule out the standing equivalence targets: **0.02% overall / 0.5% per (non-noisy) subtest** — see
> Step 5. Keep running rounds until you either find a significant mover or hit those bounds.

## Step 0 — Scope: run exactly the subtests the user named (don't make them wait)

The point of limiting subtests is to **avoid waiting for a full run**, so don't turn scoping into a
wait of its own:

- **If the user named subtests** — in the slash-command args (`/jsc-jetstream-compare delta-blue
  bigint-noble-ed25519`) or in their message ("focus on delta-blue") — **use exactly those and start
  immediately.** No confirmation prompt; that's the fast path.
- **If they asked for a full run / "is this safe to land"** — run the full suite (the only way to get
  the **overall geomean**, which most regressions are judged on; ~60+ subtests, each round a
  multi-minute browser run).
- **Only if scope is genuinely unspecified**, use the **AskUserQuestion tool** once — offering a quick
  named-subtest subset (recommended for iteration) vs the full suite — then proceed without further
  gating. The choice changes runtime by ~30×, so it's worth the single question, but never block on it
  when the user already told you what to run.

`--subtests a b c` is the limiter; pass the names literally (zsh — see the word-splitting trap). Also
pick **browser vs headless** (default: browser if a display is available, else headless — see Gotchas);
don't ask unless it's ambiguous.

## Step 1 — Identify baseline vs patched

- **Uncommitted working-tree change:** patched = working tree, baseline = `HEAD`. Base sha =
  `git rev-parse HEAD`.
- **A commit/PR:** patched = `HEAD`, baseline = `HEAD~1` (or `git merge-base HEAD main` for the whole
  branch vs trunk). Base sha = the baseline commit.

The base sha keys the cached baseline build (Step 2). Note which baseline you chose in the summary.

## Step 2 — Build both sides (baseline cached by sha)

Confirm Release / non-ASan first (`cat $WEBKIT_ROOT/WebKitBuild/Configuration` → `Release`; no
`WebKitBuild/ASan` dir), **or the numbers are meaningless.** `make release` builds full WebKit incl.
MiniBrowser and the `jsc` shell. **The patched build lives in `WebKitBuild/Release`; the baseline
(ToT) build is built natively into `/tmp/js3-builds/<base-sha>/` via `WEBKIT_OUTPUTDIR`** so repeat
runs at the same base sha skip rebuilding it.

> **Do NOT copy/rsync a build to another directory and run it.** A relocated WebKit build can't launch
> its multi-process XPC services — MiniBrowser logs `WebContent process crashed; reloading` in a loop
> (`launchd: failed lookup: name = com.apple.WebKit.WebContent, error = 3`) and the run times out.
> Copying the frameworks, adding the top-level `*.xpc` bundles, and ad-hoc re-signing all fail to fix
> it. The baseline must be a *native* build at its own location. (The jsc shell has the analogous trap
> — see the `DYLD_FRAMEWORK_PATH` gotcha.)

```bash
cd "$WEBKIT_ROOT"
BASE_SHA=$(git rev-parse HEAD)                  # or the baseline commit
CACHE=/tmp/js3-builds/$BASE_SHA                 # baseline build dir is $CACHE/Release

# 1. Patched (change in tree) → WebKitBuild/Release (incremental; first full WebKit build is long).
make release

# 2. Baseline: reuse the cache if present, else build ToT natively into its own output dir.
if [ ! -d "$CACHE/Release" ]; then
  git stash push -- <changed paths>            # or: git checkout <baseline-sha>
  WEBKIT_OUTPUTDIR="$CACHE" make release        # full native build at $CACHE/Release (long)
  git stash pop                                # or: git checkout <branch>  (restores the change)
fi
# WebKitBuild/Release is untouched (still patched); WEBKIT_OUTPUTDIR redirected the baseline build.
```

A separate `WEBKIT_OUTPUTDIR` build does not share artifacts with `WebKitBuild`, so the baseline is a
full build the first time (then cached per sha). It is large; if space is tight you can delete
`$CACHE/<*.build dirs>` afterward, keeping `$CACHE/Release/` in place (don't move it). Sanity-check:
`ls WebKitBuild/Release/{MiniBrowser.app,jsc} "$CACHE/Release/"{MiniBrowser.app,jsc}`.

## Step 3 — Run JetStream3, interleaved

Don't silently launch a long **full-suite** loop the user didn't ask for — but if they named subtests
(Step 0), just run them. `run-benchmark` runs the plan in one browser/build per invocation, so
**interleave the two builds across rounds** (cancel thermal/background drift), writing one JSON per
round. **Quiesce first** (`./quiesce.sh on` — see the Determinism checklist) and use the
`JS3_LOCAL_COPY` it prints as `--local-copy "$JS3_LOCAL_COPY"` below: the `jetstream3` plan otherwise
re-clones upstream JetStream3.0 from GitHub every invocation (network + disk noise, and the upstream
commit can shift mid-experiment). List names with `run-benchmark --plan jetstream3 --list-subtests` (the plan's set differs
from the in-tree `PerformanceTests/JetStream3` — e.g. it has `bigint-noble-ed25519`, not `-secp256k1`).

> **⚠️ The Bash tool runs `zsh`, which does NOT word-split unquoted variables.** `--subtests $SUB`
> where `SUB="delta-blue bigint-noble-ed25519"` passes the *whole string as one argument* → run fails
> with `... is not a valid subtest, skipping` / `No valid subtests were specified`. The same bug bites
> `for x in $list` (iterates once over the whole string). **Always pass list args literally**
> (`--subtests delta-blue bigint-noble-ed25519`) or build an explicit `if/else`; never rely on
> word-splitting. This silently wasted a whole run in practice.

```bash
J3=/tmp/js3-runs; mkdir -p "$J3"
CACHE=/tmp/js3-builds/$(git rev-parse HEAD)     # baseline (or the chosen base sha)
PATCHED="$WEBKIT_ROOT/WebKitBuild/Release"
LOCAL_COPY=/tmp/js3-builds/jetstream3-localcopy # the path quiesce.sh seeds & prints as JS3_LOCAL_COPY
# Pass subtests literally (zsh!). Drop the --subtests line entirely for the full suite.
run_one(){ # $1=build-dir  $2=out.json
  Tools/Scripts/run-benchmark --plan jetstream3 --browser minibrowser \
    --build-directory "$1" --output-file "$2" --count 1 \
    --local-copy "$LOCAL_COPY" \
    --subtests delta-blue bigint-noble-ed25519; }
for i in $(seq 1 8); do k=$(printf %02d $i)
  if [ $((i%2)) -eq 0 ]; then
    run_one "$CACHE/Release" "$J3/base_$k.json";    run_one "$PATCHED" "$J3/patched_$k.json"
  else
    run_one "$PATCHED" "$J3/patched_$k.json";       run_one "$CACHE/Release" "$J3/base_$k.json"
  fi
done
```

**Full suite:** delete the `--subtests …` line. Each round is a full multi-minute run; start with ~6–8
rounds and add more. Run in the **background** and await/poll — foreground `sleep` is unavailable; full
passes are long. (A short targeted-subtest round can fail in seconds — see the zsh trap above — so
**verify the first round actually wrote a valid JSON** before waiting on the whole loop.)

## Step 3b — Headless alternative (jsc shell, no display)

When there's no awake display, or to iterate fast, run the benchmark headless in the `jsc` shell.
`cli.js` runs the plan and prints scores; run one build per invocation and compare the printed scores,
or capture per-subtest scores and feed them to your own stats. **`jsc` must use its own framework** —
see the `DYLD_FRAMEWORK_PATH` gotcha.

```bash
cd "$WEBKIT_ROOT/PerformanceTests/JetStream3"
PATCHED="$WEBKIT_ROOT/WebKitBuild/Release"
# Single subtest or comma-list after `--`; omit for the full suite.
DYLD_FRAMEWORK_PATH="$PATCHED" "$PATCHED/jsc" cli.js -- bigint-noble-ed25519
# Baseline: same with $CACHE/Release. Interleave invocations and compare the printed "Score" lines.
```

This is the path the `jsc-profile` skill builds on (you profile the headless jsc run, not the
browser). For statistically-rigorous headless comparison of a single subtest's kernel, extract a
**microbenchmark** and use `run-jsc-benchmarks` (see the `jsc-microbenchmark` skill) — it interleaves
and does the stats for you.

## Step 4 — Compare with compare-results

`-a` = baseline, `-b` = patched; pass all rounds (they're merged). JetStream3 is **bigger-is-better**,
so `b/a > 1` means patched is **faster**, `b/a < 1` means patched is **slower** (a regression).

```bash
Tools/Scripts/compare-results -a /tmp/js3-runs/base_*.json -b /tmp/js3-runs/patched_*.json \
                              --breakdown --sort --csv /tmp/js3-runs/breakdown.csv
```

- `--breakdown` — per-subtest table with FDR-corrected `(significant)` flags. **Trust only flagged
  rows** (FDR corrects across subtests).
- `--sort` — order by `b/a`.
- `--category-breakdown` — split startup / worst / average. Use it to tell a **compile-time/startup**
  cost (shows in First/Worst) from a **steady-state** regression (shows in Average). A regression
  weighted toward First/Worst with a small Average is diffuse codegen / compile cost, not a hot loop.

## Step 5 — Iterate to a decision (sequential)

Run more rounds until **either**:
- **Significant mover** — a subtest is FDR-`(significant)`, OR the overall geomean move is significant.
  Apply the thresholds at the top: **0.1% significant overall is a real regression**; **>1% on a
  non-noisy subtest** is real. Report it; done.
- **Equivalence** — the CI rules out a move bigger than the **standing margins `θ`: 0.02% on the
  overall geomean, 0.5% per non-noisy subtest.** These are fixed targets, not per-run choices: always
  keep running rounds until every non-noisy subtest's 95% CI rules out a >0.5% regression AND (for a
  full-suite run) the overall geomean rules out a >0.02% move — unless a significant mover ends it first.

To check the bound, compute the **95% CI on `b/a`** per subtest (and on the geomean) from the per-round
scores — `compare-results` gives significance, not the CI, so compute it yourself (two-sample/Welch on
the per-round subtest scores; the smallest regression ruled out is `1 − lowerCI`). Stop a subtest once
`1 − lowerCI ≤ 0.5%` (overall `≤ 0.02%`).

**Margin feasibility — these targets are aggressive; budget for it.** CI half-width shrinks as `1/√N`,
so reaching margin `θ` from current half-width `w` costs ≈ `(w/θ)²` more samples. JetStream3 noise is
heterogeneous: the **overall geomean** reaches ≈ ±0.1% with modest N, so **0.02% overall needs ~25×
more rounds than ±0.1%** — many tens of full-suite rounds; run them in the background and expect a long
loop. **Per-subtest** noise ranges from ≈ ±0.05% (float-mm) to ≈ ±5%; **0.5% is reachable for quiet
subtests but the noisy list (json-parse-inspector, doxbee, Babylon, splay, tsf, async-fs, …) cannot get
there at any practical N.** So:
- **Never gate the loop on a noisy subtest.** Exempt them from the 0.5% rule; report their floor and
  state it is noise-gated, not equivalence.
- **Two important caveats that no amount of N fixes:** (1) if a subtest's point estimate already sits
  below `1 − θ` (e.g. b/a = 0.989, a 1.1% slowdown, vs a 0.5% target), more rounds will *confirm a
  regression*, not rule one out — the CI tightens around the slowdown and crosses into significance.
  (2) Targeted subsets give no overall geomean; the 0.02% target only applies to a full-suite run.
- **Always set a compute budget** and `log()` progress (current `1 − lowerCI` per subtest each batch);
  on hitting the budget, report the tightest bound achieved and which subtests still gate it.

## Step 6 — Root-cause a confirmed regression (optional, on request)

Once a subtest is confirmed slower, narrow it to a loop:

1. **Profile the headless jsc run** of that subtest (`jsc-profile` skill). The first decision is the
   **tier breakdown** from `jsc --sample`:
   - cost in **FTL/DFG/Baseline** (generated JS) → use the **JSC bytecode profiler** to find the hot
     CodeBlock/bytecode.
   - cost in **C/C++** → use **samply** to find the hot native function.
2. **Decompose by phase.** Split the subtest's work into its sub-operations and time each on both
   builds (interleaved, many reps, with a per-phase t-test). The phase(s) that move localize the code.
   Beware multiple-comparison: with k phases, require BH/Bonferroni-corrected significance, not raw p.
3. **Extract a microbenchmark** of the suspect loop and measure it directly on both builds
   (`jsc-microbenchmark` skill). If it reproduces the regression, that's the locus; if it doesn't, the
   regression is elsewhere or **diffuse** (a thin, broad codegen change across many functions — common
   for optimizer changes; shows as a small same-direction shift across many subtests + a First/Worst
   category weighting).

Real example: a DFG IntegerRangeOptimization change gave a confirmed −0.54% on bigint-noble-ed25519,
but **no single loop reproduced it** — SHA-512 (the obvious typed-array candidate) was inert because
its `Uint32Array[i] | 0` reads truncate immediately, killing the removed range fact; per-phase and
the isolated scalar-mult loop both came back non-significant; the category breakdown was First-weighted.
Conclusion: diffuse codegen cost, not a hot loop. **Report "diffuse / not localizable" honestly rather
than forcing a culprit.**

## Step 7 — Report

- Trust per-subtest claims only where `compare-results` marks `(significant)`.
- State results against the thresholds at the top (0.1% overall significant = real; >1% subtest = real).
- **Watch for a broad, same-direction shift across nearly all subtests** — including ones the change
  can't touch (crypto, wasm) — that's machine drift or a uniform compile-time cost; use
  `--category-breakdown` to distinguish.
- Report: baseline used (HEAD~1 vs main vs working tree), browser-vs-headless, rounds run, overall
  `b/a` + pValue, the FDR-significant movers, and (if root-caused) the loop or the diffuse finding.

## Determinism checklist (do these to reduce run-to-run variation)

The single biggest lever is **interleaving** baseline/patched every round; everything else trims noise.

- **Quiescing helper — run this first.** `./quiesce.sh on` (a symlink in this skill's directory to the
  top-level `wk-tools/quiesce.sh` tool) automates the
  machine/OS and network-determinism items below: it checks AC power / thermal throttle / display-asleep,
  disables Spotlight indexing (sudo), stops an in-flight Time Machine backup, starts `caffeinate`, reports
  contending CPU hogs (cloud-sync/indexing daemons) to quit, settles thermals, and seeds a **pinned local
  JetStream3 checkout**. It prints `JS3_LOCAL_COPY=<path>` on its last line — capture it and pass
  `--local-copy "$JS3_LOCAL_COPY"` to every `run-benchmark` invocation so each round copies a fixed
  checkout instead of re-cloning from GitHub (kills per-run network/disk variance and pins the commit so
  upstream can't shift mid-experiment). Run `./quiesce.sh off` afterward to re-enable Spotlight and stop
  `caffeinate`. ProMotion/variable-refresh rate is the one item it can only *warn* about — set a fixed
  display refresh rate by hand, since rAF-driven browser runs inherit refresh jitter.
- **Machine state:** plug into AC power; quit other apps and browser tabs; let the machine sit idle a
  minute to settle thermals; `caffeinate -dimsu &` during a run (note: `caffeinate` does NOT wake an
  asleep display — see Gotchas). Don't run two benchmark loops at once. (`./quiesce.sh on` does all of
  this.)
- **Interleave** base/patched within each round (the loops above alternate by parity). Never run
  all-baseline-then-all-patched (time-of-day / thermal drift aliases into the result).
- **Same build config** both sides: Release, non-ASan, same compiler. The baseline cache guarantees
  the ToT build is byte-stable across re-runs at one sha.
- **Deterministic inputs:** the `jetstream3` plan already sets `deterministicRandom: true`. For your
  own harnesses, seed a PRNG; never use `Math.random()` / `Date.now()` for inputs.
- **Warm up to steady tier before timing**, then take **median (and min) of many samples**; many outer
  invocations beat many inner iterations for cancelling drift (this is exactly what `run-jsc-benchmarks`
  does — prefer it for microbenchmarks).
- **Quote everything and pass lists literally** (zsh word-splitting trap above) so a run does what you
  think it does.
- Keep raw JSONs/CSVs under `/tmp/js3-runs/` so the user can inspect every number.

## Gotchas / workarounds (all hit in practice)

- **`jsc` needs `DYLD_FRAMEWORK_PATH` = its build dir.** Running `WebKitBuild/Release/jsc` directly
  fails with `dyld: Symbol not found: __Z20WTFCrashWithInfoImpl…  Expected in: …/JavaScriptCore.framework`
  because it links the *system* JavaScriptCore. Fix: `DYLD_FRAMEWORK_PATH="$DIR" "$DIR/jsc" …` (same for
  the baseline build's jsc). **Do NOT use `Tools/Scripts/run-jsc` for measurement** — it injects
  `--useDollarVM=1` and may wrap jsc in `lldb`, both of which perturb timing; it also only targets the
  default `WebKitBuild` dir, not the baseline cache.
- **Browser run needs an awake, attached display.** MiniBrowser renders in a real window and
  JetStream3's loop is `requestAnimationFrame`-driven; on a headless/asleep display
  (`python3 -c "import Quartz; print(Quartz.CGDisplayIsAsleep(Quartz.CGMainDisplayID()))"` → `1`) it
  logs `Running <subtest>:` then stalls and times out (black diagnostic screenshot). `caffeinate` and
  `--local-copy` don't fix it — only a live display does. **No display → use the headless jsc path
  (Step 3b).**
- **First `run-benchmark` run needs `objc` (pyobjc).** If it dies with `No module named 'objc'`:
  `python3 -m pip install --user pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-Quartz`.
- **`run-benchmark` drives MiniBrowser → needs a full `make release`**, not jsc-only. The headless path
  needs only `jsc` (still produced by `make release`).
- **Baseline cache is keyed by base sha** — change the baseline commit and it rebuilds. Delete
  `/tmp/js3-builds/<sha>` to force a fresh baseline.
- **Monitor/grep false positives:** benign log lines contain `Error:` (e.g. `lsof … returned non-zero
  … Port not found yet, retrying`). A Monitor that greps `Error` fires on these. Match precise terminal
  states (`Traceback`, `No valid subtests`) and treat a single benign `Error:` as noise — but confirm
  the run is actually progressing (scores appearing) before trusting it.
- Use `--local-copy <JetStream-checkout>` to avoid re-fetching the benchmark from GitHub each run.
- Confirm Release / non-ASan before building, or the numbers are meaningless.
