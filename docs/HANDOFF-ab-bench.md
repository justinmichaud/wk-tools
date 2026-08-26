# HANDOFF — an A/B number off the two Pi boards

What is owed to get from "both boards run a rescue" to "two arms compared".
Ordered by what blocks what. `wk help lifecycle` is the command sequence;
`docs/HANDOFF-pi-deploy.md` holds the `wk pi` lane's own remaining items and is
not repeated here.

## 1. rpi4 is unreachable, and not because of the board

It is booted on its rescue. Its only uplink is `tailnet-bridge-generic`'s USB
ethernet, and that phone is offline — so the board cannot join the tailnet
however correct its image is. `wk find rpi4` reports exactly this and blames
nothing on the board.

Owed: get the phone up (`wk bridge status tailnet-bridge-generic`, then
`wk bridge setup` — its role is older than this checkout), confirm its `lan0`
carries `10.99.1.0/24` again, then `wk find rpi4`. The board should appear and
join as `rpi4`.

This board is on the phone's cable by design, and stays there. What is owed is
making that path work, not replacing it -- so a phone that has been offline for
hours is the bug to chase (its own uptime, its charge, its dock's ethernet), and
`wk bridge status` is where that starts.

## 2. Neither board has a bench system

An A/B needs two deployed slots on a board, and both boards currently hold one
system: the rescue.

- **rpi3**: one medium, so the bench system goes in a second root on the same
  card. `wk sysimage write` writes one whole system to one whole device and
  cannot put one into a slot — `docs/HANDOFF-boot.md` items 1–3. The rescue was
  written `--rescue` (so un-grown): 55 GB of that card is free and waiting.
- **rpi4**: decided and now recorded — **rescue on the USB stick, bench system
  on the SD** (`rpi4.conf`, `wk help hardware`). It follows from the boot order:
  the board is usb-first, so the stick is what an untouched board comes up on and
  that has to be the system which answers. The arming byte is unchanged pointing
  the other way — a stick typed 0x0c boots the rescue, 0x83 steps the firmware
  over it onto the SD for one boot.
  The USB already holds the rescue, so what is owed is the **SD**: it still
  carries a pre-tailnet bench image with no tailscale in it. Needs the card in a
  reader.
  Owed with it: `b_media` and `b_system_kind` (boot/pi-usb.sh, boot/machines.sh)
  still describe the stick as the bench medium and the SD as the base image.
  Re-read both against this arrangement before trusting what `wk boot
  rpi4 --status` says about which system is running.

## 3. buildroot 2.38 on the rpi4

`wpewebkit-2.38-buildroot-rpi3-32` builds and produces a bootable `sdcard.img`
with tailscale in it. The rpi4 configuration has not been built.

**Re-check the whole defconfig question before building anything.** This
repository currently points that configuration at a defconfig it derives itself
(`image/buildroot/external/configs`, README carries the derivation), on the
conclusion that the fork ships no release-pinned rpi4 defconfig. That conclusion
is disputed -- the upstream rpi4 defconfig is in use by others -- so the
derivation may be unnecessary and may be actively wrong. Before spending a build:

- list what the fork actually ships (`make list-defconfigs` in the pinned tree,
  not the GitHub API), on the commit `BR_TREE_COMMIT` names;
- try `raspberrypi4_wpe_defconfig` as it ships, and work out where the WPE
  release pinning comes from in it -- if it comes from the tree rather than the
  defconfig name, there was never anything to derive;
- delete the derived defconfig and its README if that is the answer. A config
  this repo maintains and does not need is worse than none.

Expect new egress refusals either way, and add them the way the existing ones
got there: from a `DENY` line in the proxy log, not in anticipation.

## 4. `wk pi bench --ab` has never run

Everything under it is built: `--ab A,B [--rounds N]` alternates slots so drift
lands on both arms, `pi_bench_record` files each run into `$WK_STORE/bench`
with the same `env.json`/`result.json`/`run.log` shape as every other run, and
`wk bench compare` reads them from there.

Owed: run it, on a board with two slots, and check the parts that only a real
run exercises — that both arms are recorded, that `wk bench ls` lists them, and
that `wk bench compare` warns on the axes that differ and reports the kernel and
system delta rather than warning about it.

## 5. The report `wk bench compare` owes

`wk bench compare` ends in WebKit's `Tools/Scripts/compare-results --breakdown`
and prints what that produces. What is wanted is specified in
`docs/Urgent/HUMAN-Benchmarking variance.md`: the compare-results output **plus**
a histogram of both runs and all subtests, as an HTML report, generated
automatically -- "everything here should work with one command" -- and score
compared alongside wall time.

None of that exists yet. compare-results offers `--csv`, `--breakdown`,
`--detailed-breakdown` and `--category-breakdown` and emits no histogram, and
nothing in this repository adds one. So this is work, not a check.

The input is already there: `--ab --rounds N` produces N results per arm, each a
full `result.json` beside its `env.json` in `$WK_STORE/bench`. The report is a
reader over those, next to `wk bench compare` -- not a change to WebKit's script,
which belongs to WebKit.
