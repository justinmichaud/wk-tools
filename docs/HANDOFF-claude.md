# HANDOFF — make CLAUDE.md and the skills workspace-true

**Gated on `docs/HANDOFF-wk-in-workspace.md`**: several fixes below need the
in-workspace `wk build <config>` interface to exist before a skill can be told
to use it.

The problem, in one sentence: the skills were written on machines that no
longer exist (a host workstation, the wkdev containers) and routinely direct an
agent inside a sandboxed workspace to host-only actions — which is how "I tried
to build in the sandbox and was told to give claude access to the host"
happens.

## Rules (from the original request)

- Remove any detail that is stale now. Every skill invokes a deterministic
  tool or script, never freehand steps.
- Cap tokens spent quoting benchmark results and build failures.
- Never a hand-rolled build; always the script. Never a custom benchmark
  methodology: the script handles cli/graphical, reports GPU rendering, and
  skips SIMD subtests only on ARMv7.
- Never store useful data in /tmp.
- New Claude threads default to RC-enabled, named
  `<purpose>-<host-os/arch>-<target>`.
- Never push or commit in git unless asked.

## Concrete defects to fix (2026-08 audit)

Build guidance, three contradictory sources:
- `claude/CLAUDE.md` says use `wk`; `jsc/SKILL.md` says use the build-webkit
  skill; `build-webkit/SKILL.md` opens with a raw
  `nice -n 10 Tools/Scripts/build-webkit -j16` for a host workstation. Pick
  one owner (build-webkit), make its first branch "inside a wk workspace →
  `wk build <config>`", and have the others point at it.
- `build-webkit/SKILL.md` also carries a whole retired-machine section
  (wkdev32, `/home/<u>/Development/32`, host `podman exec` for ccache, host
  coredumpctl+gdb). Delete or label it as history.

Host-only steps that dead-end a sandboxed agent:
- `jsc-jetstream-compare/SKILL.md`: "on the HOST ... sudo cpupower", then "if
  you cannot pin it, DO NOT PROCEED" — while the promised no-privilege uclamp
  fallback section contains only "prompt the user". Restore the fallback
  (`uclampset -m 1024 -- <cmd>` survives in jsc-microbenchmark) and define an
  explicit in-sandbox degraded mode. Also: `/sdk/webkit` and `wkdev-enter`
  references, a literal-ellipsis path (`~/Development/.../OpenSource/...`),
  "ask the user to sudo kill".
- `jsc-profile` / `jsc-marker-trace`: samply hardcoded at
  `~/Development/samply/...` (no workspace provisions it — lane A step 10 is
  where it gets built), "ask the user to sudo tee perf_event_paranoid",
  "open the profile from the host's Firefox" (blocked by egress).
- `rpi3/SKILL.md`: targets a LAN IP the workspace firewall drops, and prompts
  for an IP — guaranteed stall. Either retarget to the tailnet rpi4/rpi5 (for
  which no skill exists) or mark host-only.

Consistency:
- `jsc-marker-trace` says `git checkout -- <file>`, which the jsc skill's own
  hook forbids; use `git stash push -- <file>` / `git stash pop`.
- "The Bash tool runs zsh" vs "runs bash" asserted in different skills —
  machine-specific claims don't belong in shared skills.
- Cross-references between jetstream/profile/microbenchmark need "skip if
  already loaded" qualifiers (fix-webkit-ews→jsc already has one).
- The equivalence margin in jsc-jetstream-compare (0.02% overall) implies
  ~25x normal round counts; make the compute budget mandatory with a default
  cap instead of optional.
- Deduplicate: CPU-pinning doctrine (4 copies), DYLD/LD library-path lore
  (5 copies), "record learnings in the skill" (2 copies).
- `jsc-jetstream-compare` claims `quiesce.sh` is "a symlink to
  wk-tools/quiesce.sh" — it is a regular file and the target does not exist;
  point it at `wk quiesce`.

Config plumbing (partially done 2026-08-18 — verify, don't redo):
- Host/workspace split of settings.json and CLAUDE.md is in place
  (claude/install.sh vs firstrun.sh/provision-base.sh).
- macOS guests now link ~/.claude from the synced tree; their skills are
  read-only there (t_sync_tools --delete would clobber edits). A mutable
  guest skills store, if wanted, is its own small design.

Done means: an agent started by `wk claude` in a container and in a macOS
guest can follow every skill it can trigger without hitting a host-only
instruction, and `wk skills status` is clean against the repo.
