# HANDOFF — memory benchmarking and fixed-core-count runs

Two related features, neither started. Reference: wiki
`Memory-benchmark-charts-(2.52)` and `WPE-memory-usage-investigation`.

## Remaining — memory charts (`wk bench mem <ws> <plan>`)

- **Rescue `plot-memory-log.py` out of the wiki first.** The ~300-line charting
  script exists only as a wiki code block; commit it under `container/bin/` so
  it cannot be lost and the command can call it.
- **Ship the recurring experiment patches as files** (e.g. `extra/patches/`):
  the startMemorySampler patch, the disable-prewarming patch, the 300 MB
  MemoryPressureMonitor clamp. Each is re-applied by copy-paste today.
  `wk build --patch <name>`, or a documented directory.
- **The run**: build (patched as asked), drive MiniBrowser at the plan, collect
  the `WebKitWebProcess` logs, plot (`--include c,k --max-seconds N`), and drop
  the chart and raw logs beside the result with `wk bench`'s provenance
  treatment — a chart with no sha or config recorded is as uncomparable as a
  score without one.
- Works on the Linux reference target first; WPE/GTK/JSCOnly follow unchanged,
  macOS needs its own confirmation.

## Remaining — fixed core counts, every target

`wk bench` records `cores` as a count and pins nothing. The pin has to exist per
target because the mechanisms differ and each can silently not-apply:

- container: podman `--cpuset-cpus` (a real pin) — not used anywhere today;
- macOS guest: the vCPU count at boot (not a pin — document that honestly);
- Pi devices: taskset/isolcpus on the device.

Record the *set*, not the count, and make `wk bench compare` warn on a mismatch
the way it warns on renderer and session-mode mismatches.
