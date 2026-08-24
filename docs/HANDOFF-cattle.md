# HANDOFF — cattle, not pets: the remaining gaps

**The rule:** every machine is reproducible autonomously from this repo plus
*declared* restorables, and nothing may rely on hidden state or hidden setup.
The binding statement, with its four obligations, is in `CLAUDE.md`; the
from-nothing recipe for each machine lives in that machine's own conf
(`boot/machines/*.conf`, `targets/hosts/*.conf`, `bridge/hosts/*.conf`).

Both enforcement points exist: `wk selftest --quick` checks every machine conf
loads standalone and that the listing and the directory are the same set, and
`wk doctor` ends with the machine-local-state section — each entry declared
`regenerable`, `re-authable` or `backed-up`.

**The test of the whole rule is a rebuild**, and the punch-list for the day it
happens is `docs/HANDOFF-reprovision.md`.

## Remaining — the gaps, with owners

- **`wk sysimage flash --reader`** — the first medium for a from-nothing board
  still needs another provisioned machine or a hand flash.
  (`docs/HANDOFF-sdcard.md`, `docs/HANDOFF-vocabulary.md` lifecycle item 1.)
- **`wk provision` / `wk unprovision`** — `wk pi setup` + `wk pi boot-order` are
  most of provision, but no single verb walks the lifecycle and nothing removes
  a device cleanly. (`docs/HANDOFF-vocabulary.md` item 3.)
- **Settings drift** — until the settings audits run, a hand-applied setting on
  either workstation may be relied on without being in the repo. The audits are
  the sweep; obligation 3 is the policy.
  (`docs/HANDOFF-settings-audit.md`.)
- **Bench results are data, not state, and have no backup story.** Runs and
  their provenance live in the store and are *not* regenerable — the one store
  content worth backing up — and `wk doctor`'s machine-local list does not name
  them. A rebuild loses them silently, which is exactly what obligation 2
  forbids. Small, real, and still unowned.
- **moose's BMC** — its own firmware config (users, the bmc0 network) is set by
  hand and captured nowhere. (`docs/HANDOFF-bmc.md`.)
- **The Mac's bench volume** is the fleet's one inherently hands-on artifact:
  Apple Silicon boot policy lives in the machine's own secure storage, so no
  image can produce it. That is allowed — a gap either becomes a verb or is
  recorded in its conf/doc as hands-on *with a runbook*, and this one has one
  (`docs/HANDOFF-mac-perf-mode.md`).
- **The home layer** — gateway, proxmox, nextcloud, immich, overleaf are
  machines this repo does not reproduce and does not pretend to. Out of wk's
  scope, in the principle's scope, named here so the ledger is complete.

## What "done" looks like

Every gap above either becomes a verb or config in this repo, or is recorded as
inherently hands-on with a runbook. `wk doctor` stays the enforcement point:
its machine-local section must name every machine-local thing a rebuild would
miss.
