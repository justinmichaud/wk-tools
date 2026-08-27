# HANDOFF — the Mac's bench mode

The one part of the macOS benchmark lane software cannot do: **a second macOS
install this Mac can boot, and booting it.** Apple Silicon cannot be handed an
image over the wire and cannot be told from software which volume to boot — boot
volume selection goes through a LocalPolicy in the machine's own secure storage,
changed only by an authenticated user action (`docs/HANDOFF-boot.md`, tier 2).

The volume exists (`WK Bench`, a second APFS volume in the internal container),
the unattended A/B lane runs on it, and it has produced real numbers. What
remains is below.

## Remaining

- **The `stage`/`staged` half has never run against the real install.** Three
  things unverified: a real measured run staged from a guest,
  the full `wk boot mbp` lifecycle against a real benchmark install rather than
  a disposable volume, and `wk bench stage <ws> --to mbp` from a macOS guest
  onto the volume. `wk bench compare` between a bench-mode result and a
  container run has not been done either. That is what "done means" below asks
  for.
- **`wk quiesce status` on the benchmark install, before a run** — unticked, and
  the scanner runs regardless of the preference.
- **The architecture can be simplified and has not been.** The planted agent
  exists because the run was believed undrivable. It is drivable: the bench
  install takes its *own* DHCP lease (private Wi-Fi address is on by default, so
  the two installs do not share one), and `ssh -J moose bench@<its address>`
  reaches it — moose is a tailnet node on the same LAN, so no subnet route and
  no new credential are needed. Moving the A/B lane to the `bench/mac-lane.sh`
  shape — state on the driver, reach in per phase — would remove the
  self-deleting job, the defuse-the-daemon race and the stay-up heuristic.
  Tailscale on the install is still the better answer (a name that does not
  move, no jump host, no scan) and nothing blocks it.
- **Software-update scanning is detected, never prevented**, and all three
  routes are closed: the preference does not hold, `launchctl bootout
  system/com.apple.softwareupdated` is SIP-protected and fails, and the radio
  must not be turned off (below). Per-arm scan evidence lands in `runs.tsv` as a
  fifth column and the summary names a contaminated arm. If it recurs often
  enough to matter, the only safe fix is denying Apple's update endpoints in the
  volume's `/etc/hosts`.
- **`wk quiesce` still does not raise MiniBrowser or exempt it from App Nap** —
  the one piece of the old `quiesce.sh` that never came back, and it throttles
  `requestAnimationFrame` on an occluded page
  (`docs/HANDOFF-original-helpers.md`).

## Rules this lane established — do not re-derive them

- **Never turn the radio off during a run.** A measurement fix must never be
  able to make the machine unreachable: a panic, a power cut, a SIGKILL or the
  watchdog's own reboot all leave the interface down, and in bench mode that
  needs a walk to the machine. It would also disable tailscale, the one change
  that would make this install observable.
- **Never trim the first Speedometer iteration.** With `count: 4`, positions 1,
  11, 21, 31 score ~34 against ~43 — a deterministic dip, the most reproducible
  value in the run, contributing nothing to between-run variance. Trimming it
  moved between-run sd from 0.313% to 0.342%, and trimming ten moved it to
  0.558%. Speedometer counts compilation and instantiation in the first
  iteration on purpose.
- **Never compare an arm from one session against an arm from another.**
  Absolute scores drifted ~2% between sessions on identical builds, while arms
  within a session hold to 0.4%. Interleaving inside one invocation is what
  makes a comparison valid.
- **Report the power, not just the p-value.** `wk bench ab-summary` prints the
  smallest detectable difference for the experiment it summarises, because a
  null result is uninformative without it. Rough figures at `count: 4`: 3
  rounds/arm resolves 0.75%, 8 rounds 0.35%, and `count: 12` × 8 rounds 0.20%.
  Effect sizes shrink as n grows — the same patch measured 1.05% at 3 rounds and
  0.63% at 8.
- **`--count` and `--rounds` are interchangeable for precision** (both scale as
  1/sqrt(total iterations)); buy whichever is cheaper. `--count` amortises one
  browser launch over more iterations. There *is* a small run-level term
  (~0.18%) over a 30-minute session, so a very long experiment stops improving
  as 1/sqrt(n).
- **Reading a preference tells you what the preference says, never what the
  daemon will do.** Four members of that family so far: `nvram boot-volume`
  (exits 0, changes nothing), `systemsetup -getremotelogin` (exits 0, refuses to
  answer), `AutomaticCheckEnabled` (reads back 0, scans anyway), and
  `osascript … restart` (returns 0 without rebooting when there is no GUI
  session — host mode sits at the login window, which is exactly that case).
  **Verify by effect**: the reboot is now the loginwindow Apple event, and it is
  not called done until the machine stops answering and `kern.boottime` has
  moved.
- **A killed `wk bench stage` leaves rubble** — the manifest crosses last and on
  its own, so an interrupted delivery is ignored rather than measured.
- **Give the first run after a stage a generous `--timeout`** — a first-launch
  scan of freshly copied binaries is the suspect.
- **Every path on this volume contains a space** (`WK Bench`). Anything that
  word-splits a path list breaks there and only there.

## Provisioning it, once

`wk bench mac-volume` does most of it, stopping where a person is needed:

```
wk bench mac-volume                    # the container, the room, the plan
wk bench mac-volume --create           # add the APFS volume
wk bench mac-volume --fetch            # list installers; --version to pick
wk bench mac-volume --install          # startosinstall; needs a credential, reboots
   ... Setup Assistant: one user, no Apple ID, no FileVault ...
wk bench mac-volume --provision        # in bench mode: marker, quiet, python
```

What is left for a person is **one credential** — `startosinstall
--passprompt`, because Apple Silicon will not personalise a volume without a
volume owner — plus the two clicks in the startup manager.

It deliberately does not go through the `wk quiesce` privileged helper: that
helper is a NOPASSWD allowlist and stays auditable because everything in it is
small, reversible and per-run. "Create a volume" and "run `startosinstall` as
root" are once-per-machine. `--provision` also refuses to run in host mode,
because a bench-mode marker on the workstation would make `wk bench staged`
measure a machine with a desktop under it and label it `bench_host=image`.

What the install must be:

1. **The mode marker** `/etc/wk-image` with `id=` (and `profile=`). The `id=`
   line is the only thing that tells `wk` bench mode answered; without it the
   install reports host mode and `wk bench staged` correctly refuses.
2. **Quiet, permanently**: Spotlight off for the volume, Time Machine off (no
   destination configured, not merely "no backup running"), automatic updates
   off, sleep and screen saver off, Siri and analytics off, no login items,
   **FileVault off**, and one user logged in at the console — a browser driven
   over ssh with nobody at the screen has nowhere to draw, which is a check
   `wk bench staged` makes and a failure that otherwise looks like a hang. The
   screen lock must be settled at *plant* time, before the reboot: a benchmark
   makes no input, so the idle timer runs at full load exactly as on an
   abandoned machine.
3. **Command Line Tools**, for `/usr/bin/python3` — a fresh install has no
   python at all. `xcode-select --install` is a GUI prompt no script can answer;
   the headless route is the on-demand trigger file:
   ```
   sudo touch /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress
   softwareupdate --list
   sudo softwareupdate -i "Command Line Tools for Xcode 26.6-26.6"
   ```
4. **pyobjc**, which does *not* arrive with CLT. Apple ships pip 21.2.4, too old
   for pyobjc's wheels, so it falls back to a source build and fails in a way
   that looks like a compiler problem:
   ```
   /usr/bin/python3 -m pip install --user --upgrade pip
   /usr/bin/python3 -m pip install --user --only-binary :all: \
       pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-Quartz
   ```
5. **scipy**, only if `wk bench compare` should run there.
6. **Remote Login on, per install** (`sudo systemsetup -setremotelogin on`),
   plus the driving machine's key in that install's own `authorized_keys` —
   `wk bench mac --preflight` prints it. Two installs means two
   `authorized_keys`, and it is the second that gets forgotten, because
   forgetting it is only discovered after the reboot. If the bench install is
   not on the tailnet, give it a reachable name and set `WK_MAC_BENCH_SSH`.
7. **Nothing else.** No Xcode, no checkout, no toolchain: the moment the
   benchmark install can build, it is a second workstation drifting from the
   first, and the numbers stop meaning what they say.

Two facts measured on the real volume that contradict the obvious assumptions:
`startosinstall --installpackage` **accepts an unsigned package**; and the
install makes the benchmark volume the **sticky default startup disk**, so after
it, a plain reboot keeps booting `WK Bench` and it is *host mode* that needs the
startup manager.

## The flow

```
wk build <ws> mac-release              # in a macOS guest, where builds belong
wk bench stage <ws> --to mbp           # onto the volume, while it is mounted
wk boot mbp                            # checks, records, prints the ritual
   ... choose the disk ...
wk bench staged --plan speedometer3.0  # over there, in bench mode
wk boot mbp --back                     # a plain reboot; the result stays put
wk bench staged --ls                   # read it back from host mode
```

`wk bench mac <ws>` (`bench/mac-lane.sh`) runs all six and resumes across the
reboot; `wk bench mac-ab` plants an unattended A/B on the volume. Three
properties of both are design rather than convenience:

- **They run on another machine and refuse to run on the Mac.** A phase reboots
  the computer the driver would be on; living there would mean a scheduler
  inside the benchmark install — state on the machine being measured. Driven
  from rpi5 or moose, state held there, reaching in once per phase.
- **They wait for a *mode*, not for reachability.** The machine answers ssh in
  both modes: `wk bench staged` aimed at host mode is refused, but a `wk build`
  aimed at bench mode would quietly turn the benchmark install into a
  workstation. Every phase asserts the mode from `/etc/wk-image` first.
- **Bench mode has its own ssh alias.** One address, two installs, two host
  keys, and ssh refuses a *changed* key outright. `tolken-bench` carries a
  `HostKeyAlias` so each install is pinned normally and neither can masquerade
  as the other — but pin the alias only after confirming which install answers,
  or the right key lands under the wrong alias.

## Done means

A `wk bench staged` run on the benchmark install producing a `result.json` and
an `env.json` with `bench_host=image` and `role_marker_overridden: false`,
readable from host mode after `wk boot mbp --back`, and comparable against a
container run with `wk bench compare` printing exactly the axis warnings it
should.
