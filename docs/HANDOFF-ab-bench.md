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
  The remote transition is worked out but never run: write the rescue to the
  USB stick from the running board, flip the SD's MBR type to `0x83`, let the
  USB rescue repartition and rewrite the SD as rescue+bench, flip the type
  back — one window in that sequence with no fall-through if it fails.
- **rpi4**: rescue on the USB stick, bench system on the SD (`rpi4.conf`,
  `wk help hardware` has why). The USB already holds the rescue, so what is
  owed is the **SD**: it still
  carries a pre-tailnet bench image with no tailscale in it. Needs the card in a
  reader.
  Owed with it: `b_media` and `b_system_kind` (boot/pi-usb.sh, boot/machines.sh)
  still describe the stick as the bench medium and the SD as the base image.
  Re-read both against this arrangement before trusting what `wk boot
  rpi4 --status` says about which system is running.

## 3. The buildroot builder has never built anything

`buildroot_build` (`image/buildroot.sh`) and `image/buildroot-build.sh`, the
same host/worker split the yocto lane uses, are written: `wk sysimage build
wpewebkit-2.38-buildroot-rpi3-32 --dry-run` and the rpi4-32 profile (its
defconfig is derived — `image/buildroot/external/configs/README` has the
derivation line by line) name the real workspace, defconfig and cache paths,
and the image stage's completion line is conditioned on `sdcard.img` being
newer than the run's own start (`verify_image_freshness`,
tests/test_buildroot.py), the same rule the Yocto lane carries for its own
helper's shortcut. `container/buildroot/Containerfile` is the Ubuntu 22.04
host the wiki's own recipe was driven on. What none of that proves is a build:

- [ ] a real build, through this mechanism, on either arm64 host (this Mac's
      podman VM or moose). Nothing has run past a dry run yet, so
      `image/buildroot/external/external.mk`'s libffi fix — `HOST_PYTHON_CONF_OPTS
      += --with-system-ffi`, `HOST_PYTHON_DEPENDENCIES += host-libffi`, checked
      so far only as make semantics against buildroot's own rule shape — is
      still unverified by anything that actually compiled host-python
- [ ] once a build succeeds: `wpewebkit-2.38-buildroot-rpi4-32`, whose derived
      defconfig has never been built or booted — validating it needs the board
- [ ] the rpi5's 64-bit buildroot 2.38 configuration: no defconfig exists for
      it yet at all (`image/buildroot/external/configs/README`), upstream or
      derived, so `wpewebkit-2.38-buildroot-rpi5-64` refuses by name
      (`CFG_NEEDS`) rather than attempting anything

Expect new egress refusals on the first real build, and add them the way the
existing ones got there: from a `DENY` line in the proxy log, not in
anticipation (`BR2_PRIMARY_SITE` covers most of it; `sources.buildroot.net`,
`ftpmirror.gnu.org` and `wpewebkit.org` are the fallbacks already allowed from
real refusals).

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

This is `docs/HANDOFF-bench-unify.md`'s "one record/report" item — see there.
