# HANDOFF — several lanes, several machines, one experiment

README.md ("One lane per profile, one build per machine", "The boards'
profile-guided build") is the design.

## The vocabulary this is to be rebuilt in

The words below are the ones to use; each line that differs from the tree is
owed work, and the whole of it is one decision because "target" already names
something else.

- **perf task** — a name, a timestamp, a description, and a PR, commit or
  branch to measure. Its workspaces are deletable once its results are in.
  `wk ab`'s bench task is this without the description.
- **target** — a device configuration: rpi3, rpi4, rpi5, macOS. The tree calls
  this a device or a board, and spends "target" on the *execution* target
  (`targets/hosts/*.conf`, `ws_target`, `default_target`, `where=` in every
  dispatcher line). Renaming one requires renaming the other [decision]
- **workspace** — one per (perf task, target). The tree's lane is one per
  *profile*, so two perf tasks on one board share a checkout and a build
  directory, and neither can be deleted when one completes [decision]
- **slot** — one WebKit beside the image, named for its arm and its phase
  (`A-PGO-collect`, `A-Production`). The tree names them `base`/`pr<n>` and
  `-instr`; the concept matches, the spelling does not
- **PGO profile** — as in the tree
- **time profile** — a samply or sysprof profile. The tree has these (the
  warmup round, `wk profile`) and no one name for them
- **coordinator** — the machine running the perf task: it creates the
  workspaces, schedules them and reports. Its state is written and syncable at
  any moment and synced before anything begins, so it can be killed and
  restarted on another machine. The tree has no such concept: `wk ab` runs the
  scheduler in the process it was typed in, records under that machine's own
  bench directory, and `--detach` survives the terminal but not the machine
- a perf task's raw results and first-run profiles live on one machine, and
  `wk bench ls` names them wherever they are. Today they are wherever the A/B
  was typed and nothing walks the fleet for them

## What one A/B on rpi5 still needs

The run is `wk ab 74102 --devices rpi5-64 --release 2.52 --builder yocto
--build-on tolken`: one machine, so the two arms build in turn. (`--base` is
no longer needed by hand; the base comes off the pull request's own base
branch.)

- [ ] rpi5 carries no bench system, and the image it would be written from is
      gone: the lane holds none (`wk sysimage holds webkit-2.52-yocto-rpi5-64`
      answers no, 2026-09-21). Its USB stick is attached and empty -- 223.6 GB
      on /dev/sda, no partition table -- so once a lane has built the image
      again nothing needs a hand at the board:

          wk sysimage write --from webkit-2.52-yocto-rpi5-64 \
              --disk rpi5:/dev/sda
          wk boot rpi5

      The write retires the stale `rpi5-bench` node itself (`tailnet-api` is
      stored and healthy). A spent arming record is still on the board: `wk
      boot rpi5 --disarm` [needs the image built]
- [ ] a deploy routed to a macOS workstation's podman machine holds no key of
      its own: the podman VM is `tag:wk` with an empty `~/.ssh` and no agent
      socket, and the workspace container has no `ssh` binary at all
      (measured 2026-09-17). What it reaches a board by is Tailscale SSH, and
      the tailnet grants `tag:wk -> tag:wk`: `rpi5-bench` is `tag:wk`, so a
      deploy to a board in *bench* mode should log in with no key, and one to a
      board in host mode (`rpi5`, `tag:workstation`) cannot. Untried either
      way. Measure it before giving anything a key [needs a board]
- [ ] the routed deploy and the collection have never run against a board.
      The graph names them and the tests cover the routing; nothing has put
      bytes on a board through it [needs a board]
- [ ] a second machine with a lane, for the run whose two arms overlap. moose
      answers again over ssh (2026-09-21) and has no lane at all: its store is
      `~/.local/share/wk` (root owns /var/lib/wk there) and `ws/` holds only
      `m`. Its yocto caches survive -- 118 GB sstate, 26 GB downloads, 457 GB
      free -- so the rebuild is fed by sstate rather than starting cold
      [needs moose]

## Then, for the rest of the fleet

- [ ] rpi3 and rpi4 answer nothing at all: rescue and bench nodes alike time
      out on ssh (2026-09-16), so unlike rpi5 they need power or a cable
      before anything else. Each then needs a bench system from the 2.52
      profile its conf names -- written from its own rescue once that is up,
      the way rpi5 writes its own stick. rpi4 at 2.52 also loses legs for the
      reasons docs/HANDOFF-boot.md lists (KMS intermittent, EEPROM still
      usb-first, the stick dropping off the bus) [needs a hand at the boards]

## Found by running it

- [ ] an image stage can wedge in bitbake with no output and nothing gives up
      on it: one ran from 00:24 to 06:49 with `libxml-sax-perl:do_package_qa_setscene`
      as its only active task and "no events for 22800s" as its last 40 lines
      (measured 2026-09-17, read back 2026-09-21). `yocto_wait` warns at
      WK_STALL_SECONDS and then waits for as long as it takes, deliberately --
      "a bitbake task can be silent for a long time" -- so a wedge and a long
      link are the same thing to it. What tells them apart is bitbake's own
      "Bitbake still alive (no events for Ns)" line naming the same task every
      time: after some multiple of that, the stage is wedged and the run is
      worth giving up on rather than watching for six hours [no hardware needed]
- [ ] resolving a workspace name still walks the fleet once per ask, and a
      command asks several times: `wk profile` resolves for the config's
      default, for the target and for the mode, and each walk probes every
      machine -- 9 ssh probes across 3 machines for one invocation (measured
      2026-09-21). Each probe is now bounded (WK_PROBE_SECONDS, 20s), and an
      argument refusal no longer pays for any of them, so what is left is the
      cost of a name resolved three times. A per-process memo does not reach
      it: every caller resolves inside a `$( )`, whose memo dies with the
      substitution. The shape that does is the probe file every subshell can
      read -- `WK_PREFETCH_DIR` (targets/remote.sh), which `wk status` and `wk
      remotes` already set up and nothing else does; what is owed is one
      invocation-wide probe directory, created once and removed at exit, and
      `_remote_probe_try` writing its answer into it [no hardware needed]
