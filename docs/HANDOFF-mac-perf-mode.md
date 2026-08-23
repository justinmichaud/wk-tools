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
2026-08-19; recorded in `docs/HANDOFF-boot.md` as tier 2.

**Tested rather than reasoned about, 2026-08-23** — because "SIP is off, so
surely root can just set it" is the obvious next thought and it is wrong. In a
macOS 26 guest with SIP *disabled* and passwordless root:

| attempt | result |
|---|---|
| `nvram boot-volume=<other volume group>` | exits **0** and changes nothing. The value lands under the `7C436110-…` GUID and is discarded; `IODeviceTree:/options` still holds the firmware's own value, before *and after* a reboot |
| `bless --mount … --setBoot` | "This is not supported on Apple Silicon based systems", in its own man page |
| `systemsetup -getstartupdisk` | prints `(null)`; `-liststartupdisks` prints nothing at all |
| `bputil` | changes *security policy* per volume group, and needs a volume owner's username and password. It does not select a volume |

So **SIP is not what gates this.** The variable is firmware-owned, not
SIP-protected, and disabling SIP buys nothing here. What the firmware publishes
*is* readable, and that turned out to be the useful half — see the next section.

## What the firmware default buys, once you read it

`nvram -p` publishes `boot-volume` as three colon-separated UUIDs, of which only
the last identifies anything on the disk: the APFS **volume group**. Compare it
against `diskutil info`'s group for each install and it says which install the
*next plain reboot* will enter. `wk boot mbp --status` reports it as
`firmware_default=` since 2026-08-23.

It is evidence and not a promise, in a specific and useful way: the startup
manager boots a volume **once without updating this variable**, so a machine
last started that way names a default it is not currently running. That is
exactly the state this Mac was found in on 2026-08-23 — booted `Macintosh HD`,
`boot-volume` naming `WK Bench`, because `startosinstall` made the benchmark
volume the default and somebody used the startup manager to get back.

Which matters because it decides where the one human step falls, and the two
cases are not equally expensive:

| firmware default | entering bench mode | coming back |
|---|---|---|
| `Macintosh HD` | one human step (startup manager) | free — a plain reboot |
| `WK Bench` | free — a plain reboot | one human step |

Either way it is **one human action per A/B, not per run** — which is the whole
argument for `wk bench mac-ab` below: a planted job holds every round of every
arm, so a twelve-run experiment costs the same single action as a one-run one.

## `wk bench mac-ab` — the unattended A/B, written 2026-08-23

`bench/mac-ab.sh` plus `bench/mac-bench-autorun.sh`. A different shape from
`wk bench mac`, and the difference is forced by the machine rather than chosen.

**Why it plants a job instead of driving one.** `wk bench mac` holds its state
on another machine and reaches into bench mode over ssh once per phase. That
needs bench mode to answer over the network, and this install's network is its
own: `com.apple.wifi.known-networks.plist` exists on the benchmark volume and a
run there did clone Speedometer from GitHub (2026-08-22 19:32), so it *does*
join — but nothing about that is guaranteed, its address is a DHCP lease shared
with host mode (private Wi-Fi MAC is off, so both installs present the same
hardware MAC and take the same lease), and it has no tailnet identity at all.
Fixing any of that needs the System keychain and `SystemConfiguration`, both
root, in the mode where root costs a password.

So everything the run needs is written onto the volume **while it is merely
mounted** — which is the same property that makes staging a copy rather than a
transfer — and a per-user LaunchAgent in `~bench/Library/LaunchAgents` starts it
when the bench account auto-logs in. Nothing in the path needs a password:

| step | why no password |
|---|---|
| staging | `/var/wk` on the volume is owned by uid 501: this account in host mode, `bench` over there |
| planting | `~bench` is uid 501 too, so its `LaunchAgents` is writable from host mode |
| the reboot | `osascript … restart` restarts as the logged-in user |
| the run | the bench account has NOPASSWD sudo on that install, so `wk quiesce` works with nobody to ask |
| coming back | the autorun reboots when it finishes |

**The ordering is the safety, and it is the rpi4's self-disarm reached without a
firmware register.** The autorun advances its state file *before* it runs
anything, counts its attempts (three, then it abandons the job), and arms a
watchdog that reboots the machine whatever happens to the benchmark. So a panic,
a hang, a power cut and a timed-out run all land on a boot that knows the job
was attempted and does not attempt it again.

**And it cannot loop.** If the firmware default is the benchmark volume, the
reboot at the end lands back in bench mode — where the state file says the job
is finished, so the autorun removes its own agent and **halts** rather than
running again. Worst case is a machine that is off, not a machine in a loop, and
the person who powers it on picks a disk once.

**Interleaved, not blocked.** `A B A B …`, because the machine drifts: the SSD
warms, the fans spin up, the thermal budget twenty minutes in is not the one at
the start. Blocking the arms puts all of that drift on one side of the
comparison and calls it a result. `wk pi bench --ab` interleaves for the same
reason.

**Two arms with the same staged build is the control, not a mistake.** An A/A
measures this lane's noise floor, and there is no honest way to read an A/B
without it — a 2% difference means nothing until you know whether the same build
twice differs by 3%. `wk bench ab-summary` says so in its output when it sees it.

**What the guest rehearsal caught, 2026-08-23** — the reason it was rehearsed in
a VM before the volume. The first arm died 20 s after login with

```
patch: Can't create '/var/folders/…/T/patchXXXX' … No such file or directory
```

run-benchmark applies a plan's patch through `patch`, which writes into
`DARWIN_USER_TEMP_DIR` — and that directory is created by the per-user bootstrap,
which at login has not necessarily happened yet. The second arm, sixteen seconds
later, was fine. On the real volume that would have been a missing number with
nobody in the room to see why, so the autorun now waits for a writable per-user
temp directory before it starts and settles for 90 s by default.

**What the first three real cycles cost, and what was actually wrong
(2026-08-23).** Worth recording in the order the causes were *found*, because
two of the three guesses in between were wrong and the wrong guesses are
instructive.

Every cycle staged, planted and rebooted cleanly, and the machine entered bench
mode exactly as the firmware default predicted. Every one produced no numbers.

*Guess one, wrong: the unpinned payload.* The lane had staged with no pinned
payload and warned about it, so the obvious reading was that each arm died
fetching Speedometer over a network the install may not have. It is a real
hazard — staging now **refuses** without a pinned payload rather than warning,
because the consequences of that warning arrive after a reboot on a machine
with no way to report them — but it was not what happened. The runs never got
that far.

*Guess two, wrong: the update-schedule check.* What the log actually showed was
one failing preflight check, six times:

```
warning:   updates:    Automatic checking for updates is turned on
  FAIL  quiet machine   see above -- 'wk quiesce on' first
```

Everything else passed — bench mode, `bench is logged in at the screen`, screen
free, AC power, pyobjc, the build. But that wording does not exist in the
current `lib/quiet.sh`: it is the *old* reader, the one this file's own comment
describes as lying on macOS 26. So the benchmark install was running an old tree.

*The actual cause: provisioning that could not stop running.*
`bench/mac-bench-firstboot.sh` ends by removing its own launchd job — and it did
it with `launchctl bootout system <its own label>`, which is `kill $$` with extra
steps. The job dies before the `rm`. So it had **never** removed itself: ten runs
between 2026-08-22 and 2026-08-23, both files still carrying their original
mtimes, the log saying "removing the first-boot daemon" every time.

Two things it does on each of those runs are fatal to a planted A/B:

- it `rsync --delete`s its payload copy of wk-tools over
  `~bench/Development/wk-tools`, **replacing this lane's tooling with the copy
  the install was built with** — which is why the run read an old
  `lib/quiet.sh`, and why the tree reverted between being verified and being
  used; and
- it ends with `shutdown -r +1`, so the machine reboots about a minute into the
  run. The 17:51Z cycle got two preflights in before the machine went down under
  it.

The same suicide, in the same shape, had been found in
`bench/mac-bench-autorun.sh` a few hours earlier in a guest rehearsal. Worth
knowing that it is an easy mistake to make twice.

What changed, and why each is more than a bug fix:

- **the daemon removes itself by deleting its files**, not by booting out its own
  label. Its `rm` is now checked, and it says so if the file survives.
- **the autorun defuses a daemon it finds**, because a volume provisioned before
  the fix still carries the broken one. It kills the running script *before* it
  can schedule a reboot rather than cancelling one afterwards — the two start
  within two seconds of each other, so reacting is a race that cannot be seen —
  and re-checks for a pending shutdown after the settle.
- **planted tooling goes to `/var/wk/wk-tools`, not `~bench/Development`.** The
  install's own copy is the one directory on that volume something else rewrites.
  A run should not depend on what the install happens to carry anyway.
- **`put_tree` verifies the far end** — a sentinel file and a byte count — rather
  than trusting an exit status. It was already doing this when the tree reverted,
  which is the useful part of the story: the verification was true when it was
  made. What it could not tell was that something would undo it. Hence the point
  above.
- staging refuses without a pinned payload; `wk bench seed` no longer forwards
  for a non-container workspace and no longer computes its cache path before a
  target is loaded (`mkdir: /var/lib/wk: Permission denied`, a variable read too
  early); seeded payloads drop `.git`, because git's fsmonitor leaves a Unix
  socket that no copy tool can reproduce (`rsync: mkstempsock: Invalid argument`,
  openrsync).

One measurement finding, separate from all of it and worth keeping: the update
scan was **real**. `LastFullSuccessfulDate` on the volume shows a software-update
scan starting at 15:41:17 — thirty seconds into a run. The autorun now writes
`AutomaticCheckEnabled=false` itself before quiescing and reads it back (it came
back `0`), because provisioning's attempt at the same thing is on record as not
sticking.

The pattern worth carrying, and it is the one every item above is an instance of:
**on this lane, anything that can only be discovered after the reboot has to be
refused, verified, or defused before it.** Nothing over there can tell you
anything until the machine comes back, and by then it has cost a cycle and a
trip to the keyboard.

So the fleet's arming models put the intent in different places, and this
machine is the one where the place is a person:

| machine | arming | who does it |
|---|---|---|
| rpi5 | `one-shot` — a firmware register | one ssh command |
| rpi4 | `medium` — the stick's boot partition | one ssh command |
| rpi3 | `hands-on` — the card in the slot | swap the card |
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
  host mode, which removes that variable, at the cost of living in the same
  container as the machine's real install.

**Decided 2026-08-22: the second APFS volume.** The reasoning is that the disk
is the one variable that cannot be corrected for after the fact — a number
taken against a different SSD than host mode uses is not a slower number, it is
a different measurement — whereas every cost of sharing the container is a cost
that can be seen and managed:

- the two installs share free space (APFS volumes have no fixed size), so the
  benchmark install growing is the workstation shrinking. `wk bench mac-volume`
  refuses below 120 GB free rather than warning, because the disk that fills is
  the one being worked on.
- they share the SSD's wear and thermal behaviour. That is the point — it is
  what makes them comparable — but a long run is heating the workstation's disk.
- the way back is `sudo diskutil apfs deleteVolume 'WK Bench'`, one command,
  which is the practical argument over an install on a disk you have to find.

Name the volume **`WK Bench`** (or set `WK_BENCH_VOLUME`). The name is both the
identifier `wk boot` uses and the thing you click in the startup manager.

## Provisioning it, once

`wk bench mac-volume` now does most of this, on the Mac, in four steps that
stop where a person is needed (2026-08-22):

```
wk bench mac-volume                    # the container, the room, the plan
wk bench mac-volume --create           # add the APFS volume
wk bench mac-volume --fetch            # list installers; --version to pick
wk bench mac-volume --install          # startosinstall; needs a credential, reboots
   ... Setup Assistant: one user, no Apple ID, no FileVault ...
wk bench mac-volume --provision        # in bench mode: marker, quiet, python
```

It deliberately does **not** go through the `wk quiesce` privileged helper.
That helper is a fixed allowlist granted NOPASSWD forever
(`admin/wk-quiesce-priv`), and it stays auditable because everything in it is
small, reversible and per-run. "Create a volume" and "run `startosinstall` as
root" are once-per-machine provisioning; granting them unconditionally would
trade a real root escalation for saving one password prompt on a command run
once a year. `--provision` also refuses to run in host mode, because writing a
bench-mode marker onto the workstation would make `wk bench staged` measure a
machine with a desktop under it and label the result `bench_host=image` — a
wrong number that looks right.

What is left for a person is **one credential**: `startosinstall --passprompt`,
because Apple Silicon will not personalise a volume without a volume owner.
Everything else on the old version of this list turned out to be automatable,
and was automated on 2026-08-22 — Setup Assistant (`.AppleSetupDone` in the
installed package), Command Line Tools (the trigger file below, not the GUI
prompt), the console login (auto-login), and `authorized_keys` (carried in the
package from this Mac's own).

Two things that are *not* what the old list assumed, both measured on the real
volume rather than reasoned about:

- **`startosinstall --installpackage` accepts an unsigned package.** Assumed to
  need a Developer ID Installer certificate; it does not. The receipt is written
  and the payload lands from a package reporting `no signature`.
- **The install makes the benchmark volume the *sticky* default startup disk.**
  That inverts what the rest of this document assumes: after
  `startosinstall --volume`, a plain reboot keeps booting `WK Bench`, and it is
  *host mode* that needs the startup manager. `wk boot mbp --back` is therefore
  wrong immediately after an install, and System Settings → Startup Disk is how
  the default goes back to `Macintosh HD`.

The list below is what `--provision` sets and what it only reports:

1. **The mode marker.** `/etc/wk-image`, with at least `id=<something>`:

   ```
   id=perf-macos-tolken-2026-08
   profile=perf-macos-tolken
   ```

   The `id=` line is the only thing that tells `wk` bench mode answered.
   Without it the benchmark install reports itself as host mode and
   `wk bench staged` refuses to run there — correctly, because it cannot tell.

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

3. **Command Line Tools**, for `/usr/bin/python3` — and on a fresh install
   there is no `python3` at all, which is worth stating plainly because two
   separate things fail on it: the benchmark driver's `prepare_env` does a bare
   `import objc`, and any provisioning that writes `/etc/kcpassword` with a
   Python helper silently does nothing.

   `xcode-select --install` is a GUI prompt no script can answer. The headless
   route, established 2026-08-22 on the real volume, is that `softwareupdate`
   will not even *offer* CLT until the on-demand trigger file exists:

   ```
   sudo touch /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress
   softwareupdate --list                       # now lists "Command Line Tools for Xcode 26.6"
   sudo softwareupdate -i "Command Line Tools for Xcode 26.6-26.6"
   ```

4. **pyobjc**, which does *not* arrive with the Command Line Tools despite what
   this list used to say. Measured on the real volume and in a guest: no python
   on either machine could `import objc` after CLT was installed.

   The install fails in a way that looks like a compiler problem and is not:
   Apple ships pip **21.2.4**, which is too old to take pyobjc's wheels, so it
   falls back to building `pyobjc-core` from source and fails. Upgrade pip
   first, then refuse source builds outright so a failure is loud:

   ```
   /usr/bin/python3 -m pip install --user --upgrade pip
   /usr/bin/python3 -m pip install --user --only-binary :all: \
       pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-Quartz
   ```

5. **scipy**, only if you want `wk bench compare` to run there:
   `/usr/bin/python3 -m pip install --user scipy`.

6. **ssh in**, so a run can be driven from a terminal rather than a keyboard.
   macOS ships Remote Login *off*, and it is off in host mode too as of
   2026-08-22 — which is worth saying plainly because it blocks the lane before
   anything else in this list can even be checked, and because it is not
   something the tooling can turn on for itself:

   ```
   sudo systemsetup -setremotelogin on      # or System Settings -> General -> Sharing
   ```

   Then authorise the driving machine's key in `~/.ssh/authorized_keys` —
   `wk bench mac --preflight` prints the exact key to paste. Both installs need
   this, and they need it *separately*: two installs means two
   `authorized_keys`, and it is the second one that is easy to forget, because
   forgetting it is only discovered after the reboot.

   The bench install also needs a name the driver can reach it by. If it is not
   on the tailnet — its tailscale state is its own, and a fresh install has
   none — give it one and set `WK_MAC_BENCH_SSH` to a `.local` or LAN name for
   it. The `tolken-bench` stanza assumes the tailnet name is shared.

7. Nothing else. No Xcode, no checkout, no toolchain: the moment the benchmark
   install can build, it is a second workstation drifting from the first, and
   the numbers stop meaning what they say.

## The flow, once it exists

```
wk build <ws> mac-release              # in a macOS guest, where builds belong
wk bench stage <ws> --to mbp           # onto the volume, while it is mounted
wk boot mbp                            # checks, records, prints the ritual
   ... choose the disk (see below) ...
wk bench staged --plan speedometer3.0  # over there, in bench mode
wk boot mbp --back                     # a plain reboot; the result stays put
wk bench staged --ls                   # read it back from host mode
```

### Or as one command — `wk bench mac`, written 2026-08-22

`bench/mac-lane.sh` runs all six, waits for the one that is a person, and
resumes across the reboot:

```
wk bench mac <ws> --preflight          # what the lane needs, before anyone walks over
wk bench mac <ws> --plan speedometer3.0
```

Three things about it are design rather than convenience, and each is a trap
found while building it:

- **It runs on another machine, and refuses to run on the Mac.** Phase 4
  reboots the computer the driver would be running on. Living on tolken would
  mean re-establishing itself as a launchd job in the *other* install — state
  on the machine being measured, and a benchmark install that has grown a
  scheduler, which is the same mistake as giving it a checkout. So it is driven
  from rpi5 or moose, holds the state there, and reaches in once per phase; the
  connection was never meant to survive the reboot, only the state file.
- **It waits for a *mode*, not for reachability.** The machine answers ssh in
  both modes. `wk bench staged` aimed at host mode is refused, but a `wk build`
  aimed at bench mode would quietly start turning the benchmark install into a
  workstation — so every phase asserts its mode from `/etc/wk-image` first.
- **Bench mode has its own ssh alias.** One address, two macOS installs, two
  host keys, and ssh refuses a *changed* key outright — `accept-new` accepts an
  unknown host and still refuses a changed one. So the lane would arm the
  machine, wait for it to come back, and then be unable to talk to what came
  back. `tolken-bench` in `dotfiles/ssh/config` carries a `HostKeyAlias`, which
  gives the two installs separate known-hosts identities on one address: each
  is pinned normally, neither can masquerade as the other, and a command aimed
  at the wrong mode fails on the key rather than doing the wrong thing quietly.

The phases are recorded as they complete (`wk bench mac <ws> --status`), so an
interrupted lane is resumed by repeating the command and a lane whose build is
already staged does not rebuild. `--reset` forgets it.

**Unverified against hardware**: everything above the reboot has been exercised
only as far as an unreachable machine allows — the preflight, the refusals, the
phase state machine and the state file. The lane has never completed, because
there is no benchmark volume yet and, as of 2026-08-22, Remote Login on tolken
is off.

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
they were two macOS guests rather than one Mac in two modes, which is the only
thing this task adds. 2026-08-20: built `mac-release` in one guest (8m1s
incremental, peak 7.6 GB), staged 1.5 GB of products onto another, and ran
JetStream2.2 there through `wk bench staged`: `BENCH OK`, per-subtest scores,
and a record carrying `bench_host=image`, `role_marker_overridden: false`, the
full sha and the machine. The web process launched out of the staged tree, so
the exclusions keep everything the browser driver needs.

Also verified on this Mac: the whole `wk boot mbp` lifecycle against a
disposable APFS volume, and every refusal in the preflight.

**Not verified**: this Mac, in bench mode, producing a number that means
something. Everything upstream of the reboot is exercised now; what is left is
the volume and the two clicks.

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
readable from host mode after `wk boot mbp --back`, and comparable against a
container run with `wk bench compare` printing exactly the axis warnings it
should. Then tick the `[ ]` lines under "The bare-metal benchmark run" and
"The Mac: a mode transition nobody can automate" in `docs/TESTING.md`.
