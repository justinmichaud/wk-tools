# HANDOFF — `wk profile`

The profiling recipes are the longest copy-paste blocks in the wiki
(`JSC-Samply---perf`, `Debugging-WPE-Linux-(desktop)`,
`WPE-memory-usage-investigation`) and the jsc-profile / jsc-marker-trace
skills re-teach them to agents with host-only paths baked in. Encode them once
as a command; the skills then invoke the command instead of reciting env vars.

This is the deliverable interface for HANDOFF.md lane A step 10 (build
samply/sysprof-cli/heaptrack into the workspace image) — the tools without the
wrapper leave every run hand-assembled, and the wrapper without the tools has
nothing to run.

## Shape

`wk profile <ws> [--mode M] [--config C] [--attach] [js-file | url]`

Modes, each owning its env-var wall so nobody types it again:

- `--samply` — samply + JIT dump: `JSC_useJITDump=1 JSC_useTextMarkers=1
  JSC_jitDumpDirectory=... JSC_useConcurrentJIT=0`, plus the
  MiniBrowser-vs-jsc-shell and 32-vs-64-bit differences the wiki documents.
- `--sampling` — JSC's own sampling profiler; clean stale
  `JSCSamplingProfile-*` first (stale files silently pollute the next run),
  and print the report/collation command at the end.
- `--bytecode` — bytecode profiler; print the `display-profiler-output`
  invocation at the end.
- `--heaptrack` / `--massif` — `MALLOC=0 WEB_PROCESS_CMD_PREFIX=...` per the
  memory-investigation page.
- `--strongrefs` — `JSC_enableStrongRefTracker=1` etc.

Provisioning that has to exist first (the step-10 half):

- samply and heaptrack built/installed into the container image or seeded
  like bench payloads — the wiki's rustup/cargo dance is a provisioning step,
  not a per-run instruction. No more `~/Development/samply` host paths.
- `perf_event_paranoid`: a workspace cannot set it. Decide the mechanism once
  (a quiesce-helper verb on the host is the pattern that exists) instead of
  each skill saying "ask the user to sudo tee".
- Viewing: profiler.firefox.com is outside the egress allowlist; serve the
  profile file out of the workspace (samply's own server on loopback + the
  host's browser via the forwarded port, or copy the file out) and document
  which.

## Done means

The jsc-profile and jsc-marker-trace skills contain no raw env-var walls and
no host paths — each mode is one `wk profile` line — and a fresh workspace can
run every mode without a manual install step. Update `docs/HANDOFF-claude.md`'s
worklist when that lands.
