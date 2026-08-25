# HANDOFF — the device lifecycle, and the words it uses

The rename landed 2026-08-20 and the old spellings are refused by name. This
file is what the words mean and what is still missing from the lifecycle they
describe. The binding repo-wide rules live in `CLAUDE.md`; the machine roster
lives in `boot/machines/*.conf` and `wk help hardware`.

## The lifecycle

Steps 0-3 exist **only for a bench-device**; a workstation arrives at step 4
with its own host mode, which wk must never touch.

    0  nothing            a board, a blank card, a reader in this workstation
    1  wk sysimage build seed-rpi                                   [profile missing]
    2  wk sysimage flash <id> --reader /dev/sdX                     [MISSING]
       -- card into the board, power on
    3  wk provision <machine>                                       [PARTIAL]
       tailnet, keys, boot order
    ---------------------------------------------------------------------------
    4  wk sysimage build perf-linux-<machine>                       [built]
    5  wk sysimage write <id> --disk <machine>:<device>             [built]
    6  wk boot <machine>            host mode -> bench mode, one shot [built]
    7  wk bench stage / staged      the run                          [upstream]
    8  self-returns to host mode                                     [built]

    wk unprovision <machine>        forget it; leave the hardware alone  [MISSING]

## Remaining

1. **`wk sysimage flash --reader`.** Every write path resolves its target
   through the fleet and goes over ssh, so a reader attached to *this*
   workstation cannot be named — which is why first provisioning is not
   automatic. It needs root here and must **not** get a NOPASSWD helper: "write
   any block device as root without a password" is a far larger grant than the
   port-69 one, and the step is inherently hands-on anyway. Details:
   `docs/HANDOFF-sdcard.md`.
2. **A recorded system for the first card.** The rpi4's card holds a
   hand-flashed image nothing in this repo produces.
   `wk sysimage build webkit-2.52-yocto-rpi4-64` produces an equivalent, so
   this is a naming and flashing problem rather than a build one. `seed` may
   never need a profile of its own — a seed's job is to boot and answer ssh so
   `wk provision` can do the rest, and the downstream Yocto image already does
   that on both Pis.
3. **`wk provision` / `wk unprovision`.** `wk pi setup` is most of provision
   (tailnet, key, allowlist) but presupposes a running, reachable board, and
   `wk pi boot-order` is the other half. Nothing walks the lifecycle, and
   nothing removes a device cleanly.
4. **One interactive provisioning command** composing 1-3, where the hands-on
   steps (a card in a reader, a power cable) are prompts rather than
   documentation.
5. **The layers have not moved.** No directory moves, no selftest enforcing the
   dependency rule — `docs/HANDOFF-architecture-review.md`.

## The words

**Roles** — declared per device in its conf, changed by editing one field.
Nothing about a role may be baked into a command.

| role | wk owns | has a bench mode |
|---|---|---|
| `workstation` | nothing on it | yes, borrowed for one boot |
| `bench-device` | every system on it | it is always the point |
| `build-host` | nothing; it compiles | no |
| `tailnet-bridge` | its system | no |
| `infrastructure` | nothing | no |

The one difference that matters: a **bench-device**'s host mode is a system wk
builds and owns, so wk can provision it from nothing; a **workstation**'s host
mode is *your install*, and wk can only add a bench mode beside it.

**Modes** — `mode=host|bench`, one shot, `in_bench_mode` in code.

**Systems** are named by what they are for, so adding a customer configuration
later is adding a profile: `seed` (boots and answers ssh), `perf`
(measurement-grade, matching a distro), `bridge` (a tailnet-bridge's own
system), `downstream` (a public image, for customers or evaluation). A
downstream profile carries its device (`-rpi4`, `-rpi3-32`, …) because
`IMG_MACHINE` is a per-profile fact and two boards' images must not share a
name.

**Builders**, not commands: a **distro system** is seeded from a
general-purpose distribution's own image and tuned for measurement — it stands
for what a normal Linux machine looks like; a **yocto system** is built from
source and is the embedded product image — what the customer's device runs. Both
are `wk sysimage build`, both enter one store, and `wk boot` can put a machine
into either. The identity marker records `builder=` so a run never infers it
from a profile name. "A distro perf run" and "a yocto perf run" are different
measurements of the same board, and both are wanted.

**32-bit means a 32-bit system** — a 32-bit kernel and a 32-bit userspace —
never a 32-bit process borrowing a 64-bit kernel's compat layer. They differ in
syscall path, page size and kernel pointer width, and the armhf port that ships
to customers runs the first. A number from one is not a number from the other,
which is why the run has to record `kernel_arch` beside `arch`
(the run records `kernel_arch` beside `arch`).

**The layers** (decided 2026-08-20; binding for new code, per `CLAUDE.md`).
Command names are minted only when a layer gains a second consumer.

| word | layer | rule |
|---|---|---|
| `home` | home topology, tailnet-bridge/BMC, router, home services | config only — nothing may import it |
| `lab` | target drivers, fleet/roles/modes, sysimage, boot, provision, quiesce/session, bench mechanics | must not know what WebKit is |
| `wk` | the WebKit project kit | wraps `Tools/Scripts`, never replaces it |
| `field` | crash dumps, perf dashboards, symbolization | consumes project kits; kept thin; upstreams what it grows |
| `stock` | not a layer: the pristine environment profile plus onboarding | a profile of `lab`, never a fifth codebase |

Dependency, one direction only: `home` provides facts as config; project kits
drive targets through the `lab` contract; `lab` depends on nothing; `field`
consumes project kits and nothing needs it.
