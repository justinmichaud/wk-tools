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

`wk bench <ws> <plan> --cores <set>` pins a container run with `taskset -c
<set>` (cmd/bench's `bench_cores_wrap`, `lib/wkdata.py` `cores-valid` /
`cores-wrap`), the same mechanism a remote or local Linux target would use if
`wk bench` grew support for them — the pin is not container-specific.
`bench_cores_refusal <target> <os>` (cmd/bench, pure and unit-tested) refuses
--cores where no per-run pin exists: a `vm` workspace target is a macOS guest
regardless of the driving machine's os, and a `local` target running on
macOS *is* the machine doing the benchmarking. container, remote, and a
`local` target on Linux all allow it. The set, not a count, is recorded
(`cores.set` in env.json, alongside `cores.pinned`), and `wk bench compare`
warns when two runs' `cores.set` differ, the same way it warns on a runner or
session-mode mismatch.

- **Pi devices: taskset/isolcpus on the device, via `wk pi bench`.** Not done
  here — `wk pi bench` is a separate command (cmd/pi) with its own on-board
  run path; it needs the same `--cores` flag and the same `cores.set` /
  `cores.pinned` fields in the env.json it writes, using
  `lib/wkdata.py cores-valid` / `cores-wrap` rather than a second parser.
