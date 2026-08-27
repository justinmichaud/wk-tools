# HANDOFF — memory benchmarking and fixed-core-count runs

Two related features, neither started. Reference: wiki
`Memory-benchmark-charts-(2.52)` and `WPE-memory-usage-investigation`.

## Remaining — memory charts (`wk bench mem <ws> <plan>`)

- **The recurring experiment patches are files now, but not the patches
  themselves.** `container/bin/patches/{startMemorySampler,disable-prewarming,
  memory-pressure-300mb}.patch` exist as named, documented stubs -- what each
  has to do is written down in the file that will hold it -- but not as real
  diffs: those were never in this repository, and the wiki pages this
  handoff's own reference line names 404 from here and are not indexed
  anywhere a web search reaches, which reads as Igalia-internal rather than
  lost. Capture each from wherever it is still re-applied by copy-paste today
  (`git diff > container/bin/patches/<name>.patch` in a checkout that has it)
  and these three TODOs are done.
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
