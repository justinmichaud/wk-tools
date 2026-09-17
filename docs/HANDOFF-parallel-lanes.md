# HANDOFF — several lanes, several machines, one experiment

HUMAN:
First, the nomenclature is confusing. Here is my desired higherarchy

Perf task: A named and timestamped task with a description, with a PR or commit or branch to test, at some point in time. Once the results are in, the task is complete and we can delete its workspaces.
Target: a target device configuration (rpi3, 4, 5 or macOS)
Workspace: one workspace per (perf task, target)
Slot: One of many WebKit installs on the same base image, or one of many builds in one workspace. Ex: A-PGO-collect, A-Production, B-PGO-collect, etc
PGO Profile: a PGO profile
Time Profile: a samply or sysprof profile

Coordinator: The machine running the wk task to monitor the creation of workspaces, schedule their completion, etc. This state should be written and sync-able at any time, and should be synced before beginning anything to allow the coordinator to be killed and restarted on a different machine.

Perf tasks should store all raw results plus their first-run profiles on one machine and wk bench should allow listing them no matter where they are stored.

The primary task today is to test that the scheduler is capable of scheduling a run in the case when we do not bulid in parallel. This bulid should be chosen to run on tolken under podman, targeting rpi5, with ToT as the base configuration (so ToT vs ToT + https://github.com/WebKit/WebKit/pull/74102 through a profile collection and pgo production bulid of WebKit for both the A and the B slot). Debug any issues you encounter and do not stop until you have done as much as you can so I can wake up to good results.

/HUMAN

Consider the following handoff items, but remember the human guidance above takes priority.

README.md ("One lane per profile, one build per machine", "The boards'
profile-guided build") is the design.

The run this is for is **one A/B on rpi5 with its two arms built on two
machines**: `wk ab <spec> --devices rpi5-64 --build-on <a>,<b>`. One machine
builds one thing at a time, so two machines are what makes the two arms
overlap.

## What that run still needs

- [ ] a second machine with a lane. moose has no lane at all: its store is
      `~/.local/share/wk` (root owns /var/lib/wk there) and `ws/` holds only
      `m` (2026-09-16). Its yocto caches survive -- 118 GB sstate, 26 GB
      downloads, 457 GB free -- so the rebuild is fed by sstate rather than
      starting cold [needs moose]
- [ ] a deploy routed to a macOS workstation's podman machine reaches a board
      and cannot log in to it. That machine has no ssh private key
      (`~/.ssh/id_ed25519` absent, 2026-09-16) and a board's image bakes in
      the *workstation's* public key (`driving_key`, cmd/sysimage); the
      tailnet's ssh rules name `autogroup:member` and `tag:workstation` as
      sources, and the machine is `tag:wk`. Either it is given the key the
      images already accept, or the tailnet grants `tag:wk` ssh to
      `autogroup:tagged` [decision]
- [ ] `machine_armed_barrier` (boot/machines.sh) cannot tell "probed, and the
      board is not in host mode" from "could not probe": `b_probe ... || true`
      leaves MODE unset and the barrier returns 0 either way. A deploy routed
      to a podman machine reaches the bench node and not the host-mode one --
      the tailnet grants `tag:wk -> tag:wk`, and a board in host mode is
      `tag:workstation` (measured 2026-09-16) -- so the armed check is skipped
      there in silence, which is the one thing a barrier may not do
      [no hardware needed]
- [ ] rpi5 carries no bench system. It answers over ssh in host mode (booted
      2026-09-16T16:29:37Z) and its USB stick is attached and empty -- 223.6 GB
      on /dev/sda, no partition table -- so nothing here needs a hand at the
      board: it writes its own stick over ssh (`wk sysimage write --from
      <profile> --disk rpi5:/dev/sda`, then `wk boot rpi5`). What it waits on
      is a built image. A spent arming record is still on it: `wk boot rpi5
      --disarm` [needs a built image]
- [ ] the routed deploy and the collection have never run against a board.
      The graph names them and the tests cover the routing; nothing has put
      bytes on a board through it [needs a board]

## Then, for the rest of the fleet

- [ ] rpi3 and rpi4 answer nothing at all: rescue and bench nodes alike time
      out on ssh (2026-09-16), so unlike rpi5 they need power or a cable
      before anything else. Each then needs a bench system from the 2.52
      profile its conf names -- written from its own rescue once that is up,
      the way rpi5 writes its own stick. rpi4 at 2.52 also loses legs for the
      reasons docs/HANDOFF-boot.md lists (KMS intermittent, EEPROM still
      usb-first, the stick dropping off the bus) [needs a hand at the boards]

## Found by running it

- [ ] `tests/support.py` pops `XDG_STATE_HOME` rather than pointing it at a
      scratch directory, so a host command that records a task writes into the
      real one -- `wk_record_dir` (lib/store.sh) sends a macOS workstation's
      records there, and runs of the suite left 85 records under
      `~/.local/state/wk/task` before they were deleted by hand. The shape is
      `NO_SECRETS`/`NO_REGISTRY` beside it; it moves the baseline for every
      test, so it is a decision rather than a patch [decision]
- [ ] `tests/test_vm_desktop.py`'s `test_the_refusal_can_be_crossed_on_purpose`
      failed once in a full run under a machine-sized build -- the function
      returned 0 and its `WK_VM_FORCE=1` line reached no stderr -- and passes
      alone and in discover order. Unexplained, so not yet a flake anyone
      should trust [no hardware needed]
- [ ] the plan costs one routed `wk sysimage holds` per done-predicate, and on
      a macOS workstation each is forwarded into the podman machine over ssh:
      3.5s each, ~30s for one board's five. It grows with the graph -- three
      boards is fifteen -- so what is owed is one question per lane rather
      than one per step, or a cheaper hop [no hardware needed]
- [ ] `wk selftest` refuses to start beside a build and nothing refuses a
      second `wk selftest`. Two runs contend for the podman machine the same
      way, and one was started beside another during this work. What it costs
      is wall-clock assertions failing in the loaded run and nowhere else:
      `test_interrupt`'s `test_sigint_during_task_wait_exits_promptly` (a 10s
      bound) and `test_wk_overrides_cmd2`'s
      `test_timeout_and_interval_are_both_read` (3 polls where it wants 4),
      both 2026-09-16, both passing 5/5 alone and under four spinning cores
      [no hardware needed]
