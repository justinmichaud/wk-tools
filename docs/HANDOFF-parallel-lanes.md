# HANDOFF — several lanes, several machines, one experiment

README.md ("One lane per profile, several lanes at once", "The boards'
profile-guided build") is the design. This is what is owed to drive one A/B
across rpi3, rpi4 and rpi5 from one command, with more than one machine
building.

## Lanes

- [ ] a lane per arm, so both arms of one profile build at once where disk
      allows. `_ws_profile` (cmd/sysimage) now recovers the profile from a
      suffixed lane name, so `yocto-<profile>-<arm>` is already a lane `ls`
      and `write --from` read. What is left is the plumbing that would put
      the two arms in two lanes: `yocto_ws_default` (image/yocto.sh) names a
      lane by its profile alone, and `image_slot_dir` / `image_pgo_dir`
      (lib/image.sh) find a slot by profile rather than by lane, which
      reaches cmd/pi, cmd/ab, image/pgo.sh and image/yocto.sh
      [no hardware needed]
- [ ] the cross toolchain as a store artifact -- `populate_sdk`'s installer
      keyed by its inputs -- so a slot build is a WebKit workspace plus an SDK
      rather than a 20-120 GB yocto tree. That is what lets a machine too small
      for a yocto tree build slots, and what makes the item above cheap.
      First: confirm `cross-toolchain-helper` leaves a relocatable installer,
      and that an SDK built on one aarch64 Linux machine runs on another
      [tolken's lane has a toolchain built as of 2026-09-16]

## Routing

- [ ] `wk pi deploy <profile>@<machine>` refuses anywhere but that machine
      and names the remedy; it does not yet drive the deploy there. Making it
      a `name=derived` command would reuse the dispatcher's own delegation,
      but it moves the board claim (`device_hold`) onto the far machine, and
      no board answers today to measure that with [needs a board]

## The plan

- [ ] `wk ab` runs every step on the machine it was typed on (`RUNS_ON`,
      cmd/ab) now that a lane can be named per machine [no hardware needed]

## Before the first run of the big one

- [ ] tolken is building `webkit-2.52-yocto-rpi5-64` in its own lane
      (`--target container`), on base 8a2b066102. Its toolchain stage is
      done; the webkit stage started 2026-09-16T13:21Z [in flight]
- [ ] a lane for a release profile needs that branch in its machine's mirror:
      tolken carried `main` only and the checkout failed until
      `WK_MIRROR_BRANCHES="main webkitglib/2.52" wk sync` carried it in. The
      refusal now names that; whether the build should carry it in itself is
      a decision [decision]
- [ ] disk. moose has 64 GB free of 936 and tolken's home 18 GB (measured
      2026-09-16); an image stage wants 60-120. What moose could give back:
      the 2.46 lanes, the 2.38 buildroot pair, dangling container images
      [decision, then `wk gc`]
- [ ] rpi3 and rpi5 answer nothing; rpi4 is in its rescue. Each board has to
      be carrying a bench system built from the 2.52 profile its conf names
      before any collection runs against it [needs a hand at the boards]
- [ ] rpi4 at 2.52 loses legs for reasons docs/HANDOFF-boot.md lists (KMS
      intermittent, EEPROM still usb-first, the stick dropping off the bus):
      settle those before counting on that board's arm [needs the rpi4]
- [ ] whether a main-based WebKit cross-builds against a 2.52 SDK at all is
      unmeasured, and it is what the change under measurement needs: the
      function `eng/linux-Do-not-set-high-priority-for-non-utility-threads`
      edits (`Thread::setCurrentThreadQOS`, Source/WTF/wtf/Threading.cpp)
      exists on main and not on `webkitglib/2.52`, which still has the older
      `HAVE(QOS_CLASSES)` code -- so that change cannot be cherry-picked onto
      the release branch and both arms have to be main's. Its A/B is
      `--base 8a2b0661029000540a7bcbd7c53c4ff8c708b29b` (the branch's own
      parent) [the build in flight is that base's first slot]

## Found by running it

- [ ] `tests/test_vm_desktop.py`'s `test_the_refusal_can_be_crossed_on_purpose`
      failed once in a full run under a machine-sized build -- the function
      returned 0 and its `WK_VM_FORCE=1` line reached no stderr -- and passes
      alone and in discover order. Unexplained, so not yet a flake anyone
      should trust [no hardware needed]
- [ ] two half-made workspaces are on the books and nothing is making them:
      `wk-test-yxawdf` on tolken and `m` on moose. Both are a test's rubble;
      `wk new <name> --kill` then `wk rm` clears each [no hardware needed]
