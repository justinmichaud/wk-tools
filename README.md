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
Provisioned with `wk bridge`. `wk bridge setup` also caps how far it charges
(`charge_control_end_threshold`, 80% by default, `BR_BATTERY_LIMIT` in the
host conf to change it) so a phone left on a charger for months does not
swell its cell; `wk doctor --all` reads the cap back on both phones, and
prints the honest "no OS limit exists" for this machine's own battery, since
macOS has no CLI knob for its optimized charging.

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
claude setup-token             # then paste it into the next line
wk key set claude              # every workspace this machine makes starts authenticated
wk key set litellm             # the API key `wk ai pi` reaches your endpoint with
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

**Moving a file in or out**

```sh
wk scp bug-238 :Tools/foo.js ./foo.js       # out -- `:<path>` is in the checkout
wk scp bug-238 ./patch.diff :patch.diff     # in
wk scp bug-238 -r :WebKitBuild/logs ~/logs  # a directory, into ~/logs/logs
wk scp :build.log ~/build.log               # inside a workspace, like every other command
```

Exactly one side carries the `:`; the other is a path on the machine you type
it on, which is where `wk scp` runs, whatever holds the workspace -- the podman
VM mounts nothing of this machine's, so a forwarded copy would land in the VM
instead. The bytes go through the target's own transport (`podman cp` for a
container, scp and rsync for a guest or a build machine), and a destination
that is an existing directory receives the copy under the source's own name, as
`cp` and `scp` do. A directory onto a file, a file onto a directory, and a
directory without `-r` are refused rather than guessed at.

**macOS VM workspace, for the Apple ports**

```sh
./setup --stage softnet                 # once; installs the guest's egress filter
wk new mac-rel --target vm              # builds the golden base the first time (hours, once)
wk vm start mac-rel
wk build mac-rel mac-release
wk build mac-rel jsc-debug              # JavaScriptCore alone, still Xcode
wk vm stop mac-rel
```

Xcode is the only build system in a guest -- build-webkit's CMake path wants a
generator that supports Swift -- so there is no JSCOnly port there and the
`jsc-*` configs mean the Apple port's JavaScriptCore instead, built with
`Tools/Scripts/build-jsc` into the same `WebKitBuild/<Configuration>` tree as
the `mac-*` config of the same configuration. The reverse is refused: a
`mac-*` config on a Linux target says so rather than running xcodebuild.

Every guest is an APFS clone of one golden base, so **what the base carries is
what every guest carries**: Xcode, a checkout, a warm build tree, the desktop
settled onto an empty screen, and the account's password changed from the
image's. The base is made by scripts in this tree, and editing one of them does
not change a base already built -- so the base records the hash of the inputs
that produced it, and every read recomputes that hash and compares. `wk vm ls`
prints the verdict under `BASE`, `wk new --target vm` warns before cloning a
base that predates its inputs, and `wk doctor` says the same on the way past;
each of them names `wk vm base --rebuild`, which is hours and is yours to run.
Until it is run, a clone carries what that base was built with -- the image's
password if the change came later, and the settings of the day it was sealed.

`wk vm start` settles a guest's desktop on every boot (a clone gets a new
hardware UUID, so anything `defaults -currentHost` holds does not survive
cloning) and then **prints what it actually found**: who is logged in at the
window, the screen lock, the screen saver, display sleep, Setup Assistant's
panes, the Software Update offer, and anything modal on screen right now.
`wk vm check <name>` asks again on demand, read-only, and adds what is resident
in the guest -- the shells, the editor remote server that outlives its window,
the agents, the memory left -- because a guest holds its whole allocation
whether or not it is busy, and a long-lived one runs out of memory because
something stayed. An occluded window is a throttled window: a benchmark in
there measures something else rather than failing, so this is a report worth
reading before a measurement.

**A shared build machine**

```sh
wk remote setup buildbox4               # probes it over ssh, writes targets/hosts/buildbox4.conf,
                                        # installs WebKit's build dependencies (one sudo prompt)
                                        # WK_BUILD_ARGS= in that conf is appended to every build there
wk new big-build --target buildbox4
wk build big-build jsc-release          # sized from that machine's live load
wk doctor --all                         # what every build box has, and whether what
                                        # provisioned it is still what this tree says
wk remote rm buildbox4                  # undo it; git rm the conf to forget it for good
```

A build box is provisioned once and used for months, so it records the same
thing the golden base does: `wk remote setup` writes the hash of what
provisioned it into `~/.wk-remote` there, and `wk doctor --all` recomputes that
hash and reports the machine as provisioned from this tree or as predating it,
with `wk remote setup <target>` as the remedy. Read-only, over ssh, and it
changes nothing on the machine.

**Someone else's PR, a PR by number, a rebase**

```sh
wk pr bug-238 alice:eng/some-branch      # fetched once into the mirror, then into the workspace
wk pr bug-238 1234                       # WebKit PR #1234; wpe:1234 for WPEWebKit
wk pr rebase                             # inside a workspace: fetch main, rebase onto it
wk new review-1234 --pr 1234             # a fresh workspace straight onto a PR
wk pr open bug-238                       # from the host: push the branch, open it with gh
```

**What a fresh workspace's checkout is**

The base snapshot is published on the branch it was taken from (`origin/main`,
or `WK_BRANCH`), so `wk new` leaves the checkout **on branch `main`, tracking
`origin/main`** -- `git status` says "On branch main", and `git pull`,
`git rebase @{u}` and `wk pr rebase` all have an upstream to name. Creation
also **fetches**: once, from the machine's own WebKit mirror, then a
fast-forward onto it -- a local read of a handful of refs, never the network,
and never a refresh of the mirror itself (that is `wk sync --tools`, minutes).
A workspace with no mirror in reach -- a macOS guest -- is told to `wk sync`
it instead.

Four remotes are wired into every checkout, always the same four, by the one
authority every target wires from (`wk remotes <ws>` checks them, `--fix`
re-asserts them):

- `origin` -- WebKit/WebKit, the upstream everything rebases onto. Fetch only:
  its push URL is `no-push://`, because nobody here has write access to it.
- `wpe` -- WebPlatformForEmbedded/WPEWebKit, whose release branches the board
  images are built from. Fetch only, for the same reason.
- `fork` -- your own fork of WebKit: what a branch is pushed to and what
  `wk pr open` opens a PR from. Pushes over ssh with a per-fork deploy key
  (`wk push on`).
- `forkwpe` -- your own fork of WPEWebKit, the same, for that project.

**Sync (a workspace, a target, or the furniture a machine keeps)**

A workspace goes stale in its own checkout; a machine goes stale in what it
keeps *for* workspaces -- its copy of wk-tools, its WebKit mirror, and the
snapshot the next `wk new` clones. The two are asked for separately. Inside a
workspace a bare `wk sync` fetches in it, from that machine's mirror; every
other scope is a machine's furniture and is run from the host.

A workspace's fetch takes everything from that mirror in one local read when
the mirror is mounted in it (a container's `/mirror`), and asks the upstreams
themselves only when it is not. Either way it fetches the refs the mirror
carries -- `main` of `origin`, every branch of the other three -- and no tags:
following tags re-negotiates tens of thousands of refs nothing here builds
from, and `git fetch --tags` in the workspace asks for them when they are
wanted.

The tooling reaches a machine across ssh -- a build box, a macOS guest -- as
git and never as a file copy: this tree's HEAD goes over as a git bundle, and
the copy there is a real checkout reset hard to that commit. So a machine holds
a commit that exists, `wk status` compares the two by sha, and an **uncommitted
tree here is refused** with nothing sent -- commit it and re-run. It converges
from anything already over there (no checkout, an older file copy, another
commit, a dirty tree), a killed push included: the first push over a directory
that is not already a checkout replaces it whole, ignored files included, since
loose files cannot be reconciled with a commit; every push after that is into a
checkout, and leaves that machine's ignored files -- its build directory, its
own conf -- alone. A guest takes the same checkout on every `wk vm start`, with
its marker and its egress; there a dirty tree warns rather than failing the
start. Two copies are not this: the podman VM's `/opt/wk-tools`, which this
machine copies in, and a peer workstation's own checkout, which pulls rather
than being written over.

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
wk bench ls                                      # every task, with each run's directory
wk bench compare <run-a> <run-b>                 # two run directories; warns if class/runner/host differ
```

Every benchmarking command is one *task*, named for the moment it was
requested and what it measures, under `$WK_STORE/bench/<task>/`: `task.json`
(the request and every command it ran), `runs/<run>/` (one directory per run:
`env.json`, `result.json`, `run.log`, the board's `browser.log` and
`board.log`), the command's logs and its reports. `wk bench ls` lists tasks
and their runs' directories; nothing about a task's state is stored -- planned,
ended, usable and complete are recomputed from the runs, and "running" is the
task's lock.

**Put a build on a fleet device and bench it there**

Four steps, and the third one is a hand: a card is written in a reader on one
machine and booted in a board on another, so nothing in this sequence follows
from the line above it automatically.

```sh
# 1. build the system. Hours; --detach returns at once, poll with wk status.
wk sysimage build wpewebkit-2.38-buildroot-rpi3-32 --detach

# 2. write it onto a card. <writer> is the machine holding the card reader --
#    usually not the board -- and only a removable disk plugged into it is ever
#    writable, never that machine's own system disk.
wk sysimage disks <writer>
wk sysimage write --from <path> --disk <writer>:/dev/sdX

# 3. move the card from the reader to the board. Nothing above does this and
#    nothing below checks it.

# 4. arm the board for one boot into that system. It reboots itself, comes up
#    as the bench system, and that system removes the selection as it starts --
#    so the boot after it is the rescue again, whatever happened in between.
#    Writing a card does not make anything boot it; this is what does.
wk boot rpi3
```

```sh
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
wk pi bench rpi3 speedometer3 --ab base,pr1725 --rounds 5   # interleaved A/B between two deployed slots:
                                                 # its own task, reported at the end
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
result can never be from the wrong WebKit. Every launch starts from a cold
browser cache on the board (a cached load of the benchmark's URL never starts
the benchmark on the rpi3 image; run-benchmark's own drivers give every launch
a fresh profile for the same reason). Every run's evidence lands on the
workstation, in the task's directory -- the browser's own log is moved off
the board after each run, the board's system log tail copied beside it -- so
the board keeps nothing. A yocto workspace's cross build is
deployed the same way: `wk pi deploy yocto-<profile> rpi4 --slot b`.

**An A/B of a pull request, end to end**

```sh
wk ab wpe:1725 --devices rpi3,rpi4 --bits 32 --dry-run   # the parameters and every command, nothing run
wk ab wpe:1725 --devices rpi3-32,rpi4-32,rpi5-64         # confirm, then: both slots built per image, deployed,
                                                         # every board alternated at once; a device's width is its own
wk ab wpe:1725 --devices rpi4 --bits 32 --plan jetstream3 --rounds 8 --yes   # unattended
wk ab wpe:1725 --devices rpi3,rpi4 --bits 32 --plan speedometer2.1 --count 1 --timeout 1200 --yes --detach
                                                         # confirmed here, run by a process this end cannot kill
wk ab <sha> --base <sha> --release 2.38 --devices rpi3   # A/A: two slots of one commit -- the lane's noise floor
wk status                                                # the running task: which run it is on, runs ended
wk bench ls                                              # every task, its state, each run's directory
wk bench report 20260830T140000Z-wpe-pr1725 --html      # the rounds so far, paired; says whether it is complete
```

**An A/B of two system images** -- two releases, two library stacks, both
resident on the bench medium, a boot per leg:

```sh
# both systems onto the one card (rpi3: @second and @third; from the rescue)
wk sysimage write --from <2.38 sdcard.img> --disk rpi3:/dev/mmcblk0@second --profile wpewebkit-2.38-buildroot-rpi3-32
wk sysimage write --from <2.52 wic.xz>     --disk rpi3:/dev/mmcblk0@third  --profile webkit-2.52-yocto-rpi3-32
# a slot of the same name deployed into each (boot one, deploy, boot the other, deploy)
wk boot rpi3 --system <2.38 id>   # then: wk pi deploy wpewebkit-2.38-buildroot-rpi3-32 rpi3 --slot base
wk boot rpi3 --system <2.52 id>   # then: wk pi deploy webkit-2.52-yocto-rpi3-32 rpi3 --slot base
# the A/B: A B A B, one boot per leg; every leg is verified from the running
# system's own marker -- the rescue or the wrong leg is refused, not recorded
wk pi bench rpi3 speedometer3 --ab-systems <2.38 id>,<2.52 id> --slot base --rounds 5
wk bench report <task> --html                            # paired like any A/B; each run records system= and its proof
```

The base is guessed as the merge-base of the PR head and the image's own
branch (`CFG_BRANCH`), the release from the PR's base branch; `--base` and
`--release` override either. Every invocation is one task
(`<stamp>-wpe-pr1725`, or `<stamp>-<sha12>` for a commit): its `task.json`
records the request and every command, each board's pipeline logs to
`<board>.log` beside it, and `wk bench report <task>` pairs the rounds each
run recorded (a round counts only when both arms produced a result: a crash
correlates with the heavier binary under a memory ceiling, so the surviving
arm is the one that would bias the answer), reports what is there while the
task runs, and writes one `report-<board>-<plan>.html` (histograms,
per-subtest Welch/FDR) per board and plan into the task when asked. A board
that is not booted into the image is named with the `wk sysimage write` /
`wk boot` steps that put it there; `wk ab` never writes a card.
`--timeout` is the seconds one run-benchmark iteration may take; a plan's own
figure (Speedometer: 600) is too short for an rpi3.
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

**`wk ai <agent>` in a workspace**

```sh
wk ai claude bug-238                # verifies the sandbox first, refuses to start if it fails
wk ai claude bug-238 -r             # resume
wk ai claude bug-238 --continue
wk ai claude bug-238 --rc                   # a Remote Control server the Claude app attaches to; --rc --stop ends it
wk ai pi bug-238                    # the pi coding agent, installed from npm on first use
```

A `remote` target has no sandbox to verify; `wk ai claude` there stops at a
barrier that only an explicit `--force` crosses.

Two agents, one command: the workspace, the sandbox check, the push switch and
the commit wall below are the same for both, and only `--rc` is Claude Code's
alone. `wk ai pi` installs `@earendil-works/pi-coding-agent` into the
workspace's own `~/.local` on first use (`npm install -g --ignore-scripts`,
which needs node >= 22.19.0 in there and refuses with the remedy without it),
and reaches a model through an OpenAI-compatible endpoint: `wk key set litellm`
stores the API key for every workspace this machine makes, and pi's own
`~/.pi/agent/models.json` names the endpoint URL and the models it serves --
`wk ai pi` prints the file to write when a workspace has none.

A workspace starts already authenticated, so nothing has to answer `/login` in
it -- which a macOS guest reached through an editor's remote server cannot do
anyway, having no unlocked login Keychain. One token per machine, stored by
`wk key set claude` (from `claude setup-token`) and read by `shell/bashrc` into
`CLAUDE_CODE_OAUTH_TOKEN`. Each target hands it over differently: a container
symlinks the read-only `/secrets` mount, so rotating the token reaches every
container at once; a macOS guest and a build box are given a copy when the
workspace comes up, and lose it the same way when `wk key set claude --replace`
withdraws one. Without a token a workspace simply asks for `/login` as before.

The deploy keys reach a workspace the same two ways, and `wk push` is the one
switch over both. A container links the read-only `/secrets` mount, so moving
the keys in the store is the whole of it. A macOS guest mounts nothing of ours,
so the host writes it a copy on every `wk vm start` -- the private key on
stdin, never an argument, at 0600 -- alongside the same `github-webkit` /
`github-wpe` alias blocks a container gets, whose `ProxyCommand` is how ssh
reaches `github.com:22` past Softnet at all. `wk push on|off` then converges
every running guest on this host after moving the keys, and `wk push status`
reports what each one actually holds; a guest that is stopped is converged when
it next starts, before anything in it can run. On a macOS workstation `on` and
`off` start the podman machine and say why -- the key bytes exist only in its
store -- while `status` reads without starting anything.

Nothing an agent runs can publish or commit, on any target. Publishing: the
deploy keys are held back for the session (`wk push off`, before the sandbox is
verified), the egress proxy refuses GitHub's API (`api.github.com`, so `gh` has
nothing to talk to), and `wk verify` fails on a deploy key in the mount or a
GitHub credential inside a workspace; on a build box a `gh` login in the agent's
account is a refusal. Committing: a container `wk ai claude` session runs the agent
under bwrap with the checkout's `.git` commit-parts (`objects`, `refs`, `logs`,
`HEAD`, `packed-refs`) read-only, so a commit, a stage, a stash, a branch move
or a rebase fails while a build, an edit, `git status`/`diff`/`log` all work --
the read-only binds cannot be unmounted, shadowed or escaped from inside, and
`wk verify` proves the recipe blocks a commit in that very container. Both
measures are the one switch: `wk push on` turns them off (refused while a claude
session runs -- `wk push on --force` overrides, though a running agent keeps its
wall until it exits; a human `wk enter` shell is never walled). Only the person
at the keyboard pushes or commits.

Nothing an agent runs drives a build directly either. `container/bin/wk-build-wall`
lists the tools it wraps and sits on PATH under each of their names, ahead of the
real ones, in every shell that sources `shell/path.sh` -- which is every workspace
shell *and* this workstation's own, since `shell/bashrc` reads the same file. It
refuses, naming `wk build` / `wk test` / `wk bench` / `wk run`, whenever the caller
is an agent (`CLAUDECODE=1` from Claude Code, or `WK_AGENT` from `wk ai <agent>`);
it execs the real tool for anyone else, and for wk's own build (`WK_BUILD=1`),
which is where the job count and the nice level a shared build machine needs come
from. It covers a **bare name** and only that: `Tools/Scripts/build-webkit` names
a file and never consults PATH, so the path form is covered by the
`Bash(*/<name> *)` deny rules in `claude/settings.json` and
`claude/settings-host.json` and by nothing else.

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

## Hardware

How each fleet device selects the system it boots, derived from
`boot/machines/<name>.conf` and the driver it names (`MACH_DRIVER`, one
`boot/<driver>.sh` each). Every board follows the same rules: the rescue is
written first, with `--rescue`, onto the medium the board falls back to; the
bench system is written second onto the board's *other* medium (or, with one
medium, the same card's partitions 3-4); `wk boot <board>` arms it for exactly
one boot, and the bench system disarms itself as it comes up, so a power cycle
lands on the rescue. The boards differ in one thing: how that one boot is
selected, and so in what to do when it does not come back.

**rpi3 -- `pi-sd`, one SD card.** Rescue on partitions 1-2 of `/dev/mmcblk0`
(root `p2`), bench system on 3-4 (`--disk rpi3:/dev/mmcblk0@second`). A Pi 3
has no EEPROM and no boot order to set: the firmware boots the card's first
FAT partition and only that one (the board's one-time USB-boot OTP bit is set
and cannot be cleared; it changes nothing, since the card is tried first). So
`wk boot rpi3` copies the selected bench system's kernel, device trees and
cmdline into a prefix directory on the rescue's boot partition (`second/` or
`third/`) and selects it with one `os_prefix=` line in `config.txt`, keeping
the rescue's own as `config.txt.rescue`. The bench system's first act is to
move that file back; `wk boot rpi3 --disarm` from the rescue does the same.
If the bench kernel panics before it can, every power cycle boots the same
`os_prefix` again: pull the card, and in a reader rename `config.txt.rescue`
back to `config.txt` on partition 1 (a revert that needs no hands is owed,
docs/HANDOFF-boot.md).

The armed `config.txt` is the bench system's own with the `os_prefix` line
written **first**, ahead of everything the image carries. The firmware
resolves a filename when it reads the directive asking for one, so a prefix
declared after a `dtoverlay=` line never reaches that overlay's `.dtbo`, and
one appended at the end lands inside whichever conditional section the image
closes with, where a filter can drop it outright. Both boot the armed kernel
and lose that system's display stack -- the same rule, and the same fix, in
`boot/pi-tryboot.sh`'s staging.

The privileged half of editing this card is the card helper, and **every**
system a write makes carries a copy: the rescue writes bench media with it, and
a bench system arms its sibling with it -- which is what lets an A/B leg switch
here cost one boot instead of two, since the arming is an edit to the card
rather than a firmware flag. An image bakes a copy in at build time, which
would make every fix to the helper cost an image rebuild and a card carried to
a reader; `wk pi helper <board>` installs this checkout's onto a running system
instead, root-owned, and proves it answers, while `wk sysimage write` puts the
same pair on every system it writes. A system written before that was true has
no helper: its arming says so, and the leg switch answers by going back to the
rescue and arming from there.

A card shared with a rescue carries *two* bench systems: an extended
partition 3 holds logical pairs 5-6 (`@second`) and 7-8 (`@third`), each a
256 MiB boot partition and half the card's remaining space, a geometry the
card helper derives from the disk size alone so every write converges on the
same table (admin/wk-card-priv). Two releases with two library stacks sit
side by side -- `wk sysimage write ...@second` and `...@third` -- and
`wk boot rpi3 --system <id>` arms the one it names; with two present and no
`--system`, the arming refuses to guess. A card still on the one-system
layout (primaries 3-4) keeps working; the first `@second` write onto a
rescue-bearing card lays the extended layout down and says what it replaced.

**rpi4 -- `pi-tryboot`, two media, one boot authority.** Rescue on the SD card
(`/dev/mmcblk0`, root `p2`), bench system's root on the USB stick (`/dev/sda`),
written from the rescue (`--disk rpi4:/dev/sda`). The bootloader refuses to
USB-MSD-boot the stick there (measured: armed, complete files, enumerates in
1.5 s under Linux -- skipped on either bus, warm or cold), so the boot is
split: `wk boot rpi4` stages the bench system's kernel, device tree and
cmdline out of the stick's own boot partition into `second/` on the SD's boot
partition plus a `tryboot.txt`, and reboots with the firmware's tryboot
one-shot (`systemctl reboot "0 tryboot"`). The firmware reads `tryboot.txt`
exactly once and clears the flag itself, so a panic (`panic=10` is staged
into the cmdline), a watchdog return or any reboot lands on the rescue with
nothing to put back -- there is no self-disarm to stage. The kernel then
mounts the stick by PARTUUID; the stick stays the measured medium. The EEPROM
order for this driver is `sd-first` (`wk pi boot-order rpi4`): the firmware
never boots the bench medium at all. If it goes wrong: any power cycle boots
the rescue.

The stick can hold a second bench system on partitions 3-4 (`--disk
rpi4:/dev/sda@second`, the same shape as the rpi3's card): two releases with
two library stacks resident side by side, so an A/B across *images* is an
arming choice rather than a rewrite. `wk boot rpi4` with one system on the
stick arms it; with two it refuses to guess, and `wk boot rpi4 --system <id>`
names the one to stage -- its kernel, its cmdline, its root PARTUUID all come
from the named system's own partitions. WebKit slots still deploy into
whichever system is booted (`wk pi deploy`), unchanged.

**rpi5 -- `rpi5-usb`, a workstation with a bench stick.** The board's own
install on the NVMe (`/dev/nvme0n1p2`) is never written; the bench system goes
on `/dev/sda`. Arming is a firmware mailbox one-shot (`set_reboot_order`, order
`0xf64`: USB, then NVMe, then restart) that the firmware clears after one use,
so a wedged image or a plain power cycle lands back on the NVMe with nothing to
undo. The persistent EEPROM `BOOT_ORDER` stays `local`, what `wk pi boot-order
rpi5` writes for a workstation, and is the only evidence the fallback is in
place: the mailbox order cannot be read back. No overclock is ever written to
the EEPROM; its settings are shared by both modes.

That stick holds *two* systems when a second is written to it
(`--disk rpi5:/dev/sda@second`, primaries 3-4, the same shape as the rpi4's),
and which pair a boot lands on is the firmware's own A/B rather than anything
put back afterwards: a static `autoboot.txt` on the stick's first boot
partition says `boot_partition=1` under `[all]` and `boot_partition=3` under
`[tryboot]`, so the second pair is one `reboot "0 tryboot"` away and every
other boot lands on the first. Two one-shots, both firmware-reverting, so
`wk boot rpi5 --system <id>` and `wk pi bench rpi5 --ab-systems A,B` work here
with nothing on the medium changing between legs. The file is written when the
second pair is made, never at arm time -- it does not vary -- and an arming for
the second pair refuses if it is absent, because the flag would otherwise be
ignored and the *first* pair would boot: the wrong system, with nothing to say
so. Only a board whose driver selects this way gets the file: the rpi4's stick
is a two-pair medium too, and an `autoboot.txt` there would make its tryboot
flag boot the stick rather than the kernel staged on its SD.

**mbp -- `mac-volume`, this Mac.** The bench install is the `WK Bench` APFS
volume beside the host install. Apple Silicon selects a startup volume only
through an authenticated action at the machine, so there is nothing to arm
from software: `wk bench mac-ab` stages a run and reads it back, one action at
the keyboard per experiment picks the volume, and `wk boot mbp --status`
reports which side the firmware default is on. `wk sysimage write` refuses
this machine: nothing here writes a Mac's own disks.

**benchvm -- `mac-guest`.** A Tart guest standing in for a Mac in bench mode:
`wk boot benchvm` starts the guest, and nothing measured in it is comparable
with hardware; it rehearses the path.

**moose** has no bench driver yet (docs/Urgent/HANDOFF-moose-bench.md).

## Lifecycle

Every step from a bare Raspberry Pi to an automated A/B, in order, and what
each one needs. The boards differ only in how the one boot is selected
(`wk help hardware`: one medium holding both systems on the rpi3, two media
on the rpi4 and rpi5); the commands are the same, and only the `--disk`
spelling changes.

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
wk pi helper rpi3                                 # the card helper this checkout holds, onto the running system
wk pi boot-order rpi4                             # two media only: the bench medium first, the rescue behind it
wk sysimage write --from <sdcard.img> --disk rpi4:/dev/sda --profile wpewebkit-2.38-buildroot-rpi4-32
                                                  # two media: the bench system onto the stick, from the rescue
```

`wk pi helper` is what makes the next step possible on a rescue built before
the helper it needs: the rescue writes this board's bench media, and the copy
its image baked in is as old as the image. A rescue this checkout writes gets
the same pair already.

From here on no card is carried: every later write of the bench system is
made from the rescue (`--disk rpi3:/dev/mmcblk0@second`, `--disk rpi4:/dev/sda`),
and a rewrite of the `@second` system keeps its tailnet node.

**5. Boot the bench system once** -- `wk boot <board>` arms it for one boot and
reboots; `<board>-bench` joins the tailnet; the system disarms itself as it
comes up and hands the board back to the rescue after `IMG_WATCHDOG` seconds
unless claimed (`wk boot <board> --keep`). That wait is a systemd *timer* on a
systemd image (`OnBootSec`, firing a service that returns at once) and a
backgrounded subshell in `S99wk-self-return` on a BusyBox one -- never a unit
that sleeps in its own `ExecStart`, which is not *active* until it returns and
so leaves `multi-user.target` inactive for the whole watchdog, on every boot,
with nothing bounding the start job. A first boot that never appears is
read from the rescue once it returns (see "Build interventions"); a board that
does not return is brought back by hand as `wk help hardware` says for it.

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
wk pi bench rpi3 speedometer3 --ab base,pr --rounds 5                      # interleaved; its own task, reported at the end
wk bench report <task> --html                                              # that task, again, any time
wk bench report <run-a> <run-b> --html out.html                            # any two recorded runs (wk bench ls)
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

A yocto profile whose userspace is not the machine's width sets `YOC_MULTILIB`
(the variant, `lib32`) and `YOC_MULTILIB_TUNE` (the tune it builds at), and
names the variant's image recipe in `YOC_IMAGE` (`lib32-webkit-dev-ci-tools`).
The machine is left alone, so the kernel, the device tree and the firmware are
the ones that machine always builds, and poky's own multilib gives every recipe
a variant at that tune; `meta-wk-multilib` maps the image's install list onto
them, since `MLPREFIX` is set for a multilib image but nothing rewrites
`IMAGE_INSTALL`. `wpewebkit-2.46-yocto-rpi5-32` is one: a 32-bit userspace on
the Pi 5, whose Cortex-A76 implements AArch32 at EL0 while no 32-bit kernel for
the machine exists. `WK_MULTILIB_KEEP` names any package that must stay at the
machine's width; it is empty until a `bitbake -n` says otherwise.

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
