# HANDOFF — putting this Mac into perf test mode

A task on its own, because it is the one part of the macOS benchmark lane that
software cannot do: **making a second macOS install that this Mac can boot, and
booting it.** Everything either side of that is built and, apart from the
measurement itself, exercised — see `docs/HANDOFF-benchmarking.md`, "The macOS
shape", and the `wk boot mbp` / `wk bench stage` / `wk bench staged` sections of
`docs/TESTING.md`.

Pick this up when there is a disk to spare and half an hour at the keyboard.

## Why there is a manual step at all

Apple Silicon cannot be handed an image over the wire and cannot be told from
software which volume to boot: boot volume selection goes through a LocalPolicy
held in this machine's own secure storage, changed only by an authenticated
user action. `bless --setBoot` is superseded for this purpose. Established
2026-08-19; recorded in `docs/HANDOFF-netboot.md` as tier 2.

So the fleet has three arming models and this machine has the third:

| machine | arming | who does it |
|---|---|---|
| rpi5 | `one-shot` — a firmware register | one ssh command |
| rpi4 / rpi3 | `server` — the image the server holds | change what is served |
| **mbp** | **`hands-on`** — a LocalPolicy | **a person, twice** |

`wk boot mbp` checks what it can, records the intent, and prints the ritual.
It reboots nothing, and `wk boot mbp --status` reports the machine as *armed
and waiting for a person* rather than as something that will move by itself.

## What has to exist

**A macOS install on another volume, personalised for this Mac.** Copying an
image onto a disk does not boot — the install has to be made *from* this
machine (macOS installer or Recovery). An install per Mac, maintained per Mac.

Two shapes, and the choice is a measurement decision rather than a convenience
one:

- **An external SSD over Thunderbolt.** Simple, removable, and *not the storage
  the machine normally runs on* — for anything that touches disk that is itself
  a variable.
- **A second APFS system volume in the internal container.** Same storage as
  the workstation role, which removes that variable, at the cost of living in
  the same container as the machine's real install. This is the option
  `docs/HANDOFF-benchmarking.md` flags as worth costing before ruling out; it
  has not been costed.

Name the volume **`WK Bench`** (or set `WK_BENCH_VOLUME`). The name is both the
identifier `wk boot` uses and the thing you click in the startup manager.

## Provisioning it, once

Nothing here is automated yet, and the list is short enough to do by hand. In
the new install:

1. **The role marker.** `/etc/wk-image`, with at least `id=<something>`:

   ```
   id=mac-bench-2026-08
   profile=mac-bench
   ```

   This is the only thing that tells `wk` which role answered. Without it the
   benchmark install reports itself as a workstation and `wk bench staged`
   refuses to run there — correctly, because it cannot tell.

2. **Quiet it, permanently.** `wk quiesce` covers the per-run half (a
   `caffeinate`, the analysis daemons paused, Spotlight/updates/low-power off
   through the privileged helper) and, since 2026-08-20, *measures* the result.
   What the install itself has to be:

   - Spotlight indexing off for the volume, Time Machine off (no destination
     configured, not merely "no backup running"), automatic updates off,
     sleep and screen saver off, Siri and analytics off, no login items;
   - FileVault off — decryption is CPU work in the middle of a measurement;
   - one user, logged in at the console. A browser driven over ssh with nobody
     at the screen has nowhere to draw, which is a check `wk bench staged`
     makes and a failure that otherwise looks like a hang.

3. **Command Line Tools**, for `/usr/bin/python3` — and it must be *that*
   python: the benchmark driver's `prepare_env` does a bare `import objc`, and
   pyobjc-core is not autoinstalled. Apple's python3 has it (3.9.6, pyobjc
   11.1); a Homebrew python3 does not. `xcode-select --install`.

4. **scipy**, only if you want `wk bench compare` to run there:
   `/usr/bin/python3 -m pip install --user scipy`.

5. **ssh in**, so a run can be driven from a terminal rather than a keyboard.

6. Nothing else. No Xcode, no checkout, no toolchain: the moment the benchmark
   install can build, it is a second workstation drifting from the first, and
   the numbers stop meaning what they say.

## The flow, once it exists

```
wk build <ws> mac-release              # in a macOS guest, where builds belong
wk bench stage <ws> --to mbp           # onto the volume, while it is mounted
wk boot mbp                            # checks, records, prints the ritual
   ... choose the disk (see below) ...
wk bench staged --plan speedometer3.0  # over there, in the benchmark role
wk boot mbp --back                     # a plain reboot; the result stays put
wk bench staged --ls                   # read it back from this role
```

**Which way to choose the disk matters:**

- *the startup manager* (shut down, hold the power button, pick `WK Bench`) —
  boots it **once** and leaves the default alone, so the way back is a plain
  reboot. This is the one to use.
- *System Settings → Startup Disk* — **sticky**: the machine keeps booting the
  benchmark volume until the pane is used again. Only worth it for a long
  session, and `wk boot mbp --disarm` says so because it cannot undo it for you.

## What can go wrong, and what it looks like

Every one of these is a preflight check in `wk bench staged`, because each was
either measured or found while building the runner:

| symptom | cause |
|---|---|
| `import objc` fails | the wrong python3 — see above |
| webkitpy downloads packages mid-run | first use in that tree; it installs into `Tools/Scripts/libraries/autoinstalled`. Stage from a guest where it has already happened, or let it have the network once |
| the run hangs with no window | no console session |
| numbers lower than expected, no error | on battery, or `CPU_Speed_Limit` < 100 — both reported |
| the Dock stops animating afterwards | webkitpy turns it off in `prepare_env` and only restores it on a clean exit. `wk bench staged` puts it back |
| "no `*.framework`" | the staged build directory has products missing; stage again |

## Verified, and not

**The whole path is verified, on real builds, between two real machines** —
they were two macOS guests rather than one Mac in two roles, which is the only
thing this task adds. 2026-08-20: built `mac-release` in one guest (8m1s
incremental, peak 7.6 GB), staged 1.5 GB of products onto another, and ran
JetStream2.2 there through `wk bench staged`: `BENCH OK`, per-subtest scores,
and a record carrying `bench_host=image`, `role_marker_overridden: false`, the
full sha and the machine. The web process launched out of the staged tree, so
the exclusions keep everything the browser driver needs.

Also verified on this Mac: the whole `wk boot mbp` lifecycle against a
disposable APFS volume, and every refusal in the preflight.

**Not verified**: this Mac, in its own benchmark role, producing a number that
means something. Everything upstream of the reboot is exercised now; what is
left is the volume and the two clicks.

**What the rehearsal taught, that applies here:**

- give the *first* run after a stage a generous `--timeout`. The first
  JetStream2.2 run timed out at 900 s and the second, identical, finished in
  about five minutes — not root-caused, but a first-launch scan of 1.5 GB of
  freshly copied binaries is the obvious suspect.
- the `quiet machine` check will fail on an install where automatic update
  checking is on, and `softwareupdate --schedule off` did not stick in the
  guest. Set it on the real install and confirm it stuck — `wk quiesce status`
  says whether it did.
- a killed `wk bench stage` leaves rubble; the manifest crosses last and on its
  own, so an interrupted delivery is ignored rather than measured.


## Done means

A `wk bench staged` run on the benchmark install that produces a `result.json`
and an `env.json` with `bench_host=image` and `role_marker_overridden: false`,
readable from the workstation role after `wk boot mbp --back`, and comparable
against a container run with `wk bench compare` printing exactly the axis
warnings it should. Then tick the `[ ]` lines under "The bare-metal benchmark
run" and "The Mac: a role transition nobody can automate" in `docs/TESTING.md`.
