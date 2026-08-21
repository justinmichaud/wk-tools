# HANDOFF — cattle, not pets: every machine reproducible from nothing

**The rule, decided 2026-08-21:** every machine is reproducible autonomously
from this repo plus *declared* restorables, and nothing may rely on hidden
state or hidden setup. Concretely, four obligations:

1. **A machine is defined by config in this repo** — a fleet machine by
   `boot/machines/<name>.conf` (which opens with its from-nothing recipe), a
   build host by `targets/hosts/<name>.conf`, a bridge by
   `bridge/hosts/<name>.conf`. Adding a device is adding a file; changing a
   role is editing a field. Enforced: `wk selftest --quick` checks every
   machine conf loads standalone and that the listing and the directory are
   the same set.
2. **Machine-local state is declared, never hidden.** `wk doctor` ends its
   config half with the machine-local-state section: everything a rebuild
   cannot get from this repo, each entry one of three kinds that *is* its
   restore story — `regenerable` (wk makes a new one), `re-authable` (a
   person logs in again), `backed-up` (genuinely local data; not in your
   backups means lost with the disk). A new piece of machine-local state
   that is not added there is a bug.
3. **No hand-applied setting survives.** A setting either goes through
   `./setup`/`wk backup` into the repo, or it is expected to vanish on
   rebuild. The settings audits (`docs/HANDOFF-settings-audit.md`, scheduled
   last in both lanes) are the sweep that finds what has drifted.
4. **Provisioning is a verb, not a wiki page.** Where it is not yet a verb,
   that is a named gap below, not an accepted state.

## The ledger — every machine, its from-nothing path, and what is local to it

### Fleet (boot/machines/*.conf — each conf carries the recipe)

- **rpi5** — seed: flash Ubuntu's preinstalled raspi image to the NVMe; then
  `./setup`, `host/linux/rpi5/rpi5-setup.sh` (stability half; the custom NUMA
  kernel is reproducible by `rpi5-numa-kernel.sh`), and the bench stick by
  `wk sysimage build perf-linux-rpi5` + `write`. Machine-local: `rpi5.conf`
  (the WiFi identity — gitignored because the repo is public; **backed-up**
  kind, shape documented by `rpi5.conf.example`, BSSID re-derived by scan).
- **rpi4** — wholly wk-owned: the SD rescue system is
  `wk sysimage build downstream-yocto-wpe-2.48-rpi4`, the bench stick is
  `perf-linux-rpi4`, the tailnet is `wk pi setup rpi4-test`, the firmware is
  `wk pi boot-order rpi4-test usb-first`. Machine-local: nothing but the
  tailscale auth (re-authable).
- **rpi3** — recipe in its conf; unprovisioned, hands-on stub driver, OTP
  deliberately unburned.
- **mbp/tolken** — macOS install + `./setup`; preferences round-trip through
  `wk backup` (`host/macos/defaults.conf`). The bench volume is the one
  **inherently hands-on** artifact in the fleet: Apple Silicon boot policy
  lives in the machine's own secure storage, so no image can produce it —
  the runbook (`docs/HANDOFF-mac-perf-mode.md`) is the reproducibility story,
  and that is accepted, not hidden. The golden guest is expensive cattle:
  `wk vm base` rebuilds it in hours; the one hand step inside is a
  `claude login` (re-authable).
- **benchvm** — a clone of the golden guest; pure cattle by construction.

### Build hosts (targets/hosts/*.conf)

Not ours to reproduce — they are Igalia's machines — but **wk's footprint on
them is**: `wk remote setup <name>` provisions everything wk needs without
root, `wk remote rm` undoes it, and the per-machine deploy keys are
regenerable (`wk key register`; the one manual side is registering the new
key on GitHub and revoking the old). A rebuilt workstation reacquires every
build host from the shared conf alone.

### Bridges (bridge/hosts/*.conf)

Designed as cattle from the start: pmOS onto the phone (`wk help bridge`),
`wk bridge setup <name>` for everything after, `wk bridge rm` as the verified
inverse. The hardware half is pending (TESTING.md §8), not hidden.

### Workstation hosts themselves (moose, and any successor)

OS install + `git clone` + `./setup` (idempotent; `SETUP.md` is the runbook)
+ `wk sync`. dconf and the apt manifest round-trip through `wk backup`. The
store is regenerable in full: the mirror by `wk sync`, snapshots from it,
systems by `wk sysimage build`, secrets by `wk key register`. The two
declared exceptions are in `wk doctor`'s section: this device's own target
overrides (`~/.config/wk/targets/`, `targets/local/` — backed-up kind), and
credentials (re-authable).

## The gaps — named, with owners

- **`wk sysimage flash --reader`** — the first medium for a from-nothing
  board still needs another provisioned machine or a hand flash
  (`docs/HANDOFF-vocabulary.md` lifecycle item 1, `docs/HANDOFF-sdcard.md`).
- **`wk provision` / `wk unprovision`** — `wk pi setup` + `wk pi boot-order`
  are most of provision but no single verb walks the lifecycle, and nothing
  removes a device cleanly (vocabulary lifecycle; the interactive
  provisioning ask lives there too). The registry half of that item landed
  2026-08-21 (`boot/machines/*.conf`).
- **Settings drift** — until the settings audits run (scheduled last, both
  lanes), a hand-applied setting on either workstation may be relied on
  without being in the repo. The audits are the sweep; rule 3 is the policy.
- **Bench results are data, not state** — runs and their provenance live in
  the store and are *not* regenerable; they are the one store content worth
  a backup story, and none exists. Small, real, unowned.
- **moose's BMC** — its own firmware config (users, the bmc0 network) is
  configured by hand and captured nowhere; `docs/HANDOFF-bmc.md`'s hardware
  half is where that belongs.
- **The home layer** — gateway, proxmox, nextcloud, immich, overleaf are
  machines the user owns that this repo does not reproduce and does not
  pretend to. Under the generalizing decomposition
  (`docs/HANDOFF-generalizing.md`) they are `home`-layer work: out of wk's
  scope, in the principle's scope, and named here so the ledger is complete.

## What "done" looks like

Every gap above either becomes a verb/config in this repo, or is recorded in
its conf/doc as inherently hands-on with a runbook (the mbp's volume is the
model). `wk doctor` stays the enforcement point for rule 2: the section must
name every machine-local thing a rebuild would miss.
