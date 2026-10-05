# wk-tools

Note: this document should only be edited by humans.

`wk` builds, runs, tests and benchmarks WebKit/JSC in disposable, sandboxed
workspaces. It also drives a small fleet of build
machines and Raspberry Pi/Mac benchmark boards connected by tailnet.

## Architecture

**workspace** — a named, disposable environment for one task, sitting on one computer.

`wk new`, `wk rm`

Credentials required for git, git-webkit, github, claude, etc are shared or revoked using `wk key push`.

**place** — where a workspace lives, named with `--on`; a **driver** makes and runs one

- `container` (rootless podman or podman VM on macOS)
- `vm` (a macOS guest under Tart)
- `remote` (a shared build machine or unsandboxed computer, borrowed but not managed by wk)
- `local` (used for routing commands only when already inside a workspace)

**build machine** — a computer `wk` drives as a place for workspaces, declared once in
`machines/<name>.conf`

**bench machine** - a board or Mac that can be booted into a system for perf testing, in `machines/<name>.conf`

**bridge** - a device running pmOS connecting an ethernet port to the network, in `machines/<name>.conf`. This is currently only used to connect my bmc to tailnet.

**bench system** — the OS image on a bench machine that gets measured

built by `wk sysimage build`, written to a card by `wk sysimage write`, armed for one
boot by `wk boot`

**rescue** — what a bench machine falls back to, and is reached by, whenever its bench
system is disarmed, unbootable, or was never written. On a workstation the
rescue is the host install itself; on a bench machine it is a system `wk` owns
on its own medium.

A rescue must provide a way to write, arm and boot the bench system.

The two systems are two
tailnet nodes with two names -- the rescue `<board>-rescue` (`ssh`), the
bench system `<board>-bench` (`bench_ssh`)

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
- **Verbs and build presets are the dispatcher's too.** `verbs=` names a
  command's subverbs: an unknown one is refused, and the command gets a
  declared one as its first argument wherever it was typed; `default=` is
  the verb a bare invocation stands for (`wk quiesce` is `wk quiesce
  status`), and a first word that is no verb is its argument when it takes
  one (`wk pr 1234`). A removed flag or verb is refused like any unknown
  word. `preset=--preset` (or `preset=arg`, `wk build`'s positional) says the
  command takes a build preset: `-h` lists every name in lib/wk/presets.py,
  one it does not hold is refused, and the preset reaches the command as
  `WK_PRESET`, never in argv; an exported `WK_PRESET` the arguments do not
  name is refused. `passthrough=all` is
  `=tail` with nothing in the tail read by wk, not even `-h` or `--force`
  (`wk ai pi <ws> --help` is pi's).
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

- **The workspace's place.** `new`, `rm`, `start`, `stop`, `build`, `run`,
  `test`, `ai`, `bench`, `status`, `ls`, `enter`, `scp`, `pr`, `gui`, `zed`,
  and `sync` and `doctor` when they name a workspace. A workspace
  lives on exactly one machine, and the command goes to that machine: on a
  macOS host a `container` workspace's command is forwarded into the podman VM
  over `podman machine ssh`, and a workspace on a machine that runs `wk` for
  itself — a build box, or a peer workstation — is handed over whole, so that
  machine's own `wk` resolves the name and does the work. The dispatcher
  exports `WK_NAME`, `WK_PLACE` and `WK_PRESET` to the command it runs. Either
  hop carries the global flags as environment (`WK_QUIET`, `WK_FORCE`,
  `WK_YES`, `WK_DRY_RUN`, `WK_DEBUG`), with `WK_ROW_LABEL` (the
  machine its rows name), `WK_NO_DELEGATE` (answer for itself, hand nothing
  on), `WK_ZED_PUBKEY` (the asking machine's zed key) and `WK_SDK_IMAGE`
  (the workspace image override), and never
  `WK_PLACE` or `WK_STORE`, which the far side resolves for itself; the
  podman VM is also told `WK_IN_VM` and `WK_HOST_SELF`, since it is part of
  this machine and its records name it. `wk zed` is the one
  exception, since the editor runs where you typed the command: it asks the
  machine holding the workspace for a route and opens that from here.
- **This host's own hardware, refused inside a workspace and on a
  build machine.** `machine`, `key`, `quiesce`, `boot`, `sysimage`, `gc`. These act on fleet devices, bridges, or this machine's own
  provisioning, so they never run against a checkout inside a sandbox, and a
  shared build box refuses them too — a build box builds, it does not own
  fleet hardware.
- **This machine, never forwarded.** `doctor`, `selftest` —
  read-only reports about the machine you typed the command on.

# Tailnet

A machine is named by its tailnet name and nothing else. Only reach machines by tailnet.

# Detatched commands

Every command that outlives its terminal writes one record of
the same shape (`lib/wk/record.py`): the plan it declared before its first step,
the state of each of those steps, the machine and pid liveness is asked of, its log, the
command a person types to stop it, and what resources it holds.

`wk status` renders this, and each task can always be killed or restarted.

We never run more than one task at a time.

*** Claude edit below here ***

## Layers

The tree is five layers. A layer uses only the layers above it in this table,
never one below.

| layer | what it holds | rule |
| --- | --- | --- |
| `home` | the fleet as facts: `machines/*.conf`, the tailnet, bridges, the BMC | config only; nothing imports it, every layer reads it |
| `lab` | places and drivers, the fleet walk, sysimage, boot, quiesce and session, the bench pipeline's mechanics | knows nothing of WebKit |
| `wk` | the WebKit kit: build presets, `Tools/Scripts` wrappers, plans, PGO, the PR flow | wraps `Tools/Scripts`, never replaces it; drives places through `lab` |
| `field` | what reads results: reports, crash dumps, symbolication | consumes `wk`; nothing depends on it |
| `stock` | the pristine environment plus onboarding (`./setup`) | a profile of `lab`, not a codebase of its own |

`lint.layering` holds the rule: a `lab` module names no WebKit and imports no
`wk` or `field` module. `cmd/*`, the dispatcher, completion, `wk gc`, `wk
doctor`'s disk section and the verb tables of `wk bench` and `wk sysimage` are
the CLI over every layer, and no layer imports them.

What `lab` needs of WebKit by name -- the checkout, mirror and build-tree
names, the upstreams and forks, the PR tool, the build presets, the SDK, the
browser and a plan's runner -- is `lib/wk/project.json`, which `wk` owns and
`lab` reads through `wk.project.get`. A WebKit step a `lab` mechanism runs is
handed in by the command that calls it: `lib/wk/bench/plans.py` as a bench
run's `kit` (the build preset, the runner's arguments, the pinned payload, a
board's PGO collection), `lib/wk/pr.py` to `wk new --pr`.

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
wk key sudo setup              # closes sudo's timestamp and NOPASSWD
gh auth login                  # wk key setup uses this once, for the deploy keys
claude setup-token             # the token wk key setup asks for
wk key setup                   # every credential, one at a time; Enter skips one
wk doctor                      # what is provisioned, what is missing, the fix for each
wk sync                        # the WebKit mirror and the snapshot; then wk new works
```

`wk doctor` names what is missing and the command that fixes it. It also
checks every machine it reaches against this tree: git identity and speed
settings, the wk-tools commit, the provisioning hash.

## Workflows

**A workspace, start to finish**

```sh
wk new bug-238                          # refresh the mirror, overlay the snapshot, fast-forward
wk build bug-238 jsc-release --detach   # prints the build line; wk status follows it
wk build bug-238 --kill
wk run   bug-238 -- -e 'print(1+1)'
wk run   bug-238 --until-crash --max 50 -- crash.js   # repeat until it fails; keeps log and core
wk run   bug-238 --rr -- crash.js       # record it with rr (Linux ports); wk gui --rr records the browser
wk run   bug-238 --replay               # the latest recording, under lldb
wk test  bug-238
wk status bug-238 --log --follow
wk enter bug-238 -- ls                  # a shell or one command, on any place
wk stop  bug-238                        # parked; wk start brings it back
wk rm    bug-238
```

A workspace's checkout is on `main`, tracking `origin/main`, with four
remotes: `origin` (WebKit/WebKit, fetch only), `wpe` (WPEWebKit, fetch
only), `fork` and `forkwpe` (yours; the only ones a push reaches). Every
fetch is a local read of the machine's mirror. `git-webkit setup` has already
run. `wk sync <ws>` reads the wiring back as it fetches; `--fix` re-asserts it.

**Files in and out**

```sh
wk scp bug-238 :Tools/foo.js ./foo.js   # `:` marks the side inside the checkout
wk scp bug-238 ./patch.diff :patch.diff
wk scp bug-238 -r :WebKitBuild/logs ~/logs
```

Runs where you type it; the other path is on this machine.

**Working on wk-tools**

```sh
wk new tools --repo wk-tools            # a container holding wk-tools instead of WebKit
wk ai claude tools                      # the agent starts in /src/wk-tools
wk enter tools                          # a shell there; ./wk selftest runs lint and unit
wk sync tools                           # fetches its origin
```

wk-tools is worked on from such a workspace, not from a session on the host.
Its checkout is cloned at first start from this machine's wk-tools `origin`
(its GitHub repository, over https), so it needs no mirror and no snapshot.
The commit wall and the push switch are a WebKit workspace's: an agent there
cannot commit or push, and a person turns push on. The live tier of `wk
selftest` stays the host's. Only the container place holds one for now; `--repo`
on another is refused.
TODO: a push from it needs a deploy key for the wk-tools repository in `wk key`'s set (docs/PLAN.md, Owed).
TODO: the host session goes once the live check `tests/test_repo_workspace_live.py` passes (claude/CLAUDE-host.md).

**A macOS guest, for the Apple ports**

```sh
./setup --stage softnet                 # once: the guest's egress filter
wk new mac-rel --on vm                  # builds the golden base the first time (hours, once)
wk start mac-rel                        # boots it and writes its ssh alias, wk-mac-rel
wk build mac-rel mac-release
wk build mac-rel mac-release-pgo        # instrument, collect, rebuild: the perf build
wk stop mac-rel
wk doctor mac-rel                       # its base, its desktop, what is resident in it
wk sysimage build macos-guest-base --rebuild   # hours; yours to run
wk sysimage build macos-guest-base --rm        # erase it; asks again about the pulled image
```

A guest is an APFS clone of one base carrying Xcode and a settled desktop.
Everything else (the checkout, credentials, the shell) converges on every
start. `wk start` prints what it found on the desktop and refuses a guest
with anything in front of it; `wk doctor <name>` asks again. The base is
stale once an input that made it changes, and `wk doctor` says so.

Everything wk runs in a guest, and every copy in or out, goes through `tart
exec`, the guest agent's own channel, never the network (macOS refuses a
launchd job's connection to a guest). The `wk-<name>` alias the editor uses
reaches the guest's sshd the same way: its ProxyCommand runs `nc 127.0.0.1 22`
under `tart exec`. A guest mounts two host directories: agent-rw on macOS's
automount tag, and the mirror read-only on its own tag, `wk-mirror`, which a
LaunchDaemon the base installs mounts at boot under `/Volumes/wk-mirror`.
Each start forwards the host's request broker to `~/.wk-broker.sock` in the
guest, so `wk sync` in there asks the broker as a container does.

**A build machine**

```sh
wk machine setup buildbox4 --kind build # probes it, writes machines/buildbox4.conf,
                                        # installs WebKit's build dependencies
wk new big-build --on buildbox4
wk build big-build jsc-release          # sized from the machine's live load
wk machine rm buildbox4
wk machine ls                           # every machine in machines/, and its tailnet names
wk machine probe rpi4                   # how it is reached; a board that does not answer is swept for
wk machine probe                        # every device on every segment a sweep can see
```

A build machine is someone else's: no credential rests on it, `wk ai` there
needs `--force`, and `USE_LIBBACKTRACE` is off.

`wk new`, `wk rm` and `wk build` on a build machine's workspace are handed to
that machine's own wk, which runs each (sizing a build) and keeps its record
there. The workspace is a clone from the box's mirror, so the box and every
workstation read one state for it, and the box's `wk status` and every
workstation's show the one build, and `wk status --log`, `wk status
--wait` and `wk build --kill` reach it the same way. It builds with the
wk-tools the box has, and a box whose wk-tools commit differs from this
checkout's is refused naming `wk sync --tools buildbox4` (`--force` crosses
it); `wk status` and `wk status --log` still hand over and report the difference. A
box that does not answer, or has no wk-tools of its own, is refused with the
remedy.

**Pull requests**

```sh
wk pr bug-238 1234                      # WebKit PR #1234 into the workspace; wpe:1234 for WPE
wk pr bug-238 alice:eng/branch          # a fork's branch
wk new review-1234 --pr 1234
wk pr rebase                            # inside a workspace: fetch main, rebase onto it
wk pr open bug-238                      # from the host: push the branch, open the PR
wk pr report                            # from the host: your last 7 days on WebKit/WebKit (--since <date>)
```

A PR head goes straight into the checkout, never through the mirror.

**Sync**

```sh
wk sync                                 # this machine: tooling, mirror, snapshot, then every workspace
wk sync                                 # inside a workspace: the mirror (asked of the broker), then this one
wk sync bug-238                         # one workspace's fetch
wk sync --mirror                        # the mirror alone
wk sync --tools buildbox4               # that machine's wk-tools, mirror and snapshot
wk sync --all                           # every machine
wk sync bug-238 --fix                   # re-assert its remotes and git-webkit setup, then fetch
```

A sync fetches and never checks out, and names any checkout, or snapshot,
whose remotes are wired wrong. Every workspace, guest and the podman VM mounts
the mirror read-only, so a refresh from one of them is asked of the machine
that keeps it, through the broker. A refresh on a Mac then remounts the mirror's
share (never agent-rw) in each running guest, whose old mount keeps reading the refs as they were;
one that cannot is named with `wk stop`/`wk start`. A workspace overlays a
snapshot it never writes, so a newer tree is a new snapshot, hard-linked from
the last; checkouts are wired with `core.trustctime false`, since each link
moves every file's ctime. Tooling goes to a machine as a git bundle of HEAD;
an uncommitted tree here is refused.

**Profile**

```sh
wk run bug-238 --profile script.js              # jsc's sampling profiler
wk run bug-238 --profile=samply --browser       # native sampling, MiniBrowser
wk run bug-238 --profile=bytecode --fetch       # per-bytecode tier report, copied out
wk run bug-238 --profile=sysprof script.js      # sysprof-cli, JS frames named from the JIT dump
wk test bug-238 --profile=native --attach <pid> # the same profiler, from wk test
```

**Benchmark in a workspace**

```sh
wk quiesce on && wk quiesce session on
wk bench run bug-238 speedometer3
wk bench run bug-238 jetstream3 --cores 0-3      # pinned; recorded and compared
wk bench run bug-238 jetstream3 --preset jsc-release --a-args '' --b-args '--useFoo=1' --rounds 10
                                                 # one build, a jsc option toggled in alternating rounds
wk bench ls                                      # every task on every machine, where it is
wk bench compare <run-a> <run-b>
wk bench report <task> --html
```

Every measurement is a task: `task.json`, then `runs/<run>/` with
`env.json`, `result.json` and the logs. A task lives in its workspace's
directory (`ws/<name>/bench/<task>`), on the machine holding that workspace
-- the podman VM, a build box, this one -- and a command driving it from
elsewhere reads and writes it through that machine. The workspace is
`wk bench run`'s; for `wk bench ab` across boards, the first device's image
workspace, the task recorded once its image step has made it; for a
`--systems` A/B or a Mac A/B, `--workspace` (the one that built system A, or
the Mac's arms). `wk bench ls` asks every machine. `wk rm` refuses a
workspace holding a task that no export holds as it is now, naming
`wk bench export <task>`; `--force` destroys it anyway. A task left in a store's
`bench/`, outside any workspace, is no command's to read and none takes it:
`wk gc` names each with the `mv` into its workspace's `bench/`, and `wk
doctor` lists it as backed-up state while one is there.

**Results**

```sh
wk bench report <task>                  # the commit each arm measured, each check's verdict
wk bench export <task>                  # ~/Downloads/<task>.zip
wk bench export <task> --to <dir> --force   # a task stopped short: what it has
wk bench run <ws> <plan> --task <task>  # restart a one-run task; nothing if it holds its run ok
```

The report heads with the commit each arm's runs recorded, checked against
the one the task names, and a verdict per check: the preflight (failed if a
run was forced past it), the warmup round's evidence, and for a PGO build
the profile reading its run carries (unknown when it carries none). A task
stopped short names the command that restarts it; an A/B restarted with
`--task <task>` runs only the rounds the task does not already hold with
both arms. The export is the report as text and html, `task.json`, every
run's json and the warmup round's evidence and profiles, written whole or
not at all; replacing an earlier export asks first. A task in the podman
VM's store or on a build box is read from there and zipped here. A PGO
slot's board run carries the reading of its image workspace's collection.

**A bench machine: build, write, arm, measure**

An **image preset** (`image/presets/<image-preset>.conf`, listed by `wk sysimage
presets`) declares one bootable system: its builder, board, release and base
branch. It names the base image, never a WebKit build; that is a build preset.

```sh
wk sysimage build wpewebkit-2.38-buildroot-rpi3-32 --detach   # hours
wk sysimage disks <writer>                                     # which /dev the card is
wk sysimage write --from <img> --disk <writer>:/dev/sdX --image-preset <image-preset>
# carry the card to the board
wk boot rpi3                            # armed for one boot; the system disarms itself as it comes up
wk boot rpi3 --keep                     # claim it past the watchdog
wk boot rpi3 --status
```

Only a removable disk plugged into `<writer>` is ever written, never its own
system disk. A write refuses without the tailnet key or the board's WiFi
credentials, and when a node of that name already exists on the tailnet.

The image is the runtime and is built once, in its **image workspace**
(`<builder>-<image-preset>[-<arm>]`, which `wk ls` lists). A **slot** is one WebKit
built against it, deployed onto the booted board without a reflash:

```sh
wk sysimage webkit <image-preset> --commit <sha> --slot base --detach   # at 2.52+ this is instrument,
                                                                        # collect on the board, rebuild
wk bench deploy <image-ws> rpi3 --slot base                        # verified byte for byte
wk bench run <image-ws> speedometer3 --system rpi3 --slot base     # run-benchmark here, the browser there
wk bench run <image-ws> speedometer3 --system rpi3 --ab base,pr --rounds 5   # two slots, no reboot between
```

A board run measures the bench system that is up: it refuses one in host
mode, its rescue, or one a `wk boot` arming is about to replace, and its
preflight wants a display and a pinned clock (`--force` records either
missing). The board reaches this host's page server through an ssh reverse
forward held for the run. An A/B prepares the board once per boot (the
clock pin, the claim, the session) and re-reads its system and slot every
leg. In a workspace, `wk bench deploy <board>` and `wk bench run <plan>
--system <board>` are requests to the broker. An A/B is driven from the
workstation; its deploys and board runs run where the image workspace is.

**An A/B of a pull request, one command**

```sh
wk bench ab wpe:1725 --devices rpi3-32,rpi4-32,rpi5-64 --dry-run   # every step and its cost, nothing run
wk bench ab wpe:1725 --devices rpi4 --bits 32 --plan jetstream3 --rounds 8 --yes --detach
wk bench ab <task> --kill
wk bench ab <sha> --base <sha> --release 2.38 --devices rpi3       # A/A: the noise floor
wk bench ab --systems <id-a>,<id-b> --devices rpi5 --workspace <image-ws>   # two system images, one slot
wk bench report <task> --html
```

Both slots built per image, deployed, alternated on every board at once.
The base is the merge-base with the pull request's own base branch; a base
more than one commit behind the head is refused. An A/B is a graph
(`lib/wk/sched.py`) run in one process: each step names what it needs, what
it holds and how to tell it is done, so a re-run with `--task <task>`
continues instead of rebuilding. One machine builds one thing at a time;
`--build-on a,b` builds the two arms on two machines. Before it runs, an A/B
states its cost: each board's legs times the median leg of that plan measured
there before. The report compares only rounds both arms finished on one
payload pin (the runner commit and the benchmark copy).

Rounds are counterbalanced (AB, BA, ...). `--detect PCT` goes on past
`--rounds` until the rounds resolve PCT per cent, up to `--max-rounds`: the
same precision `wk bench precision` reports, asked between rounds. Every leg after a boot discards a
settle run. The clock is pinned, not governed. A warmup round measures the
live process: which GPU driver it mapped, whether the GPU did work, which
JIT tiers it reached, and a profile; any of those wrong refuses the A/B.
Subtests one arm cannot run are dropped from both
(`bench/subtest-exclusions.conf`).

**An A/B of two systems** — two releases resident on one medium, a boot per leg:

```sh
wk sysimage write --from <2.38 img> --disk rpi3:/dev/mmcblk0@second --image-preset <2.38 image preset>
wk sysimage write --from <2.52 img> --disk rpi3:/dev/mmcblk0@third  --image-preset <2.52 image preset>
wk boot rpi3 --system <id>              # then wk bench deploy into each
wk bench run <image-ws> speedometer3 --system rpi3 --ab-systems <a>,<b> --slot base --rounds 5
```

Each leg arms its arm's system where the board allows it, or from the rescue,
and runs only once the running system's own marker names it. At the end the
board is handed back to its rescue with its arming record cleared.

**The Mac as a bench machine**

```sh
# on the Mac, once: ./setup --stage quiesce   (the helpers; one password)
wk boot mbp --status                    # which volume the firmware default is
wk sysimage build perf-macos-tolken --all   # on the Mac: the WK Bench volume, installed and armed
wk bench ab --devices mbp --patch <ref> --base <ref> --workspace mac-rel --detect 0.3
wk bench ab --devices mbp --systems <staged-a>,<staged-b> --workspace mac-rel   # two builds already staged
wk bench ab --devices mbp --status     # the planted job, read over either install
wk bench ab --devices mbp --collect    # its result onto the task, then reported
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
(`lib/wk/pgo.py`). A board's WebKit at 2.52 or later is built the same way:
`wk sysimage webkit <image-preset> --commit <sha> --slot <s>` instruments, collects
with `wk bench run --collect` on the board, mixes and rebuilds.

The display mode is declared (`display`), held, and checked before the
restart and in every leg. Brightness is driven to minimum. What is on the
screen is asked of the window server, and anything wk did not put there
refuses the leg. `bench/quiet/macos.tsv` is the one table of what a
quiet Mac is; `wk quiesce status` reads it back off the machine.

**Quiesce and session, before any measurement**

```sh
wk quiesce on                           # background daemons paused, App Nap off, display held
wk quiesce session on                           # a compositor on the attached monitor
wk quiesce status                       # read off the machine, not off a record
wk quiesce off && wk quiesce session off
```

**Add a bench machine**

```sh
$EDITOR machines/<name>.conf            # kind=board (or mac, guest), ssh, bench_ssh,
                                        # driver, device, root, image_preset,
                                        # net, dtb, role
git add machines/<name>.conf && git commit
wk boot --list
```

Every machine is one `machines/<name>.conf`, named as the CLI names it, with a
`kind`: `build` or `peer` (a place), `board`, `mac` or `guest` (a bench
machine), or `bridge`. Values are literals. A bench role that is also a peer
names the peer (`ssh=<peer>`). A place whose `hostname -s`
is not its name says what it is (`hostname`): that is how its far
end knows which machine it is, however many share its home. A conf in
`~/.config/wk/machines/` sets keys over the shared one's, for this device only.

**Provision a bridge phone**

```sh
wk sysimage build recovery-pinephone        # Jumpdrive: 'wk sysimage write' it to a card, boot the phone
                                             # from it, and its internal storage appears on that machine
wk sysimage disks rpi5                       # which device the phone's storage is
wk machine setup tailnet-bridge-generic --disk rpi5:/dev/sda
                                             # writes the bridge image there, prints the hand steps,
                                             # waits for the phone, applies the role, prints the policy
wk machine setup tailnet-bridge-generic      # re-apply: renders the role here, the phone applies it
wk machine tailnet tailnet-bridge-generic    # the tailnet join alone, after setup --no-tailnet
wk machine status tailnet-bridge-generic     # its health check; no name: every bridge
wk machine rm tailnet-bridge-generic         # the role and its tailnet login go; postmarketOS stays
```

**An agent in a workspace**

```sh
wk ai claude bug-238                    # verifies the sandbox first; refuses if it fails
wk ai claude bug-238 -r                 # resume
wk ai pi bug-238                        # the pi agent
wk ai claude                            # inside a workspace: this one
```

An agent cannot push, commit or build directly. Pushing needs a key that is
not there (`wk key push`, below). Committing is walled: the checkout's `.git`
commit parts are mounted read-only under the agent. Building goes through
`wk build`: the build tools on `PATH` refuse an agent by name. `wk doctor
<ws>` measures all three from inside.

`wk new` installs both agents into the workspace (a macOS guest gets them at
its first `wk start`), and `wk ai` throws the push switch and starts the
session, nothing more; a workspace made without an agent is refused, naming
`wk rm` and `wk new`. pi needs node 22.19 or newer where it is made.

A Claude session on a terminal starts with Remote Control on, named after the
workspace, so claude.ai/code and the mobile app can join it. It needs the
claude.ai login, which `/login` in any session makes; where the inference
token authenticates the session (a build machine), it starts without Remote
Control and says so.

**`wk key`: every credential, one fleet**

```sh
wk key setup                            # deploy keys, then every credential this machine lacks
wk key check                            # one row per credential, what its issuer says now
wk key set github-pat                   # one by name; --replace rotates it
wk key set claude                       # the inference token, for build machines
wk key deploy --rotate                  # the deploy keys, revoked and reissued fleet-wide
```

`lib/credcheck.py` holds one rule per credential: what it must do and must
not do. Nothing is stored until it passes, and no verdict is remembered. A
credential is the fleet's: `wk key setup` asks every workstation what it
holds, the best working one wins, and it is put everywhere. The claude.ai
login is not one of them: the Claude CLI makes it (`/login` in a session)
and renews it itself, in `~/.config/wk/agent-rw`, the one directory a
workspace mounts read-write, so every workspace on the machine shares it.
`CLAUDE_CODE_OAUTH_TOKEN` is what a build machine gets instead.

**`wk key push`: publishing without the credentials inside**

```sh
wk key push on                              # asks once; ends every agent session first
wk key push status                          # asks the agent, not a record
wk key push off
```

The deploy keys live in an ssh-agent on the machine running the workspaces;
a workspace's ssh config names the socket, so ssh signs with a key it can
never read. The GitHub token and Bugzilla key go to the injector
(`container/proxy/github-inject.py`), which terminates TLS for those two
hosts and puts the credential on the request: a read always, a write only
while push is on. With push off a write is refused with 412 naming `wk key push
on`. A macOS guest gets the same through an ssh-agent on the host forwarded
per guest over its sshd on `tart exec`. A build box holds no deploy key and nothing forwards one to it,
so a push is made from the workstation and `wk key push status --on <box>`
says off: `wk pr open <ws>` fetches the box's branch into this machine's
mirror over ssh and pushes it from here, through the agent `wk key push on`
loads (on a macOS host, the one it runs for its guests). A ref a killed push
leaves in the mirror is `wk gc` rubble. A push on the box itself, `git push`
or `git-webkit pr`, is refused naming `wk pr open`.

**Housekeeping**

```sh
wk status                               # every workspace, task, machine and bench machine
wk doctor --all                         # this machine (its wk-tools and disk too) and every build machine
wk gc                                   # asks once, takes what loses no work, names the rest with what takes it
wk gc --purge-rubble                    # half-made workspaces nothing is creating, instrumented slots on a board
wk gc --purge-mirror                    # the mirror and every snapshot; refused with a live workspace
wk stop --tasks                         # everything running here, each by its own kill line
```

**Reprovision from scratch**

```sh
git clone https://github.com/justinmichaud/wk-tools ~/Development/wk-tools
cd ~/Development/wk-tools && ./setup && wk sync
```

Every machine the repo knows is in it. Nothing else is machine-specific but
what `wk key backup` captures and the credentials, which are never in git. On
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
back. EEPROM order is `sd-first` (`wk boot rpi4 --boot-order sd-first`). A second system
on the stick is `@second`; `wk boot rpi4 --system <id>` names one.

**rpi5 — `rpi5-usb`, a workstation with a bench stick.** The NVMe is never
written. Arming is a firmware mailbox one-shot (USB, then NVMe) that clears
after one use. EEPROM `BOOT_ORDER` stays `local`, the only evidence the
fallback is in place. Two systems on the stick are the firmware's own A/B: a
static `autoboot.txt` selects the second pair under `[tryboot]`. As a
workstation it is tuned by `host/linux/rpi5/rpi5-setup.sh` (run by `./setup`),
which holds the settings. A bench system runs a stock kernel, since customers ship one; an
overclock belongs to an `-oc` image preset's `config.txt.append`, never the
EEPROM, which both modes share.

Reading a medium the board is not booted from goes through the card helper
(`admin/wk-card-priv`, the driver's `medium_read`): read-only, three file names, one
partition number, bounded. Every system a write makes carries the helper;
installing this checkout's onto a running one is `wk machine setup <board>`.

**mbp — `mac-volume`.** The `WK Bench` APFS volume beside the host install.
Apple Silicon selects a startup volume only at the keyboard, so `wk boot mbp`
reports and stages and a person picks the volume. `wk sysimage write` refuses
a Mac's own disks.

**benchvm — `mac-guest`.** A Tart guest rehearsing the Mac path. Nothing
measured in it is comparable with hardware.

## Lifecycle

From a bare board to an automated A/B. The boards differ only in the
`--disk` spelling (`wk help hardware`).

1. **Declare it.** `machines/<name>.conf`; commit.
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
   wk sysimage write --from <rescue.wic.xz> --disk rpi5:/dev/mmcblk0 --rescue --image-preset webkit-2.52-yocto-rpi3-32
   wk sysimage write --from <sdcard.img>    --disk rpi5:/dev/mmcblk0@second --image-preset wpewebkit-2.38-buildroot-rpi3-32
   ```
4. **Boot the rescue.** Carry the card, power on; `<board>-rescue` joins the
   tailnet. On two media, `wk boot <board> --boot-order <order>` and the
   bench write from the rescue (`--disk rpi4:/dev/sda`).
   No card is carried after this.
5. **Boot the bench system once.** `wk boot <board>`; `<board>-bench` joins;
   it hands the board back after `IMG_WATCHDOG` seconds unless `--keep`.
6. **The A/B.** `wk bench ab wpe:1725 --devices rpi3 --bits 32 --dry-run`, then
   without it. By hand, the same steps: `wk sysimage webkit` twice, `wk boot
   --keep`, `wk bench deploy` twice, `wk bench run --ab`, `wk bench report`.

**A buildroot configuration of your own** is an external defconfig under
`image/buildroot/external/configs/`, named by the image preset's `BR_DEFCONFIG`.
A bench image needs `wpa_supplicant`, OpenSSH (dropbear refuses ed25519) and
a kernel with TUN and netfilter; copy a 2.38 defconfig. A 32-bit userspace on
a 64-bit machine is a yocto multilib image preset (`YOC_MULTILIB`).

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
`WK_EXTRA_ENV`, `WK_CCACHE_DIR`, `WK_CCACHE_MAXSIZE`, `WK_DRIVER`,
`WK_REMOTE_MAX_JOBS`, `WK_MB_PER_JOB`, `WK_PGO_COLLECT_TIMEOUT`.

**How much of the machine** — `WK_MAX_JOBS`, `WK_LOAD`, `WK_AVAIL_MB`,
`WK_RESERVE_CORES`, `WK_RESERVE_MB`, `WK_HEADLESS_RESERVE_CORES`,
`WK_HEADLESS_RESERVE_MB`, `WK_CGROUP_CORES`, `WK_CGROUP_MB`,
`WK_BUILD_MACHINE`, `WK_BUILD_DISK_GB`.

**Where state lives** — `WK_LOCAL_STORE`, `WK_REMOTE_STORE`, `WK_LOCK_DIR`,
`WK_MARKER`, `WK_REMOTE_MARKER`, `WK_IMAGE_MARKER`,
`WK_SESSION_MODE_FILE`, `WK_MIRROR_BRANCHES`, `WK_TART_CACHE_GB`, `WK_CMD`.
What lives there has six parts, each with one name in code, help and prose
(`lib/wk/store.py`):

- the **store**, a place's data: `ws/`, `base/`, `cache/` under `$WK_STORE`;
- the **records**, what outlives a command: `task/`, `log/` and the locks
  (`~/.local/state/wk/locks`, or `WK_LOCK_DIR`);
- the **mirror**, `git/WebKit.git`;
- the **snapshots**, `base/<id>`, the clones a workspace starts from;
- the **keyring**: `secrets/`, with `agent-rw/` and `push-keys/` beside it;
- the **runtime**, the broker socket (`$XDG_RUNTIME_DIR/wk/broker.sock`,
  `/run/wk/broker.sock` in a workspace, or `WK_BROKER_SOCKET`).

On a macOS workstation the store is the podman machine's, so this machine's
own records and mirror go under `~/.local/state/wk` and its keyring under
`~/.config/wk/secrets`.

**The container place** — `WK_SDK`, `WK_SDK_IMAGE`, `WK_CONTAINER_USER`,
`WK_TOOLS_SRC`, `WK_MACHINE`, `WK_MIRROR` (the mirror's path
inside a container).

**The macOS guest** — `WK_VM_IMAGE`, `WK_VM_BASE`, `WK_VM_USER`,
`WK_VM_PASSWORD`, `WK_VM_CPUS`, `WK_VM_MEM_MB`, `WK_VM_BASE_CPUS`,
`WK_VM_BASE_MEM_MB`, `WK_VM_DISK_GB`, `WK_VM_DISPLAY`, `WK_VM_SUBNET`,
`WK_VM_PROXY_ADDR`, `WK_VM_PROXY_PORT`, `WK_HOST_FREE_WARN_GB`,
`WK_VM_SHELLS_WARN`, `WK_VM_MEM_FREE_WARN_PCT`, `WK_VM_SWAP_WARN_MB`,
`WK_VM_FORCE` (crosses a stale base or a blocked desktop, recorded),
`WK_VM_STORE` (the guests' store and records, apart from the container's).

**The Mac's bench install** — `WK_BENCH_USER`, `WK_BENCH_VOLUME`.

**Credentials and the tailnet** — `WK_PUSH_AGENT_SOCK`, `WK_PUSH_PAT_FILE`,
`WK_PUSH_READ_PAT_FILE`, `WK_PUSH_BUGZILLA_KEY_FILE`, `WK_TS_AUTHKEY`,
`WK_TS_API_SECRET`, `WK_IMAGE_KEY`, `WK_ANY_ROOT`, `WK_TAILSCALE_TIMEOUT`,
`WK_SOFTNET_BIN`, `WK_PROBE_SECONDS` (how long a build machine's probe may take),
`WK_SSH_TIMEOUT` (ssh's connect timeout on every hop, default 10).

**Waiting and reporting** — `WK_READY_TIMEOUT`, `WK_READY_WAIT`,
`WK_POLL_SECONDS`, `WK_HEARTBEAT_SECONDS`, `WK_STATUS_PORT`,
`WK_STATUS_INTERVAL`, `WK_BROKER_SOCKET`, `WK_SCREEN_EXPECTED`,
`WK_BENCH_RUNNER_REF`.

**The rest, one line each**

- `WK_BENCH_ADMIN` the volume owner that authorises the bench install (default: the invoking user).
- `WK_BENCH_GUEST` the name of the bench guest workspace (default `wk-bench`).
- `WK_BENCH_NEED_GB` free GB the bench volume's container must have before the volume is added.
- `WK_BENCH_WIRED` set when the bench install is on ethernet, so no Wi-Fi credential is copied.
- `WK_HOST_FREE_MIN_GB` free GB on the host below which a macOS guest is refused (`WK_HOST_FREE_WARN_GB` only warns).
- `WK_HOST_SECRETS` the macOS host's keyring (default `~/.config/wk/secrets`).
- `WK_IMAGE_HOST` the address the bench image is reached at, ahead of the fleet peer lookup.
- `WK_JOB_PID_TRIES` polls a watched job gets to announce its pid (default 900).
- `WK_MACHINES_DIR` the directory of machine confs (default `machines/`).
- `WK_MAC_BENCH_HOLD` seconds the Mac bench autorun stays up after its last job (default 900).
- `WK_MAC_BENCH_SSH` the ssh destination of the Mac's bench install, over the conf's `bench_ssh`.
- `WK_MAC_BENCH_TOOLS` where the wk-tools checkout is on the Mac's bench install.
- `WK_PMOS_HOST` the postmarketOS build host, over the image preset's `PMO_BUILD_HOST`.
- `WK_PMOS_ROOT` the postmarketOS build root on that host (default `~/wk-pmos`).
- `WK_QUIESCE_STATE` the directory quiesce records live in (default `~/.local/state/wk/quiesce`).
- `WK_NTFY_API` the ntfy server notifications are published to (default `https://ntfy.sh`).
- `WK_SCREEN_WATCH_SECONDS` how often the screen watch samples (default 10).
- `WK_STORE_DEFAULT` the machine's own store when it differs from `WK_STORE`; its secrets live under it.
- `WK_TAILNET_API` the tailnet API endpoint.
- `WK_TAILNET_TAG` the tag a joining node advertises (default `tag:wk`).
- `WK_VM_CLOCK_SKEW` seconds a guest's clock may differ before it is reset (default 30).
- `WK_VM_MAX` running guests at which starting another is refused (default 2).
- `WK_VM_SHARE` starts a guest that does not fit the memory envelope anyway.
- `WK_VM_UNFILTERED` boots a guest with the open network, without the egress filter.

## Where the rest is

`wk help` prints this file, `wk help <topic>` one section (`wk help
lifecycle`, `wk help hardware`, `wk help push`), `wk <cmd> -h` one command.
CLAUDE.md is for anyone editing this repository. docs/PLAN.md is the order in
which the first half of this file becomes true.
