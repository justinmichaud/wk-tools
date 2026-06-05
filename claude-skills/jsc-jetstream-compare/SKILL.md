---
name: jsc-jetstream-compare
description: Use when measuring the JetStream3 performance impact of a JavaScriptCore (or WebKit) change — e.g. "run JetStream3 to measure this PR", "is there a regression on delta-blue", "compare perf of two commits". Builds MiniBrowser for before/after, runs the official browser-driven JetStream3 via Tools/Scripts/run-benchmark, and compares per-subtest with Tools/Scripts/compare-results (Welch + FDR).
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
  - Bash(rsync:*)
  - Bash(cp:*)
  - Bash(mkdir:*)
  - Bash(ls:*)
  - Bash(cat:*)
---

# Measuring a JSC change on JetStream3 (browser, statistically)

Quantify the performance effect of a JavaScriptCore/WebKit change on **JetStream3**, the official
way: drive the real benchmark in **MiniBrowser** via `Tools/Scripts/run-benchmark --plan jetstream3`,
and compare two builds per-subtest with `Tools/Scripts/compare-results` (Welch's t-test + FDR). This
is the representative measurement — real engine configuration and the upstream JetStream3.0 the plan
fetches — and matches how regressions are validated. It requires a **full WebKit (MiniBrowser)
build** per side (heavier than a jsc-only build).

Terminology: **baseline** = the "before" build (ToT), **patched** = the "after" build (with the
change under test). `$WEBKIT_ROOT` is the repo root; all `Tools/Scripts/*` run from there.

## Step 1 — Identify baseline vs patched

- **Uncommitted working-tree change:** patched = working tree, baseline = `HEAD`. Base sha =
  `git rev-parse HEAD`.
- **A commit/PR:** patched = `HEAD`, baseline = `HEAD~1` (or `git merge-base HEAD main` for the whole
  branch vs trunk). Base sha = the baseline commit.

The base sha keys the cached baseline build (Step 2). Note which baseline you chose in the summary.

## Step 2 — Build MiniBrowser for both sides (baseline cached by sha)

Confirm Release / non-ASan first (`cat $WEBKIT_ROOT/WebKitBuild/Configuration`; no `WebKitBuild/ASan`).
`make release` builds full WebKit incl. MiniBrowser. **The patched build lives in
`WebKitBuild/Release`; the baseline (ToT) build is cached at `/tmp/js3-builds/<base-sha>/`** so repeat
runs at the same base sha skip rebuilding it.

```bash
cd "$WEBKIT_ROOT"
BASE_SHA=$(git rev-parse HEAD)                 # or the baseline commit
CACHE=/tmp/js3-builds/$BASE_SHA

# 1. Patched (change in tree) → WebKitBuild/Release (incremental; first full WebKit build is long).
make release

# 2. Baseline: reuse the cache if present, else derive it by removing the change and rebuilding
#    incrementally (only the changed files recompile), then copy the build aside.
if [ ! -d "$CACHE" ]; then
  git stash push -- <changed paths>            # or: git checkout <baseline-sha>
  make release                                 # incremental → WebKitBuild/Release is now ToT
  mkdir -p "$CACHE" && rsync -a --delete "$WEBKIT_ROOT/WebKitBuild/Release/" "$CACHE/"
  git stash pop                                # or: git checkout <branch>
  make release                                 # incremental → WebKitBuild/Release back to patched
fi
```

Sanity-check both have MiniBrowser:
`ls WebKitBuild/Release/MiniBrowser.app "$CACHE/MiniBrowser.app"`.

(For a committed baseline you can `git checkout <sha>` instead of stashing; the cache means you only
pay the ToT build once per base sha.)

## Step 3 — Run JetStream3, interleaved

`run-benchmark` runs the plan in one browser/build per invocation, so interleave the two builds
yourself across rounds to cancel thermal/background drift, writing one JSON per round. The
`jetstream3` plan fetches upstream JetStream3.0 (≈ network on first run; use `--local-copy <dir>` to
point at a local JetStream checkout). Restrict to subtests with `--subtests` for a quick targeted
check (`run-benchmark --plan jetstream3 --list-subtests` lists names — note the plan's set differs
from the in-tree `PerformanceTests/JetStream3`).

```bash
J3=/tmp/js3-runs; mkdir -p "$J3"
run_one(){ # <build-dir> <out.json> [subtest args...]
  Tools/Scripts/run-benchmark --plan jetstream3 --browser minibrowser \
    --build-directory "$1" --count 1 --output-file "$2" "${@:3}"; }

for i in $(seq 1 8); do k=$(printf %02d $i)
  if [ $((i%2)) -eq 0 ]; then
    run_one "$CACHE"                       "$J3/base_$k.json"  --subtests OfflineAssembler gaussian-blur
    run_one "$WEBKIT_ROOT/WebKitBuild/Release" "$J3/patched_$k.json" --subtests OfflineAssembler gaussian-blur
  else
    run_one "$WEBKIT_ROOT/WebKitBuild/Release" "$J3/patched_$k.json" --subtests OfflineAssembler gaussian-blur
    run_one "$CACHE"                       "$J3/base_$k.json"  --subtests OfflineAssembler gaussian-blur
  fi
done
```

Drop `--subtests` to run the full suite (each round is a full ~few-minute browser run; ~60+
subtests). Run in the background and poll/await — full passes are long.

## Step 4 — Compare with compare-results

`-a` = baseline, `-b` = patched; pass all rounds (they're merged). JetStream3 is bigger-is-better,
so `b/a > 1` means patched is faster.

```bash
Tools/Scripts/compare-results -a /tmp/js3-runs/base_*.json -b /tmp/js3-runs/patched_*.json \
                              --breakdown --sort --csv /tmp/js3-runs/breakdown.csv
```

`--breakdown` gives the per-subtest table with FDR-corrected `(significant)` flags (FDR corrects
across subtests — trust only flagged rows); `--sort` orders by `b/a`; `--category-breakdown` splits
startup/worst/average (use it to tell a compile-time cost from a steady-state regression).

## Step 5 — Iterate to a decision (sequential)

To "keep going until conclusive," run more rounds and stop when **either**:
- **Significant mover** — a subtest is FDR-`(significant)` in `compare-results`. Report it; done.
- **Equivalence** — for every subtest the upper bound on the relative slowdown is below a margin `θ`.

Otherwise add rounds (raise the loop count / `--count`) and re-compare over all JSONs.

**Margin feasibility — pick the right granularity.** CI half-width shrinks as `1/√N`, so reaching
margin `θ` from current half-width `w` costs ≈ `(w/θ)²` more samples. JetStream3 noise is **very
heterogeneous**: the **overall geomean** is tight (≈ `±0.1%` with modest N — aggregation over ~60
subtests), which is why **0.2% overall** regressions are routinely validatable; **per-subtest** noise
ranges from ≈ `±0.05%` (e.g. float-mm) to ≈ `±5%` (json-parse-inspector, doxbee, Babylon, splay —
GC-/startup-dominated, stubborn regardless of N). So a *per-subtest* equivalence bound is gated by the
noisiest subtest: `θ = 0.2%` is feasible at the median but needs hundreds of rounds for the tail, and
`θ = 0.01%` (~`400×` harder than 0.2%, ~`250,000×` for the tail) **effectively never terminates**.
Choose deliberately: rule out on the **overall geomean** (reaches ~0.1% quickly), or set a per-subtest
`θ` only as tight as the noisy subtests allow (≈ `0.5–1%` realistic). **Always set a compute budget**;
when you hit it, report the tightest per-subtest bound achieved and which subtests gate it.

## Step 6 — Report

- Trust per-subtest claims only where `compare-results` marks `(significant)` (FDR-corrected).
- **Watch for a broad, same-direction shift across nearly all subtests** — including ones the change
  can't touch (crypto, wasm) — that's machine drift or a uniform compile-time cost; use
  `--category-breakdown` (startup vs steady-state) to distinguish.
- Report the baseline (HEAD~1 vs main), rounds run, the overall `b/a` + pValue, and the significant
  movers.

## Gotchas

- **Needs an awake, attached display.** MiniBrowser renders in a real window and JetStream3's loop is
  `requestAnimationFrame`-driven; on a headless/asleep display (`python3 -c "import Quartz;
  print(Quartz.CGDisplayIsAsleep(Quartz.CGMainDisplayID()))"` → `1`) the benchmark logs
  `Running <subtest>:` then stalls with no results and times out (black diagnostic screenshot).
  `caffeinate` and `--local-copy` don't fix it — only a live display does. For headless measurement,
  use the jsc shell (`PerformanceTests/JetStream3/cli.js` + `compare-results`) instead.
- **First run needs `objc` (pyobjc).** If `run-benchmark` dies with `No module named 'objc'`, install
  it for its python3: `python3 -m pip install --user pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-Quartz`.
- **Browser build required.** `run-benchmark` drives MiniBrowser, so both sides need a full
  `make release` build, not jsc-only. The first full WebKit build is long; afterward the baseline
  cache (`/tmp/js3-builds/<sha>`) and incremental patched rebuilds keep repeat runs cheap.
- **Baseline cache is keyed by base sha** — if you change the baseline commit, it rebuilds. Delete
  `/tmp/js3-builds/<sha>` to force a fresh baseline.
- Use `--local-copy <JetStream-checkout>` to avoid re-fetching the benchmark from GitHub each run.
- The plan's subtest set is upstream JetStream3.0 and differs from the in-tree
  `PerformanceTests/JetStream3` (e.g. it has `bigint-noble-ed25519`, not `-secp256k1`); confirm names
  with `--list-subtests`.
- Confirm Release / non-ASan before building, or the numbers are meaningless.
- Interleave baseline/patched rounds; don't run all-baseline-then-all-patched (time-of-day drift).
- Foreground `sleep` is unavailable; run in the background and wait on completion or a Monitor loop.
