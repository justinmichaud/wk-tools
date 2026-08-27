# HANDOFF — memory benchmarking

## Remaining

- [ ] capture `container/bin/patches/{startMemorySampler,disable-prewarming,memory-pressure-300mb}.patch` as real diffs from wherever they are still re-applied by copy-paste today (`git diff > container/bin/patches/<name>.patch` in a checkout that has it) [needs a checkout carrying the patches]
- [ ] build `wk bench mem <ws> <plan>`: build patched, drive MiniBrowser at the plan, collect the `WebKitWebProcess` logs, plot (`--include c,k --max-seconds N`), and drop the chart and raw logs beside the result with `wk bench`'s provenance treatment [decision]
- [ ] confirm the memory-chart path on WPE/GTK/JSCOnly and macOS beyond the Linux reference target [needs those targets]
