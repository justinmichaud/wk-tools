# HANDOFF — an A/B number off the two Pi boards

What is owed to get from "both boards run a rescue" to "two arms compared".

- [ ] `test_build_wall.py`'s `test_it_strips_the_wall_and_keeps_everything_else`
      hands `bash -c` a fixed `env={"PATH": ...}` but not `--noprofile
      --norc`, so a startup file that prepends to PATH makes it fail with
      entries the test never set (seen 2026-09-05: the assertion got
      `/home/jmichaud/Development/wk-tools/bin:/.local/bin:...`). It passes
      alone and fails under `unittest discover`, which reads as a regression
      in whatever change happens to be in the tree. Give the subprocess a
      shell that reads no rc [no hardware needed]

- [ ] v3d on kernel 6.6.22 publishes no fdinfo `drm-engine-*` counters, so GPU
      engine time is unreadable on both rpi5 images (measured 2026-09-05, task
      `20260905T200814Z`: the web process holds `/dev/dri/renderD128` and maps
      `v3d_dri.so`, and `gpu.measured` is still false). The warmup now records
      that as a note and refuses only when no render node is held either.
      Whether a newer kernel publishes them, or whether v3d exposes usage
      somewhere else worth reading, is unanswered [needs a board]

- [ ] the warmup round's probes are read off a board for the 32-bit arm only
      (`20260905T200814Z`, arm B: 32-bit ARM, `v3d_dri.so` + renderD128, DFG
      38406/FTL 0, 32 MB executable). Arm A's evidence was overwritten by the
      samply capture that shared its name, so the 64-bit probe output has still
      never been seen; a re-run with the split names settles it.
      The earlier run (2026-09-05, `20260905T152355Z-rpi5-systems`) proved the boot,
      the marker check, the session, the samply fetch/stage/attach, the
      `perf_event_paranoid` read and the JSC tier report (64-bit arm: FTL 1465,
      DFG 7317; 32-bit arm: DFG 40, FTL 0), but both legs ended without a
      result, so `record_warmup_evidence` never ran and no `warmup/*.json` was
      written. What is still unread on hardware: `_WARMUP_SH`'s `od`/`awk`
      under the image's shell, whether the web process maps a `*_dri.so` at all
      (the samply profile's lib list says it maps `v3d_dri.so`, which is
      evidence but not the same probe), and whether v3d publishes
      `drm-engine-*` in fdinfo on 6.6.22 [needs one leg that completes]
- [ ] why the rpi5's JetStream3 warmup leg ran 40 minutes without finishing is
      not established (2026-09-05, `20260905T152355Z-rpi5-systems`, leg A,
      `--count 1 --timeout 2400`). That leg carried samply at ~49 Hz *and*
      `JSC_reportDFGCompileTimes`/`reportFTLCompileTimes`, so it is not a
      measured leg's timing and says nothing about how long a plain run takes;
      2.46 is reported to complete JS3 on 64-bit. It was progressing, not stuck
      -- every fetch done inside 45 s, the profile flat, no function over 0.95%
      self time, and it had reached `source-map-wtb`. Time an unprofiled leg
      before changing the plan or the timeout [needs the rpi5]
- [ ] the warmup round's instrumentation makes its leg slower than the rounds
      it precedes, which is fine for evidence and wrong for the elapsed-time
      figure printed beside it. Either say the leg is instrumented where the
      time is reported, or time the arms without it [no hardware needed]
- [ ] a warmup leg that times out yields a profile but no evidence file: the
      driver writes the record from `add_additional_results`, which
      run-benchmark never calls without a result. The tier counts and the GPU
      delta are both readable at that moment regardless, and the run.log and
      browser.log survive -- so a timed-out leg could still say what the arm
      was, instead of only refusing [no hardware needed]
- [ ] samply profiles come back unsymbolicated: 0 of 66,122 functions carried a
      name in the first real capture, because samply resolves symbols when it
      loads a profile and the board is no symbol server. The host holds the
      byte-identical unstripped binaries in the slot dir
      (`build/wk-slots/<slot>/root`), so the fix is to record that path beside
      the capture and hand it to `samply load`, or symbolicate on the way out
      [no hardware needed]
- [ ] neither profiler has been run to completion on a board. samply publishes only
      x86_64 and aarch64 (checked against the release, 2026-09-05). aarch64
      samply is proven on the rpi5's 64-bit image -- fetched, staged, attached
      (`attached=samply pid=1434`), 25 MB captured, and that kernel reports
      `perf_event_paranoid=-1`. Every *armhf userspace* falls to the image's
      `sysprof-cli`, which the rpi5-32 sysroot does carry; whether it is in the
      deployed image, and whether its flags match `bench/wk_board_driver.py`'s
      `_PROFILE_START_SH`, is still unmeasured [needs a board up]
- [ ] `bench/board-profile.sh` invokes `sysprof-cli --force --pid <pid> <out>`
      from the flag set of no particular sysprof version. Check it against the
      version an image actually ships before relying on the capture
      [needs a board with sysprof-cli]
- [ ] rpi3, second launch at a cached URL: with the browser's disk cache warm for the benchmark's URL, the page loads from disk (only `index.html` and `benchmark-report.js` reach the server), `load` fires and the benchmark never starts -- web process asleep at 0.4 s CPU until the timeout (2026-08-30: task `20260830T142104Z-wpe-pr1725` rounds 1-2, both slots; `wk pi bench --count 2` iteration 2; a fresh invocation right after completes in 7.3 min). `wk pi bench` now clears the cache before every launch (WK_BOARD_RESET); what in a cache-served load stops `startBenchmark` from running is not known [needs the rpi3 in bench mode; optional]
- [ ] the rpi3 bench system carries two `while true; do free -m ...` loops and a cog left from earlier sessions (pids 1213, 1579, 12057 on 2026-08-30); nothing in `wk pi bench` ends a shell loop, so decide whether a run should refuse a board with foreign processes on it [decision]
- [ ] rpi4: the 32-bit kernel still does not come up -- serial console next. Ledger (2026-08-31): tryboot lane proven (the rescue's arm64 kernel boots through it, chosen/bootloader/tryboot=1); the image rebuilt with the tree's own bcm2711 defconfig (all 2711 essentials verified in .config, gcc plugins off for the 9.2 host toolchain), stick rewritten and sha-verified; boots tried and silent before 75 s (no wk-diag.txt, no watchdog return): fork rpi34-config zImage, bcm2711-config zImage, bcm2711-config zImage with explicit arm_64bit=0 (the driver now states bitness from the kernel magic -- the SD's 2024 firmware defaults 64-bit). Working vs hanging differ only in: 32-bit-ness/armstub path, the image cmdline (console=ttyS0 vs the rescue's), the kernel-built dtb. Blind permutations cost a power cycle each; attach a serial cable (BOOT_UART=1, or enable_uart is already in the image config.txt) and read where it dies. Both 2.38 slots are built and deploy in minutes once it boots. Alternative for an rpi4 datapoint today: the 2.46/2.52 or a 64-bit lane whose kernel is proven [needs serial at the board]
- [ ] the wpewebkit-2.38-buildroot-rpi4-32 image itself carries that 2020-01 rpi-firmware and so can never boot a rev 1.4/1.5 Pi 4 on its own, whatever medium or lane; if the image should ever boot without pi-tryboot, pin a newer rpi-firmware in the external defconfig (or swap the pair in post-image) and rebuild [decision]
- [ ] boot/pi-mbr.sh has no machine behind it now that rpi4 declares pi-tryboot: delete it, or re-adopt it when a firmware-bootable SSD replaces the stick (its tests still exercise it directly) [decision]
- [ ] rpi4's EEPROM still says BOOT_ORDER=0xf14 (usb-first); harmless today (the firmware skips the stick) but `wk pi boot-order rpi4` now defaults to sd-first for this driver -- write it on the next EEPROM touch [needs the board on]
- [ ] rpi4: the stick (`/dev/sdb`, `wpewebkit-2.38-buildroot-rpi4-32-bb4ac3575fdd`, armed 0x0c, boot files complete) is not booted by the firmware: `wk boot rpi4` with BOOT_ORDER 0xf14 lands back on the SD rescue every time. An empty RTL9201 USB enclosure sits on the same bus as `/dev/sda` (0 bytes); unplug it and boot again before suspecting the image -- there is no serial console to read the bootloader's USB discovery [needs a hand at the rpi4]
- [ ] first boot of `wpewebkit-2.38-buildroot-rpi4-32` on hardware: the image has never booted; the self-disarm and self-return have run only in tests [needs the step above]
- [ ] validate the base and pr1725 slots of `wpewebkit-2.38-buildroot-rpi{3,4}-32` on a booted board: `wk ab wpe:1725 --devices rpi3,rpi4 --bits 32` end to end, and confirm both arms are recorded, `wk bench ls` lists them, and the html report renders [needs a board booted into the image]
- [ ] write and validate an rpi5-64 buildroot 2.38 defconfig — none exists yet [needs the rpi5]
- [ ] the tailnet ACL lets `tag:wk` boards reach only `tag:wk`; `wk pi bench` tunnels the runner's port over ssh instead. Decide whether boards should reach a workstation's benchmark server directly [decision]
- [ ] rpi3: Speedometer 3 does not complete on the standard image (10 min timeout, no result posted) while Speedometer 2.1 does (11.27 pt, base slot, 2026-08-30); during 2.1 the web process alone held 74% of the 623 MB, so Speedometer 3 is most likely out of memory there. Measure it (logread for the OOM killer during a run) and decide: 2.1 as this board's plan, or swap/zram for 3 [needs the rpi3 in bench mode]
- [ ] `wk sysimage build <profile> --stop` reports "asked the build to stop" and the build carries on: it SIGTERMs the wrapper, whose bash defers the trap until the foreground `make` returns -- hours. Kill the process group, or have the wrapper trap and pass the signal on; and say nothing about stopping until the pid is gone (2026-09-01: a 2.46 build kept compiling host gcc for 20 minutes after the stop) [no hardware needed]
- [ ] an rpi4 image at any release but 2.38 needs a defconfig this repo derives: the fork release-pins its cog defconfigs for the rpi3 only. raspberrypi4_wpe_2_38_cog_defconfig is the worked example and its README entry is the derivation, so a 2.46 one is that diff re-applied to raspberrypi3_wpe_2_46_cog_defconfig [needs one image build to verify]
- [ ] the rpi4 half of the cross-image A/B: nothing on that board can be the B arm today. Its 2.52 yocto system boots and cannot render (HANDOFF-boot.md), and 2.46 needs the defconfig above [needs the rpi4 powered back on]
- [ ] seeding a benchmark payload `git clone`s WebKit from github per task, into `$WK_STORE/cache/bench/.tmp-XXXX/repo`, while this machine already keeps the mirror every `wk new` clones from. Two boards benched at once clone it twice, concurrently. Seed from the mirror instead [no hardware needed]
- [ ] an interrupted seed leaves its multi-GB `.tmp-*` under `cache/bench` and nothing reclaims it -- `wk gc` reports that directory as "kept; prune by hand" and never enters it (17 GB from 2026-08-29 was still there on 2026-09-02). Either `wk gc` removes an abandoned temp dir under the store, or seeding cleans up after itself on the way out [no hardware needed]
- [ ] `wk pi helper <machine>` cannot install onto a workstation that holds a reader: it writes root-owned paths over a channel that is root only on a bench-device, and `wk` takes no passwordless sudo on a workstation. The refusal now names `./setup --stage quiesce` on that machine; decide whether a wk-driven path should exist at all [decision]
- [ ] `builds_running` (lib/resources.sh) deletes a build's budget record when `_build_holder_alive` cannot resolve the holder -- a missing `ws_target` returns "not alive" and the record is pruned, so an indeterminate read destroys the accounting a live build depends on. Prune only what is provably gone. Every in-tree caller loads the target libs, so nothing in wk reaches it today [no hardware needed]
- [ ] an A/B across two system images is four commands per board (`wk sysimage write` twice, `wk boot --system`, `wk pi deploy` per system, then `wk pi bench --ab-systems`) with no single driver; `wk ab` does exactly this shape for a PR's two slots. The second consumer exists now (2.38-vs-2.52 and 2.46-vs-2.52), so mint one or say why not [decision]
- [ ] `b_boot_system` (cmd/pi) loses a leg it actually booted: the loop spends each of its `PI_SYSTEM_TRIES` passes *arming*, and after the last arm it returns without probing, so a board that comes up correctly on the final attempt is reported "would not come up ... the leg is lost". On the rpi3 every leg switch needs the rescue round-trip (arm from bench, does not hold; arm again, refused; go back; arm from the rescue -- three passes), so *every* switch loses its leg and no round ever has both arms: 2026-09-03, tasks 20260903T012156Z-rpi3-systems and the re-run, 0 usable rounds of 5 both times, while the next round's own probe shows the board running exactly the system the lost leg asked for. Verify after the last arm rather than only at the top of the next pass [no hardware needed]
