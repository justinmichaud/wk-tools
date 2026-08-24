# HANDOFF — make the skills workspace-true

The skills were written on machines that no longer exist (a host workstation,
the wkdev containers) and routinely direct an agent inside a sandboxed workspace
to host-only actions — which is how "I tried to build in the sandbox and was
told to give claude access to the host" happens.

Almost nothing on this list has been fixed; each item below was re-checked
against `claude/skills/` and is still true.

## Rules

- Remove any detail that is stale. Every skill invokes a deterministic tool or
  script, never freehand steps.
- Cap tokens spent quoting benchmark results and build failures.
- Never a hand-rolled build; always the script. Never a custom benchmark
  methodology: the script handles cli/graphical, reports GPU rendering, and
  skips SIMD subtests only on ARMv7.
- Never store useful data in /tmp.
- New Claude threads default to RC-enabled, named
  `<purpose>-<host-os/arch>-<target>`.
- Never push or commit in git unless asked.

## Remaining — defects

**Build guidance, three contradictory sources.** `claude/CLAUDE.md` says use
`wk`; `jsc/SKILL.md` says use the build-webkit skill; `build-webkit/SKILL.md`
opens with a raw `nice -n 10 Tools/Scripts/build-webkit --gtk --release -j16`
called the "shared workstation default" (lines 36-44). Pick one owner
(build-webkit), make its first branch "inside a wk workspace → `wk build
<config>`", and have the others point at it. That file also still carries a
whole retired-machine section (wkdev32, `/home/<u>/Development/32`, host
`podman exec` for ccache, host coredumpctl+gdb) — delete it or label it history.

**Host-only steps that dead-end a sandboxed agent.**
- `jsc-jetstream-compare/SKILL.md`: "on the HOST … sudo cpupower" (line 59) then
  "if you cannot pin it, DO NOT PROCEED", while the promised no-privilege
  fallback contains only "prompt the user". Restore it (`uclampset -m 1024 --
  <cmd>` survives in jsc-microbenchmark) and define an explicit in-sandbox
  degraded mode. Also `/sdk/webkit`, a literal-ellipsis path, "ask the user to
  sudo kill", and the claim that `quiesce.sh` is "a symlink to
  wk-tools/quiesce.sh" (line 251) — the file does not exist; `wk quiesce` is
  what to name.
- **`wkdev-enter` is in three skills' allowed-tools** (`jsc-microbenchmark`,
  `jsc-jetstream-compare`, `jsc-review`) and in one body — a container the fleet
  no longer has.
- **The hardcoded samply path survived the partial `wk profile` fix**:
  `jsc-marker-trace/capture.sh:19` still defaults to
  `$HOME/Development/samply/target/release/samply`, and the same path is in that
  skill's allowed-tools and README. The provisioning half is
  `docs/Urgent/HANDOFF-profile.md`.
- **`rpi3/SKILL.md` prompts for a LAN IP** and offers `root@192.168.1.159` — a
  guaranteed stall from a workspace, and wrong besides: the rpi4 is reached at
  10.99.1.10 through the bridge and the rpi3 is unprovisioned. Retarget to the
  fleet bench devices or mark host-only. There is still no rpi4 skill.

**Consistency.**
- `jsc-marker-trace` says `git checkout -- <file>`, which the jsc skill's own
  hook forbids; use `git stash push -- <file>` / `git stash pop`.
- "The Bash tool runs zsh" vs "runs bash" asserted in different skills —
  machine-specific claims do not belong in shared skills.
- Cross-references between jetstream/profile/microbenchmark need "skip if
  already loaded" qualifiers (fix-webkit-ews→jsc already has one).
- The 0.02% equivalence margin in jsc-jetstream-compare implies ~25x normal
  round counts; make the compute budget mandatory with a default cap.
- Deduplicate: CPU-pinning doctrine (4 copies), DYLD/LD library-path lore (5
  copies), "record learnings in the skill" (2 copies).

**Verify, do not redo:** the host/workspace split of settings.json and CLAUDE.md
is in place (`claude/install.sh` links the `-host` variants; `container/
firstrun.sh` and `vm/provision-base.sh` link the workspace ones). macOS guests
link `~/.claude` from the synced tree and their skills are read-only there — a
mutable guest skills store, if wanted, is its own small design.

## Done means

An agent started by `wk claude` in a container and in a macOS guest can follow
every skill it can trigger without hitting a host-only instruction, and
`wk skills status` is clean against the repo.
