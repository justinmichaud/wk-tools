# HANDOFF — capabilities from the pre-`wk` helper scripts

Deciding, once per capability, whether a pre-`wk` shell script's capability is restored or deliberately dropped.

- [ ] decide `jscrr`/`jscrrr` (rr record; `container/lldb/rr.py` is wired for replay only) [decision]
- [ ] decide `jscsp` (sysprof-cli, and the sysprof JIT-dump patch) [decision]
- [ ] decide the wasm wrappers: `wasm-compile`, `wasm-test`, `wasm-fast-stress`, `wasm-debug`, `wasm-test-v8` [decision]
- [ ] decide `bench-js2-simd-nosimd` and `-v8` (SIMD on/off, and against V8) [decision]
- [ ] decide `report` (the weekly gh-authenticated summary) [decision]
- [ ] build the option-toggle A/B benchmark mode (N rounds toggling one JSC option), in the shape of `wk pi bench --ab` or `bench/mac-ab.sh` [decision]
- [ ] cover PGO profile collection and build [decision]
- [ ] check `wk bench compare`'s `compare-results --breakdown` against one run for per-subtest confidence intervals [needs a benchmark run]
- [ ] confirm a baseline build is still reachable outside `wk bench` for ad-hoc runs [decision]
