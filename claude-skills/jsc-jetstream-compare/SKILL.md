---
name: jsc-jetstream-compare
description: Use when measuring the JetStream3 performance impact of a JavaScriptCore (or WebKit) change — e.g. "run JetStream3 to measure this PR", "is there a regression on delta-blue", "compare perf of two commits". Builds before/after, runs JetStream3 either in MiniBrowser (official, Tools/Scripts/run-benchmark) or headless in the jsc shell (PerformanceTests/JetStream3/cli.js), compares per-subtest with Tools/Scripts/compare-results (Welch + FDR), and can narrow a regression to a loop via profiling and microbenchmarks.
user-invocable: true
allowed-tools:
  - Bash(make release:*)
  - Bash(Tools/Scripts/run-benchmark:*)
  - Bash(Tools/Scripts/compare-results:*)
  - Bash(git stash:*)
  - Bash(git rev-parse:*)
  - Bash(git log:*)
  - Bash(git merge-base:*)
  - Bash(git diff:*)
  - Bash(cp:*)
  - Bash(mkdir:*)
  - Bash(ls:*)
  - Bash(cat:*)
  # Linux / headless / 32-bit container path:
  - Bash(wkdev-enter:*)
  - Bash(taskset:*)
  - Bash(python3:*)
  - Bash(grep:*)
  - Bash(shuf:*)
  - Bash(seq:*)
  - Bash(printf:*)
  # Linux browser-run quiescing (display blanking / suspend):
  - Bash(gsettings:*)
  - Bash(systemd-inhibit:*)
  - Bash(xset:*)
---

# Measuring a JSC change on JetStream3 (statistically)

Compare two builds of a JSC/WebKit change per-subtest with `Tools/Scripts/compare-results` (Welch's
t-test + FDR). `$WEBKIT_ROOT` is the repo root; run every `Tools/Scripts/*` from there. **baseline** =
before the change (ToT); **patched** = after. JetStream3 is **bigger-is-better**: `b/a > 1` means
patched is faster, `b/a < 1` is a regression.

## Pick a run mode first — it decides which build and commands you use

| | Browser | Headless (jsc shell) |
| --- | --- | --- |
| Platform | **macOS** with an awake display, or **Linux/WPE with a Wayland display** (works in a wkdev container) | any platform; needs no display |
| Runner | `Tools/Scripts/run-benchmark` + MiniBrowser (macOS) or Cog (WPE) | `PerformanceTests/JetStream3/cli.js` in `jsc` |
| Build needed | full `make release` (both sides) | a `jsc` build is enough |
| Fidelity | official; matches how regressions are validated | same driver, same output JSON, slightly different engine config |
| Follow | [Browser rounds (macOS)](#browser-rounds-macos) or [Browser rounds (Linux/WPE)](#browser-rounds-linuxwpe-cog-in-a-wkdev-container) | [Headless rounds (any platform)](#headless-rounds-any-platform) |

**Default to browser mode with the official harness (`run-benchmark`) whenever a display is
available — on Linux/WPE too.** Use headless only when there is no display at all, or to iterate
fast / root-cause — the `jsc-profile` and `jsc-microbenchmark` skills both build on the headless
jsc run.

Everything except the "run the rounds" step is **identical across modes**: scoping, choosing
baseline/patched, `compare-results`, the decision rule, root-causing, and reporting are shared and
platform-neutral. Only the two run-mode sections differ.

## What counts as a regression — read before reporting

JetStream3 aggregates ~60 subtests, so the **overall geomean is extremely stable**. Apply this scale
in *every* report:

- **Overall score: a 0.1% FDR-significant regression is HUGE.** The overall noise floor is ~±0.1%, so
  a statistically significant 0.1% drop is a real, ship-blocking regression. 0.2% overall is routinely
  taken seriously. Do not dismiss sub-1% overall moves.
- **Per-subtest: a >1% FDR-flagged move on a non-noisy subtest is real** and worth root-causing.
- **Noisy subtests** (json-parse-inspector, doxbee, Babylon, splay, tsf, async-fs, and other GC-/
  startup-dominated ones) swing ±2-5% regardless of N. Believe only large, FDR-flagged moves there,
  and never gate an equivalence bound on them.

"Not significant" is not "no effect" until the CI is tight enough to rule out the standing equivalence
targets (**0.02% overall, 0.5% per non-noisy subtest** — see [Decide](#iterate-to-a-decision)). Keep
running rounds until you find a significant mover or hit those bounds.

## Scope: run exactly what the user named

Limiting subtests exists to avoid waiting for a full run, so don't turn scoping into its own wait:

- **User named subtests** (in the command args `/jsc-jetstream-compare delta-blue bigint-noble-ed25519`
  or in prose "focus on delta-blue"): run exactly those, start immediately, no confirmation.
- **User asked for a full run / "is this safe to land"**: run the full suite. It is the only way to
  get the overall geomean, which most regressions are judged on (~60 subtests, each round multi-minute).
- **Scope genuinely unspecified**: ask once with the AskUserQuestion tool (named subset for iteration
  vs full suite), then proceed. The choice changes runtime ~30x, so the one question is worth it; never
  block further.

## Baseline vs patched

- **Uncommitted working-tree change:** patched = working tree, baseline = `HEAD`. Base sha `git rev-parse HEAD`.
- **A different commit / PR:** patched = `HEAD`, baseline = `HEAD~1` (or `git merge-base HEAD main` for
  a whole branch vs trunk). Base sha = the baseline commit. The skill does **not** switch the tree to
  build this — check out and build the baseline yourself and point the run at its build dir.

The base sha keys the cached baseline build. Note which baseline you chose in the report.

These skills change the git tree only with `git stash push` / `git stash apply` (never `pop`, never
`checkout`), and only to build a baseline from your working-tree change. They never commit, amend,
push, post, or draft a commit message or comment.

**Before reporting any number, confirm the binary you measured actually contains the patch** — its mtime
is newer than your last edit, a symbol or string you added greps out of it, or a build fingerprint
matches. A number from a stale or baseline binary is worse than no number. Do not stash the user's
working-tree patch except to build the baseline, and always `git stash apply` it back.

---

## Browser rounds (macOS)

The official, representative path. Everything in this section is **macOS-only**.

### Build both sides

Confirm Release / non-ASan first, or the numbers are meaningless: `cat
$WEBKIT_ROOT/WebKitBuild/Configuration` reads `Release` and there is no `WebKitBuild/ASan` dir. Build
the patched side into `WebKitBuild/Release` and the baseline natively into a per-sha cache via
`WEBKIT_OUTPUTDIR`, so repeat runs at the same base sha skip the rebuild:

```bash
cd "$WEBKIT_ROOT"
BASE_SHA=$(git rev-parse HEAD)                  # or the baseline commit
CACHE=/tmp/js3-builds/$BASE_SHA                 # baseline build lands in $CACHE/Release

make release                                     # patched (change in tree); first build is long

if [ ! -d "$CACHE/Release" ]; then               # baseline: build ToT once, then cached per sha
  git stash push -- <changed paths>              # the only git mutation these skills make
  WEBKIT_OUTPUTDIR="$CACHE" make release          # full native build at $CACHE/Release
  git stash apply                                # restore the change; apply (never pop) keeps the stash safe
fi
ls WebKitBuild/Release/{MiniBrowser.app,jsc} "$CACHE/Release/"{MiniBrowser.app,jsc}   # sanity-check
```

**Build each side natively at its own path; never copy/rsync a build elsewhere and run it.** A
relocated WebKit build cannot launch its XPC services — MiniBrowser loops `WebContent process crashed;
reloading` (`launchd: failed lookup: name = com.apple.WebKit.WebContent, error = 3`) and the run times
out. Re-signing and copying `*.xpc` bundles do not fix it. If space is tight, delete `$CACHE/*.build`
afterward but keep `$CACHE/Release/` in place.

### Quiesce, then run interleaved

Run `./quiesce.sh on` first (a symlink here to `wk-tools/quiesce.sh`). It handles the macOS determinism
items: checks AC power / thermal / display-asleep, disables Spotlight indexing (sudo), stops an
in-flight Time Machine backup, starts `caffeinate`, reports CPU-hog daemons to quit, settles thermals,
and seeds a pinned local JetStream3 checkout. It prints `JS3_LOCAL_COPY=<path>` on its last line — pass
that as `--local-copy` to every round so each run copies a fixed checkout instead of re-cloning
upstream JetStream3.0 from GitHub (which adds network/disk noise and can shift the commit
mid-experiment). Run `./quiesce.sh off` afterward. Set a fixed display refresh rate by hand —
ProMotion/VRR is the one thing quiesce.sh can only warn about, and rAF-driven runs inherit its jitter.

`run-benchmark` runs the plan once per invocation, so **interleave the two builds across rounds** to
cancel thermal/background drift, one JSON per round. List names with `run-benchmark --plan jetstream3
--list-subtests` (this plan's set differs from the in-tree `PerformanceTests/JetStream3` — e.g. it has
`bigint-noble-ed25519`, not `-secp256k1`).

```bash
J3=/tmp/js3-runs; mkdir -p "$J3"
CACHE=/tmp/js3-builds/$(git rev-parse HEAD)
PATCHED="$WEBKIT_ROOT/WebKitBuild/Release"
LOCAL_COPY="$JS3_LOCAL_COPY"                      # printed by quiesce.sh
run_one(){ # $1=build-dir  $2=out.json
  Tools/Scripts/run-benchmark --plan jetstream3 --browser minibrowser \
    --build-directory "$1" --output-file "$2" --count 1 --local-copy "$LOCAL_COPY" \
    --subtests delta-blue bigint-noble-ed25519; }   # omit --subtests for the full suite
for i in $(seq 1 8); do k=$(printf %02d $i)
  if [ $((i%2)) -eq 0 ]; then
    run_one "$CACHE/Release" "$J3/base_$k.json";    run_one "$PATCHED" "$J3/patched_$k.json"
  else
    run_one "$PATCHED" "$J3/patched_$k.json";       run_one "$CACHE/Release" "$J3/base_$k.json"
  fi
done
```

**Pass list arguments literally** (`--subtests delta-blue bigint-noble-ed25519`), never through an
unquoted variable. The macOS Bash tool runs **zsh**, which does not word-split, so `--subtests $SUB`
passes the whole string as one arg and the run fails with `... is not a valid subtest`. Literal args
are correct on every shell. For the full suite, drop `--subtests`, run ~6-8 rounds in the background,
and **verify the first round wrote a valid JSON** before waiting on the loop (a mis-typed subtest run
fails in seconds).

### macOS run requirements

- **Awake, attached display, required.** MiniBrowser renders in a real window and JetStream3 is
  rAF-driven; on a headless/asleep display it logs `Running <subtest>:`, stalls, and times out.
  `caffeinate` does not wake the display. Check with
  `python3 -c "import Quartz; print(Quartz.CGDisplayIsAsleep(Quartz.CGMainDisplayID()))"` (1 = asleep).
  No live display → use headless instead.
- **First run needs pyobjc:** if it dies with `No module named 'objc'`, run
  `python3 -m pip install --user pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-Quartz`.
- **Benign `Error:` log lines** (e.g. `lsof ... Port not found yet, retrying`) will trip a Monitor that
  greps `Error`. Match precise terminal states (`Traceback`) and confirm scores are appearing.

---

## Browser rounds (Linux/WPE: Cog, in a wkdev container)

`run-benchmark` browser rounds work on Linux WPE builds — including 32-bit ARM builds inside a wkdev
container — with `--browser cog`. This is the official harness; prefer it over headless when the
container has a Wayland display (`ls /run/user/$(id -u)/wayland-*`). Speedometer3 and MotionMark run
this way too (they have no headless mode at all).

- **Launch Cog, never WPE MiniBrowser, when there is no GPU.** With no `/dev/dri` in the container the
  WebProcess software-renders into SHM buffers; MiniBrowser's `WindowViewBackend` logs
  `cannot yet handle wpe_fdo_shm_exported_buffer`, presents nothing, and **rAF never fires** — pages
  load (network activity, `fetch` works) but every rAF-driven benchmark stalls forever at ~0% CPU.
  Cog's `--platform=wl` presents SHM buffers fine. (`run-minibrowser --wpe` may default Cog to
  `--platform=gtk4`, which can segfault in a container — force `wl`.) Verify rAF first with a tick
  page: `requestAnimationFrame` loop that `fetch()`es every 60 frames; watch the server log.
- **Pin each side with a PATH wrapper.** The Linux drivers search cwd then `$PATH` for
  `Tools/Scripts/run-minibrowser`, then `cog`. Run `run-benchmark` from a **neutral cwd** (not a WebKit
  checkout) and prepend a dir containing an executable `cog` wrapper per side:
  `exec env LD_LIBRARY_PATH=$COGB/core:$BUILD/lib:<deps>/lib COG_MODULEDIR=$COGB/platform \
   WEBKIT_EXEC_PATH=$BUILD/bin WEBKIT_INJECTED_BUNDLE_PATH=$BUILD/lib \
   $COGB/launcher/cog --platform=wl "$@"` where `COGB=$BUILD/Tools/cog-prefix/src/cog-build` (each
  build has its own Cog; they are not interchangeable across WPE API versions) and `<deps>` is where
  libWPEBackend-fdo etc. live (wkdev: `/jhbuild/install`). Without `WEBKIT_EXEC_PATH` Cog dies
  spawning `/usr/local/libexec/wpe-webkit-*/WPENetworkProcess`.
- **Export `XDG_RUNTIME_DIR=/run/user/$(id -u)` and `WAYLAND_DISPLAY=wayland-0`** before
  `run-benchmark`; batch container shells have neither set.
- **Old branches' python tooling may not run on the container's python** (e.g. a 2.38-era
  `run-minibrowser`/autoinstaller dies on python 3.12). Drive everything from the newer tree's
  `Tools/Scripts/run-benchmark` and pin builds via the wrappers; never mix per-tree runners.
- **Plans:** old branches may lack `jetstream3.plan` — the one from `main` works verbatim (drop it into
  `Tools/Scripts/webkitpy/benchmark_runner/data/plans/`). Pre-clone each plan's repo at its pinned
  rev and pass `--local-copy` so rounds don't re-download.
- The comparison includes the whole WPE stack (compositor, launcher version), not just JSC — expect
  rAF-bound suites (MotionMark) to reflect that; note it when reporting.

---

## Headless rounds (any platform)

Run the benchmark directly in the `jsc` shell — no display, faster to iterate. Use it only when no
display is available or for root-causing loops; with a display, prefer the official `run-benchmark`
browser mode (see the macOS and Linux/WPE sections above). `cli.js` runs the real `JetStreamDriver`.

### Launch jsc correctly

- **Select subtests with the `testList` global, not argv** — `cli.js` never reads `arguments`. Set it
  with `-e` before the driver loads, and run from the JetStream3 dir (the driver `load()`s
  `./JetStreamDriver.js` and the benchmarks by relative path). Subtest names are the `name:` fields in
  `JetStreamDriver.js` (e.g. `crypto`, `hash-map`, `stanford-crypto-*`, `bigint-noble-*`).
- **Emit the exact JSON `run-benchmark` produces** by setting `dumpJSONResults=true`, then grep the
  result line — this is what keeps the headless path on the official methodology (see
  [Compare](#compare-with-compare-results)).
- **Library path:** a Linux JSCOnly `bin/jsc` is statically linked to its sibling `lib*` and runs in
  place; if a build ever needs it, use `LD_LIBRARY_PATH=$DIR/lib`. On macOS prefix
  `DYLD_FRAMEWORK_PATH=$DIR` (a bare `WebKitBuild/Release/jsc` otherwise links the *system*
  JavaScriptCore and dies with `dyld: Symbol not found`). **Never use `Tools/Scripts/run-jsc`** — it
  injects `--useDollarVM=1` and may wrap jsc in lldb, both of which perturb timing.

```bash
cd "$WEBKIT_ROOT/PerformanceTests/JetStream3"
DIR="$WEBKIT_ROOT/WebKitBuild/JSCOnly/Release"                       # Linux JSCOnly example
"$DIR/bin/jsc" -e 'var dumpJSONResults=true; var testList=["hash-map"];' cli.js \
  | grep '^{"JetStream3.0"' > /tmp/js3-runs/patched_r01_hash-map.json
# prints {"JetStream3.0":{"metrics":{"Score":["Geometric"]},"tests":{...}}}
```

### Compare engine configs in one build

Pass jsc flags to sweep tiers: `--useDFGJIT=`, `--useConcurrentJIT=`, `--useFTLJIT=`. A bogus flag is
rejected with `ERROR: invalid option`, so a typo can't silently pass. On **32-bit ARM there is no FTL**
(64-bit only), so DFG is the top tier and a `--useDFGJIT=0` config is LLInt+baseline only — it scores
far lower, which is expected, not a regression.

### 32-bit ARM specifics

- **Run one subtest per `jsc` process, then assemble per-round JSONs.** A shared-process full-suite
  `cli.js` run OOMs on 32-bit: the shell driver never disposes each benchmark's global (only the
  browser/iframe and d8/`Realm.dispose` paths free it — `JetStreamDriver.js` ~line 684), so memory
  climbs and the suite dies with `RangeError: Out of memory` after ~24 tests. One process per subtest
  is the faithful equivalent of run-benchmark's per-iframe isolation (fresh realm, freed after) and
  lets each benchmark complete.
  1. Per (round, cell, subtest): one `jsc` invocation with `dumpJSONResults=true` and a single-element
     `testList`; save the result line to `<cell>_r<NN>_<subtest>.json`. Interleave cells per subtest,
     randomize subtest order each round (`shuf`), pin with `taskset -c 2-9`.
  2. Assemble each (cell, round)'s per-subtest JSONs into one round JSON whose `JetStream3.0.tests` is
     the **union** of the per-test score objects, copied verbatim (no stats). `benchmark_json_merge.py`
     (`mergeJSONs`/`deepAppend`) cannot do this — it requires identical test sets and `KeyError`s
     otherwise — so combine structurally, and restrict every emitted round to the **intersection** of
     subtests that succeeded in all cells/rounds.
  3. Compare with `python3 Tools/Scripts/compare-results ...` (invoke via `python3`; the script's
     `#!/usr/bin/env python3 -u` shebang fails on Linux).
  A reusable implementation is at `~/Development/.../OpenSource/js3_runloop.sh` + `js3_combine.py`.
- **Skip tests that can't run headless on 32-bit, excluded equally from both cells.** Large/SIMD wasm
  tests crash (`tfjs-wasm`, `tfjs-wasm-simd`, `argon2-wasm`, `argon2-wasm-simd`, `8bitbench-wasm`);
  `gcc-loops-wasm`, `HashSet-wasm`, `quicksort-wasm`, `richards-wasm`, `tsf-wasm` run fine.
  `Worker`-based tests (`segmentation`, `bomb-workers`) throw `ReferenceError: Can't find variable:
  Worker` in the shell. A failing subtest just drops out of the intersection — note which you skipped.

### Linux quiescing

A server-class box is usually already quiet — check, don't assume:

```bash
nproc
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor   # want performance (often already set,
                                                            # and not writable inside a container)
cat /proc/loadavg                                           # want it low
cat /sys/class/thermal/thermal_zone0/temp                   # server ARM idles ~35C, no throttling
```

- **Pin every run with `taskset -c <lo-hi>`** (e.g. `taskset -c 2-9`) so placement is identical across
  cells; give concurrent-JIT threads room (don't pin to a single core when `--useConcurrentJIT=1`).
- **`caffeinate` and `quiesce.sh` do not exist on Linux.** For a headless jsc run, screen blanking is
  irrelevant (no display dependency); only a full system suspend matters. Check
  `gsettings get org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type` — if `'nothing'`, the
  box never idle-suspends on AC. Otherwise hold `systemd-inhibit --what=idle:sleep --mode=block
  --why=bench sleep <dur>` in the background. (A *browser* run on Linux does need blanking stopped —
  Wayland: `gsettings set org.gnome.desktop.session idle-delay 0` and `... power idle-dim false`; X11:
  `xset s off; xset -dpms`. `systemd-inhibit`'s idle lock alone does not stop GNOME blanking.)
- The **Bash tool here runs bash** (unquoted variables *do* word-split), but still build `shuf` lists
  and arrays explicitly rather than relying on it.

### wkdev container access

Interactive `wkdev-enter --name <ctr>`; batch `wkdev-enter --name <ctr> --exec -- bash -lc '<cmd>'`.
Paths are in-container; the host sees them under the mapped prefix (container `/home/<u>/Development`
= host `/home/<u>/Development/32/Development`). Write results under the mapped prefix so you can read
logs from the host while the loop runs inside.

### Browser runs on Linux: find the Wayland display (wkdev container)

A browser round on Linux (MiniBrowser GTK, Chrome) needs a live display, and inside a wkdev
container `WAYLAND_DISPLAY`/`DISPLAY`/`XDG_RUNTIME_DIR` are usually **unset** even though the
sockets are mounted. Discover and export them:

```bash
ls /run/user/$(id -u)/            # look for wayland-* sockets; in wkdev they are symlinks
                                  # to the host compositor, e.g. wayland-0 -> /host/run/wayland-0
ls /tmp/.X11-unix/                # X fallback: Xn means DISPLAY=:n (Xwayland)
export XDG_RUNTIME_DIR=/run/user/$(id -u) WAYLAND_DISPLAY=wayland-0
```

- Both vars are required — GTK/Chromium resolve the socket as `$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY`.
- `wayland-info` is typically not installed; verify by launching the actual browser under
  `timeout 10` with a URL: exit 124 (still alive when the timeout fires, no display error on
  stderr) means the connection works. An immediate exit means it didn't.
- Chromium needs `--ozone-platform=wayland` or it tries X11.
- `run-benchmark`'s Linux drivers pass the invoking environment through to the browser (only
  `HOME` is swapped for a temp profile), so exporting the two vars before `run-benchmark` is
  sufficient. The drivers find the binary by searching **cwd then `$PATH`** for a fixed name list
  (`chrome`/`chromium`/..., `MiniBrowser`, or `Tools/Scripts/run-minibrowser` relative to cwd) —
  to pin a specific build, prepend a wrapper dir to `PATH` and run from outside the WebKit tree.

---

## Compare with compare-results

Shared across modes. `-a` = baseline, `-b` = patched; pass all rounds (they merge). Always use this
script — never hand-roll Welch/CI, which changes the methodology and makes results non-comparable.

```bash
Tools/Scripts/compare-results -a /tmp/js3-runs/base_*.json -b /tmp/js3-runs/patched_*.json \
                              --breakdown --sort --csv /tmp/js3-runs/breakdown.csv
# Linux: prefix `python3` (the script's shebang fails there).
```

- `--breakdown` — per-subtest table with FDR-corrected `(significant)` flags. **Trust only flagged
  rows.**
- `--sort` — order by `b/a`.
- `--category-breakdown` — split startup / worst / average. A move weighted toward First/Worst with a
  small Average is a **compile-time/startup** or diffuse-codegen cost, not a hot steady-state loop.

## Iterate to a decision

Run more rounds until one of these ends it:

- **Significant mover** — a subtest is FDR-`(significant)`, or the overall geomean move is significant.
  Apply the scale above (0.1% overall significant = real; >1% non-noisy subtest = real). Report; done.
- **Equivalence** — the 95% CI rules out a move bigger than the standing margins: **0.02% overall,
  0.5% per non-noisy subtest.** Keep running until every non-noisy subtest's CI rules out a >0.5%
  regression and (full-suite only) the overall geomean rules out >0.02%.

`compare-results` gives significance, not the CI, so compute the **95% CI on `b/a`** yourself
(two-sample/Welch on the per-round subtest scores; the smallest regression ruled out is `1 - lowerCI`).
Stop a subtest once `1 - lowerCI <= 0.5%` (overall `<= 0.02%`).

**Budget for the margins — they are aggressive.** CI half-width shrinks as `1/sqrt(N)`, so reaching
margin `θ` from half-width `w` costs ~`(w/θ)^2` more samples. The overall geomean hits ~±0.1% with
modest N, so **0.02% overall needs ~25x more rounds** — many tens of full-suite rounds; run them in the
background. Per-subtest noise ranges from ~±0.05% (float-mm) to ~±5%.

- **Never gate the loop on a noisy subtest.** Exempt the noisy list; report its floor as noise-gated,
  not equivalence.
- **Two things no amount of N fixes:** (1) if a point estimate already sits below `1 - θ` (e.g.
  b/a = 0.989 vs a 0.5% target), more rounds *confirm a regression*, not rule one out. (2) A targeted
  subset gives no overall geomean, so the 0.02% target applies only to a full-suite run.
- **Set a compute budget** and log progress (current `1 - lowerCI` per subtest each batch); on hitting
  it, report the tightest bound achieved and which subtests still gate it.

## Root-cause a confirmed regression (on request)

Narrow a confirmed-slower subtest to a loop. Delegate the code-tracing parts (where is this loop, what
changed between the two builds) to a subagent so the main context stays on the measurement state — but
run the interleaved measurement loop itself in one context.

1. **Profile the headless jsc run** (`jsc-profile` skill). Start from the tier breakdown: cost in
   FTL/DFG/Baseline (generated JS) → use the bytecode profiler to find the hot CodeBlock; cost in
   C/C++ → use samply to find the hot native function.
2. **Decompose by phase.** Split the subtest into sub-operations and time each on both builds
   (interleaved, many reps). Require BH/Bonferroni-corrected significance across the k phases, not raw p.
3. **Extract a microbenchmark** of the suspect loop (`jsc-microbenchmark` skill). If it reproduces the
   regression, that's the locus; if not, the cost is elsewhere or **diffuse** — a thin, broad codegen
   change across many functions, which shows as a small same-direction shift on many subtests plus a
   First/Worst category weighting.

Example: a DFG IntegerRangeOptimization change gave a confirmed -0.54% on bigint-noble-ed25519, but no
single loop reproduced it — SHA-512 was inert because its `Uint32Array[i] | 0` reads truncate
immediately, killing the removed range fact; per-phase and the isolated scalar-mult loop were both
non-significant; the category breakdown was First-weighted. Conclusion: diffuse codegen cost. **Report
"diffuse / not localizable" honestly rather than forcing a culprit.**

## Report

- Trust per-subtest claims only where `compare-results` marks `(significant)`.
- State results against the regression scale (0.1% overall significant = real; >1% subtest = real).
- **A broad same-direction shift across nearly all subtests** — including ones the change can't touch
  (crypto, wasm) — is machine drift or a uniform compile-time cost; use `--category-breakdown` to tell
  them apart.
- Report: baseline used (HEAD~1 vs main vs working tree), run mode (browser/headless), rounds run,
  overall `b/a` + pValue, the FDR-significant movers, and (if root-caused) the loop or the diffuse finding.

## Shared determinism principles

The single biggest lever is **interleaving baseline/patched every round** (the loops above alternate by
parity); never run all-baseline-then-all-patched, which aliases time-of-day/thermal drift into the
result. Beyond that:

- **Same build config both sides:** Release, non-ASan, same compiler. The per-sha baseline cache keeps
  the ToT build byte-stable across re-runs.
- **Deterministic inputs:** the `jetstream3` plan sets `deterministicRandom: true`; for your own
  harnesses seed a PRNG — never `Math.random()` / `Date.now()`.
- **Warm up to steady tier, then take median (and min) of many samples.** Many outer invocations beat
  many inner iterations for cancelling drift — exactly what `run-jsc-benchmarks` does, so prefer it for
  microbenchmarks.
- Keep raw JSONs/CSVs under `/tmp/js3-runs/` so the user can inspect every number.
- **Long / overnight runs:** write each round's JSON the moment it finishes so the run survives a restart or context compaction, and never stop because the context grew long or was compacted — the only stop is the decision rule. Kill leftover `jsc`/profiler processes between and after rounds (a hung profiler times out the whole suite — `exit 124`, no JSON), pin with `taskset`, and give each round a timeout.
- Platform quiescing lives with each run mode: `quiesce.sh` under [Browser](#quiesce-then-run-interleaved),
  `taskset`/governor under [Linux quiescing](#linux-quiescing).
