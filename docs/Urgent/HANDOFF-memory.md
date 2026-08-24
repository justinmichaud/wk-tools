# HANDOFF — memory benchmarking and fixed-core-count runs

Two related features; the memory half has a rescue step that should happen
first because its tooling currently lives only in the wiki.

## 1. Memory charts (`wk bench mem <ws> <plan>`)

Reference: wiki `Memory-benchmark-charts-(2.52)` and
`WPE-memory-usage-investigation`.

- **Rescue `plot-memory-log.py` out of the wiki.** The ~300-line charting
  script exists only as a wiki code block ("clanked"); commit it under
  `container/bin/` so it cannot be lost and the command below can call it.
- Ship the recurring experiment patches as files (e.g. `extra/patches/`):
  the startMemorySampler patch, the disable-prewarming patch, the 300 MB
  MemoryPressureMonitor clamp — each is re-applied by copy-paste from the
  wiki today. `wk build --patch <name>`, or just a documented directory.
- The run: build (patched as requested), drive MiniBrowser at the plan,
  collect the `WebKitWebProcess` logs, run the plot
  (`--include c,k --max-seconds N`), and drop the chart and raw logs next to
  the result with `wk bench`'s provenance treatment — a memory chart with no
  sha/config recorded is as uncomparable as a score without one.
- Applies unchanged to WPE/GTK/JSCOnly once it works on the Linux reference
  target; macOS needs its own confirmation (lane B).

## 2. Fixed core counts, every target

Running *and* benchmarking pinned to N cores must exist per target, because
the mechanisms differ and each can silently not-apply:

- container: podman `--cpuset-cpus` (a real pin);
- macOS guest: the vCPU count at boot (not a pin — document that honestly);
- Pi devices: taskset/isolcpus on the device.

`wk bench` already records `cores` and the container envelope's `cpus` in
env.json; what is missing is the pin itself, recording the *set* rather than
the count, and making `wk bench compare` warn on a mismatch the same way it
warns on renderer or session-mode mismatches.
