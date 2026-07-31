---
name: jsc-jetstream-compare
description: Use when measuring the JetStream3 (or Speedometer/MotionMark) performance impact of a JavaScriptCore or WebKit change — e.g. "run JetStream3 to measure this PR", "is there a regression on delta-blue", "compare perf of two commits", "benchmark this on 2 cores". Builds before/after, runs JetStream3 either in MiniBrowser (official, Tools/Scripts/run-benchmark) or headless in the jsc shell (PerformanceTests/JetStream3/cli.js), compares per-subtest with Tools/Scripts/compare-results (Welch + FDR), and can narrow a regression to a loop via profiling and microbenchmarks. Every run pins the CPU frequency unless told otherwise. Also covers core-constrained (small-device / N-core) runs, the DVFS traps that invalidate them, and the mandatory-GPU rules for Speedometer and MotionMark rounds.
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
  # Core-constrained runs: config inspection, live verification, clock measurement:
  - Bash(nproc:*)
  - Bash(pgrep:*)
  - Bash(ps:*)
  - Bash(uclampset:*)
  - Bash(perf stat:*)
  - Bash(nvidia-smi:*)
  - Bash(comm:*)
  - Bash(sort:*)
  - Bash(diff:*)
---

# Measuring a JSC change on JetStream3 (statistically)

Compare two builds of a JSC/WebKit change per-subtest with `Tools/Scripts/compare-results` (Welch's
t-test + FDR). `$WEBKIT_ROOT` is the repo root; run every `Tools/Scripts/*` from there. **baseline** =
before the change (ToT); **patched** = after. JetStream3 is **bigger-is-better**: `b/a > 1` means
patched is faster, `b/a < 1` is a regression.

## Pin the CPU frequency before every run (required)

**Every performance run in this skill pins the CPU frequency, unless the user explicitly asks otherwise.**
Do this before the first round, for every suite (JS3, SP3, MotionMark) and both run modes — not just
core-constrained runs. An unpinned clock is the most common way to produce a *confident, low-variance,
completely wrong* number: DVFS responds to how much idle time a workload leaves, so it varies **between
cells**, and the resulting delta looks exactly like a code change.

```bash
# Linux — do this on the HOST (see below), then verify:
sudo cpupower frequency-set -g performance          # or: write 'performance' to each policy's
                                                    # scaling_governor, or raise scaling_min_freq to max
for p in /sys/devices/system/cpu/cpufreq/policy*; do cat $p/scaling_governor; done | sort -u  # want only 'performance'
```

- **`/sys` is typically read-only inside a container, and `sudo` there cannot write it** — pin on the
  host. If you have no host access, use the `uclamp` fallback in
  [Pinning the clock from inside a container](#pinning-the-clock-from-inside-a-container-uclamp),
  which needs no privileges at all.
- **Verify the clock you actually got, and don't trust `scaling_cur_freq` to tell you** — it is the
  governor *setpoint* on some drivers, so an idle core reads the policy max (see
  [the clock-measurement notes](#simulating-a-small-device-pinning-to-exactly-n-cores-eg-2)).
- **macOS has no governor knob.** The analogous requirements are AC power, settled thermals, no
  competing load, and a **fixed display refresh rate** (ProMotion/VRR jitters rAF-driven runs) — all
  handled or warned about by `quiesce.sh`.
- **If you cannot pin it, DO NOT PROCEED!**
- If the user *does* ask for the machine's default governor (e.g. to reproduce a user-visible effect),
  that's fine — state the governor and range in the report so the number isn't mistaken for a pinned one.

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
- **A different commit / PR:** patched = `HEAD`, baseline = `HEAD~1` (or the branch point for a whole
  branch vs trunk). Base sha = the baseline commit. The skill does **not** switch the tree to
  build this — check out and build the baseline yourself and point the run at its build dir.
- **`git merge-base HEAD main` is only the branch point if local `main` is current — verify it.** A
  stale local `main` silently gives a baseline hundreds or thousands of commits too old (a real case
  here: 2860 commits / 23k files, WebKitGTK 2.53.3 vs 2.53.4), so you measure release drift, not the
  patch. Check `git log --oneline -1 main` against the remote, or just use `HEAD~<N>` / the actual
  parent when the branch's commit count is known. Sanity-check the diff size:
  `git diff --stat <baseline>..HEAD` should look like the change under test.

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

**The plan's `subtests` list is incomplete — it gates `--subtests`, not the suite.** `jetstream3.plan`
enumerates only **74 of the 77 subtests JetStream3.0 actually runs**; `bomb-workers` and `segmentation`
are among the missing ones. Consequences, both silent-ish:

- With **no** `--subtests` flag the full 77 run regardless — the list only declares what `--subtests`
  *may* select. So a full-suite comparison is unaffected.
- `--subtests bomb-workers` prints `... is not a valid subtest, skipping` and produces an **empty run**.
  If you need one of the missing names, copy the plan, add the names to its `subtests` list, and pass
  the **path** to `--plan` (`_find_plan_file()` accepts an existing path) — no need to dirty the tree.

Both missing names are `Worker`-driven, and they are also the ones the headless jsc path cannot run at
all — so check whether they are present before concluding a change is Worker-neutral.

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
cat /proc/loadavg                                           # want it low
cat /sys/class/thermal/thermal_zone0/temp                   # server ARM idles ~35C, no throttling
```

**The CPU frequency must already be pinned before you get here** — that is a hard prerequisite for every
run, not a Linux nicety; see
[Pin the CPU frequency before every run](#pin-the-cpu-frequency-before-every-run-required). Confirm it
rather than assuming a server-class box is set up that way (this box defaults to `schedutil` 1.0–3.0 GHz).

- **Pin every run with `taskset -c <lo-hi>`** (e.g. `taskset -c 2-9`) so placement is identical across
  cells; give concurrent-JIT threads room (don't pin to a single core when `--useConcurrentJIT=1`). To
  pin down to a small core count on purpose (a 2-core device simulation), `taskset` alone is not
  sufficient — see [Simulating a small device](#simulating-a-small-device-pinning-to-exactly-n-cores-eg-2).
- **`caffeinate` and `quiesce.sh` do not exist on Linux.** For a headless jsc run, screen blanking is
  irrelevant (no display dependency); only a full system suspend matters. Check
  `gsettings get org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type` — if `'nothing'`, the
  box never idle-suspends on AC. Otherwise hold `systemd-inhibit --what=idle:sleep --mode=block
  --why=bench sleep <dur>` in the background. (A *browser* run on Linux does need blanking stopped —
  Wayland: `gsettings set org.gnome.desktop.session idle-delay 0` and `... power idle-dim false`; X11:
  `xset s off; xset -dpms`. `systemd-inhibit`'s idle lock alone does not stop GNOME blanking.)
- The **Bash tool here runs bash** (unquoted variables *do* word-split), but still build `shuf` lists
  and arrays explicitly rather than relying on it.

---

## Simulating a small device: pinning to exactly N cores (e.g. 2)

Applies to **both** run modes — the browser sections above and the headless one. Read it in full before
the first round; most of these mistakes are only detectable while a round is in flight.

Asked to measure "on 2 cores" (mobile/embedded-like contention), `taskset` alone is **not enough** and
will silently produce a nonsense configuration. Six things are required; verify each at runtime, never
assume. Items 5–6 are about keeping the two cells honest and are not 2-core-specific, but a
core-constrained A/B is where they bite hardest (small deltas, long rounds, many rebuilds).

Every one of these was hit in real work here and each produced *plausible* numbers while being wrong —
that is the hazard. Budget for a **preflight step that asserts the two cells differ only by the patch**
(same lib set, same core count, same core list, same governor, correct RPATH) and re-assert it after
every round, rather than trusting setup done once at the start.

**1. Pin the browser, not the harness.** Put `taskset -c 4,5` in a per-cell wrapper that `exec`s the
browser, so the UI process and everything it spawns (WebProcess, GPUProcess, NetworkProcess, even
`gst-plugin-scanner`) inherits the mask, while `run-benchmark`'s python driver and its http server stay
on the other cores and don't steal measured CPU. `run-benchmark` finds the browser by searching cwd then
`$PATH` for a fixed name, so a dir containing an executable named `MiniBrowser` (or `cog`), prepended to
`PATH`, pins the round to one build. Run from a neutral cwd so a checkout's
`./Tools/Scripts/run-minibrowser` isn't picked up instead. Avoid core 0 (more IRQ work).

**2. Override the core count — mandatory.** `WTF::numberOfProcessorCores()` on Linux is
`sysconf(_SC_NPROCESSORS_ONLN)` (`Source/WTF/wtf/NumberOfCores.cpp`) and **ignores the affinity mask**,
so JSC sizes its thread pools for the whole machine. Measured on an 80-core box pinned to 2 cores:

| | GC markers | DFG | FTL | Wasm | Baseline |
|---|---|---|---|---|---|
| unpinned | 8 | 2 | 7 | 79 | 3 |
| `taskset -c 4,5` only | 8 | 2 | 7 | **79** | 3 |
| `taskset` + `WTF_numberOfProcessorCores=2` | 2 | 1 | 1 | 1 | 2 |

Always export `WTF_numberOfProcessorCores=<N>` (its own env override, same file) in the wrapper so it
reaches every child. Confirm with `jsc --dumpOptions=2 -e '' | grep -E 'numberOf(GCMarkers|.*CompilerThreads)'`.

**3. Verify in the WebProcess, not just the UI process**, while a round is in flight. **Never identify
WebKit's auxiliary processes by `/proc/<pid>/comm` — the kernel truncates it to 15 characters**, so
`WebKitWebProcess` appears as `WebKitWebProces` and `WebKitNetworkProcess` as `WebKitNetworkPr`. An
exact-comm match therefore finds only `MiniBrowser` and **silently reports a plausible-looking subset**
(here: 27 threads instead of 734, missing every JIT/GC/marking thread — i.e. all the interesting ones).
Match `argv[0]`'s basename from `/proc/<pid>/cmdline` instead, and sanity-check that all the process
kinds you expect actually appear before believing any per-thread summary. `pgrep -f` is also required
for these names, and warns when given a >15-char pattern:

```bash
for p in $(pgrep -f 'bin/(MiniBrowser|WebKitWebProcess|WebKitGPUProcess)'); do
    grep -E '^Cpus_allowed_list' /proc/$p/status              # must be your core list
    tr '\0' '\n' < /proc/$p/environ | grep WTF_numberOfProc   # must be present
    ps -L -o tid=,cls=,comm= -p $p                            # thread list, for a cell-vs-cell diff
done
```
Capture this for both cells and diff it: identical thread inventories are cheap evidence that the cells
differ only by the patch, and any live per-process property the patch changes doubles as a **build
fingerprint** proving which build a round actually measured, independently of paths.

**4. Pin the CPU frequency — mandatory everywhere, and doubly so here.** Follow
[Pin the CPU frequency before every run](#pin-the-cpu-frequency-before-every-run-required); this section
only adds what is specific to a core-constrained run. Two cores running a benchmark leave a *lot* of idle
time, and DVFS reacts to exactly that: with `schedutil` over a wide range (e.g. 1.0–3.0 GHz) a workload
leaving half of two cores idle sinks toward the **minimum** frequency, so an unpinned core-constrained
run measures the governor rather than the code. The effect is large enough to swamp any plausible code
delta — a real case: MotionMark `Suits` scored 208 @ 1.84 GHz in one cell vs 126 @ 1.10 GHz in the other,
a "47% regression" that was **entirely clock** (score/GHz flat at 113.2 vs 115.4).

Because the clock is the dominant term, **verify the achieved frequency per cell even after pinning**,
and treat any cross-cell clock gap as invalidating that comparison.

- **`scaling_cur_freq` is the governor *setpoint* on some drivers, not an achieved clock.** With
  `cppc_cpufreq` under `performance` an entirely idle core reads the policy max (3000000), so sampling
  it "confirms" a clock that was never delivered. Measure the **effective** clock instead, per browser
  PID: `perf stat -e cycles` divided by `task-clock` over the sampled window. Report score/GHz, not just
  score. A clock gap between cells invalidates the comparison; a **flat score/GHz across cells means
  you found a DVFS artifact, not a code change.**
- **When `perf` is unavailable, use the ACPI CPPC feedback counters.** In a container the PMU is often
  not exposed at all (`perf stat -e cycles:u` → `EINVAL`, "No supported events found") even at
  `perf_event_paranoid=2`. On a CPPC platform (`scaling_driver` = `cppc_cpufreq`, common on server ARM)
  `/sys/devices/system/cpu/cpu*/acpi_cppc/` is world-readable and needs no privileges:
  `feedback_ctrs` gives `ref:<N> del:<N>`, and
  `MHz = reference_perf * (Δdel/Δref) * nominal_freq/nominal_perf`. Snapshot before and after and diff;
  the counters are cumulative, so a slow sampling rate costs only trajectory resolution, not accuracy.
- **Calibrate any clock metric against known duty cycles before trusting it, and state which average
  it is.** Pin busy loops at 100/50/25% duty and check what the metric reads. The CPPC pair measured
  here is **wall-clock referenced** (100% → 3000 MHz, 50% → 2185, 25% → 1282), i.e. a *time*-weighted
  average that **includes idle time** — not "the clock while executing". Consequences: on a many-core
  box a few-thread benchmark leaves every core mostly idle, so the per-core average sits near the
  policy floor and the machine-wide figure says more about idleness than about the workload. Report the
  machine-wide average *and* a busy-time-weighted one, and do not try to invert
  `M = busy·f_busy + (1−busy)·f_idle` for `f_busy` — at low busy fractions the inversion amplifies
  `/proc/stat` tick error and returns impossible values (>policy max). To get the clock the work
  actually ran at, `taskset` the browser to a few cores so the busy fraction is high and the metric
  reads it directly.
- **Measure the clock in dedicated rounds, excluded from scored data — the probe perturbs the run.**
  Attaching a `perf` clock probe every 8 s stretched one JS3 round from the normal 220–247 s to
  **1028 s** at 53% CPU. Never score a round you instrumented.
- Suspect this whenever a regression is concentrated in one low-utilisation subtest while CPU-saturating
  ones are flat, or when per-thread CPU time and work distribution are identical between cells yet the
  score differs. `voluntary_ctxt_switches`/`nonvoluntary_ctxt_switches` from `/proc/<pid>/status` plus
  per-thread CPU time are high-signal and far cheaper than a sampled profile for scheduling-shaped
  effects — and in a container `samply`/`perf` may only manage ~18 Hz with unsymbolicatable leaves,
  so counters may be all you get.
- **Which comparisons survive an unpinned clock:** cells built from the same source that differ only in
  their environment (a tuning env var, a variant worktree) share whatever DVFS regime the machine is in
  and stay comparable, whereas a baseline-vs-patched delta does not. So if pinning is genuinely
  unavailable, restructure the question into same-build cells rather than trusting a baseline delta.

**5. Keep each cell loading its own libraries** (GTK/container builds — the highest-severity trap here,
because it produces scores from the *wrong build*). In a webkit-container-sdk setup `/sdk/webkit` is a
**single symlink shared by every tree**, and all binaries carry the absolute RPATH
`/sdk/webkit/WebKitBuild/GTK/Release/lib`. `build-webkit` repoints that symlink, so **after building
tree B, tree A's MiniBrowser loads B's libraries** — or the *system* WebKitGTK, which at least fails
loudly (`symbol lookup error`). `LD_LIBRARY_PATH` cannot fix it: `DT_RPATH` wins. Either:

- point the symlink per round and assert it —
  `WEBKIT_SOURCE_DIR=<tree> Tools/Scripts/container-sdk-rootdir-wrapper --create-symlink`, check the
  target inside the wrapper immediately before `exec`, and re-check after the round finished; or
- run each cell under that wrapper's default mode (a private mount namespace bind-mounting the cell's
  own tree at `/sdk/webkit`) — hermetic, and the better default.

Keep the baseline in a `git worktree` rather than stashing back and forth, so both trees exist
simultaneously and can be rebuilt independently.

**6. Build the *same* target set in both cells, then diff the outputs.** Trimming the build to save time
is how cells silently diverge: `--makeargs="MiniBrowser WebKitWebProcess ..."` does **not** build
`webkitgtkinjectedbundle`, so one cell logged `Error loading the injected bundle`, ran without it, **and
still produced scores** while the other loaded it. Name `webkitgtkinjectedbundle` explicitly in both,
then diff `lib/*.so*` and `bin/` between the trees (`LC_ALL=C sort` the listings — unsorted input makes
`comm` lie). Grep every round's log for `Error loading the injected bundle` and discard the round.

#### Building two trees on one box

- **Don't run two `-j40` WebKit builds concurrently on a ~125 GB box — it global-OOMs** (`cc1plus` on
  `JSDOMWindow.cpp` peaks ~4.5 GB RSS). Build the cells **sequentially at `-j32`**.
- **GCC 15.2 `-Werror=uninitialized` false positives** break `TestWebKitAPI/Tests/WTF/HashSet.cpp` in
  any tree (and WebCore `UnifiedSource-css-18.cpp` on older `main`).
  `-DDEVELOPER_MODE_FATAL_WARNINGS=OFF` is the clean escape — diagnostics only, **no codegen change**,
  so it does not affect the comparison. Prefer it over patching source in one cell only.
- A 5 GiB ccache is too small for two WebKit trees (~22% hit rate); raise it or expect full rebuilds.

#### Pinning the clock from inside a container (`uclamp`)

When `/sys` is read-only and you have no host access, prompt the user. DO NOT TRY TO WORK AROUND THIS!

#### Sandboxing, budget, and what to report

- **bwrap does not work inside a podman container** ("Bubblewrap does not work inside of this container")
  regardless of launcher, so **all numbers from such a container are unsandboxed** — state that in the
  report, since the sandbox has its own cost.
- **Budget:** on 2 pinned cores a full 77-subtest JS3 round is ≈ **4 min**; a 6-subtest subset ≈ **48 s**.
  Interleaved, that is ~8 min per JS3 round-pair — so 6 rounds/cell is roughly an hour, and SP3 rounds
  (12/cell is a reasonable target for a ~1% effect) dominate a session. Plan the round count against the
  equivalence margins *before* starting.
- **A change can move two suites in opposite directions**, so when the user cares about more than JS3,
  run each suite they named and report them separately with their movers named — an overall geomean from
  one suite is not evidence about another.

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

### GPU rendering is MANDATORY — never accept software rendering

Every browser round (**Speedometer and MotionMark especially**) must render on the GPU. **Never
permit software rendering** — it is not just noisier, it produces a *different, meaningless* score
(MotionMark ≈ **1050 on the GPU vs ≈ 2.6 on llvmpipe** — a ~400× gap; Speedometer ≈ **7.2 vs 6.8**).
A "quiet, low-variance" software number is worthless. Enforce this:

**JS3 is the exception:** it is JS-bound and GPU-invariant, so a software compositor is acceptable for
JetStream3 (it only needs rAF to fire at all). SP3 and MotionMark are not — never accept software there.
Do not assume the box is headless because you reached it over ssh: a wkdev container commonly has a real
mutter/GNOME Wayland desktop and a discrete GPU mounted through.

- **Detect the fallback and reject the round.** After each run, grep its log for
  `libEGL.*fd -1`, `llvmpipe`, `swrast`, `SwiftShader` — any hit means the WebProcess software-rendered;
  delete that JSON and re-run. Bake this guard into the loop (a round that silently fell back to
  software otherwise pollutes the comparison).
- **On a workstation with a real GPU, use the real desktop compositor `wayland-0`** (mutter, backed by
  the discrete GPU): `export WAYLAND_DISPLAY=wayland-0 GDK_BACKEND=wayland
  XDG_RUNTIME_DIR=/run/user/$(id -u)` and **`unset DISPLAY`** so GTK cannot quietly fall back to
  Xwayland (which reintroduces software paths). Confirm real HW with `nvidia-smi` (util climbs,
  `fd -1` absent) — a bare `WAYLAND_DISPLAY=wayland-0` is not proof by itself.
- **The display must stay awake for the whole experiment.** mutter throttles `requestAnimationFrame`
  frame-callbacks for any surface it isn't presenting — a **blanked/asleep monitor, a locked session,
  or an occluded/unfocused window all stall every rAF-driven benchmark** (SP3/MM/JS3 hang, then
  time out). Symptom: the page loads (resources fetched) but never posts results. Keep the display on
  (`gsettings set org.gnome.desktop.session idle-delay 0`, screensaver off) — this is the Linux analog
  of macOS quiesce.sh's App-Nap-off + "raiser".
- **A headless Weston (`--backend=headless`) is a trap for MM/SP3.** Its default renderer is
  **pixman = software** (→ the 400× -low score above); rAF *does* fire (good for JS3), but the numbers
  are software. `weston --renderer=gl` runs on the GPU and is offscreen (doesn't steal the screen, no
  display-awake dependency), but its offscreen presentation gives an **unrepresentative** MM score
  (≈ 33 vs 1050 on the real desktop) — fine only as a last-resort rAF unblocker for JS3, never for a
  real MM/SP3 number. Prefer `wayland-0`.
- **Run GPU browser rounds UNCONTENDED.** Sustained GPU + CPU contention (e.g. a second benchmark loop
  on other cores, or parallel GPU experiments) makes the WebKitGTK+NVIDIA WebProcess **crash mid-run**
  (`WebProcess CRASHED`, no JSON) at varying points. The identical MM run that crashes under contention
  completes cleanly on an idle machine. Never run two GPU rounds (or a debug experiment) at once, and
  never overlap a GPU loop with another measurement loop — the overlap both crashes runs and
  contaminates the concurrent one's variance.
- **Beware self-terminating `pkill`/`pgrep`.** `pkill -f 'run-benchmark'` — or `pgrep -f <pat> | xargs
  kill`, or an `until ! pgrep -f <pat>` wait loop — kills the very shell running it whenever the pattern
  appears in that command's own `/proc/self/cmdline` (exit ~143/144, nothing runs). Naming the binary
  (`bin/MiniBrowser`) does **not** help: the string is still in your command line. Always bracket a
  character so the pattern cannot match itself: `pgrep -f 'MiniBrow[s]er'`, `pgrep -f 'run-benchmar[k]'`.
  **Bracketing is not sufficient inside a harness.** `pkill -f 'MiniBrow[s]er'` still matches any *other*
  shell whose command line contains the literal string — including the tool call that launched the
  harness, if that command merely `cat`s or `grep`s a wrapper named `MiniBrowser`. Killing browsers from
  inside a script must therefore match on **`/proc/<pid>/exe`**, which only a real browser process has
  pointing into a build dir, and skip `$$`:
  ```bash
  kill_browsers() { local pid exe; for pid in $(ls /proc | grep -E '^[0-9]+$'); do
      [ "$pid" = $$ ] && continue
      exe=$(readlink -f /proc/$pid/exe 2>/dev/null) || continue
      case "$exe" in */WebKitBuild/*/bin/MiniBrowser|*/WebKitBuild/*/bin/WebKit*Process) kill -9 "$pid";; esac
    done; }
  ```
- **Launch every long-running job with `setsid`, and have the launching tool call return immediately.**
  A backgrounded `nohup ... &` is still in the tool call's process group, so when that call hits its
  timeout the whole group gets SIGTERM — a 20-minute build died at `ninja: build stopped: interrupted by
  user` because the launching call slept past its limit. `setsid nohup cmd >log 2>&1 </dev/null & disown`
  survives, and the wait belongs in a *separate* backgrounded call.
- **Verify only one instance of a sweep/loop is running before trusting its output.** A killed tool call
  does not necessarily kill the script it launched: an earlier `sweep.sh 0 0` survived its parent's death
  and ran concurrently with a freshly launched `sweep.sh 1 1`, two browsers at a time, each killing the
  other's processes. The tell was a cycle number in the log that no live invocation could have produced.
  `pgrep -af <script>` before every launch, and treat an unexpected cycle/round label as contamination.
- **Don't gate a wait loop on a process that hasn't started yet.** `until ! pgrep -f 'run-benchmar[k]';
  do sleep 15; done` right after launching the runner in the background exits immediately — the runner
  needs a second or two to exec. Wait for the output file to appear, or sleep once before the loop.
- **Never read build progress from a `tail` of a piped ninja log.** The wrapper buffers in multi-KB
  chunks, so the last line lags by minutes and makes a healthy build look stalled (and then look like it
  leapt forward). Measure throughput from ground truth instead — `find <builddir> -name '*.o' | wc -l`
  over a fixed interval — and use an **absolute** builddir path: a `cd` inside a previous tool call does
  not persist, so a relative `find .` silently counts a different directory and manufactures a phantom
  stall.

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
- Say which frequency regime you measured in (pinned, or the governor and its range), and note anything
  that makes the numbers non-comparable to a default desktop (unsandboxed container, pinned core subset,
  overridden core count).

## Record what you learn here, not in project memory

Benchmarking sessions produce durable knowledge — a new trap, a verification step, a threshold, a tool
invocation that finally worked. **Any of it that would still be true on another machine or in another
checkout belongs in this file, edited in place. Never store a global rule in project memory:** memory is
keyed to one checkout path on one workstation and the user works across several, so a rule left there
silently fails to travel with them. Add the general form to the right section here, and let the example
that taught it stay an example.

Project memory is for what genuinely does not generalise: this box's hardware and paths, in-flight
project state, and *measured results* for a particular branch (those belong in memory, not here — this
file must stay a methodology, not a findings log). If you find a global rule sitting in memory, move it
here and delete it there.

Deliberately **out of scope for this skill**: thread-priority and scheduling tuning guidance (nice
levels, RT policies, priority brokers). Do not add it back — `uclamp` appears here only as a way to pin
the clock. Suite-specific tuning findings for a given branch go in the report or project memory.

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
- **A pinned CPU frequency is a prerequisite for every run** — see
  [Pin the CPU frequency before every run](#pin-the-cpu-frequency-before-every-run-required). The rest of
  the platform quiescing lives with each run mode: `quiesce.sh` under
  [Browser](#quiesce-then-run-interleaved), `taskset` under [Linux quiescing](#linux-quiescing). For a
  core-constrained run (`taskset` + core-count override + pinned clock + hermetic cells) see
  [Simulating a small device](#simulating-a-small-device-pinning-to-exactly-n-cores-eg-2) — `taskset`
  alone silently produces a wrong configuration.
