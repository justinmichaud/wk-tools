# HANDOFF — the 2.46 comparison set

Three Speedometer 2.1 comparisons, all yocto, all measured from slots built
from each system's own release tip. What is owed to finish them. Fleet-wide
and lane-wide items live in `HANDOFF-fleet.md` and `HANDOFF-ab-bench.md`.

Done, and nothing is owed for either: **rpi4, 64-bit, 2.46 vs 2.52** —
`$WK_STORE/bench/20260903T012124Z-rpi4-systems`, 4 usable rounds,
A=22.681+-0.528 (wpewebkit-2.46), B=22.330+-0.572 (webkit-2.52), B 1.55%
slower, p=0.0000. **rpi3, 32-bit, 2.46 vs 2.52** —
`$WK_STORE/bench/20260903T131642Z-rpi3-systems`, 10/10 runs, 5 usable rounds
of 5, A=10.788+-0.228, B=9.325+-0.114, B 13.55% slower, p=0.0000. Both carry
a `report-<board>-speedometer2.1.html` beside the runs.

- [ ] the rpi3 pair regresses nine times harder than the rpi4 pair, broadly
      rather than in one subtest, and mostly in *Sync* time (Elm
      CompletingAllItems +26%, Elm DeletingItems +22%, Angular2 Adding100Items
      +23%). Whether that is 32-bit, the board, or the release is not
      answered by these two runs. The rpi5 pair below holds the board and the
      release fixed and varies only the width, which is the measurement that
      separates them [needs the rpi5 pair]

## rpi5, 2.46 32-bit userspace vs 2.46 64-bit

**Done.** `$WK_STORE/bench/20260905T040613Z-rpi5-systems`, 10/10 runs, 5 usable
rounds of 5, `report-rpi5-speedometer2.1.html` beside them.
A=`wpewebkit-2.46-yocto-rpi5-64-a88fba47e77d` 46.680+-2.365,
B=`wpewebkit-2.46-yocto-rpi5-32-15c9b77bde09` 46.294+-1.758, B 0.83% slower,
**p=0.0645 -- not significant**. Every leg verified that the reporting
WPEWebProcess was the slot's own binary (build-ids 2f19338d5dad and
ae181e9f0629).

The totals are indistinguishable and the headline is one run wide: round 1's
A leg scored 51.048 where A's other four sit at 45.17-45.85, and dropping
round 1 from both arms turns B 0.83% slower into B 1.4% faster. What the
subtests show instead is a redistribution, and it is not subtle -- of 48 Sync
measurements, 44 significant, 32-bit is slower in 33 (median +12.9%); of 48
Async, 40 significant, 32-bit is faster in 30 (median -8.9%).

That bears on the rpi3 question above. The rpi3's 2.46-vs-2.52 regression was
nine times the rpi4's and concentrated in *Sync* time -- and width alone, at
one release on one board, moves Sync by a median +12.9%. Same signature, so a
32-bit userspace is the suspect the rpi5 pair was built to identify. It is not
proof: this measures width at fixed release, and the rpi3 result is release at
fixed width. The measurement that would close it is 2.46-vs-2.52 run on both
widths of this board, which the lane can now do.

- [ ] the rpi5's scores drift between rounds by more than their within-run
      stdev and the cause is not established: A scored 51.048 then 45.756,
      45.171, 45.571, 45.854, against a within-run stdev of 1.8%, while B held
      46.494, 46.505, 46.330, 46.892, 45.249. Round 1 looks like a first-run
      effect rather than a decline. Throttling is not the evidence -- 56.8C
      between runs with the clock at 2400MHz, well under the 85C the SoC
      throttles at, and the image pins the performance governor. What is
      certainly missing is clock pinning: the rpi4 profile sets
      `force_turbo=1`, `arm_freq=1500`, `arm_freq_min=1500`, `arm_boost=0`,
      and the rpi5 has no profile `config.txt.append` at all. Find the cause
      before pinning anything [no hardware for a change; a rewrite and re-run
      to use it]

- [ ] a comment-trimming pass over the tree has twice deleted a function body
      and left its callers: `r_is_root` (boot/machines.sh), and
      `_from_resolve` / `_profile_image_path` / `_built_profiles`
      (cmd/sysimage). Both broke only at run time -- `wk boot --back` and
      `wk sysimage write` answered "command not found" -- and both were found
      by running the command, not by a test. `wk selftest` has no check that
      every function a shell file calls is defined somewhere it sources; a
      naive scan for it is swamped by variables and by shell snippets sent to
      other machines, so what is owed is a check narrow enough to be worth
      having [no hardware needed]

- [ ] `NODE_DTB` (boot/machines/rpi5.conf) is a stored copy of a fact the board
      can be asked for: the firmware picks its tree from the board revision,
      one read of `/proc/cpuinfo` on a machine that must be up for
      `boot-check` to run at all. Derive it in `image_dtb_for` and the class
      of "boot-check passed a card that cannot boot" goes with it
      [no hardware needed]

- [ ] `wk boot --status` printed a boot time taken from the arming record
      while the board was mid-reboot, which reads as a machine up since a boot
      it has already left. The fallback is deliberate; what is owed is that a
      fallback value says it is one [no hardware needed]

- [ ] both of the stick's boot partitions are labelled `boot`, so the
      automounter's `/run/media/<user>/boot` and `boot1` swap between boots.
      Anything addressing them by path rather than by partition number is
      addressing whichever mounted first [no hardware needed]

## Constraints that bound all of the above

- [ ] one build at a time on this host, and no build while a board is
      measuring: concurrent builds have OOM-killed podman here, and the
      benchmark driver runs on this machine [no hardware needed]
- [ ] never edit this checkout while a `wk` command runs -- a detached build
      and every `wk boot` re-read scripts from it mid-run [no hardware needed]
