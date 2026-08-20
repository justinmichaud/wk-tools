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

## State, 2026-08-20 — the command exists

`cmd/profile` is written and its resolution is verified (in `wk selftest
--quick`, against a workspace that is a marker rather than a machine): the
loader variable per port, JSC options *before* the script rather than after it,
`--mode native` resolving to xctrace on the Apple ports and samply everywhere
else, and every mode either resolving or refusing with a reason.

What it does **not** have yet, and why each is left:

- **A run.** Nothing here has been profiled, because this host has no guest and
  no container workspace to profile in. Every mode's command line is composed
  and checked; none has been executed.
- **`--mode strongrefs`.** The option name below (`JSC_enableStrongRefTracker`)
  is from this file and has not been checked against `OptionsList.h`, and a
  typo'd `JSC_` variable is ignored in silence — which is the one failure that
  makes a profile look like the code changed. `--env NAME=VALUE` sets any
  option in the meantime, and the mode goes in when somebody with a checkout
  can read the spelling off it.
- **The provisioning half.** samply, heaptrack and valgrind are not in the
  workspace image; the command checks for each by name and refuses with the
  install line rather than failing inside the run. That is lane A step 10 and
  is unchanged by this.
- **`--browser` on the CMake ports.** Refused, with the reason: the browser is
  started by `Tools/Scripts/run-minibrowser` and the process that matters is a
  child of it, so a profiler pointed at the launcher records the launcher.
  `--attach` is the way in until that is wired up.

`t_pull` grew implementations for the `vm` and `remote` targets on the way (it
had only container and local), and `t_pull_dir` joined it — a recording has to
come out of the workspace that made it, and a profile is exactly the binary
that `t_exec … cat` corrupts.

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
  each skill saying "ask the user to sudo tee". **On the netbooted image
  (`docs/HANDOFF-netboot.md`, lane A's new first step, whose first consumer is
  profiling) it is simply ours to set** — no sandbox, no host to ask. That does
  not remove the need for an answer inside a workspace; it means there is a
  machine where the hardest modes work while that answer is being found.
- Viewing: profiler.firefox.com is outside the egress allowlist; serve the
  profile file out of the workspace (samply's own server on loopback + the
  host's browser via the forwarded port, or copy the file out) and document
  which.

## Done means

The jsc-profile and jsc-marker-trace skills contain no raw env-var walls and
no host paths — each mode is one `wk profile` line — and a fresh workspace can
run every mode without a manual install step. Update `docs/HANDOFF-claude.md`'s
worklist when that lands.
