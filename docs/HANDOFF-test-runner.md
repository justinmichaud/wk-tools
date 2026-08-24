# HANDOFF — `wk selftest` coverage

The machinery is built and the design holds; what is left is coverage.
`cmd/selftest` encodes **55 checks** across three sections — `quick` (no
workspace, no podman, no ssh), `state` and `remote` — against **756 line items**
in `docs/TESTING.md`. The plan grows faster than the runner, and the printed
coverage line is the authority on where that stands.

## Remaining

1. **A `container` section** (§1, the largest). Needs the podman machine and a
   workspace: create one, run the lifecycle/sandbox/build checks against it,
   remove it. SKIP the whole section when podman is absent or stopped — starting
   it is exactly what this must not do unasked.
2. **A `vm` section** (§2). Same, with a guest, and the expensive prerequisite
   (a golden base, a booted guest) means most of it should skip by default and
   run only on `--section vm`.
3. **`remote`** has two checks encoded (the conf resolves, the machine answers a
   listing). The rest need a workspace on a machine — cheap now, not free.
4. **The regressions table.** The hermetic rows are encoded; the rest need a
   workspace or a guest, and they are the highest-value ones left because each
   already cost a debugging session.
5. **A manual section.** `docs/TESTING.md` still mixes automatable lines with
   ones that need a human (watching a monitor go dark, judging a desktop). Mark
   them there so the runner can print them as "verify by hand" rather than
   leaving them indistinguishable from coverage nobody has written.

## Properties to preserve

- **Each check names the plan line it implements**, looked up in
  `docs/TESTING.md` at run time; a line reworded or deleted without touching the
  runner reports **DRIFT**, which is a failure.
- **A missing prerequisite is a visible SKIP**, never nothing — exit 77, with
  its output as the reason. 77 rather than a separate probe function, because a
  prerequisite is usually only discoverable by starting to do the thing.
- **Every run prints its own coverage.** A runner reporting ok over a fraction
  of the plan and saying nothing about the rest is the silent pass the plan
  forbids.
- **It starts nothing** — never the podman machine, never a guest, unless the
  section that needs it was explicitly requested.
- **bash 5 and bash 3.2**, checked for the whole tree, the runner included.
- **A check that cannot fail loudly is not a check.**

## Relationship to `wk doctor`

Deliberate and unchanged: `wk doctor` answers "what is provisioned here" by
inspecting state, read-only, safe anywhere; `wk selftest` answers "does it
behave" by running commands, and may take minutes. Do not merge them.

## Done means

`wk selftest` on a freshly set-up machine reports ok/SKIP everywhere and exits 0
— true today for what is encoded. The honest measure of the rest is the coverage
line the runner prints.
