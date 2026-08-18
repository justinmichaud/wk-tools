# HANDOFF — `wk selftest`: run the test plan autonomously

`docs/TESTING.md` is the test plan, and today it is a hand-ticked checklist:
nothing in the repo reads or executes it. That fails the plan's own purpose —
after building something, after a re-install, or after an upgrade, it should be
possible to run one command and learn what broke.

## What to build

`cmd/selftest`, run as `wk selftest [--section N] [--quick]`:

- Each automatable line item in TESTING.md becomes a check function: run the
  command, assert the observable outcome, report ok/FAIL in the style of
  `cmd/verify` (which is the model to copy — it already tests properties
  rather than configuration and exits 1 on any failure).
- Checks that need expensive state (a synced mirror, a built workspace, a
  running guest) declare it and are skipped with an explicit SKIP when it is
  absent — a skip must be visible, never silent.
- Checks that are inherently manual (watching a monitor go dark, judging a
  desktop) stay in TESTING.md as a short manual section; the runner prints
  them at the end as "verify by hand".
- `--quick` runs only what needs no workspace (help output, dispatch, syntax,
  config parsing, store layout) so it can gate any edit cheaply.
- The regressions table at the bottom of TESTING.md is the priority list:
  each row already cost a debugging session, so encode those first.

## Constraints

- bash 5 and bash 3.2 (`bash -n` and `/bin/bash -n`), or make the case for
  python and follow `cmd/mcp`'s precedent.
- Never start the podman machine or boot a guest unless the section that
  needs it was explicitly requested.
- A failing check must name the TESTING.md line it implements, so the plan
  and the runner cannot drift apart silently.

## Relationship to `wk doctor`

`wk doctor` (exists) answers "what is provisioned on this machine" by
inspecting state, read-only. `wk selftest` answers "does what is provisioned
actually behave" by running commands. Don't merge them: doctor must stay safe
to run anywhere anytime, while selftest may build, create workspaces, and take
minutes. selftest should *start* by running doctor and skipping sections whose
prerequisites doctor reports missing.

## Done means

`wk selftest` on a freshly set-up machine reports ok/SKIP everywhere, exits 0,
and `docs/TESTING.md` says at the top that the runner exists and how the
manual remainder is organised.
