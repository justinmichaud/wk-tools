# HANDOFF — several lanes, several machines, one experiment

README.md ("One lane per profile, several lanes at once", "The boards'
profile-guided build") is the design.

The first run that exercises the whole planning path is **one A/B on rpi5 with
its two arms built on two machines** -- the base leg on moose, the patched leg
on tolken. One board, so nothing waits on rpi3 or rpi4; two lanes on two
machines, so the scheduler, the routing and the cross-machine deploy are all
under test at once. What is owed for that run comes first below.

A lane per arm is *not* owed for it: two machines give two lanes, so the two
arms are two checkouts already.

## What that run needs

- [ ] `wk ab` puts every step on the machine it was typed on (`RUNS_ON`,
      cmd/ab). The graph already carries a machine per step, but lib/sched.py
      only prints it (`s.machine or "here"`) -- nothing routes by it -- so the
      routing belongs in the step's own command, which `<profile>@<machine>`
      now expresses. cmd/ab needs to be told which machine each arm builds on
      and to spell it that way [no hardware needed]
- [ ] the two arms would serialise even so: both slot steps declare
      `holds lane:<profile>`, one resource, so the scheduler runs one at a
      time. Two lanes on two machines are two resources, and the key has to
      say which [no hardware needed]
- [ ] `image:<profile>` is one step for both arms, and each machine's lane
      needs its own image and toolchain before a slot can be built in it
      [no hardware needed]
- [ ] the done-predicates answer for this machine only: `image_slot_holds`
      (image/pgo.sh) and `ab_image_path` (cmd/ab) both resolve `wk_ws_dir`
      here, so a slot already built on moose reads as missing and is built
      again [no hardware needed]
- [ ] `wk pi deploy` runs where it is typed. rpi5 takes `base` from moose's
      lane and the patched slot from tolken's, and a deploy streams the bytes
      out of the lane that built them, so each must run on its own machine.
      Today it refuses anywhere else and names the machine
      (`<profile>@<machine>`, cmd/pi); driving it there is what is left.
      Making it a `name=derived` command reuses the dispatcher's delegation,
      and moves the board claim onto the far machine -- see the next item
      [needs a board]
- [ ] a board is claimed in the claiming machine's own store (`device_hold`,
      lib/task.sh), so two machines deploying to rpi5 each hold it and neither
      sees the other. Inside one `wk ab` the scheduler's `holds device:rpi5`
      serialises them and the gap does not show; two drivers at once is where
      it does [decision]
- [ ] moose has no lane for this profile any more (2026-09-16: its
      `yocto-webkit-2.52-yocto-rpi5-64` is gone). It needs the lane created
      and built through `toolchain` before it can hold an arm -- hours -- and
      its mirror needs the branch the lane checks out [needs moose]
- [ ] disk, measured 2026-09-16. A yocto lane is ~84 GB of build tree plus the
      machine's shared caches (tolken: 21 GB downloads, 13 GB sstate; moose's
      sstate reached 118 GB across four lanes), and `disk_admit` refuses an
      image or toolchain stage under **120 GB free** (60 + 60, `rm_work` being
      off by default; 60 with it on, which image/yocto.sh records as still
      peaking at 79 GB). A slot build wants 25 GB, a pgo-mix 2 GB. So one lane
      is ~120 GB to start and ~120 GB to hold, and three profiles at once on
      one machine is ~400 GB.

      moose has 409 GB free and holds that. tolken's workspaces live in the
      podman VM, which was 200 GB -- one lane plus caches is already ~118 GB of
      it -- so `host/macos/machine.sh` now declares 500 GiB and `./setup`
      grows an existing machine to it rather than applying the figure only at
      `podman machine init`. The disk is sparse, so it costs what is written.
      What is left is the host behind it: 926 GB with 45 GB free, of which
      ~346 GB is Tart (151 GB re-fetchable pulled-image cache, 125 GB guest
      `vmcheck`, 70 GB golden base). Growing the VM does not create host space
      [decision, then the growth]
- [ ] rpi5 answers nothing. It has to be carrying a bench system built from
      the 2.52 profile its conf names before any deploy or bench reaches it
      [needs a hand at the board]

## Then, for the rest of the fleet

- [ ] a lane per arm, so both arms of one profile build at once on **one**
      machine. `_ws_profile` (cmd/sysimage) already recovers the profile from
      a suffixed lane name, so `yocto-<profile>-<arm>` is a lane `ls` and
      `write --from` read. What is left is that `yocto_ws_default`
      (image/yocto.sh) names a lane by its profile alone, and `image_slot_dir`
      / `image_pgo_dir` (lib/image.sh) find a slot by profile rather than by
      lane -- which reaches cmd/pi, cmd/ab, image/pgo.sh and image/yocto.sh
      [no hardware needed]
- [ ] rpi3 and rpi4 each need a bench system from the 2.52 profile its conf
      names; rpi4 at 2.52 also loses legs for the reasons docs/HANDOFF-boot.md
      lists (KMS intermittent, EEPROM still usb-first, the stick dropping off
      the bus) [needs a hand at the boards]
- [ ] a lane for a release profile needs that branch in its machine's mirror:
      tolken carried `main` only and the checkout failed until
      `WK_MIRROR_BRANCHES="main webkitglib/2.52" wk sync` carried it in. The
      refusal names that; whether the build should carry it in itself is a
      decision [decision]

## Found by running it

- [ ] a test run and a build on one machine starve each other, and only one
      direction is guarded. The container tests skip while a build is on the
      machine's books (`requires_podman_vm`, tests/support.py); nothing stops
      a test run from taking the machine out from under a build. On
      2026-09-16 a full `wk selftest` beside a cross build took the podman VM
      to 1536MB free and the build's own floor check killed it. Whether
      `wk selftest` should refuse to start beside a build is a decision
      [decision]
- [ ] build/mem-watchdog.sh sees the ninja half of a slot build and not the
      bitbake half. The same stage, twice on 2026-09-16: killed on the machine
      floor mid-bitbake at `peak 96MB of budget 12800MB` while saying "this
      build is using 70MB of it", and finishing its ninja phase at `peak
      12663MB of budget 15360MB`. `_tree` walks descendants in eight passes,
      so what it misses is what bitbake puts outside the tree -- its cooker
      detaches. While that holds, the budget branch cannot fire during the
      long phase, and the floor branch kills a build whose measured footprint
      says it is innocent [no hardware needed]
- [ ] `tests/test_vm_desktop.py`'s `test_the_refusal_can_be_crossed_on_purpose`
      failed once in a full run under a machine-sized build -- the function
      returned 0 and its `WK_VM_FORCE=1` line reached no stderr -- and passes
      alone and in discover order. Unexplained, so not yet a flake anyone
      should trust [no hardware needed]
