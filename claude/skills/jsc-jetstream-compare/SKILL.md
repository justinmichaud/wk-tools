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
# Linux, on the HOST: one command, and it does the rest of the quieting too.
wk quiesce on
wk quiesce status                                   # what it set, measured rather than claimed

# Verifying it from anywhere, including inside a workspace:
for p in /sys/devices/system/cpu/cpufreq/policy*; do cat $p/scaling_governor; done | sort -u  # want only 'performance'
```

- **`/sys` is read-only inside a workspace and `sudo` there cannot write it** — that is the sandbox
  working, not a problem to route around. `wk quiesce on` runs on the host through a privileged helper
  with a fixed allowlist; ask the person at the keyboard for it rather than looking for another way in.
- **With no host access, run the degraded mode deliberately** — see
  [Pinning the clock from inside a container](#pinning-the-clock-from-inside-a-container-uclamp).
  It needs no privileges, and its results carry a label.
- **Verify the clock you actually got, and don't trust `scaling_cur_freq` to tell you** — it is the
  governor *setpoint* on some drivers, so an idle core reads the policy max (see
  [the clock-measurement notes](#simulating-a-small-device-pinning-to-exactly-n-cores-eg-2)).
- **macOS has no governor knob.** The analogous requirements are AC power, settled thermals, no
  competing load, and a **fixed display refresh rate** (ProMotion/VRR jitters rAF-driven runs) — all
  handled or warned about by `wk quiesce`.
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
- **FDR controls error *within* one block, not across blocks — replicate a single-subtest finding in an
  independent block before reporting it as real.** Interleaving cancels drift inside a block; it does
  nothing about a level shift between blocks run hours apart, so a noisy subtest can come out flagged in
  one block and dead flat in the next. A measured case: MotionMark `Canvas Lines` (per-round CoV ~4-5%)
  gave b/a 0.958 with p=0.0019, robust to median/trimmed-mean/Mann-Whitney, and reproduced neither when
  that subtest was run alone (1.003, p=0.70, with CPU and context-switch counters identical between
  cells) nor in a fresh full-suite block hours later (0.995, p=0.77). Pooling the two blocks still reads
  0.976 p=0.017, which is how a one-block artifact survives into a report if you only ever pool. Two
  cheap guards: check whether the two blocks disagree by more than noise
  (`z = (r2-r1)/sqrt(se1^2+se2^2)`; near or above 2 means do not pool), and re-run the finding as its
  own experiment. Treat a lone flagged noisy subtest as a hypothesis, never a result.

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

**Checking the files on disk is not enough: confirm what each process actually loads.** On Linux a
WebKit-built `bin/jsc` carries **DT_RPATH** naming its own build directory, and DT_RPATH is searched
*before* `LD_LIBRARY_PATH`, so a staged copy of `bin/jsc` silently loads the library from the build dir
no matter what you set. Both cells of an A/B then run the same library and every delta is noise —
which looks exactly like "no regression", so it does not announce itself. Fingerprinting the staged
`.so` files proves nothing here; they were never loaded. Before any A/B, per cell:

```bash
readelf -d "$CELL/bin/jsc" | grep -E 'RPATH|RUNPATH'          # RPATH pointing at a build dir = trap
patchelf --set-rpath '$ORIGIN/../lib' "$CELL/bin/jsc"         # make it self-relative, once per cell
ldd "$CELL/bin/jsc" | grep JavaScriptCore                     # MUST name $CELL's own lib, not the build dir
```

Then prove the two cells differ *at runtime*, not just on disk: a symbol only one side imports
(`nm -D` for e.g. `sched_yield` vs `nanosleep`), a constant folded into an immediate that differs
(`objdump -d` the function you changed), or distinct `.so` md5s **plus** the `ldd` check above. A cell
whose `jsc` was built against a different member layout than the `.so` it loads will often segfault
instead of lying — treat any crash of a staged binary as a staging bug, not a flaky test.

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
CACHE=~/js3-builds/$BASE_SHA                    # baseline build lands in $CACHE/Release
                                                 # NOT /tmp: macOS sweeps it nightly, see below

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

**macOS sweeps `/tmp` daily, and the sweep itself wrecks a run in progress.** `/etc/periodic/daily/
110.clean-tmps` runs as root around midnight and walks all of `/tmp` with

```
find -dx . -fstype local -type f -atime +3 -mtime +3 -ctime +3 ... -delete -print
```

Two distinct hazards, and the second is the one that bites first:

- **It steals the machine.** Point several WebKit build trees at `/tmp` (each ~30 GB) and that find
  runs for *hours* at ~80% of a core, hammering the disk, and its `-delete` traffic spikes
  `fseventsd` past 100% CPU as a side effect. Measured here: a full build that normally takes ~40 min
  was still unfinished after 3 hours, throughput down from ~590 objects/min to ~4. Any benchmark round
  overlapping it is contaminated. It runs as **root**, so you cannot kill it without the user's sudo.
- **It can delete a cached build**, but only files older than 3 days in atime *and* mtime *and*
  ctime, so a per-sha baseline cache you keep across a week is exactly the thing at risk. Builds made
  during the session are safe.

**This is why `$CACHE` above defaults to `~/js3-builds` and not `/tmp` — do not move it back.** The
same goes for `git worktree` trees built for variant cells: put them under `~/js3-builds/<name>/src`.
Result JSONs can stay in `/tmp/js3-runs` (small, and recreated each session); it is the multi-GB build
trees that make the sweep pathological.

Add the sweep to your list of suspects whenever a build or round is inexplicably slow on macOS:

```bash
ps -A -o pid,%cpu,etime,args | grep 'find -d[x]'     # the sweep, if it is running
```

Then **stamp the times before discarding data** — compare the sweep's `etime` against when each block
ran. A sweep that started after your last round did not affect it, and rounds that predate it stay
valid. If it is running and you need the machine, it is root-owned: ask the user to
`sudo kill <pid>` rather than trying to work around it.

### Quiesce, then run interleaved

```bash
wk quiesce on         # on the host: everything below, in one command
wk quiesce status     # what it actually achieved, measured rather than claimed
wk quiesce off        # afterwards
```

`wk quiesce` covers the macOS determinism items: `caffeinate`, the discretionary analysis daemons
SIGSTOPped, Notification Center booted out (a banner is compositor work in the middle of a
measurement and it can steal focus), the screensaver and its lock disabled, App Nap off for
MiniBrowser and a "raiser" keeping it frontmost, and — through a privileged helper with a fixed
allowlist — Spotlight indexing, automatic updates, low power mode and sleep. `wk quiesce status`
then *measures* the result, including the thermal state, which no setting controls.

The privileged half needs a password unless `./setup` has installed the sudoers rule; the
unprivileged half runs regardless, which is the important half (caffeinate, App Nap, the raiser,
the daemon SIGSTOP). **Never work around a sudo prompt** — ask the person at the keyboard, or accept
the unprivileged subset and say so in the report. Before doing either, check whether the
sudo-gated items are *already* in the desired state: `mdutil -s /` (Spotlight),
`pmset -g | grep lowpowermode`, `tmutil currentphase`. Often they are, and the difference is cosmetic.

**The pinned local copy of the benchmark is `wk bench seed`'s job**, not the quiescing script's: it
pins payloads by commit so a run copies a fixed checkout instead of re-cloning upstream from GitHub
(which adds network and disk noise and can shift the commit mid-experiment). Pass the seeded path as
`--local-copy` to every round.

**Set a fixed display refresh rate by hand** — ProMotion/VRR is the one thing nothing here can
control, and rAF-driven runs inherit its jitter. (A MacBook Air panel is a fixed 60 Hz with no
ProMotion, so the warning is moot there — check `system_profiler SPDisplaysDataType` rather than
assuming every Mac needs the manual step.)

**`timeout` does not exist on macOS** (it is GNU coreutils; `gtimeout` only if you installed it). A
round wrapper written as `timeout 3000 Tools/Scripts/run-benchmark ...` dies instantly with
`command not found`, which a loop can mistake for a failed round. Rely on the plan's own
`"timeout": 1200` per iteration instead, or install coreutils.

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
CACHE=~/js3-builds/$(git rev-parse HEAD)
PATCHED="$WEBKIT_ROOT/WebKitBuild/Release"
LOCAL_COPY="$JS3_LOCAL_COPY"                      # the seeded payload (wk bench seed)
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

**Validate each round on its scores, not just on the JSON parsing.** A check like "the `tests` dict is
non-empty" passes on a run that produced structure but no useful measurement. Assert the subtests you
asked for are all present and each has a plausible `Score`, and discard the round otherwise.

Conversely, **do not treat a fast round as a broken one.** A targeted subset is genuinely quick — 10
subtests on an M4 laptop complete in ~17 s per cell (~35 s per interleaved pair), against ~4 min for
the full 77. Before suspecting a no-op run, look at a subtest's `First`/`Worst`/`Average` block: real
JetStream3 output has an `Average` over many iterations (the repeating decimal gives the iteration
count away), which a stub or failed run does not. Cheap subsets are an opportunity — at 35 s a pair,
60 round-pairs across two blocks costs about half an hour, so budget for the replication block below
rather than settling for one block's N.

**Pass list arguments literally** (`--subtests delta-blue bigint-noble-ed25519`), never through an
unquoted variable. Which shell the Bash tool runs is a property of the machine you happen to be on,
not something to encode here: under zsh an unquoted `$SUB` does *not* word-split, so it arrives as
one argument and the run fails with `... is not a valid subtest`, while under bash it does split and
appears to work. Literal args are correct on every shell, which is why they are the rule. For the full suite, drop `--subtests`, run ~6-8 rounds in the background,
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
- **A fanless Mac (MacBook Air) throttles across a long block — check every block for drift.** The
  laptop is fine for short subset rounds and then quietly degrades once rounds are long and
  back-to-back. Measured on an M4 Air: a 10-round full-suite block run straight after another
  full-suite block plus a build decayed monotonically, 422 → 392 overall geomean in both cells
  together (~7.5%), taking per-cell CoV from **0.69% to 3.2%** and the equivalence bound from 0.204%
  to 2.6%. Nothing warned: `pmset -g therm` reported no thermal level the whole time, so *absence of
  a recorded thermal warning is not evidence of no throttling.*

  Interleaving still protects the point estimate (both cells decay together), so this corrupts the
  *bound*, not the direction — but the bound is usually what you are trying to earn.

  1. **Always print the per-round series, not just the mean.** A monotonic slide across rounds is
     throttling; scatter without a trend is ordinary noise. A mean and CoV alone hide the difference.
  2. **Analyse interleaved rounds paired** — per-round `log(patched/base)`, then a one-sample t on
     those. Common-mode drift cancels within a round. On the block above this recovered the bound
     from 2.56% to 1.36%.
  3. **Pairing is a repair, not a fix.** 1.36% was still 7x worse than the healthy block. Cool the
     machine (20+ min idle) between long blocks and re-run rather than shipping the degraded bound.
  4. Budget for it: back-to-back full-suite blocks on a fanless machine need idle gaps, so plan
     wall-clock accordingly instead of queueing blocks one after another.

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
- **Isolate every Cog from the session bus: `export DBUS_SESSION_BUS_ADDRESS=unix:path=/nonexistent`.**
  Cog is a single-instance GApplication (`com.igalia.Cog`), so a second Cog that can reach the bus
  hands its URL to the first one and exits 0 — the benchmark then runs in the *other* build's browser
  (or another container's, since they share `/run/user/$(id -u)`), and `run-benchmark` waits out its
  full timeout with no output. Unsetting the variable is not enough: GDBus still finds
  `$XDG_RUNTIME_DIR/bus` by convention. The tell is a Cog that exits 0 instantly and prints nothing.
- **`--platform=headless` needs `--platform-params=60`, or MotionMark scores 1.0 on every subtest.**
  The headless plug-in paces frames with a `g_timeout` at `max_fps`, and its default is 30 — under
  MotionMark's 60fps target, so the ramp controller never raises complexity and every subtest parks at
  the floor. That one flag moved a real measurement from 1.01 to 231. (`--platform-params` for this
  plug-in is the refresh rate, not geometry; anything else logs `Invalid refresh rate value`.)
- **Prefer `headless` over `wl` for rAF-bound suites, and reach for it when the onscreen platforms
  break.** It is GPU-accelerated when `/dev/dri` is present (check with `ls -l /proc/<WebProcess>/fd |
  grep dri` and look for `libEGL_nvidia`/mesa in `/proc/<pid>/maps`), it takes its frame clock from the
  flag above rather than the host compositor, and it sidesteps two failures seen on modern desktops:
  `--platform=wl` asserts on `s_eglCreateWaylandBufferFromImageWL` where the compositor no longer
  advertises `wl_drm`, and `--platform=x11` can kill the WebProcess inside
  `libnvidia-egl-wayland`'s `dmabuf_feedback_check_main_device`.
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
- **Library path:** a Linux JSCOnly `bin/jsc` runs in place against its sibling `lib*`; if a build ever
  needs it, use `LD_LIBRARY_PATH=$DIR/lib`. **In place is the operative word** — it finds that `lib*` by
  DT_RPATH, so a copy of `bin/jsc` elsewhere keeps loading the original build dir and `LD_LIBRARY_PATH`
  cannot override it. Stage `bin/` and `lib/` together and repoint the rpath (see the verification block
  above) whenever you run A/B cells from copies. On macOS prefix
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
  A reusable implementation is in this repo: `container/bench/js3-run-loop.sh`.
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
- **`caffeinate` does not exist on Linux, and `wk quiesce` does something different there.** For a headless jsc run, screen blanking is
  irrelevant (no display dependency); only a full system suspend matters. Check
  `gsettings get org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type` — if `'nothing'`, the
  box never idle-suspends on AC. Otherwise hold `systemd-inhibit --what=idle:sleep --mode=block
  --why=bench sleep <dur>` in the background. (A *browser* run on Linux does need blanking stopped —
  Wayland: `gsettings set org.gnome.desktop.session idle-delay 0` and `... power idle-dim false`; X11:
  `xset s off; xset -dpms`. `systemd-inhibit`'s idle lock alone does not stop GNOME blanking.)
- **Build `shuf` lists and arrays explicitly** rather than relying on word-splitting, which differs
  between the shells the Bash tool may be running (see the rule above).

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

**5. Keep each cell loading its own libraries** (GTK builds — the highest-severity trap here, because
it produces scores from the *wrong build*). Binaries carry an **absolute RPATH**, and `DT_RPATH` beats
`LD_LIBRARY_PATH`, so a tree whose library directory is reached through a shared path loads whatever
that path points at now — not what it was built against. The symptom is either a plausible wrong
number or a loud `symbol lookup error` from the system WebKitGTK.

**In a wk workspace this trap cannot occur, and that is the reason to use one per cell**: a workspace
holds exactly one checkout at `/src/WebKit` with its own `WebKitBuild`, and nothing is shared between
two of them. Two cells means two workspaces (`wk new a`, `wk new b`), each built and run on its own.
The historical form of this — a `/sdk/webkit` symlink shared by every tree, repointed by whichever
`build-webkit` ran last — is what a per-workspace tree removes.

Outside a workspace, on a shared tree, assert the path per round instead: check what it resolves to
immediately before `exec` and re-check after the round finished.

**Never identify a cell by `/proc/<pid>/exe` from outside the namespace it runs in.** The link is
resolved in the *reader's* mount namespace, so a shared path resolves through whatever that path means
out there, not what the process is actually running. Every cell therefore appears
to be running that one tree's binary, which looks exactly like the shared-symlink bug you are trying
to rule out (a real false alarm here: the host symlink pointed at the baseline, so both cells' browsers
resolved to baseline paths while actually running the right builds). The authoritative check is the
**inode of the loaded library, `stat`ed inside the round's own mount namespace**, compared against the
cell's expected inode — have the in-namespace wrapper append it to a per-round meta file before
`exec`ing the browser, and verify every round afterwards. `readlink -f` is still fine for *killing*
browsers, since any resolution lands on a `WebKitBuild/*/bin/*` path.

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

`/sys` is read-only in a workspace, so the governor cannot be set from in there. Two honest options,
in order:

1. **Ask for `wk quiesce on` on the host.** One command, no sudo prompt beyond the helper, and it
   pins the governor and quiets the machine in the same pass. This is the answer whenever there is a
   person available.
2. **The degraded mode, when there is not.** `uclampset` asks the scheduler to treat the task as if it
   needed the full clock; it needs no privileges and no host access:

   ```bash
   uclampset -m 1024 -- <the run command>            # per-process clock hint, unprivileged
   ```

   It is a *hint to the scheduler*, not a governor setting: it raises the frequency the task asks for
   and cannot stop another workload lowering it, so the clock is still not fixed and the variance is
   still higher than a pinned run's. **A run made this way is reported as unpinned** — say
   `uclampset -m 1024, governor not pinned` in the report, next to the number, every time. Do not
   quietly compare it against a pinned run.

Anything else — remounting `/sys`, a privileged container, hunting for a sudo timestamp — is working
around the boundary rather than measuring, and the number would not be defensible anyway.

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

### Where a run happens

Inside a **wk workspace**, which is where an agent already is: the checkout is `/src/WebKit` on Linux
and `/Users/admin/WebKit` in a macOS guest, and `wk build` / `wk run` / `wk test` need no workspace
name in there. The host drives it with `wk build <ws> …`, reads progress with `wk status <ws>` and
`wk logs <ws>`, and nothing on the host reaches into the workspace's filesystem — so write results
where the run can read them back, not where the host expects to find them.

(The `wkdev-enter` containers this section used to describe, and their mapped `~/Development/32`
prefix, are gone.)

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
  of macOS's App-Nap-off + "raiser", which `wk quiesce on` does for you.
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
  **But `pgrep -af 'harnes[s].sh'` cannot do this check**: bracketing stops the *pattern* from matching
  itself, yet the launch command contains the literal target string (`$S/harness.sh`), so the guard
  matches the very tool call performing it and reports "already running" every time. Test **argv
  position** instead — for a shebang script, `argv[1]` is its path — which no launcher command line can
  spoof:
  ```bash
  running() { local p a1; for p in $(ls /proc | grep -E '^[0-9]+$'); do
      a1=$(tr '\0' '\n' < /proc/$p/cmdline 2>/dev/null | sed -n 2p)
      [ "$a1" = "$SCRIPT" ] && echo "$p"; done; }
  ```
- **Never wait on a completion marker in a log a previous run also wrote to.** `until grep -q 'HARNESS
  COMPLETE' progress.log` fires instantly when an earlier smoke-test run left that line in the shared
  log, so the "run finished" notification arrives hours early and the analysis reads a half-empty
  results dir. Gate on something unique to *this* run (`CYCLE DONE <config> <last-cycle>`, or an
  expected JSON count), or truncate/rotate the log at launch.
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
- **A compute budget is mandatory, and it has a default.** Decide it *before* the first round and
  say it in the report. Absent an instruction from the user, the cap is **8 hours of wall clock or 40
  full-suite rounds per arm, whichever comes first** — which is roughly what an overnight run buys and
  is nowhere near the ~25x that 0.02% overall would need. Log progress each batch (current
  `1 - lowerCI` per subtest); on hitting the cap, stop and report **the tightest bound actually
  achieved** and which subtests still gate it. A bound of "0.4%" honestly reached is a result; an
  unbounded loop chasing 0.02% is not.

## Root-cause a confirmed regression (on request)

Narrow a confirmed-slower subtest to a loop. Delegate the code-tracing parts (where is this loop, what
changed between the two builds) to a subagent so the main context stays on the measurement state — but
run the interleaved measurement loop itself in one context.

1. **Profile the headless jsc run** — invoke the `jsc-profile` skill (skip only if it is already
   loaded this conversation). Start from the tier breakdown: cost in
   FTL/DFG/Baseline (generated JS) → use the bytecode profiler to find the hot CodeBlock; cost in
   C/C++ → use samply to find the hot native function.
2. **Decompose by phase.** Split the subtest into sub-operations and time each on both builds
   (interleaved, many reps). Require BH/Bonferroni-corrected significance across the k phases, not raw p.
3. **Extract a microbenchmark** of the suspect loop — invoke the `jsc-microbenchmark` skill (skip
   only if it is already loaded this conversation). If it reproduces the
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
  the platform quiescing lives with each run mode: `wk quiesce` under
  [Browser](#quiesce-then-run-interleaved), `taskset` under [Linux quiescing](#linux-quiescing). For a
  core-constrained run (`taskset` + core-count override + pinned clock + hermetic cells) see
  [Simulating a small device](#simulating-a-small-device-pinning-to-exactly-n-cores-eg-2) — `taskset`
  alone silently produces a wrong configuration.
