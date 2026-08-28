# wk-tools

`wk` builds, runs, tests and benchmarks WebKit/JSC in disposable, sandboxed
workspaces so the host stays clean, and it drives a small fleet of build
machines and Raspberry Pi/Mac benchmark boards, each reached by its tailnet
name. One CLI, `wk`, covers every workflow below.

## Architecture

**workspace** — a named, disposable environment for one task: a checkout, a
build tree, an agent's blast radius. `wk new` creates one, `wk rm` destroys it
completely. It remembers the target it was created on.

**target** — where a command's work actually happens. Four kinds behind one
driver contract: `container` (rootless podman, the default, the only sandboxed
target on Linux — on macOS it lives inside a podman VM), `vm` (a macOS guest
under Tart, for the Apple ports only), `remote` (a shared build machine or
another workstation, named after the machine, no sandbox), `local` (the
degenerate driver a workspace uses on itself, which is what makes `wk build`
work from inside one). A workspace remembers its target; `--target` belongs to
`wk new` alone.

**machine** — a computer `wk` drives as a build target, declared once in
`targets/hosts/<name>.conf`: a shared build box, or a peer workstation. This
registry is distinct from two others: a **fleet device** (a board or Mac `wk`
can boot into a measured system, `boot/machines/<name>.conf`) and a **bridge**
(a phone routing an unreachable segment onto the tailnet,
`bridge/hosts/<name>.conf`). Never call a fleet device or a bridge a bare
"machine".

**bench system** — the system on a fleet device that gets measured: built by
`wk sysimage build`, written to a card by `wk sysimage write`, armed for one
boot by `wk boot`. It never shares a medium with the device's own recovery
path where the hardware allows separating them.

**rescue** — what a board falls back to, and is reached by, whenever its bench
system is disarmed, unbootable, or was never written. On a workstation the
rescue is the host install itself; on a bench-device it is a system `wk` owns
on its own medium.

**arm/disarm** — select, or deselect, what a fleet device boots next
(`wk boot`). Every armed system disarms and reverts itself after one boot;
nothing here is a persistent switch.

**bridge** — a phone with two network legs (house WiFi, USB-C Ethernet to an
isolated segment) that routes that segment onto the tailnet, so a bench device
or a BMC behind it is reachable without the house network reaching either.
Provisioned with `wk bridge`.

**store** — `$WK_STORE`, the one place on a machine for artifacts kept by
reference count or content key, never by hand: base snapshots, seeded
benchmark payloads, bench results, credentials. On macOS it is two halves —
the podman VM's copy and this device's own — because a Mac cannot write
`/var/lib/wk`. A built system image is deliberately *not* in it: it is an
artifact the workspace that built it already names, so a second, catalogued
copy would be one fact kept twice.

**the state rules** — every mutating command keeps the smallest possible
state (a fact is recomputed from evidence at read time, never cached, except
for re-fetchable/re-derivable artifacts like ccache or a base snapshot); is
crash-only (killed at any point, a re-run converges to the declared final
state, "already exists" is never the answer to a half-made thing); wipes
rather than repairs (destroying and recreating beats patching around an
unexpected state); takes one lock per mutated resource; believes the machine
over its own record when the two disagree; and never lets a read-only command
(`wk status`, `wk ls`, `wk logs`, `wk doctor`) change anything or block on a
run it is only reporting.

## Local vs remote

Every `wk <cmd> -h` prints a `runs on:` line, and commands fall into three
groups:

- **The workspace's target.** `build`, `run`, `test`, `claude`, `bench`,
  `profile`, `status`, `ls`, `sync`, `enter`, `logs`, `gui`, `zed`. A workspace
  lives on exactly one machine, and the command goes to that machine: on a
  macOS host a `container` workspace's command is forwarded into the podman VM
  over `podman machine ssh`, and a workspace on a machine that runs `wk` for
  itself — a build box, or a peer workstation — is handed over whole, so that
  machine's own `wk` resolves the name and does the work. `wk zed` is the one
  exception, since the editor runs where you typed the command: it asks the
  machine holding the workspace for a route and opens that from here.
- **This host's own store or hardware, refused inside a workspace and on a
  build machine.** `remote`, `key`, `push`, `sudo`, `quiesce`, `session`,
  `boot`, `pi`, `sysimage`, `bridge`, `vm`, `find`, `backup`, `start`, `stop`,
  `gc`. These act on fleet devices, bridges, or this machine's own
  provisioning, so they never run against a checkout inside a sandbox, and a
  shared build box refuses them too — a build box builds, it does not own
  fleet hardware.
- **This machine, never forwarded.** `disk`, `doctor`, `version`, `selftest` —
  read-only reports about the machine you typed the command on.

`wk new` and `wk rm` run on "the workstation that keeps the workspace record"
— even for a `remote` workspace, because the record of which target a
workspace belongs to lives here, not on the machine doing the build. A peer
workstation keeps its own records, so it makes and destroys its own
workspaces: `wk new <name> --target <peer>` and `wk rm` of one of its
workspaces refuse here and name the command to run there.

A machine is named by its tailnet name and nothing else — no `.local`
address, no IP, no ssh `ProxyJump` stored anywhere in this repo. `wk ls` and
`wk status` show every target and every fleet device in one listing with a
`TARGET`/machine column, so which machine answered a command is always in the
same output as the command's result, never left to be inferred.

## Setup

Prerequisites:

- macOS: Xcode command line tools (`xcode-select --install`), podman from
  <https://podman.io> (the official installer, not Homebrew), Tailscale
  (<https://tailscale.com/download/macos>), Zed (<https://zed.dev/download>,
  only for `wk zed`), Tart (<https://tart.run>, only for building Apple
  ports; install the `.app` bundle and symlink `tart` from inside it -- the
  binary needs the bundle's entitlement, so a copied-out executable fails).
- Ubuntu 26.04: nothing beforehand — `./setup` installs the stock apt packages
  and Tailscale itself.
- Both: a GitHub fork of WebKit under your own account
  (`github.com/<you>/WebKit`) — every push writes there, never to a shared
  maintainer's URL. Membership of a tailnet you administer (the first device
  on a tailnet needs admin rights to add others). A card reader on a machine
  `wk` can reach, only if writing the first medium for an unprovisioned bench
  board.

Steps:

```sh
git clone https://github.com/justinmichaud/wk-tools ~/Development/wk-tools
cd ~/Development/wk-tools
./setup                        # idempotent; a second run in a row prints no changes
```

```sh
./setup --stage quiesce        # one sudo prompt; installs the quiesce/session helper
wk sudo setup                  # closes sudo's 5-minute timestamp and NOPASSWD
gh auth login                  # wk key register calls the GitHub API with this
wk key register                # a deploy key per fork, registered with write access
wk push on                     # exposes the keys to workspaces
wk sync                        # clones WebKit into the mirror, publishes a snapshot
eval "$(wk completion bash)"   # shell/bashrc does this for you; zsh: wk completion zsh
```

Boards that reach the bench over WiFi (`MACH_NET=wifi`) take their credential
from the WiFi connection of the machine holding the card reader, read by the
privileged card helper at write time -- there is no hand-made credential file;
`wk sysimage write` refuses when that machine is not on WiFi.

```sh
wk new bug-238
wk build bug-238 jsc-release
wk run   bug-238 -- -e '1+1'
wk test  bug-238
wk rm    bug-238
```

`wk doctor` is the checklist at any point: what is provisioned, and the exact
command for what is not.

## Workflows

**Container workspace, full cycle**

```sh
wk new bug-238                          # instant overlay, any checkout size
wk build bug-238 jsc-release            # prints the exact build line it runs
wk build bug-238 jsc-release --no-defaults   # ignore the machine's WK_BUILD_ARGS
wk run   bug-238 -- -e 'print(1+1)'
wk run   bug-238 --until-crash --max 50 -- crash.js   # repeat until it fails; keeps the log and core
wk test  bug-238
wk logs  bug-238 --follow               # the build log, noise stripped
wk enter bug-238 -- ls                  # a shell, or one command, in it -- any target
wk stop  bug-238                        # park it: environment stopped, everything kept
wk start bug-238
wk rm    bug-238 other-ws               # reclaims everything each created
```

**macOS VM workspace, for the Apple ports**

```sh
./setup --stage softnet                 # once; installs the guest's egress filter
wk new mac-rel --target vm              # builds the golden base the first time (hours, once)
wk vm start mac-rel
wk build mac-rel mac-release
wk vm stop mac-rel
```

**A shared build machine**

```sh
wk remote setup buildbox4               # probes it over ssh, writes targets/hosts/buildbox4.conf,
                                        # installs WebKit's build dependencies (one sudo prompt)
                                        # WK_BUILD_ARGS= in that conf is appended to every build there
wk new big-build --target buildbox4
wk build big-build jsc-release          # sized from that machine's live load
wk remote rm buildbox4                  # undo it; git rm the conf to forget it for good
```

**Someone else's PR, a PR by number, a rebase**

```sh
wk pr bug-238 alice:eng/some-branch      # fetched once into the mirror, then into the workspace
wk pr bug-238 1234                       # WebKit PR #1234; wpe:1234 for WPEWebKit
wk pr rebase                             # inside a workspace: fetch main, rebase onto it
wk new review-1234 --pr 1234             # a fresh workspace straight onto a PR
wk pr open bug-238                       # from the host: push the branch, open it with gh
```

**Sync (a workspace, a target, or the furniture a machine keeps)**

A workspace goes stale in its own checkout; a machine goes stale in what it
keeps *for* workspaces -- its copy of wk-tools, its WebKit mirror, and the
snapshot the next `wk new` clones. The two are asked for separately.

```sh
wk sync                                 # one workspace here; asked when there are several
wk sync bug-238                         # a named one
wk sync --target moose                  # every workspace on that target
wk sync --all                           # every workspace on every target
wk sync --tools                         # every machine's wk-tools, mirror and snapshot
wk sync --tools buildbox4               # just that machine's
```

**Profile a run**

```sh
wk profile bug-238 script.js                    # sampling profiler (default), jsc shell
wk profile bug-238 --mode samply --browser       # native sampling, MiniBrowser
wk profile bug-238 --mode bytecode --fetch       # per-bytecode tier report, copied out
```

**Benchmark in a workspace, and compare two runs**

```sh
wk quiesce on
wk session on
wk bench bug-238 speedometer3
wk bench bug-238 motionmark1.3.1 --count 5
wk bench bug-238 jetstream3 --cores 0-3             # pinned with taskset; recorded and compared
wk bench compare <run-a> <run-b>                 # warns if class/runner/host differ
```

**Put a build on a fleet device and bench it there**

```sh
wk sysimage build wpewebkit-2.38-buildroot-rpi3-32 --detach   # hours; poll with wk status
wk sysimage disks <writer>                       # removable disks on the machine holding the card reader
wk sysimage write --from <path> --disk <writer>:/dev/sdX
wk boot rpi3                                     # one-shot: arms, reboots, self-reverts
# a write refuses, with no --force, when the tailnet auth key or the board's
# WiFi credentials are missing, or when the tailnet already has a node named
# rpi3-bench -- a fresh join would come up as rpi3-bench-1 and nothing could
# reach it.
# Remove the stale node in the admin console first.
wk pi deploy bug-238 rpi3 --skeleton              # once per board
wk pi bench rpi3 speedometer3
wk pi bench rpi3 speedometer3 --ab A,B --rounds 5 # interleaved A/B between two deployed slots
```

Or stage a workspace build straight onto bench media without a full image
rebuild:

```sh
wk bench stage bug-238 --to rpi3
wk bench staged --plan speedometer3
wk bench staged --ls                             # what is staged, and what ran
```

**The Mac lane**

```sh
wk boot mbp --status                             # which side the firmware default is on
wk bench mac-ab mac-rel                          # stages, plants a launch agent, reboots, reads back
                                                  # needs one action at the keyboard per experiment
```

**Add a new fleet device**

Write `boot/machines/<name>.conf`:

```sh
MACH_SSH=<name>               # tailnet name, once provisioned
MACH_DRIVER=<driver>          # boot/<driver>.sh -- which mechanism arms it
MACH_DEVICE=<device>          # the block device an image is written to
MACH_ROOT=<device>            # its host mode's root device -- never written to
MACH_PROFILE=<profile>        # its default system profile ('wk sysimage -h')
MACH_MAC=<aa:bb:cc:dd:ee:ff>  # its NIC's address, for 'wk find' pre-tailnet
MACH_BRIDGE=<bridge-name>     # only if it sits behind a tailnet bridge
MACH_ROLE=workstation|bench-device
MACH_OS=any
MACH_NET=wifi|ethernet        # how the board reaches the network at the bench
MACH_DTB=<file.dtb>           # the device tree a Pi image boots with; empty otherwise
MACH_BENCH_SSH=<name>-bench   # what a system wk writes for it joins the tailnet as;
                              # the machine's own install keeps MACH_SSH
MACH_NOTE="one line, for the listing"
```

```sh
git add boot/machines/<name>.conf && git commit
wk boot --list                                   # picks it up
```

**Provision a bridge phone**

The phone runs postmarketOS (a `pine64-pinephone` or `purism-librem5` image
that `wk bridge provision` builds); the one step no command does is holding the
phone's buttons to boot the Jumpdrive card when prompted.

```sh
wk bridge provision tailnet-bridge-generic       # image, card, phone, role, tailnet policy, health check
# prompts at the two steps that need a hand: the physical card, and pasting
# the tailnet policy it prints into the admin console
wk bridge setup tailnet-bridge-generic           # idempotent re-apply, after any change
wk bridge status tailnet-bridge-generic
```

**Quiesce and session, before any measurement**

```sh
wk quiesce on            # pause background daemons, disable screensaver/App Nap
wk session on             # a real compositor on the attached monitor
wk quiesce status
wk session status
wk quiesce off
wk session off
```

**`wk claude` in a workspace**

```sh
wk claude bug-238                # verifies the sandbox first, refuses to start if it fails
wk claude bug-238 -r             # resume
wk claude bug-238 --continue
wk claude bug-238 --rc                   # a Remote Control server the Claude app attaches to; --rc --stop ends it
```

A `remote` target has no sandbox to verify; `wk claude` there stops at a
barrier that only an explicit `--force` crosses.

**Housekeeping**

```sh
wk doctor                # what is provisioned, what is missing, and the command to fix it
wk status                # every target and fleet device, without guessing
wk disk                  # where the disk went, with the total
wk gc                    # reclaim disk by reference count; never loses work
wk gc --purge-mirror     # also erase the git mirror and every base snapshot (refused with a live workspace)
```

**Reprovision a machine from scratch**

```sh
git clone https://github.com/justinmichaud/wk-tools ~/Development/wk-tools
cd ~/Development/wk-tools && ./setup
wk sync
```

Every machine this repo knows is itself in the repo (`targets/hosts/*.conf`,
`boot/machines/*.conf`), so a fresh clone already knows the whole fleet.
Nothing else is machine-specific except what `wk backup` captures, and keys
and secrets, which never live in git. Last resort, discarding a macOS host's
whole container store:

```sh
podman machine rm wk && ./setup && wk sync
```

## Where the rest is

`wk help` prints this file; `wk <command> -h` prints what any single command
does, what it acts on, and whether it changes anything. CLAUDE.md is for
anyone editing this repository itself.
