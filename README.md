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
on its own medium. A rescue is also the board's own card writer: it carries
`admin/wk-card-priv` (the yocto `meta-wk-rescue` layer), so
`wk sysimage write --from <image> --disk <board>:<device>` and `wk boot`
put a bench system on the board's *other* medium from the rescue itself, and
an A/B never needs a card carried to a reader. The two systems are two
tailnet nodes with two names -- the rescue `<board>-rescue` (`MACH_SSH`), the
bench system `<board>-bench` (`MACH_BENCH_SSH`) -- since each written card
joins as its own node and a second join under one name comes up renamed. A rescue written from an image
that predates that layer (the rpi3's) cannot; it is
rewritten once, from a reader, and never again. The only card a person
handles is a board's first rescue.

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
wk boot rpi4                                     # one-shot: arms, reboots, self-reverts
# a write refuses, with no --force, when the tailnet auth key or the board's
# WiFi credentials are missing, or when the tailnet already has a node named
# rpi3-bench -- a fresh join would come up as rpi3-bench-1 and nothing could
# reach it.
# Remove the stale node in the admin console first -- or, when the node is the
# board's own rescue being replaced from itself, --force and remove it before
# the reboot.
# A board with one medium (the rpi3) keeps both systems on it: the rescue on
# partitions 1-2, the bench system on 3-4 (`@second`). `wk boot rpi3` selects
# the second for one boot with an os_prefix line in the rescue's config.txt,
# which the bench system's self-disarm removes as it comes up. Both writes
# come from a reader the first time, and from the rescue itself after that
# (--disk rpi3:/dev/mmcblk0@second). A rewrite keeps the bench system's
# tailnet node: it comes back as rpi3-bench with no join and no stale node.
wk sysimage write --from <rescue .wic.xz> --disk <writer>:/dev/mmcblk0 --rescue --profile webkit-2.52-yocto-rpi3-32
wk sysimage write --from <sdcard.img> --disk <writer>:/dev/mmcblk0@second --profile wpewebkit-2.38-buildroot-rpi3-32
wk boot rpi3
wk sysimage webkit wpewebkit-2.38-buildroot-rpi3-32 --commit <sha> --slot base --detach
                                                 # one WebKit built against the image's own toolchain,
                                                 # into a named slot beside the image (wk sysimage ls)
wk pi deploy wpewebkit-2.38-buildroot-rpi3-32 rpi3 --slot base   # onto the booted board, verified byte for byte
wk pi bench rpi3 speedometer3 --slot base
wk pi bench rpi3 speedometer3 --ab base,pr1725 --rounds 5   # interleaved A/B between two deployed slots
```

The image is the runtime and is built once; a *slot* is one WebKit
(`/var/wk/slots/<name>/` on the board), and a board carries as many as an A/B
needs, alternated with no reflash and no reboot between them. A slot is built
by buildroot's own developer workflow -- `WPEWEBKIT_OVERRIDE_SRCDIR` in
`local.mk`, `make wpewebkit-rebuild`, the files buildroot recorded installing
-- so it is configured exactly as the image's WebKit was, and the next image
build drops the override and rebuilds the package from the pinned tarball.
A yocto image's slot is WebKit's own cross build -- `build-webkit --wpe
--cross-target=<target>` through `cross-toolchain-helper`, the workspace's
`webkit` stage -- of the named commit, packed the same way.
Every binary the image build links carries a build-id (`BR2_TARGET_LDFLAGS`);
that, and the sha256 of the library the reporting process mapped, is what
`wk pi bench` checks. `run-benchmark`
runs on the workstation, out of a `Tools/Scripts` tree exported from the
mirror, and drives the board's browser over ssh through a reverse tunnel; the
process that reports each number is checked against the slot's build-id, so a
result can never be from the wrong WebKit. A yocto workspace's cross build is
deployed the same way: `wk pi deploy yocto-<profile> rpi4 --slot b`.

**An A/B of a pull request, end to end**

```sh
wk ab wpe:1725 --devices rpi3,rpi4 --bits 32 --dry-run   # the parameters and every command, nothing run
wk ab wpe:1725 --devices rpi3-32,rpi4-32,rpi5-64         # confirm, then: both slots built per image, deployed,
                                                         # every board alternated at once; a device's width is its own
wk ab wpe:1725 --devices rpi4 --bits 32 --plan jetstream3 --rounds 8 --yes   # unattended
wk ab <sha> --base <sha> --release 2.38 --devices rpi3   # A/A: two slots of one commit -- the lane's noise floor
```

The base is guessed as the merge-base of the PR head and the image's own
branch (`CFG_BRANCH`), the release from the PR's base branch; `--base` and
`--release` override either. Each `--ab` ends with `wk bench report`, as text
and as one self-contained html file (histograms, per-subtest Welch/FDR) in the
result store. A board that is not booted into the image is named with the
`wk sysimage write` / `wk boot` steps that put it there; `wk ab` never writes a card.
Every build `wk` starts -- `wk build`, an image, a slot -- is on the machine's
books while it runs (a budget record that dies with it), and the next build is
sized against the memory and cores left, refused when fewer than four jobs
would fit; inside the target each runs under the same guard (`build/guard.sh`:
cgroup clamp, memory watchdog, nice). Two machine-sized builds at once is what
hands a host to the OOM killer, so `wk ab` runs its builds in order.
Benchmarks on different boards run at once.

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

## Lifecycle

Every step from a bare Raspberry Pi to an automated A/B, in order, and what
each one needs. Two arrangements exist (`wk help hardware`): one medium
holding both systems (the rpi3: rescue on SD partitions 1-2, bench system on
3-4, `@second`), and two media (the rpi4: rescue on the SD, bench system on
the USB stick, one MBR byte arming it). The commands are the same; only the
`--disk` spelling and the driver differ.

**1. Declare the board** -- `boot/machines/<name>.conf` ("Add a new fleet
device" above): its driver (`pi-sd` for one medium, `pi-mbr` for two), the
bench medium (`MACH_DEVICE`), the rescue's root partition (`MACH_ROOT`), its
two tailnet names, `MACH_NET`, `MACH_DTB`. Commit it.

**2. Build the two images** -- the rescue (a yocto profile, `webkit-2.52-yocto-<board>`)
and the bench system (the profile under test, e.g. `wpewebkit-2.38-buildroot-<board>-32`),
each in its own workspace, hours each, one at a time:

```sh
wk sysimage build webkit-2.52-yocto-rpi3-32 --detach
wk sysimage build wpewebkit-2.38-buildroot-rpi3-32 --detach
wk sysimage ls                                    # both, with the paths --from takes
```

**3. Write the first card from a reader** -- the only card a person handles.
The machine holding the reader (rpi5 here) needs the card helper installed
(`./setup --stage quiesce` there, one sudo prompt) and must be on the WiFi
the board will use, since the card takes that credential. The tailnet must
not already have a node of the name the card will join under (a stale
`<board>-rescue` from an earlier card is removed in the admin console; the
write refuses the collision, with no `--force`).

```sh
wk sysimage disks rpi5                            # which /dev the card is
# one medium: the rescue, leaving the rest of the card, then the bench system beside it
wk sysimage write --from <rescue.wic.xz> --disk rpi5:/dev/mmcblk0 --rescue --profile webkit-2.52-yocto-rpi3-32
wk sysimage write --from <sdcard.img>    --disk rpi5:/dev/mmcblk0@second --profile wpewebkit-2.38-buildroot-rpi3-32
# two media: the rescue onto the SD; the stick is written later, from the rescue
wk sysimage write --from <rescue.wic.xz> --disk rpi5:/dev/mmcblk0 --rescue --profile webkit-2.52-yocto-rpi4-64
```

**4. Boot the rescue** -- carry the card to the board, pull any older medium
that would boot first, power on. `<board>-rescue` joins the tailnet by itself;
`wk boot <board> --status` reports it as the base image.

```sh
wk pi boot-order rpi4                             # two media only: the bench medium first, the rescue behind it
wk sysimage write --from <sdcard.img> --disk rpi4:/dev/sda --profile wpewebkit-2.38-buildroot-rpi4-32
                                                  # two media: the bench system onto the stick, from the rescue
```

From here on no card is carried: every later write of the bench system is
made from the rescue (`--disk rpi3:/dev/mmcblk0@second`, `--disk rpi4:/dev/sda`),
and a rewrite of the `@second` system keeps its tailnet node.

**5. Boot the bench system once** -- `wk boot <board>` arms it for one boot and
reboots; `<board>-bench` joins the tailnet; the system disarms itself as it
comes up and hands the board back to the rescue after `IMG_WATCHDOG` seconds
unless claimed (`wk boot <board> --keep`). A first boot that never appears is
read from the rescue once it returns (see "Build interventions").

**6. The A/B** -- one command; it builds both WebKits against the image,
deploys them as slots, alternates them and writes the report:

```sh
wk ab wpe:1725 --devices rpi3 --bits 32 --dry-run   # every step it will take, nothing run
wk ab wpe:1725 --devices rpi3 --bits 32 --yes
```

**Manual A/B, and a custom buildroot configuration**

`wk ab` is these commands in order; run them by hand to vary any one of them
-- a different plan, a hand-picked pair of commits, or an image built from
your own buildroot configuration:

```sh
wk sysimage webkit <profile> --commit <base-sha> --slot base --detach      # one WebKit build at a time
wk sysimage webkit <profile> --commit <patched-sha> --slot pr --detach
wk sysimage ls                                                             # both slots listed under the image
wk boot rpi3 && wk boot rpi3 --keep                                        # bench mode, claimed
wk pi deploy <profile> rpi3 --slot base                                    # verified byte for byte on the board
wk pi deploy <profile> rpi3 --slot pr
wk pi bench rpi3 speedometer3 --ab base,pr --rounds 5                      # interleaved; ends with wk bench report
wk bench report <run-a> <run-b> --html                                     # any two recorded runs
```

A buildroot configuration of your own is an external defconfig:
`image/buildroot/external/configs/<name>_defconfig`, which buildroot resolves
before the fork's own of the same name (that directory's README lists what
each one changes and why). A profile names it in `image/configs/<profile>.conf`
(`BR_DEFCONFIG`, with `BR_EXTERNAL=1`); kernel settings go in
`image/buildroot/external/board/*.fragment`, named from the defconfig by
`BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES`. A fleet bench image needs, beyond
what the fork's cog defconfigs carry: `wpa_supplicant` (a board with no cable),
OpenSSH (dropbear 2019.78 refuses ed25519, the driving key), and a kernel with
TUN and netfilter (or tailscaled cannot create `tailscale0`). The two 2.38
defconfigs there carry all three; copy them when deriving a new one.

**Build interventions**

`wk sysimage build <profile>` is re-runnable: with the workspace's tree
intact it rebuilds what changed and repacks the image in minutes. Two things
cost more than they look:

- **A rebuild after slot builds rebuilds WPE from the tarball** -- hours.
  `wk sysimage webkit` leaves buildroot's `local.mk` pointing wpewebkit at the
  slot's source, and the image stage drops it and `wpewebkit-dirclean`s, so the
  image never carries a slot's WebKit. Change defconfigs before the slots, or
  budget the hours.
- **Deselecting a package does not remove its files.** Buildroot's incremental
  build leaves a deselected package's files in `output/target` and in the
  staging sysroot -- switching dropbear to OpenSSH left `S50dropbear` beside
  `S50sshd`, and dropbear would have taken port 22 first; a library the next
  package wants to install over an old copy fails the build outright. A
  from-scratch build (`wk rm` the workspace, build again) is clean; to avoid
  the hours, remove exactly the files buildroot
  recorded installing for that package (`output/build/<pkg>-<version>/
  .files-list.txt` for the target, `.files-list-staging.txt` for staging,
  one `pkg,./path` per line), then re-run the image stage:

  ```sh
  wk enter buildroot-<profile> -- sh -c 'cd /src/WebKit/WebKitBuild/buildroot/<profile>/output &&
      cut -d, -f2 build/<pkg>-*/.files-list.txt         | while read -r f; do rm -f "target/$f";  done &&
      cut -d, -f2 build/<pkg>-*/.files-list-staging.txt | while read -r f; do rm -f "staging/$f"; done'
  wk sysimage build <profile> --detach
  ```

  A package that was *built against* the removed one keeps linking its
  libraries, and the next package to link them fails with undefined
  references. Find them with the toolchain's readelf over `output/staging`
  and `output/target` (`NEEDED` entries naming the gone libraries), and
  rebuild exactly those: `make <pkg>-dirclean` in the buildroot tree inside
  the workspace, then the image stage again.

Reading a bench system that never appeared: once the self-return has handed
the board back, the rescue can mount the bench partition read-only and its
logs are there -- `/var/log`, and tailscaled's own under
`/var/lib/tailscale/tailscaled.log*.txt` (a `1970` clock in them is normal
before ntp).

```sh
wk boot rpi3 --diag                                       # a yocto bench system's own boot account
ssh root@rpi3-rescue 'mount -o ro /dev/mmcblk0p4 /mnt && ls /mnt/var/log; umount /mnt'
```

## Where the rest is

`wk help` prints this file, `wk help <topic>` one section of it (`wk help
lifecycle`, `wk help hardware`); `wk <command> -h` prints what any single
command does, what it acts on, and whether it changes anything. CLAUDE.md is
for anyone editing this repository itself.
