# Handoff: the original helper scripts

Before `wk`, this repository was a flat directory of ~70 shell scripts driven
by three `init-*` files that exported `$BUILDDIR` and `$CONFIG` into the
shell. They are all recoverable:

```sh
git show 7a76d6d^ --stat            # the tree as it was
git archive 7a76d6d^ | tar -x -C /tmp/orig
```

This document exists so nothing in them is lost by accident. Every script is
listed with what it did and where that capability now lives. **"gone"** means
the capability does not exist in `wk` today and would have to be rebuilt.

The point is not to port them one for one. Most were two lines wrapping a path
that only made sense on one machine, and the reason `wk` exists is that those
paths were wrong in five different ways across three checkouts. The point is
that the *capability* should be a deliberate decision, not an accident of what
happened to get rewritten.

---

## The model they used, and why it went away

```sh
. init-release          # export BUILDDIR=~/Development/ReleaseVersion/OpenSource
                        # export CONFIG=Release; cd $BUILDDIR
jscb                    # build
jscr foo.js             # run
```

`$BUILDDIR` and `$CONFIG` were ambient shell state. Every script read them, so
which build you ran depended on which `init-*` you had sourced in *this*
terminal, and there was no way to tell from the command itself. Two terminals
meant two different answers to `jscr foo.js`.

`wk` replaced the ambient pair with an explicit workspace name and a named
config: `wk run bug-238 --config jsc-release`. The `init-*` scripts have no
successor and should not get one.

Three build-tree layouts were hardcoded across these scripts
(`WebKitBuild/$CONFIG`, `WebKitBuild/JSCOnly/$CONFIG`, and the Apple one), and
they disagreed. `build/configs.sh:config_build_dir` is now the single authority.

---

## Running JSC

| original | did | now |
|---|---|---|
| `jscrr` | run under `rr record` | **gone** (Linux only; `container/lldb/rr.py` is wired for replay) |
| `jscrrr` | `rr replay` | **gone** |
| `jscs` | run under `samply`, with `--useJITDump --useTextMarkers` | **gone** — the `jsc-profile` skill documents the invocation |
| `jscsp` | run under `sysprof-cli` | **gone** |
| `jsc-stress` | run-jsc-stress-tests over the build | `wk test <ws>` |
| `strip-addresses` | strip hex addresses from disassembly so two dumps diff | **gone** — small and worth restoring; see `container/bin/` |
| `show-profiled-functions` | parse the bytecode profiler dump | **gone** — `container/bin/` has a copy |

`jscs`/`jscsp` mattered: both set `perf_event_paranoid` to -1 first, which is
a root operation, and both depend on a profiler that lives on the host. Under
the current sandbox a workspace has no such privilege. If profiling comes back
it should be a `wk` verb that runs the profiler outside and attaches, not a
script inside that needs root.

We need to support collecting samply, sysprof profiles. Heaptrack too, plus the jsc sampling profiler. This should work on cli jsc builds, gtk, wpe and macos MiniBrowser too, and support jit dump (extra jsc flags, my sysprof patch). Build these from source.

## Building

Support running a layout test in the debugger
Support running a js test in the debugger
Support collecting and building a PGO profile + build

## Benchmarking

| original | did | now |
|---|---|---|
| `bench-js2` | 10 interleaved JetStream2 rounds, patched vs baseline, via `run-benchmark` | `wk bench` |
| `bench-js2-cli` | 60 rounds of a 5-test JS2 subset in the jsc shell, toggling one JSC option | **partly** — `wk bench` does not do option-toggle A/B |
| `bench-sp2` | 2 rounds of Speedometer 2 | `wk bench` |
| `bench-js3-switch` | 20 rounds of `8bitbench-wasm` comparing `--minCasesForTable=7` vs `10` | **gone** — the same option-toggle gap |
| `bench-js2-simd-nosimd`, `-v8` | SIMD on/off, and against V8 | **gone** |
| `bench-show-results*` | `compare-results -a ToT*.result -b Patched*.result --detailed-breakdown` | `wk bench` reports its own |
| `js3-run-loop.sh` | interleaved full-suite JS3 loop, one JSON per build per round | `wk bench` |
| `js3-ci.py` | per-subtest b/a ratio with 95% CI | **check** — `wk bench` must do this or it is a regression |
| `quiesce.sh` | 298 lines: Spotlight, Software Update, App Nap, `caffeinate`, keeping MiniBrowser frontmost | `wk quiesce` + `admin/wk-quiesce-priv` |

Two things in `quiesce.sh` are easy to lose and were hard-won:

- **The frontmost-window raiser.** MiniBrowser is launched by raw exec to
  inject `DYLD_FRAMEWORK_PATH`, so macOS never activates it. Occluded pages get
  their `requestAnimationFrame` throttled, which stalls the rAF-driven
  JetStream3 loop. The original disabled App Nap for MiniBrowser and ran a
  background raiser. **Confirm `wk quiesce` still does this.**
- **A pinned local copy of the benchmark**, so a run is not measuring whatever
  the network served that day.

**Baseline builds.** `jscrb` and every `bench-*` script assumed a second tree
at `WebKitBuildBaseline/`, built from ToT, living beside the patched one. `wk`
has no equivalent: a workspace holds one checkout and one build tree per
config. `wk bench` builds its own baseline elsewhere. Worth confirming that a
baseline is still reachable for ad-hoc runs, not only inside `wk bench`.

## Wasm

| original | did | now |
|---|---|---|
| `wasm-compile` | compile a `.wat` with the build's tooling | **gone** |
| `wasm-test`, `wasm-fast-stress` | wasm stress suites | **partly** — `wk test` runs the JSC suites |
| `wasm-debug` | wasm run under a debugger | **gone** |
| `wasm-test-v8` | the same against V8 | **gone** |

All five are thin wrappers over `$VM/bin/jsc` plus `wabt`, which the old PATH
pulled from `/Volumes/WebKit/wabt/bin`. If they come back they belong in
`container/bin/`, where the workspace can see them.

Git and GitHub helpers (`gpr`, `git-sync-fork`, `git-clean`, `commit-count`,
`report`) moved out to `docs/HANDOFF-git-tools.md` — they don't share anything
with the profiling/benchmarking/wasm material in this file.

## Editor, shell and misc

| original | did | now |
|---|---|---|
| `lldbinit.py`, `lldb-run-file` | lldb helpers | `dotfiles/lldbinit`, `container/lldb/` |
| `rpi5-tune/` | Pi 5 NUMA kernel, overclock sweep, stress and verify | `host/linux/rpi5/` |

---

## What to do with this

1. Decide, once, about each **gone** row: restore it as a `wk` verb, restore it
   as a script in `container/bin/` where the workspace can see it, or record
   that it is deliberately dropped.
2. The two worth doing first, because nothing covers them and each has real
   logic: the option-toggle A/B benchmark mode (`bench-js2-cli`,
   `bench-js3-switch`), and `strip-addresses`. (`gpr` is the equivalent
   priority pick in `docs/HANDOFF-git-tools.md`.)
3. Verify the two `quiesce.sh` subtleties above survived into `wk quiesce`, and
   that `wk bench` reports per-subtest confidence intervals the way `js3-ci.py`
   did. Both are silent regressions if they did not: the numbers still appear,
   they are just worth less.
