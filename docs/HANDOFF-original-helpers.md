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
| `jscr` | run jsc, `--validateOptions=1`, right loader var per platform | `wk run <ws> [--config C]` |
| `jscd` | same under lldb | `wk run <ws> --lldb` |
| `jscrb` | run the *baseline* build from `WebKitBuildBaseline/` | **gone** — see "Baseline builds" |
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

## Building

| original | did | now |
|---|---|---|
| `jscb` | `build-jsc --release --export-compile-commands` (per-platform branches) | `wk build <ws> <config>` |
| `jscb-configure` | CMake configure with 32/64-bit and arch detection | `build/configs.sh` + `wk build` |
| `init-debug`/`init-release`/`init-ios-release` | set `BUILDDIR`/`CONFIG`/`PATH`, `cd` | replaced by named configs; see above |
| `build-safari-ios` | build Safari from the Internal tree | **gone** — needs an Internal checkout |
| `minibrowser-debug` | `sudo lldb --attach-name ...WebContent.Development` | **gone** |

`init-debug` also carried two commented-out lines that set the CPU governor to
`performance` and cleared an MSR bit. That intent survives as `wk quiesce`.

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

## Git and GitHub

| original | did | now |
|---|---|---|
| `gpr` | check out a fork branch in `user:branch` PR syntax, adding the remote if needed, confirming every git command, diffing local vs remote before offering `reset --hard` | **gone** — 262 lines, the most substantial helper here |
| `git-sync-fork` | fetch both remotes, rebase `main` onto upstream | **gone** |
| `git-clean` | `git reset . && git checkout . && git clean -fd` | **gone** (one line, but destructive-by-design and easy to want) |
| `git-fuckit` | `git add . && git commit -m . && git push` | **gone** — the commit history in this repo is what it produced |
| `clean-github` | delete every local branch except the current | **gone** |
| `commit-count` | `git shortlog --summary --since "1 year"` | **gone** |
| `report` | weekly GitHub activity summary via `gh` | `wk report` |

`gpr` is the one to restore first. It is the only helper here with real logic
rather than a hardcoded path, it prints and confirms every git command before
running it, and nothing in `wk` covers checking out someone else's PR branch.

## Editor, shell and misc

| original | did | now |
|---|---|---|
| `bashrc`, `zshrc-macos` | history settings, bash→zsh switch, PATH | `shell/bashrc`, sourced from both `~/.bashrc` and `~/.zshrc` |
| `helix/`, `kitty/`, `sway/` | editor and desktop config | `container/helix/`, `dotfiles/`; sway/kitty **gone** |
| `lldbinit.py`, `lldb-run-file` | lldb helpers | `dotfiles/lldbinit`, `container/lldb/` |
| `configs/*` | dumps of dotfiles and machine config | `host/*/`, `dotfiles/` |
| `rpi5-tune/` | Pi 5 NUMA kernel, overclock sweep, stress and verify | `host/linux/rpi5/` |
| `claude-skills/`, `claude-hooks/` | agent skills | `claude/skills/`, `claude/hooks/` |
| `Notes.md` | freeform notes | **gone** |

---

## What to do with this

1. Decide, once, about each **gone** row: restore it as a `wk` verb, restore it
   as a script in `container/bin/` where the workspace can see it, or record
   that it is deliberately dropped.
2. The three worth doing first, because nothing covers them and each has real
   logic: `gpr`, the option-toggle A/B benchmark mode (`bench-js2-cli`,
   `bench-js3-switch`), and `strip-addresses`.
3. Verify the two `quiesce.sh` subtleties above survived into `wk quiesce`, and
   that `wk bench` reports per-subtest confidence intervals the way `js3-ci.py`
   did. Both are silent regressions if they did not: the numbers still appear,
   they are just worth less.
