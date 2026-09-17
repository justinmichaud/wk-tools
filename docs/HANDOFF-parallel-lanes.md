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
--build-on tolken --base <head^>`: one machine, so the two arms build in turn.

- [ ] the A/B graph runs no toolchain step, so the SDK is built under a budget
      sized for something else and the memory watchdog kills it. The `webkit`
      stage books `WEBKIT_JOBS * YOCTO_WEBKIT_MB_PER_JOB` = 6 * 2560 =
      15360MB (`yocto_stage_budget`, image/yocto.sh) -- WebCore's unified
      sources, per that constant's own comment -- but `build-webkit
      --cross-target` has `cross-toolchain-helper` bitbake the whole
      nativesdk stack under that same guard. Measured 2026-09-17: peak
      15406MB, killed 46MB over, at bitbake task 7658 of 13213, where every
      other stage books the machine envelope (17899MB) and the image stage
      peaked 10833MB. Either the graph runs `--stage toolchain` first (it is
      machine-sized, and it exists) or the webkit stage books the larger of
      the two. `wk status` also shows `[ ] toolchain` unticked while exactly
      that work runs under `[>] webkit` [no hardware needed]
- [ ] a killed `webkit` stage leaves the lane's checkout mid-checkout and no
      wk command converges it. After the kill above: HEAD at the slot's
      commit, 4819 tracked files modified and 741 untracked across
      LayoutTests/Source/JSTests, and every later command refused with "your
      local changes would be overwritten by checkout". Nothing in `wk` repairs
      it -- it took `git checkout -f` and `git clean -fd` by hand, which is
      the crash-only rule broken: a re-run has to converge [no hardware needed]
- [ ] running the `webkit` stage in a lane destroys the image the `image`
      stage built. `wk sysimage ls` reported the image ready at 03:52 and the
      A/B's first step read it as already done; after two webkit stages in
      that lane the `build/image/` directory is gone and `ls` reports none
      (2026-09-17). So `wk sysimage holds <profile>` flips back to no once a
      slot is built, and a re-run of `wk ab` rebuilds the image every time
      [no hardware needed]
- [ ] a deploy routed to a macOS workstation's podman machine holds no key of
      its own: the podman VM is `tag:wk` with an empty `~/.ssh` and no agent
      socket, and the workspace container has no `ssh` binary at all
      (measured 2026-09-17). What it reaches a board by is Tailscale SSH, and
      the tailnet grants `tag:wk -> tag:wk`: `rpi5-bench` is `tag:wk`, so a
      deploy to a board in *bench* mode should log in with no key, and one to a
      board in host mode (`rpi5`, `tag:workstation`) cannot. Untried either
      way. Measure it before giving anything a key [needs a board]
- [ ] rpi5 carries no bench system. The image is built (`wk sysimage ls`:
      yocto-webkit-2.52-yocto-rpi5-64, ready, 2026-09-17), it answers over ssh
      in host mode, and its USB stick is attached and empty -- 223.6 GB on
      /dev/sda, no partition table -- so nothing needs a hand at the board:

          wk sysimage write --from vm:<the path wk sysimage ls prints> \
              --profile webkit-2.52-yocto-rpi5-64 --disk rpi5:/dev/sda
          wk boot rpi5

      The write retires the stale `rpi5-bench` node itself (`tailnet-api` is
      stored and healthy). A spent arming record is still on the board: `wk
      boot rpi5 --disarm` [needs the write to be run]
- [ ] the routed deploy and the collection have never run against a board.
      The graph names them and the tests cover the routing; nothing has put
      bytes on a board through it [needs a board]
- [ ] a second machine with a lane, for the run whose two arms overlap. moose
      has no lane at all: its store is `~/.local/share/wk` (root owns
      /var/lib/wk there) and `ws/` holds only `m` (2026-09-16), and it answers
      nothing over ssh. Its yocto caches survive -- 118 GB sstate, 26 GB
      downloads, 457 GB free -- so the rebuild is fed by sstate rather than
      starting cold [needs moose]
- [ ] `machine_armed_barrier` (boot/machines.sh) cannot tell "probed, and the
      board is not in host mode" from "could not probe": `b_probe ... || true`
      leaves MODE unset and the barrier returns 0 either way. A deploy routed
      to a podman machine reaches the bench node and not the host-mode one --
      the tailnet grants `tag:wk -> tag:wk`, and a board in host mode is
      `tag:workstation` (measured 2026-09-16) -- so the armed check is skipped
      there in silence, which is the one thing a barrier may not do
      [no hardware needed]

## Then, for the rest of the fleet

- [ ] rpi3 and rpi4 answer nothing at all: rescue and bench nodes alike time
      out on ssh (2026-09-16), so unlike rpi5 they need power or a cable
      before anything else. Each then needs a bench system from the 2.52
      profile its conf names -- written from its own rescue once that is up,
      the way rpi5 writes its own stick. rpi4 at 2.52 also loses legs for the
      reasons docs/HANDOFF-boot.md lists (KMS intermittent, EEPROM still
      usb-first, the stick dropping off the bus) [needs a hand at the boards]

## Found by running it

- [ ] a yocto stage records no exit status, so `wk status` reports a build
      that succeeded as `died`. The toolchain stage wrote "stage 'toolchain'
      done", installed the SDK and left `environment-setup-cortexa76-poky-linux`
      in the lane; `wk status` read `died -- no exit recorded` (2026-09-17).
      The same record shape hid a failure earlier the same night, where the
      bitbake server and a runaway child outlived the driver and `wk sysimage
      build ... --stop` answered "no 'image' build is running" while both were
      still in the container -- `wk stop <ws>` is what cleared them. Two rules
      broken: the record carries no exit status the driver had, and a stop
      believes the record over the machine [no hardware needed]
- [ ] `wk sysimage write --from <profile>` cannot find an image built in this
      machine's podman VM, and refuses with "no workspace here has built it
      yet" naming the build that already ran. `write` is `where=host` and its
      `_image_path` reads the host's own store; `ls` is `where=dynamic` and
      prints the image (2026-09-17). The spelling that works is `--from
      vm:<path> --profile <configuration>`, which the refusal knows enough to
      name [no hardware needed]
- [ ] a write's dry run does not mention retiring a stale tailnet node.
      `_tailnet_name_preflight` (cmd/sysimage) is in the real-write branch
      only, so `--dry-run` neither performs nor prints a state change that
      deletes a node from the tailnet [no hardware needed]
- [ ] `--devices rpi5-64 --release 2.52` is refused for naming two images, and
      the remedy offered is `--devices rpi5-32 or rpi5-64` -- the width that
      was already given. The choice is `--builder` (cmd/ab, `ab_profile_for`)
      [no hardware needed]
- [ ] a pull request against `main` measured on a 2.52 image guesses its base
      as the merge-base with `webkitglib/2.52`: 15374 commits for PR 74102, so
      the barrier refuses and every such run needs `--base <head^>` by hand.
      Either the guess reads the PR's own base branch, or the refusal names
      `--base <head^>` rather than `--base <sha>` [no hardware needed]
- [ ] `tests/support.py` pops `XDG_STATE_HOME` rather than pointing it at a
      scratch directory, so a host command that records a task writes into the
      real one -- `wk_record_dir` (lib/store.sh) sends a macOS workstation's
      records there, and runs of the suite left 85 records under
      `~/.local/state/wk/task` before they were deleted by hand. The shape is
      `NO_SECRETS`/`NO_REGISTRY` beside it; it moves the baseline for every
      test, so it is a decision rather than a patch [decision]
- [ ] the same isolation gap makes a test go red for as long as a real lane
      exists: `test_lane_routing`'s `test_an_image_nothing_has_built_is_no`
      asks `wk sysimage holds webkit-2.52-yocto-rpi5-64` against the machine's
      own store, and answers `yes` once that image is built (2026-09-17). The
      assertion is about a lane nothing has built, so it needs a store of its
      own, not a profile no one uses [no hardware needed]
- [ ] `cmd/profile` asks moose over ssh three times before it refuses an
      argument. Invoked directly -- as `tests/test_profile_debug.py` does, to
      test a refusal that needs no workspace -- `./cmd/profile --process ui
      --dry-run` prints three "could not ask moose over ssh ... Operation timed
      out" warnings and outlives a 30s timeout while moose is down
      (2026-09-17); three tests error on it. Resolving a name should not walk
      the fleet, and a reach that can block should be capped the way
      `wk_tailscale_peers` caps its own (lib/reach.sh).
      The same unbounded reach makes the reporting commands unusable exactly
      when they are wanted: with moose down and a build running, `wk status
      <ws>` and `wk logs <ws>` both outlived a 300s wait (2026-09-17), so
      there is no way to read a running build's progress through `wk`
      [no hardware needed]
- [ ] `tests/test_ai_inside.py`'s `test_every_probes_verdict_is_reported`
      wants a `commit_wall` verdict and the run reports nine probes without
      one (push_here, github_api, bugzilla_api, github, allowlist,
      off_allowlist, isolation, no_credentials_inside, gitwebkit_setup). It
      passed in one full run and failed in the next, so what decides whether
      that probe is in the set is not yet known [no hardware needed]
- [ ] `tests/test_vm_desktop.py`'s `test_the_refusal_can_be_crossed_on_purpose`
      failed once in a full run under a machine-sized build -- the function
      returned 0 and its `WK_VM_FORCE=1` line reached no stderr -- and passes
      alone and in discover order. Unexplained, so not yet a flake anyone
      should trust [no hardware needed]
- [ ] the plan costs one routed `wk sysimage holds` per done-predicate, and on
      a macOS workstation each is forwarded into the podman machine over ssh:
      0.8-1.1s each against a lane that exists (2026-09-17), five per board.
      It grows with the graph, so what is owed is one question per lane rather
      than one per step [no hardware needed]
- [ ] `wk selftest` refuses to start beside a build and nothing refuses a
      second `wk selftest`. Two runs contend for the podman machine the same
      way, and one was started beside another during this work. What it costs
      is wall-clock assertions failing in the loaded run and nowhere else:
      `test_interrupt`'s `test_sigint_during_task_wait_exits_promptly` (a 10s
      bound) and `test_wk_overrides_cmd2`'s
      `test_timeout_and_interval_are_both_read` (3 polls where it wants 4),
      both 2026-09-16, both passing 5/5 alone and under four spinning cores
      [no hardware needed]
