# HANDOFF — `wk selftest`: run the test plan autonomously

`cmd/selftest` exists as of 2026-08-19 (13 checks then), and has grown with
every lane since: as of 2026-08-20 `wk selftest --quick` reports **34 ok,
2 skipped**, needing no workspace, no podman and no ssh — the 2026-08-20
locking, consistency and boot work all landed their checks there, since the
boot-file checks drive the resolver against fixtures rather than a board.
A bare `wk selftest` adds the `state` section (read-only commands are
read-only, one walk behind both listings) and the `remote` section. What is
left is coverage, not machinery.

## What it does

- **Each check names the plan line it implements**, as a phrase looked up in
  `docs/TESTING.md` at run time. A line reworded or deleted without touching
  the runner reports **DRIFT**, which is a failure. That is the whole answer to
  "the plan and the runner must not drift apart silently", and it works: two of
  the first checks written reported DRIFT immediately, because they were checks
  for lines nobody had added to the plan yet.
- **A missing prerequisite is a visible SKIP**, never nothing. Checks report by
  exit status — 0 passed, 77 the prerequisite is absent (its output is the
  reason), anything else failed (its output is the evidence). 77 rather than a
  separate probe function, because a prerequisite is usually only discoverable
  by starting to do the thing.
- **Every run prints its own coverage** — "588 line items in docs/TESTING.md,
  45 encoded here" as of 2026-08-20; both numbers move, and the printed line is
  the authority. A runner that reports ok over a fraction of the plan and says
  nothing about the rest is exactly the silent pass the plan forbids.
- **It starts nothing.** Verified: the podman machine is in the same state
  after a run as before it, including a full run.
- `wk doctor` runs first for context and its verdict is printed, but nothing is
  gated on it: each check probes its own prerequisite, which is more honest
  than parsing a checklist.

## What it found while being written

Three real defects, all in the first run:

- `wk selftest` itself was not in `is_host_command`, so on a macOS host it
  forwarded into the podman VM — which booted the machine and then reported
  "unknown command: selftest" from a stale copy of wk-tools. Exactly the class
  of bug the `wk build --list` regression row is about.
- The first `sudo` check flagged `cmd/gc`, which uses `sudo -n` and prints
  "run 'sudo fstrim -av' yourself" when it cannot. Two lessons: the property is
  "no sudo that can *prompt*", and a grep for a command name has to look at
  command position or it reads advice as a call.
- The argument-form check booted the podman VM: on a macOS host an
  unregistered workspace name cannot be resolved without asking the VM. Pinned
  with `WK_IN_VM=1`, which keeps the check on the argument parsing it is
  actually about.

## What is left

**Coverage.** 45 of 588 line items (2026-08-20; the plan grows faster than the
runner). The cheap ones are done — the `--quick` and `state` sections are
hermetic — and what remains needs state:

1. **Container section (§1, ~118 items).** Needs the podman machine and a
   workspace. The shape to follow: a `container` section that creates one
   workspace, runs the lifecycle/sandbox/build checks against it, and removes
   it; SKIP the whole section when podman is absent or stopped, since starting
   it is exactly what this must not do without being asked.
2. **vm section (§2, ~62 items).** Same, with a guest — and the expensive
   prerequisite (a golden base, a booted guest) means most of it should skip by
   default and run only on `--section vm`.
3. **Remote section (§3, ~51 items).** Two encoded (the conf resolves, the
   machine answers a listing). The rest need a workspace on a machine, which is
   cheap now (39 s) but not free.
4. **The regressions table.** 31 rows now. The hermetic ones are encoded; the
   remainder need a workspace or a guest, and they are the highest-value ones
   left, because each already cost a debugging session.
5. **A manual section.** The plan still mixes automatable lines with ones that
   need a human (watching a monitor go dark, judging a desktop). Those should
   be marked as such in `docs/TESTING.md` so the runner can print them at the
   end as "verify by hand" rather than leaving them indistinguishable from
   coverage nobody has written yet.

## Constraints that still hold

- bash 5 and bash 3.2. The runner checks this for the whole tree, itself
  included.
- Never start the podman machine or boot a guest unless the section that needs
  it was explicitly requested.
- A check that cannot fail loudly is not a check.

## Relationship to `wk doctor`

Unchanged and deliberate: `wk doctor` answers "what is provisioned here" by
inspecting state, read-only, safe anywhere; `wk selftest` answers "does it
behave" by running commands, and may take minutes. Don't merge them.

## Done means

`wk selftest` on a freshly set-up machine reports ok/SKIP everywhere and exits
0 — true today for what is encoded. The remaining work is the four sections
above, and the honest measure of it is the coverage line the runner prints.
