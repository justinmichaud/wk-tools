# wk-tools

Note: this document should only be edited by humans.

`wk` builds, runs, tests and benchmarks WebKit/JSC in disposable, sandboxed
workspaces. It also drives a small fleet of build
machines and Raspberry Pi/Mac benchmark boards connected by tailnet.

## Architecture

**workspace** — a named, disposable environment for one task, sitting on one computer.

`wk new`, `wk rm`

Credentials required for git, git-webkit, github, claude, etc are shared or revoked using `wk push`.

**target** — How to execute a workspace

- `container` (rootless podman or podman VM on macOS)
- `vm` (a macOS guest under Tart)
- `remote` (a shared build machine or unsandboxed computer, borrowed but not managed by wk)
- `local` (used for routing commands only when already inside a workspace)

**build machine** — a computer `wk` drives as a build target, declared once in
`targets/hosts/<name>.conf`

**bench machine** - a board or Mac that can be booted into a system for perf testing, in `boot/machines/<name>.conf`

**bridge** - a device running pmOS connecting an ethernet port to the network, in `bridge/hosts/<name>.conf`. This is currently only used to connect my bmc to tailnet.

**bench system** — the OS image on a bench machine that gets measured

built by `wk sysimage build`, written to a card by `wk sysimage write`, armed for one
boot by `wk boot`

**rescue** — what a bench machine falls back to, and is reached by, whenever its bench
system is disarmed, unbootable, or was never written. On a workstation the
rescue is the host install itself; on a bench-device it is a system `wk` owns
on its own medium.

A rescue must provide a way to write, arm and boot the bench system.

The two systems are two
tailnet nodes with two names -- the rescue `<board>-rescue` (`NODE_SSH`), the
bench system `<board>-bench` (`NODE_BENCH_SSH`)

**arm/disarm** — select, or deselect, what a bench machine boots next
(`wk boot`). Every armed system disarms and reverts itself after one boot

**state** — We always re-compute status, we never store state. Everything is stateless when possible. When state is required, it is carefully managed so that any command can be re-started if killed, and it is trivial to clean up after getting killed or having an error. For example, there is no list of workspaces: these are enumerated each time.

**results** - Results (like A/B task results) are collected and stored in the worspace for that task. A task keeps track of its progress and can be restarted at any time. Once the final report is generated, we can copy out the deliverables as a zip file to a location specified by the user (defaulting to Downloads) (the report, the individual run jsons, and the sampler profiles). The report confirms the git hash built, and the status of checks (like the check that the pgo profile is valid).

## Every command, the same way

A command is a file in `cmd/`, and the `# wk:` lines at its top declare its
shape to the dispatcher (`wk`), which enforces the same rules for all of them
before the command runs. `wk <cmd> -h` prints what those declarations say.

- **What it changes.** `readonly` commands start nothing and write nothing;
  the rest change things; a `destructive` command, subverb or flag removes,
  overwrites or revokes something and **asks once before it acts**. The
  question defaults to No and declines without a terminal; `--yes` (`-y`)
  answers it. Nothing that is not destructive ever asks.
- **`--dry-run` (`-n`), for every command.** Every state change a command
  makes goes through one library function, `act`, which under `--dry-run`
  prints the command it would run and runs nothing. A command whose changes
  are not all on that path yet is refused the flag with the reason, rather
  than let through to change something; `docs/HANDOFF-cli.md` lists those.
- **Arguments are checked against the declaration.** `opts` names the options
  a command takes (`--x=` for one with a value), `takes` how many positionals
  follow the name, `passthrough` where the rest belongs to another program
  (after `--`, or `=tail` after the last positional). Anything else is refused
  with the usage line, once, in the dispatcher.
- **`--force`, `--quiet`** are the dispatcher's too: `--force` crosses a
  refusal that exists because of a rule and says so again at the end;
  `--quiet` drops narration and keeps results, warnings and errors.
- **What the dispatcher tells the command.** `WK_FORCE`, `WK_QUIET`,
  `WK_DRY_RUN` and `WK_YES` carry the flags above; `WK_DESTRUCTIVE` says the
  invocation reached a destructive arm, and `confirm` sets `WK_CONFIRMED` once
  the question is answered, which is what lets `act` act. None is set by hand.

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
- **This host's own hardware, refused inside a workspace and on a
  build machine.** `remote`, `key`, `push`, `sudo`, `quiesce`, `session`,
  `boot`, `pi`, `sysimage`, `bridge`, `vm`, `find`, `backup`, `start`, `stop`,
  `gc`. These act on fleet devices, bridges, or this machine's own
  provisioning, so they never run against a checkout inside a sandbox, and a
  shared build box refuses them too — a build box builds, it does not own
  fleet hardware.
- **This machine, never forwarded.** `disk`, `doctor`, `version`, `selftest` —
  read-only reports about the machine you typed the command on.

# Tailnet

A machine is named by its tailnet name and nothing else. Only reach machines by tailnet.

# Detatched commands

Every command that outlives its terminal writes one record of
the same shape (`lib/task.sh`): the plan it declared before its first step,
the state of each of those steps, the machine and pid liveness is asked of, its log, the
command a person types to stop it, and what resources it holds.

`wk status` renders this, and each task can always be killed or restarted.

We never run more than one task at a time.

*** Claude edit below here ***

## Setup

**macOS** — Xcode command line tools, podman (the official installer, not
Homebrew), Tailscale, Zed (for `wk zed`), Tart (for the Apple ports; install
the `.app` and symlink `tart` from inside it, the binary needs the bundle's
entitlement).

**Ubuntu** — nothing beforehand; `./setup` installs the packages and Tailscale.

**Both** — a GitHub fork of WebKit under your own account, and a tailnet you
administer. A card reader, only to write the first medium for a new bench
machine.

```sh
git clone https://github.com/justinmichaud/wk-tools ~/Development/wk-tools
cd ~/Development/wk-tools
./setup                        # idempotent; a second run prints no changes
./setup --dry-run              # what it would change
./setup --stage quiesce        # one sudo prompt: the privileged helpers
wk sudo setup                  # closes sudo's timestamp and NOPASSWD
gh auth login                  # wk key setup uses this once, for the deploy keys
claude setup-token             # the token wk key setup asks for
wk key setup                   # every credential, one at a time; Enter skips one
wk doctor                      # what is provisioned, what is missing, the fix for each
wk sync                        # the WebKit mirror and the base snapshot; then wk new works
```

`wk doctor` names what is missing and the command that fixes it. It also
checks every machine it reaches against this tree: git identity and speed
settings, the wk-tools commit, the provisioning hash.

## Workflows

**A workspace, start to finish**

```sh
wk new bug-238                          # an overlay on the base snapshot; seconds
wk build bug-238 jsc-release --detach   # prints the build line; wk status follows it
wk build bug-238 --kill
wk run   bug-238 -- -e 'print(1+1)'
wk run   bug-238 --until-crash --max 50 -- crash.js   # repeat until it fails; keeps log and core
wk test  bug-238
wk logs  bug-238 --follow
wk enter bug-238 -- ls                  # a shell or one command, on any target
wk stop  bug-238                        # parked; wk start brings it back
wk rm    bug-238
```

A workspace's checkout is on `main`, tracking `origin/main`, with four
remotes: `origin` (WebKit/WebKit, fetch only), `wpe` (WPEWebKit, fetch
only), `fork` and `forkwpe` (yours; the only ones a push reaches). Every
fetch is a local read of the machine's mirror. `git-webkit setup` has already
run. `wk remotes <ws>` checks the wiring; `--fix` re-asserts it.

**Files in and out**

```sh
wk scp bug-238 :Tools/foo.js ./foo.js   # `:` marks the side inside the checkout
wk scp bug-238 ./patch.diff :patch.diff
wk scp bug-238 -r :WebKitBuild/logs ~/logs
```

Runs where you type it; the other path is on this machine.

**A macOS guest, for the Apple ports**

```sh
./setup --stage softnet                 # once: the guest's egress filter
wk new mac-rel --target vm              # builds the golden base the first time (hours, once)
wk vm start mac-rel
wk build mac-rel mac-release
wk build mac-rel mac-release-pgo        # instrument, collect, rebuild: the perf build
wk vm stop mac-rel
wk vm ls                                # every guest; BASE says whether the base predates its inputs
wk vm base --rebuild                    # hours; yours to run
```

A guest is an APFS clone of one base carrying Xcode and a settled desktop.
Everything else (the checkout, credentials, the shell) converges on every
start. `wk vm start` prints what it found on the desktop and refuses a guest
with anything in front of it. `wk vm check <name>` asks again.

**A build machine**

```sh
wk remote setup buildbox4               # probes it, writes targets/hosts/buildbox4.conf,
                                        # installs WebKit's build dependencies
wk new big-build --target buildbox4
wk build big-build jsc-release          # sized from the machine's live load
wk remote rm buildbox4
```

A build machine is someone else's: no credential rests on it, `wk ai` there
needs `--force`, and `USE_LIBBACKTRACE` is off.

**Pull requests**

```sh
wk pr bug-238 1234                      # WebKit PR #1234 into the workspace; wpe:1234 for WPE
wk pr bug-238 alice:eng/branch          # a fork's branch
wk new review-1234 --pr 1234
wk pr rebase                            # inside a workspace: fetch main, rebase onto it
wk pr open bug-238                      # from the host: push the branch, open the PR
```

A PR head goes straight into the checkout, never through the mirror.

**Sync**

```sh
wk sync                                 # this machine: tooling, mirror, snapshot, then every workspace
wk sync bug-238                         # one workspace's fetch
wk sync --mirror                        # the mirror alone
wk sync --tools buildbox4               # that machine's wk-tools, mirror and snapshot
wk sync --all                           # every machine
```

A sync fetches and never checks out. Tooling goes to a machine as a git
bundle of HEAD; an uncommitted tree here is refused.

**Profile**

```sh
wk profile bug-238 script.js                    # jsc's sampling profiler
wk profile bug-238 --mode samply --browser       # native sampling, MiniBrowser
wk profile bug-238 --mode bytecode --fetch       # per-bytecode tier report, copied out
```

**Benchmark in a workspace**

```sh
wk quiesce on && wk session on
wk bench bug-238 speedometer3
wk bench bug-238 jetstream3 --cores 0-3          # pinned; recorded and compared
wk bench ls                                      # every task on every machine, where it is
wk bench compare <run-a> <run-b>
wk bench report <task> --html
```

Every measurement is a task: `task.json`, then `runs/<run>/` with
`env.json`, `result.json` and the logs. A task stays on the machine that
took it, and `wk bench ls` asks them all.

**A bench machine: build, write, arm, measure**

```sh
wk sysimage build wpewebkit-2.38-buildroot-rpi3-32 --detach   # hours
wk sysimage disks <writer>                                     # which /dev the card is
wk sysimage write --from <img> --disk <writer>:/dev/sdX --profile <profile>
# carry the card to the board
wk boot rpi3                            # armed for one boot; the system disarms itself as it comes up
wk boot rpi3 --keep                     # claim it past the watchdog
wk boot rpi3 --status
```

Only a removable disk plugged into `<writer>` is ever written, never its own
system disk. A write refuses without the tailnet key or the board's WiFi
credentials, and when a node of that name already exists on the tailnet.

The image is the runtime and is built once. A **slot** is one WebKit built
against it, deployed onto the booted board without a reflash:

```sh
wk sysimage webkit <profile> --commit <sha> --slot base --detach   # at 2.52+ this is instrument,
                                                                   # collect on the board, rebuild
wk pi deploy <profile> rpi3 --slot base                            # verified byte for byte
wk pi bench rpi3 speedometer3 --slot base
wk pi bench rpi3 speedometer3 --ab base,pr --rounds 5              # two slots, no reboot between
```

**An A/B of a pull request, one command**

```sh
wk ab wpe:1725 --devices rpi3-32,rpi4-32,rpi5-64 --dry-run   # every step, nothing run
wk ab wpe:1725 --devices rpi4 --bits 32 --plan jetstream3 --rounds 8 --yes --detach
wk ab <task> --kill
wk ab <sha> --base <sha> --release 2.38 --devices rpi3       # A/A: the noise floor
wk bench report <task> --html
```

Both slots built per image, deployed, alternated on every board at once.
The base is the merge-base with the image's branch; a base more than one
commit behind the head is refused. An A/B is a graph (`lib/sched.py`): each
step names what it needs, what it holds and how to tell it is done, so a
re-run continues instead of rebuilding. One machine builds one thing at a
time; `--build-on a,b` builds the two arms on two machines.

Rounds are counterbalanced (AB, BA, ...). Every leg after a boot discards a
settle run. The clock is pinned, not governed. A warmup round measures the
live process: which GPU driver it mapped, whether the GPU did work, which
JIT tiers it reached, and a profile; any of those wrong refuses the A/B.
Subtests one arm cannot run are dropped from both
(`bench/subtest-exclusions.conf`).

**An A/B of two systems** — two releases resident on one medium, a boot per leg:

```sh
wk sysimage write --from <2.38 img> --disk rpi3:/dev/mmcblk0@second --profile <2.38 profile>
wk sysimage write --from <2.52 img> --disk rpi3:/dev/mmcblk0@third  --profile <2.52 profile>
wk boot rpi3 --system <id>              # then wk pi deploy into each
wk pi bench rpi3 speedometer3 --ab-systems <a>,<b> --slot base --rounds 5
```

**The Mac as a bench machine**

```sh
wk boot mbp --prepare                   # this tree and the helpers onto the Mac; one password, once
wk boot mbp --status                    # which volume the firmware default is
wk bench mac-volume --all               # on the Mac: the WK Bench volume, installed and armed
wk bench mac-ab mac-rel --patch <ref> --base <ref> --detect 0.3
wk bench precision <run-a> <run-b>      # what the rounds so far resolve
```

Driven from another machine. The bench install is its own tailnet node, so
results read back the moment a leg ends. The run stops when every plan
resolves `--detect` (0.3% by default), or at `--max-rounds`. Getting the
desktop back is the one manual step: the host install stops for a password.

Every macOS number is from `mac-release-pgo`: an instrumented build, a
collection through the three benchmarks, then the measured build, per arm.
The collection is gated: a WebGL context, the GPU process on the accelerator,
an unthrottled frame rate; then the profile is read back and judged
(`lib/wkpgo.py`). A board's WebKit at 2.52 or later is built the same way
(`image/pgo.sh`).

The display mode is declared (`NODE_DISPLAY`), held, and checked before the
restart and in every leg. Brightness is driven to minimum. What is on the
screen is asked of the window server, and anything wk did not put there
refuses the leg. `bench/mac-quiet-desktop.sh` is the one table of what a
quiet Mac is; `wk quiesce status` reads it back off the machine.

**Quiesce and session, before any measurement**

```sh
wk quiesce on                           # background daemons paused, App Nap off, display held
wk session on                           # a compositor on the attached monitor
wk quiesce status                       # read off the machine, not off a record
wk quiesce off && wk session off
```

**Add a bench machine**

```sh
$EDITOR boot/machines/<name>.conf       # NODE_SSH, NODE_BENCH_SSH, NODE_DRIVER, NODE_DEVICE,
                                        # NODE_ROOT, NODE_PROFILE, NODE_NET, NODE_DTB, NODE_ROLE
git add boot/machines/<name>.conf && git commit
wk boot --list
```

**Provision a bridge phone**

```sh
wk bridge provision tailnet-bridge-generic   # image, card, phone, tailnet policy; prompts at the
                                             # two hand steps: the card, and pasting the policy
wk bridge setup tailnet-bridge-generic       # re-apply
wk bridge status tailnet-bridge-generic
```

**An agent in a workspace**

```sh
wk ai claude bug-238                    # verifies the sandbox first; refuses if it fails
wk ai claude bug-238 -r                 # resume
wk ai claude bug-238 --rc               # a Remote Control server; --rc --stop ends it
wk ai pi bug-238                        # the pi agent
wk ai claude                            # inside a workspace: this one
```

An agent cannot push, commit or build directly. Pushing needs a key that is
not there (`wk push`, below). Committing is walled: the checkout's `.git`
commit parts are mounted read-only under the agent. Building goes through
`wk build`: the build tools on `PATH` refuse an agent by name. `wk verify
<ws>` measures all three from inside.

**`wk key`: every credential, one fleet**

```sh
wk key setup                            # deploy keys, then every credential this machine lacks
wk key check                            # one row per credential, what its issuer says now
wk key set github-pat                   # one by name; --replace rotates it
wk key set claude                       # the inference token, for build machines
wk key set claude-login                 # the account login, which --rc needs
wk key deploy --rotate                  # the deploy keys, revoked and reissued fleet-wide
```

`lib/credcheck.py` holds one rule per credential: what it must do and must
not do. Nothing is stored until it passes, and no verdict is remembered. A
credential is the fleet's: `wk key setup` asks every workstation what it
holds, the best working one wins, and it is put everywhere. The claude.ai
login is the exception, one per machine, because a second holder of a
refresh token locks the first out. It lands in `~/.config/wk/agent-rw`, the
one directory a workspace mounts read-write, so the CLI rotates the file
every workspace reads. `CLAUDE_CODE_OAUTH_TOKEN` is what a build machine
gets instead.

**`wk push`: publishing without the credentials inside**

```sh
wk push on                              # asks once; ends every agent session first
wk push status                          # asks the agent, not a record
wk push off
```

The deploy keys live in an ssh-agent on the machine running the workspaces;
a workspace's ssh config names the socket, so ssh signs with a key it can
never read. The GitHub token and Bugzilla key go to the injector
(`container/proxy/github-inject.py`), which terminates TLS for those two
hosts and puts the credential on the request: a read always, a write only
while push is on. With push off a write is refused with 412 naming `wk push
on`. A macOS guest gets the same through an ssh-agent on the host forwarded
per guest.

**Housekeeping**

```sh
wk status                               # every workspace, task, machine and bench device
wk doctor --all                         # this machine and every build machine
wk disk
wk gc                                   # reclaims by reference count; names what it will not take
wk gc --purge-rubble                    # half-made workspaces nothing is creating
wk gc --purge-mirror                    # the mirror and every snapshot; refused with a live workspace
wk stop --tasks                         # everything running here, each by its own kill line
```

**Reprovision from scratch**

```sh
git clone https://github.com/justinmichaud/wk-tools ~/Development/wk-tools
cd ~/Development/wk-tools && ./setup && wk sync
```

Every machine the repo knows is in it. Nothing else is machine-specific but
what `wk backup` captures and the credentials, which are never in git. On
macOS, `podman machine rm wk && ./setup` discards the container store
(workspaces, snapshots) and nothing of the host's.

## Hardware

How each bench machine selects the one boot. Everything else is the same:
the rescue is written first with `--rescue`, the bench system second, `wk
boot <board>` arms it once, and a power cycle lands on the rescue.

**rpi3 — `pi-sd`, one SD card.** Rescue on partitions 1-2, bench system on
3-4 (`@second`); a third system goes in an extended partition (`@third`).
The firmware boots the first FAT partition only, so arming writes an
`os_prefix=` line first in `config.txt`, keeping the rescue's copy beside
it. The bench system moves it back as it comes up. If it panics first, the
same system boots every time: pull the card and rename `config.txt.rescue`
back by hand in a reader.

**rpi4 — `pi-tryboot`, two media.** Rescue on the SD, bench root on the USB
stick. The firmware will not boot the stick, so arming stages the bench
kernel and cmdline onto the SD with a `tryboot.txt` and reboots with the
firmware's one-shot. The firmware clears the flag itself; nothing is put
back. EEPROM order is `sd-first` (`wk pi boot-order rpi4`). A second system
on the stick is `@second`; `wk boot rpi4 --system <id>` names one.

**rpi5 — `rpi5-usb`, a workstation with a bench stick.** The NVMe is never
written. Arming is a firmware mailbox one-shot (USB, then NVMe) that clears
after one use. EEPROM `BOOT_ORDER` stays `local`, the only evidence the
fallback is in place. Two systems on the stick are the firmware's own A/B: a
static `autoboot.txt` selects the second pair under `[tryboot]`.

Reading a medium the board is not booted from goes through the card helper
(`admin/wk-card-priv`, `b_medium_read`): read-only, three file names, one
partition number, bounded. Every system a write makes carries the helper;
`wk pi helper <board>` installs this checkout's onto a running one.

**mbp — `mac-volume`.** The `WK Bench` APFS volume beside the host install.
Apple Silicon selects a startup volume only at the keyboard, so `wk boot mbp`
reports and stages and a person picks the volume. `wk sysimage write` refuses
a Mac's own disks.

**benchvm — `mac-guest`.** A Tart guest rehearsing the Mac path. Nothing
measured in it is comparable with hardware.

**moose** has no bench driver yet (docs/Urgent/HANDOFF-moose-bench.md).

## Lifecycle

From a bare board to an automated A/B. The boards differ only in the
`--disk` spelling (`wk help hardware`).

1. **Declare it.** `boot/machines/<name>.conf`; commit.
2. **Build both images.** The rescue (`webkit-2.52-yocto-<board>`) and the
   system under test, each in its own workspace, hours each:
   ```sh
   wk sysimage build webkit-2.52-yocto-rpi3-32 --detach
   wk sysimage build wpewebkit-2.38-buildroot-rpi3-32 --detach
   wk sysimage ls
   ```
3. **Write the first card from a reader.** The machine with the reader
   needs the card helper (`./setup --stage quiesce`) and the board's WiFi.
   A stale `<board>-rescue` node is removed in the tailnet admin console
   first; the write refuses the collision.
   ```sh
   wk sysimage disks rpi5
   wk sysimage write --from <rescue.wic.xz> --disk rpi5:/dev/mmcblk0 --rescue --profile webkit-2.52-yocto-rpi3-32
   wk sysimage write --from <sdcard.img>    --disk rpi5:/dev/mmcblk0@second --profile wpewebkit-2.38-buildroot-rpi3-32
   ```
4. **Boot the rescue.** Carry the card, power on; `<board>-rescue` joins the
   tailnet. Then `wk pi helper <board>`, and on two media `wk pi boot-order
   <board>` and the bench write from the rescue (`--disk rpi4:/dev/sda`).
   No card is carried after this.
5. **Boot the bench system once.** `wk boot <board>`; `<board>-bench` joins;
   it hands the board back after `IMG_WATCHDOG` seconds unless `--keep`.
6. **The A/B.** `wk ab wpe:1725 --devices rpi3 --bits 32 --dry-run`, then
   without it. By hand, the same steps: `wk sysimage webkit` twice, `wk boot
   --keep`, `wk pi deploy` twice, `wk pi bench --ab`, `wk bench report`.

**A buildroot configuration of your own** is an external defconfig under
`image/buildroot/external/configs/`, named by the profile's `BR_DEFCONFIG`.
A bench image needs `wpa_supplicant`, OpenSSH (dropbear refuses ed25519) and
a kernel with TUN and netfilter; copy a 2.38 defconfig. A 32-bit userspace on
a 64-bit machine is a yocto multilib profile (`YOC_MULTILIB`).

**Build interventions.** `wk sysimage build` is re-runnable and rebuilds
what changed. Two things cost more than they look: a rebuild after slot
builds drops `local.mk` and `wpewebkit-dirclean`s, so WPE rebuilds from the
tarball (hours); and buildroot leaves a deselected package's files in place
(dropbear's `S50dropbear` beside `S50sshd`), so remove the files its
`.files-list.txt` names through `wk enter`, or build from scratch. A bench
system that never appeared is read from the rescue: `wk boot <board> --diag`,
or mount its root read-only and read `/var/log` and `tailscaled.log`.

## Overrides

Every `WK_*` variable is read with a default; each moves one decision.

**What to build** — `WK_CC`, `WK_CXX`, `WK_EXTRA_CMAKE`, `WK_BUILD_CMAKE`,
`WK_EXTRA_ENV`, `WK_CCACHE_DIR`, `WK_CCACHE_MAXSIZE`, `WK_TARGET_KIND`,
`WK_REMOTE_MAX_JOBS`, `WK_MB_PER_JOB`, `WK_PGO_COLLECT_TIMEOUT`.

**How much of the machine** — `WK_MAX_JOBS`, `WK_LOAD`, `WK_AVAIL_MB`,
`WK_RESERVE_CORES`, `WK_RESERVE_MB`, `WK_HEADLESS_RESERVE_CORES`,
`WK_HEADLESS_RESERVE_MB`, `WK_CGROUP_CORES`, `WK_CGROUP_MB`,
`WK_BUILD_MACHINE`, `WK_BUILD_DISK_GB`.

**Where state lives** — `WK_LOCAL_STORE`, `WK_REMOTE_STORE`, `WK_LOCK_DIR`,
`WK_PREFETCH_DIR`, `WK_MARKER`, `WK_REMOTE_MARKER`, `WK_IMAGE_MARKER`,
`WK_SESSION_MODE_FILE`, `WK_MIRROR_BRANCHES`, `WK_TART_CACHE_GB`, `WK_CMD`.
On a macOS workstation the store is the podman machine's, so this machine's
own records go under `~/.local/state/wk`.

**The container target** — `WK_SDK`, `WK_SDK_IMAGE`, `WK_CONTAINER_USER`,
`WK_TOOLS_SRC`, `WK_MACHINE`, `WK_SANDBOX`, `WK_MIRROR` (the mirror's path
inside a container).

**The macOS guest** — `WK_VM_IMAGE`, `WK_VM_BASE`, `WK_VM_USER`,
`WK_VM_PASSWORD`, `WK_VM_CPUS`, `WK_VM_MEM_MB`, `WK_VM_BASE_CPUS`,
`WK_VM_BASE_MEM_MB`, `WK_VM_DISK_GB`, `WK_VM_DISPLAY`, `WK_VM_SUBNET`,
`WK_VM_PROXY_ADDR`, `WK_VM_PROXY_PORT`, `WK_HOST_FREE_WARN_GB`,
`WK_VM_SHELLS_WARN`, `WK_VM_MEM_FREE_WARN_PCT`, `WK_VM_SWAP_WARN_MB`,
`WK_VM_FORCE` (crosses a stale base or a blocked desktop, recorded),
`WK_VM_STORE` (where the guests' records live, apart from the container store).

**The Mac's bench install** — `WK_BENCH_USER`, `WK_BENCH_VOLUME`.

**Credentials and the tailnet** — `WK_PUSH_AGENT_SOCK`, `WK_PUSH_PAT_FILE`,
`WK_PUSH_READ_PAT_FILE`, `WK_PUSH_BUGZILLA_KEY_FILE`, `WK_TS_AUTHKEY`,
`WK_TS_API_SECRET`, `WK_IMAGE_KEY`, `WK_ANY_ROOT`, `WK_TAILSCALE_TIMEOUT`,
`WK_SOFTNET_BIN`, `WK_PROBE_SECONDS` (how long a build machine's probe may take).

**Waiting and reporting** — `WK_READY_TIMEOUT`, `WK_READY_WAIT`,
`WK_POLL_SECONDS`, `WK_HEARTBEAT_SECONDS`, `WK_STATUS_PORT`,
`WK_STATUS_INTERVAL`, `WK_BROKER_SOCKET`, `WK_SCREEN_EXPECTED`,
`WK_BENCH_RUNNER_REF`.

## Where the rest is

`wk help` prints this file, `wk help <topic>` one section (`wk help
lifecycle`, `wk help hardware`, `wk help push`), `wk <cmd> -h` one command.
CLAUDE.md is for anyone editing this repository. docs/PLAN.md is the order in
which the first half of this file becomes true.
