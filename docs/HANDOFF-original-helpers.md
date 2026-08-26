# HANDOFF — capabilities from the pre-`wk` helper scripts

Before `wk`, this repository was ~70 flat shell scripts driven by `init-*` files
that exported `$BUILDDIR` and `$CONFIG` into the shell. They are recoverable:

```sh
git show 7a76d6d^ --stat            # the tree as it was
git archive 7a76d6d^ | tar -x -C /tmp/orig
```

This file exists so no *capability* is lost by accident. The point is not to
port them one for one — most were two lines wrapping a path that made sense on
one machine — but that dropping one should be a decision.

## Remaining

1. **Decide, once, about each capability that is still gone**: restore it as a
   `wk` verb, restore it as a script in `container/bin/` where the workspace can
   see it, or record that it is deliberately dropped. Still gone today:
   - `jscrr`/`jscrrr` (rr record/replay; `container/lldb/rr.py` is wired for
     replay only), `jscsp` (sysprof-cli, and the sysprof JIT-dump patch);
   - the wasm wrappers — `wasm-compile`, `wasm-test`, `wasm-fast-stress`,
     `wasm-debug`, `wasm-test-v8`. All thin wrappers over `$VM/bin/jsc` plus
     `wabt`; if they come back they belong in `container/bin/`;
   - `bench-js2-simd-nosimd` and `-v8` (SIMD on/off, and against V8);
   (The git helpers are done: `wk pr`, `wk pick`, and `git-clean`,
   `commit-count`, `git-sync-fork` in `container/bin/`.)
2. **The option-toggle A/B benchmark mode** (`bench-js2-cli`,
   `bench-js3-switch`) — the one worth doing first, because nothing covers it
   and it has real logic: N rounds toggling one JSC option. A/B arrived twice
   elsewhere on different axes — `wk pi bench --ab A,B` alternates two deployed
   slots, `bench/mac-ab.sh` interleaves two builds — so copy one of those
   shapes rather than designing a third.
3. **PGO profile collection and build** — still uncovered; nothing in
   `cmd/build` or `build/` mentions it.
4. **The frontmost-window raiser, still missing from `wk quiesce`.** MiniBrowser
   is launched by raw exec to inject `DYLD_FRAMEWORK_PATH`, so macOS never
   activates it; an occluded page gets its `requestAnimationFrame` throttled,
   which stalls the rAF-driven JetStream3 loop. The original disabled App Nap
   for MiniBrowser and ran a background raiser. `wk quiesce` does caffeinate,
   daemon-pausing and the privileged helper, and nothing raises or App-Nap-exempts
   the browser. A real gap for macOS browser runs.
5. **Per-subtest confidence intervals** — `wk bench compare` runs
   `compare-results --breakdown`, which should cover what `js3-ci.py` did.
   Check one run against it. Silent while open: the
   numbers still appear, they are just worth less.
6. **Baseline builds for ad-hoc runs.** Every `bench-*` script assumed a second
   tree at `WebKitBuildBaseline/` built from ToT. A workspace holds one checkout
   and one build tree per config, and `wk bench` builds its own baseline
   elsewhere — worth confirming a baseline is still reachable outside
   `wk bench`.

## Already covered, for reference

`jsc-stress`→`wk test`; the JS2/SP2/JS3 loops→`wk bench`; `bench-show-results*`
→`wk bench compare`; `quiesce.sh`→`wk quiesce` + `admin/wk-quiesce-priv`;
`strip-addresses` and `show-profiled-functions`→`container/bin/`; `gpr`→`wk pr`;
`report`→`wk report`; the profiling runs→`wk profile`; `lldbinit.py`→
`dotfiles/lldbinit` + `container/lldb/`; `rpi5-tune/`→`host/linux/rpi5/`.

Two things from `quiesce.sh` that were hard-won: the raiser above, and **a
pinned local copy of the benchmark** so a run does not measure whatever the
network served that day — that one survived, as `wk bench seed`.
