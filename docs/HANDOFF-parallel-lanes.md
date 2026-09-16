# HANDOFF — several lanes, several machines, one experiment

README.md ("One lane per profile, several lanes at once", "The boards'
profile-guided build") is the design. This is what is owed to drive one A/B
across rpi3, rpi4 and rpi5 from one command, with more than one machine
building.

## Routing: one command drives a lane wherever it is

- [ ] which machine a *new* lane goes on cannot be said. The derived name
      resolves to whichever machine already holds a lane of it, so a build
      typed here went to moose (2026-09-16) and refused on its disk; a second
      machine needs `--workspace <name>` to get a lane of its own. Either
      `wk sysimage build` takes `--target`, being the command that creates the
      lane, or the derived name carries the machine [no hardware needed]
- [ ] `wk pi deploy <profile> <board>` with it: a slot's bytes are in the
      workspace that built it, so the deploy runs from that machine [no hardware needed]
- [ ] `wk sysimage ls` prints a refusal per peer until the tree is on them:
      moose answers `unknown option: --continued`, the build boxes answer the
      old `where=host` refusal. `wk sync --tools` settles it [mutates other machines]

## Lanes

- [ ] a lane records which profile it is, so the name stops being
      load-bearing. Today it is read in both directions: `_ws_profile` and
      `_profile_from_path` (cmd/sysimage), `ws_image_base` (lib/target.sh),
      `image_slot_dir` and `image_pgo_dir` (lib/image.sh), `yocto_ws_default`
      (image/yocto.sh) and buildroot's `ws="${ws:-buildroot-$profile}"`; and
      changing `image_slot_dir` to take the lane reaches cmd/pi, cmd/ab,
      image/pgo.sh and image/yocto.sh. One decision first: every lane in the
      fleet predates such a record, so reading the record *instead of* the
      name blinds `wk sysimage ls` and `write --from` to all of them until
      they are rebuilt, and reading the record *or else* the name is the
      second path the no-fallbacks rule refuses [decision]
- [ ] a lane per arm, so both arms of one profile build at once where disk
      allows: the workspace name is the profile alone today
      (`yocto_ws_default`, image/yocto.sh), and the two arms are two commits in
      one checkout [no hardware needed]
- [ ] the cross toolchain as a store artifact -- `populate_sdk`'s installer
      keyed by its inputs -- so a slot build is a WebKit workspace plus an SDK
      rather than a 20-120 GB yocto tree. That is what lets a machine too small
      for a yocto tree build slots, and what makes the item above cheap.
      First: confirm `cross-toolchain-helper` leaves a relocatable installer,
      and that an SDK built on one aarch64 Linux machine runs on another [needs a yocto lane]

## The plan

- [ ] `wk status` renders a graph plan: `task_step` is a line number into a
      flat list (lib/task.sh), and the scheduler tells the record which step
      started (`sched_run --on-start`), so two steps running at once read as
      one [no hardware needed]
- [ ] a step refused for capacity is "not now" to the scheduler, which retries
      it once another step ends, and that is `--retry-exit` (75, lib/sched.py).
      Nothing produces it: `build_admit`'s refusal goes through `barrier`
      (lib/common.sh), which exits 1 like any failure, so a build that will not
      fit beside the ones running cascades as one [no hardware needed]
- [ ] `wk sysimage -h` does not name the configs one phase of the cycle is
      asked for by (`wk sysimage webkit <2.52 profile> --commit <sha> --slot
      <name> --config wpe-cross-pgo-collect|wpe-cross-pgo-use`); the refusal in
      image/pgo.sh is the only place they are written down [no hardware needed]
- [ ] `wk ab` guesses a base as the merge-base of the head and the image's
      branch, which for an `eng/` branch cut from main puts the two arms
      thousands of commits apart -- 15305 for the branch measured on
      2026-09-16. It says so in the line above the plan and does not refuse.
      Decide whether a distance that large is a barrier [decision]
- [ ] whether a main-based WebKit cross-builds against a 2.52 SDK at all is
      unmeasured, and it is what the change under measurement needs: the
      function `eng/linux-Do-not-set-high-priority-for-non-utility-threads`
      edits (`Thread::setCurrentThreadQOS`, Source/WTF/wtf/Threading.cpp)
      exists on main and not on `webkitglib/2.52`, which still has the older
      `HAVE(QOS_CLASSES)` code -- so that change cannot be cherry-picked onto
      the release branch and both arms have to be main's. Its A/B is
      `--base 8a2b0661029000540a7bcbd7c53c4ff8c708b29b` (the branch's own
      parent), and the first thing to try once a toolchain is built is one
      slot of that base [needs a lane with its toolchain built]

## Found by running it

- [ ] the four tests that create a real container workspace
      (tests/test_container_workspace.py's two, tests/test_crash_only.py's
      two) fail while a machine-sized build runs in the same VM, and nothing
      else does. Measured 2026-09-16 against a running image build: `wk new`
      took 173.7s where it takes seconds, one `wk new` exited 1 printing only
      the SDK patch script's own lines, and `build_admit` refused the build
      test's build because the machine's memory was spoken for -- which is
      that check working. A `wk new` run by hand at the same moment
      succeeded. They skip when the podman machine is stopped and have no
      notion of one that is busy; decide whether a machine with a build on
      its books (`builds_running`, lib/resources.sh) is a skip, and re-run
      them on an idle machine before believing any of this [no hardware needed]
- [ ] `tests/test_vm_desktop.py`'s `test_the_refusal_can_be_crossed_on_purpose`
      failed once in a full run under that same load -- the function returned
      0 and its `WK_VM_FORCE=1` line reached no stderr -- and passes alone and
      in discover order. Unexplained, so not yet a flake anyone should trust
      [no hardware needed]

## Before the first run of the big one

- [ ] tolken is building `webkit-2.52-yocto-rpi5-64` in its own lane, pinned
      to this machine with `WK_TARGET=container` so it takes the name every
      other command derives (started 2026-09-16T04:56Z, cold sstate). Its
      toolchain stage is chained behind it; a slot build needs that SDK
      [in flight]
- [ ] a lane for a release profile needs that branch in its machine's mirror:
      tolken carried `main` only and the checkout failed until
      `WK_MIRROR_BRANCHES="main webkitglib/2.52" wk sync` carried it in. The
      refusal now names that; whether the build should carry it in itself is
      a decision [decision]
- [ ] moose has 11 GB free of 936 GB (2026-09-16), and an image stage wants
      60-120: reclaim the 2.46 lanes (243 GB), the 2.38 buildroot pair (44 GB)
      and the dangling container images (84 GB) [decision, then `wk gc`]
- [ ] rpi3 and rpi5 answer nothing; rpi4 is in its rescue. Each board has to
      be carrying a bench system built from the 2.52 profile its conf names
      before any collection runs against it [needs a hand at the boards]
- [ ] rpi4 at 2.52 loses legs for reasons docs/HANDOFF-boot.md lists (KMS
      intermittent, EEPROM still usb-first, the stick dropping off the bus):
      settle those before counting on that board's arm [needs the rpi4]
