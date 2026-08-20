# HANDOFF — vocabulary, roles, and the device lifecycle

**Status: the vocabulary is decided (2026-08-20); the code has not caught up.**
`wk sysimage`, `role`/`mode`, and `bench-device`/`workstation` are settled and
this file is what they mean. The rename itself, and the three missing commands
in the lifecycle below, are not written yet.

It exists because four words in this repo each mean three or four different
things, and the confusion had started costing mistakes rather than just reading
time.

## The problem, precisely

**`image` means four things**, and one command does two of them:

| meaning | where |
|---|---|
| a wk-seeded bootable artifact | `wk image build rpi4-perf` |
| a Yocto distro build | `wk image build rpi4-wpe-2.48` — a different mechanism entirely (`IMG_BUILDER=yocto`) |
| the state a machine is running in | `cmd/boot`: `ROLE="image $id"`, `in_image_role` |
| the upstream base a build starts from | `IMG_BASE_URL` |

**`role` means two things**, and they appear in the same sentence:

| meaning | where |
|---|---|
| what a machine permanently *is* | `MACH_ROLE=workstation\|test-device` |
| what it is running *right now* | `cmd/boot`'s `ROLE`; `cmd/bench`'s "benchmark role", "workstation role", "bare-metal role" |

So `wk boot --status` prints `rpi4: test-device` for one question and
`rpi4: image <id>` for the other, in the same slot.

**`machine` means two registries**: the bootable fleet (`boot/machines.sh`) and
the build hosts (`targets/hosts/*.conf`) — and `wk help machines` documents the
second. **`target`** is both a workspace target and Yocto's `YOC_TARGET`.

## The fix, in four words

- **role** — what a machine *is*. Permanent, config-driven, changeable.
- **mode** — what a machine is *running now*: `host` or `bench`. Transient.
- **system** — a bootable artifact wk builds. Never bare "image".
- **fleet** — machines wk can boot. Build hosts are *build hosts*, not machines.

`wk boot <machine>` puts a machine from **host mode** into **bench mode** for one
boot. `wk boot --back` returns it. That sentence is now unambiguous, and it is
the same sentence for every machine in the fleet.

## Roles

A role is declared per device and changed by editing one field. Nothing about a
role may be baked into a command.

| role | wk owns | has a bench mode | today |
|---|---|---|---|
| `workstation` | nothing on it | yes, borrowed for one boot | moose, tolken, rpi5 |
| `bench-device` | every system on it | it is always the point | rpi4, rpi3, benchvm |
| `build-host` | nothing; it compiles | no | buildbox4, devbox-arm64-2, moose |
| `tailnet-bridge` | its system | no | librem5-oob |
| `infrastructure` | nothing | no | gateway, nextcloud, immich, overleaf |

The difference that matters for the lifecycle, and the only one:

> A **bench-device**'s host mode is a system **wk builds and owns**, so wk can
> provision it from nothing. A **workstation**'s host mode is *your install*,
> which wk must never touch — it can only add a bench mode beside it.

A device can hold more than one role (moose is a workstation *and* a build
host), and roles must be changeable: making the rpi5 a bench-device, or the
rpi4 a workstation, should be a config edit plus `wk unprovision` /
`wk provision`, never a code change.

**`benchmark-runner` is not a role.** It is a per-run assignment: the machine
driving a run, which must not be the machine under test
(`docs/HANDOFF-benchmarking.md` — "an image run needs a *second* machine to
hold the runner"). It belongs to the run, not to the device.

## Systems, named by what they are for

One command, one store, one write path. The category lives in the profile name,
so adding a customer configuration later is adding a profile:

| category | what it is | profiles |
|---|---|---|
| `seed` | the smallest system that boots, answers ssh, and can be provisioned | none yet — see below |
| `perf` | measurement-grade, matching a distro | `perf-linux-rpi4`, `perf-linux-rpi5`, `perf-macos-tolken` |
| `bridge` | a tailnet-bridge's own system | `bridge-librem5`, `bridge-android` |
| `downstream` | a public image, for customers or evaluation | `downstream-yocto-wpe-2.48`, `downstream-yocto-wpe-2.48-32`, `downstream-buildroot-*`, `downstream-yocto-wpe-evaluation` |

**`seed` has no profile and may never need one.** A seed's whole job is to boot
and answer ssh so that `wk provision` can do the rest over the wire — and on
both Pis the downstream Yocto image already does that. The rpi4's card holds one
today and it is what `wk boot rpi4 --back` returns the board to. So the seed
category is a *role a system plays*, not necessarily a system of its own; a
dedicated one is worth building only if a board turns up that the downstream
image cannot boot.

### distro systems and yocto systems — the noun

The word is **distro**, and it is already this repo's own: `IMG_BUILDER`
defaults to `distro`, and `cmd/image` says "everything below this line is the
distro builder". It had simply never surfaced in anything a person types.

- A **distro system** is seeded from a general-purpose distribution's own image
  — Ubuntu's preinstalled raspi — and tuned for measurement. It stands for what
  a normal Linux machine looks like. `rpi5-perf`, `rpi4-perf`.
- A **yocto system** is built from source and is the embedded product image. It
  stands for what the customer's device runs. `rpi4-wpe-2.48` and friends.

"A distro perf run" and "a yocto perf run" are therefore different measurements
of the same board, and both are wanted. The identity marker now records
`builder=` so a run never has to infer it from a profile name.

Yocto and buildroot stay *builders* (`IMG_BUILDER=yocto`) rather than becoming
their own commands. That is what makes the requirement work: **`wk boot` can put
a machine into any system in the store**, so testing a downstream Yocto image on
real hardware is the same verb as testing a perf system, with a different
profile. Splitting the commands would have made those two different operations.

Renames, current → proposed:

    wk image build rpi4-perf          wk sysimage build perf-linux-rpi4
    wk image build rpi4-wpe-2.48      wk sysimage build downstream-yocto-wpe-2.48
    wk image write ... --disk m:/dev  wk sysimage write ... --disk m:/dev
    (nothing)                         wk sysimage flash <id> --reader /dev/sdX
    MACH_ROLE=test-device             role=bench-device
    ROLE=workstation | image <id>     mode=host | bench
    in_image_role                     in_bench_mode

## 32-bit and 64-bit

**A 32-bit perf run measures a 32-bit system** — a 32-bit kernel and a 32-bit
userspace — and never a 32-bit process borrowing a 64-bit kernel's compat layer.
The two differ in syscall path, page size and kernel pointer width, and the
armhf port that ships to customers runs the first. A number from one is not a
number from the other.

So the 32-bit systems are **Yocto builds**, and that follows from two facts
rather than from preference:

- **Ubuntu 26.04 publishes no armhf raspi image at all.** Checked 2026-08-20:
  `arm64` desktop and `arm64` server, and nothing else. The route every other
  `perf-linux-*` profile takes simply does not exist in 32-bit.
- **`Tools/yocto/targets.conf` on `webkitglib/2.48` already had the targets** —
  `rpi3-32bits-mesa`, `rpi3-32bits-userland`, `rpi3-64bits-mesa` and
  `rpi4-32bits-mesa`, beside the `rpi4-64bits-mesa` that was the only one named
  in `image/profiles.sh`. The systems were buildable; nothing had named them.

| device | 64-bit perf system | 32-bit perf system |
|---|---|---|
| rpi3 (BCM2837, A53) | `rpi3-wpe-2.48-64` — marginal at 931 MB | `rpi3-wpe-2.48-32` — its native width |
| rpi4 (BCM2711, A72) | `rpi4-perf` (Ubuntu) or `rpi4-wpe-2.48` | `rpi4-wpe-2.48-32` |
| rpi5 (BCM2712, A76) | `rpi5-perf` (distro), `rpi5-wpe-2.48` (yocto) | **none, and deliberately so** |

The rpi5 is the "where possible" case. Its kernel is built `CONFIG_COMPAT=y`
(read off the board, 2026-08-20), so it *can* execute a 32-bit binary — and
that is exactly the compat-layer measurement the rule above excludes. There is
also no `rpi5-*` section in `targets.conf`, so there is no 32-bit system to
boot even if the rule allowed it. Offering the rpi5 as a 32-bit perf target
would produce numbers that look comparable to the rpi4's and are not.

This also closes the question `image/profiles.sh` was holding the rpi3 open on.
It asked for "a 32-bit base for this profile, or an explicit 'the rpi3 becomes
arm64'", and the answer is neither: the rpi3's perf systems are Yocto builds, in
whichever width the run is measuring, and no distro base is involved at all.

**Provenance.** `cmd/bench` already separates comparisons by the *build's* arch
(`t_arch`, and `axes += '/' + m['arch']`). That is not enough on its own: an
armhf build on an armhf kernel and an armhf build on an arm64 kernel both record
`arch=armhf`. The run has to record **`kernel_arch`** beside it, and two runs
whose `kernel_arch` differs are two series — the same rule as `bench_host` and
`root_device` in `docs/HANDOFF-benchmarking.md`.

## The rpi5 holds three things at once

The rpi5 has to be a workstation, a distro perf target, and a yocto perf target,
and none of the three may cost the others anything. It now is, and the mechanism
did not have to change to allow it:

| what | where it lives | how it is entered |
|---|---|---|
| workstation (host mode) | its NVMe install | the default; never written by wk |
| distro bench mode | `rpi5-perf` on the USB stick | firmware one-shot (`0xf64`) |
| yocto bench mode | `rpi5-wpe-2.48` on the same stick | the same one-shot |

`wk boot rpi5 --image <id>` already takes any system in the store, and the
one-shot is a firmware register, so it is indifferent to what the stick holds.
The NVMe is not touched in any of the three cases. That is the whole of the
"without breaking the workstation" requirement, and it was already true.

**What did have to change is that a yocto system was not a fleet system.** The
two builders "share only what comes after the manifest", and the consequence
was invisible until one was wanted as a bench mode: a yocto image has no
`/etc/wk-image`, so `b_probe` cannot tell a board running it from a board that
never left host mode — `wk boot --status` would report a machine in bench mode
as though nothing had happened. It also had no self-return watchdog, so it would
never hand the machine back, and no self-disarm, so on a medium-armed machine
like the rpi4 the stick would stay armed for ever.

None of that was a property of Yocto. It was a property of the code having been
written down the distro path. `install_fleet_integration` is now shared by both
builders: the identity marker, the watchdog, the self-disarm, the perf sysctls
and the diagnostics dump. Exercised against the real Dev@CI image
(2026-08-20) — marker with `builder=yocto`, all five units, sysctls — without
booting anything.

Two portability fixes fell out of it, because the distro path had hardcoded its
own layout: the diagnostics dump and the self-disarm both looked for
`/boot/firmware`, which is where *Ubuntu* mounts the boot partition. Yocto
mounts it at `/boot`. Both now find it.

**The one thing still blocking a yocto rpi5** is upstream and small.
`Tools/yocto/targets.conf` on `webkitglib/2.48` has no `[rpi5-64bits-mesa]`
section — while the branch already ships `Tools/yocto/rpi/local-rpi5-64bits-mesa.conf`
and the pinned `meta-raspberrypi` already carries `raspberrypi5.conf`. Only the
stanza tying them together is missing. `wk image build rpi5-wpe-2.48` refuses
before spawning a build and prints the eight lines to add.

## The lifecycle

Steps 0–3 exist **only for a bench-device**; a workstation arrives already at
step 4 with its own host mode.

    0  nothing            a board, a blank card, a reader in this workstation
    1  wk sysimage build seed-rpi                                   [profile missing]
    2  wk sysimage flash <id> --reader /dev/sdX                     [MISSING]
       -- card into the board, power on
    3  wk provision <machine>                                       [PARTIAL]
       tailnet, keys, boot order, fleet record
    ---------------------------------------------------------------------------
    4  wk sysimage build perf-linux-<machine>                       [built]
    5  wk sysimage write <id> --disk <machine>:<device>             [built]
    6  wk boot <machine>            host mode -> bench mode, one shot [built]
    7  wk bench stage / staged      the run                          [upstream]
    8  self-returns to host mode                                     [built]

    wk unprovision <machine>        forget it; leave the hardware alone  [MISSING]

### What is actually missing

1. **`wk sysimage flash --reader`.** Every write path resolves its target
   through the fleet and goes over ssh (`cmd/image:890`), so a card reader
   attached to *this* workstation cannot be named. This is why provisioning is
   not automatic: there is no command that writes the first card.

   It needs root on the workstation, and it should **not** get a NOPASSWD
   helper. "Write any block device as root, without a password" is a far larger
   grant than the port-69 one, and the step is inherently hands-on — you are
   standing there with a card. An interactive sudo costs nothing.

2. **A recorded system for the card.** The rpi4's card holds a hand-flashed
   WebKit Dev@CI Yocto image that nothing in this repo produces or records —
   `wk sysimage build rpi4-wpe-2.48` now produces that system, so step 1 is a
   naming and flashing problem rather than a build one. The rpi3's 32-bit base
   question, which used to block this, is closed: see "32-bit and 64-bit".

3. **`wk provision` / `wk unprovision`.** `wk pi setup` is most of provision
   (tailnet, key, allowlist) but presupposes a running, reachable board, and
   `wk pi boot-order` is the other half. Neither records the device in a fleet
   registry, because the registry is a `case` statement in `boot/machines.sh`
   rather than config. Roles cannot be changed without editing code today.

4. **A device registry.** `boot/machines.sh`'s case statement should become
   `machines/<name>.conf`, the same shape `targets/hosts/*.conf` already uses,
   carrying `role`, `hardware`, `ssh`, `seed_profile`, `bench_profile`,
   `device`, `mac`, and `os`. `MACH_OS` is not cosmetic: `wk boot mbp --status`
   run from Linux used to exit 127 partway through printing a status, because
   nothing recorded that the machine is macOS-only.

## The devices

Everything on the tailnet or in the fleet, and what it is for.

### Fleet — machines `wk boot` can put into bench mode

| device | hardware | role | arming | notes |
|---|---|---|---|---|
| `moose` | Ampere, 80c, 128 GB, Ubuntu | workstation, build-host | — | the machine wk is driven from; bench mode not built |
| `tolken` | Apple Silicon Mac (M4) | workstation | `hands-on` | `mbp` in the fleet; boots the `WK Bench` volume, chosen by a person |
| `rpi5` | Pi 5, NVMe + WiFi | workstation | `one-shot` | firmware register; NVMe untouched; **64-bit only**; distro *and* yocto bench modes |
| `rpi4` | Pi 4B, **4 GB**, wired | bench-device | `medium` | USB stick is bench mode, SD card is host mode; 32- and 64-bit |
| `rpi3` | Pi 3, armv7l, 931 MB | bench-device | — | **not provisioned**, no DNS entry; 32- and 64-bit systems both named now |
| `benchvm` | macOS guest | bench-device | — | scriptable rehearsal for tolken |

### Auxiliary — not booted by wk, but part of how the fleet is reached

| device | role | notes |
|---|---|---|
| `librem5-oob` | tailnet-bridge | Librem 5; DHCP, routing and tailscale proxy for the `bmc0` segment. Called "bmc" today; `docs/HANDOFF-bmc-2.md` scopes doing the same job with a spare Android phone |
| moose's ASPEED BMC | out-of-band console | `10.99.0.2`, behind the bridge; KVM-over-IP and virtual media |
| `fbi-surveillance-gateway` | infrastructure | home gateway |
| `nextcloud`, `immich`, `overleaf`, `nc`, `leaf` | infrastructure | home services; `tag:server`, which the Pi ACL must not reuse |

### Build hosts — `wk new --target`, never booted

`buildbox4`, `devbox-arm64-2`, `moose`. Reachable-but-unmanaged work machines
(`devbox-armhf-2/4`, `arm-bothost-*`, `rpi4-compilers-0`) are ssh entries only
and deliberately not fleet members — `rpi4-compilers-0` is why
`host/dotfiles.sh` now refuses a hand-written stanza naming a fleet machine.

### Not wk's

`justin-iphone`, `karen-ipad`, `karens-macbook-pro`.
