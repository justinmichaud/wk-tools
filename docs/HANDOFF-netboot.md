# Handoff: booting an image — the shared substrate

**Moved to the front of lane A on 2026-08-19, at the user's direction**, and
**scoped to all three machines the same day**, also at the user's direction:
whatever gets built here has to boot **moose** and the **MBP**, not just the
rpi5. It was previously buried inside `docs/HANDOFF-benchmarking.md` as an open
design question ("who serves TFTP/NFS"); it is now its own step, first, because
four later steps all consume it:

| consumer | what it needs from here |
|---|---|
| profiling (`docs/HANDOFF-profile.md`) | a machine with no sandbox, where `perf_event_paranoid` is ours to set |
| benchmarking (`docs/HANDOFF-benchmarking.md`) | `bench_host=image` — the whole machine, perf-tuned, remote-driven |
| cross-compile (`docs/HANDOFF-cross-compile.md`) | a slim distro with no SDK on it, to run a cross-built GTK MiniBrowser |
| yocto + `wk pi setup` (`docs/HANDOFF-yocto.md`, `docs/HANDOFF-linux-pi.md`) | a way to boot a freshly built rpi4 image without touching an SD card |

Profiling comes first of the four, deliberately: it is the least demanding
consumer (it wants no-sandbox, not perf stability), so it proves the mechanism
without also having to settle the storage question benchmarking raises.

## The headline: "netboot" is not one mechanism, and one machine cannot do it

Three machines, three last miles, and they do not converge:

| machine | mechanism | remotely armable? |
|---|---|---|
| rpi5, rpi4, rpi3 | firmware network boot (`BOOT_ORDER` nibble 2), one-shot via `set_reboot_order` | **yes** — one SSH command |
| moose (Ampere, ASPEED BMC) | UEFI HTTP/PXE boot, **or** BMC virtual media | **yes** — either path |
| MBP (M4) | personalised external volume. **Netboot does not exist here.** | **no** — authenticated, hands-on |

**Apple Silicon cannot boot from the network, at all.** NetBoot and NetInstall
were Intel-era; the Apple Silicon boot chain requires a LocalPolicy held in the
machine's own secure storage, so there is nothing to hand an image to over the
wire. Confirmed again 2026-08-19 while scoping this: boot volume selection goes
through LocalPolicy (`bputil -d` to inspect), and changing it means System
Settings → Startup Disk or Startup Security Utility in Recovery — both
*authenticated user actions*, not scriptable ones. `bless --setBoot` has been
superseded for this purpose and its `folder` option survives on Apple Silicon
only for external media. (eclecticlight.co's LocalPolicy and
external-bootable-disk write-ups are the clearest references; `bless(8)` for
what is left of the tool.)

So the honest shape is **two tiers**, and the design must not pretend
otherwise:

- **Tier 1 — armed remotely, no media, no hands.** rpi5, rpi4, rpi3, moose. One
  command puts the machine in the image for one boot; the next boot is normal.
- **Tier 2 — the MBP.** A macOS install on an external SSD, *personalised for
  that Mac* (installed or blessed from it, not merely copied to it), selected by
  an authenticated action. **Accepted by the user 2026-08-19 on the condition
  that a switch is only authentication plus a reboot** — which is what it is,
  once the volume exists: System Settings → General → Startup Disk, pick it,
  authenticate as a volume owner, restart, and the same three steps to come
  back. Recovery and Startup Security Utility are needed only for changing the
  security policy, not for selecting a personalised macOS volume, and the switch
  can be driven over Screen Sharing on an unlocked machine. The one-time cost is
  the install/personalisation itself, which is hands-on per Mac. What it is
  *not* is automatic — nothing arms a Mac for a run the way one SSH command arms
  a Pi, so a scheduled or unattended macOS run does not exist. The fallback worth
  costing is a second APFS system volume in the internal container, which also
  removes the external-SSD-is-not-the-normal-storage variable from any
  disk-touching benchmark.

**What actually unifies the three, then, is not the boot mechanism.** It is the
image store, the runner, and the vocabulary: one place images are built and
kept, one remote-driven runner, and `bench_host=image` recorded identically
whichever machine it ran on (`cmd/bench`'s header fixed that vocabulary on
2026-08-18). Build for that, with a per-machine **boot driver** behind it —
which is exactly the shape `targets/*.sh` already uses for
container/local/remote/vm, so the pattern is in the repo and needs no
invention.

Note the corollary for the Mac: its image is a *macOS install*, not the slim
Linux rootfs the other three boot. Same store, same runner, same recorded
fields — different contents, built a different way. Pretending one image serves
all three is the thing that would waste the most time here.

## The Pi one-shot primitive, confirmed 2026-08-19

The Pi 5 firmware has exactly the semantics this wants, with no EEPROM write per
run, no local state, and no physical access:

```sh
ssh rpi5 'sudo vcmailbox 0x0003808b 4 4 0xf4612 && sudo reboot'
```

`set_reboot_order` passes a `BOOT_ORDER` to the bootloader through a reset-safe
register; "as with `tryboot`, this is a one-time setting and is automatically
cleared after use" (`config_txt/boot.adoc` — the command above is the
documentation's own network-boot example). `0xf4612` reads lowest-nibble-first:
network (2), SD (1), NVMe (6), USB (4), restart (f).

Three properties follow, and together they are why this is the mechanism:

- **It is one-shot.** Any later reboot is a normal NVMe boot, so the rpi5 is a
  workstation again without anyone having to remember to put it back.
- **It fails back rather than hanging.** Netboot is first for one boot only and
  the NVMe is next in the same order, so a server that is down or a run that
  wedges recovers by itself. This matters: a forum report has a Pi 5 with
  network-first in the *EEPROM* hanging outright rather than falling through
  (raspberrypi.com forums t=381280), and that failure mode needs a person in the
  room. Setting `BOOT_ORDER` permanently is therefore the thing not to do.
- **It never touches the workstation install.** No EEPROM overclock, no
  `config.txt` edit, nothing written to the NVMe — the firmware-boundary rule
  that `host/linux/rpi5/HANDOFF.md` and `docs/HANDOFF-benchmarking.md` both
  insist on. The perf tuning belongs to the image, which supplies its own
  `config.txt` over the wire.

`tryboot` is *not* the mechanism, and the benchmarking handoff's guess that it
would be is superseded: `tryboot` selects an alternate config on whatever medium
is already booting, which leaves the medium unchosen. `set_reboot_order` chooses
the medium, which is the actual question.

The rpi4 has firmware network boot too (same nibble, EEPROM bootloader), which
is what lets each yocto image be tested the moment it builds — no card, no trip
to the machine. It has no `set_reboot_order`, so arming it is an EEPROM config
change rather than a one-shot register; plan for that difference.

## moose: two routes, and it cannot serve its own boot

- **UEFI HTTP/PXE boot**, with the image as a RAM root so nothing on the NVMe is
  touched or even mounted. RAM is ample (115 GB free, measured earlier), so a
  full image in RAM is realistic and removes every question about disk state
  affecting a benchmark.
- **BMC virtual media.** moose has a real ASPEED BMC (SETUP.md §"Screen, GPU
  and egress" drives its display-only `ast` chip for `wk session --bmc`),
  reachable as `moosebmc` — 192.168.1.41, ssh on 2200 — so an image can be
  attached over the network and presented to the host as USB storage. Which BMC
  firmware it runs decides whether that is scriptable: virtual media differs
  between OpenBMC and AMI, and only some expose it over Redfish. Check before
  designing on it. It needs nothing from DHCP
  and it works when the machine is otherwise unreachable, which is also the
  recovery story `docs/HANDOFF-bmc.md` wants.

**The constraint that decides the topology:** the netboot server cannot be
moose, because moose is one of the clients. Nor can it be the MBP. It has to be
a machine that is always on and never a boot client. The same applies to the
image file behind BMC virtual media — an nbd export served by moose dies the
moment moose reboots into it.

**Netboot has no WiFi.** "Booting over wireless LAN is not supported, nor is
booting from any other wired network device" (`boot-net.adoc`) for the Pis, and
UEFI PXE/HTTP boot has no WiFi stack either. moose is WiFi-only today (`wlo1`
holds 192.168.1.40; all three wired NICs are DOWN with **carrier=0** — no cable
in any of them, measured 2026-08-19). So a cable is required for moose to be a
boot client at all, and shape choices that avoid cabling only ever applied to
the Pi.

### Where the server goes — decided 2026-08-19, after measuring

The three machines "sit in separate areas", and the Pi can be cabled to the
LAN. Two measurements settle the rest:

- **The Proxmox host is out.** `fbi-surveillance-gateway` (x86_64, Debian 13,
  PVE 7.0.14-6, 55 GB free) is the ideal always-on server on paper — never a
  boot client — but it sits on 192.168.4.60/24 and **is not reachable from the
  house LAN at all**: from moose the route goes via 192.168.1.254 and dies.
  moose only reaches it over the tailnet, and firmware cannot use tailscale.
  Unless that segment gets a routed path or a port on 192.168.1.0/24, it cannot
  serve a bootloader. It stays useful as an off-machine *image store*.
- **Bandwidth stops mattering once the root is in RAM.** A squashfs pulled once
  at boot and pivoted into RAM means the server's link speed is a boot-time
  cost, not a per-run variable — so the server does not need to be co-located
  with the client, on the same switch, or even wired. That is what makes
  "separate areas" a non-problem.

### The serving role floats — required 2026-08-19

The user's requirement: *any* device that happens to be free should be able to
serve, so moose and the rpi5 could both be served by the Mac when that is the
idle machine. That is a constraint on three things, and only one of them is
awkward:

- **Running the daemons anywhere.** Cheap. Linux has `tftpd-hpa`/`dnsmasq` +
  any static HTTP server; macOS has `/usr/libexec/tftpd` under launchd, a
  built-in NFS server, and a static HTTP server is a one-liner. No machine is
  excluded.
- **Having the image.** Make the image store content-addressed and syncable, so
  "this machine can serve" means "this machine has the blob, or can fetch it".
  The PVE host is the natural always-on origin even though it cannot serve
  firmware — it is reachable from everywhere over the tailnet, which is enough
  for *storing* images, just not for booting from them.
- **The client's server address, which is baked into firmware.** This is the
  awkward one: `TFTP_IP` lives in the Pi's EEPROM and names one address, so a
  server that moves breaks it. The fix is not to re-flash EEPROMs — it is a
  **service alias IP** that whichever machine is serving claims for the
  duration: `ip addr add 192.168.1.9/24 dev <if>` on Linux, `ifconfig en0 alias
  192.168.1.9` on macOS. One address in firmware, any machine behind it. moose's
  BMC virtual-media path needs none of this, because the image URL is supplied
  at attach time and is therefore already server-agnostic.

Keep the election explicit and dumb: `wk boot <machine> --server <host>`, with
`--server auto` walking a priority list and taking the first candidate that
answers and is not the machine under test. Nothing to debug at 2am.

With that in place the default arrangement is just a policy, not an
architecture:

- **rpi5, rpi4, rpi3 ← moose.** `tftpd-hpa` + a static HTTP root on moose, over
  its WiFi link. With `TFTP_IP` and the static-IP keys set, the Pi's firmware
  skips DHCP entirely, so there is no broadcast-domain requirement and no
  second DHCP server on the family LAN — only routability. Cable the Pi to the
  LAN and that holds.
- **moose ← the rpi5** (in its always-on workstation role), because moose cannot
  serve its own boot: an HTTP or nbd export on moose dies the moment moose
  reboots into the image it is serving. This is the one dependency that has to
  point away from moose.
- **The Librem 5** (`docs/HANDOFF-bmc.md`) stays what it is — DHCP and routing
  for the isolated guest network, attached to moose over USB Ethernet
  (`cdc_ether`, carrier up, 425 Mb/s). It is not the image server: a phone
  serving multi-gigabyte roots is the wrong shape, and it was unreachable as of
  2026-08-19.

moose's own last mile then depends on whether a cable can reach it. If yes, UEFI
HTTP boot from the rpi5's root. If no, **virtual media through its BMC** —
moose has a real ASPEED BMC with KVM-over-IP (SETUP.md's session `--bmc` mode
drives its display chip; ssh on `moosebmc`, 192.168.1.41:2200, though it did not
answer on 2026-08-19). That path needs nothing from DHCP and works when the
host's NICs are down, which is also `docs/HANDOFF-bmc.md`'s recovery story.
Confirm which BMC stack it runs before designing on it — virtual-media support
differs between OpenBMC and the AMI firmware, and only some expose it over
Redfish.

DHCP is the part that looked hard and is not. Either the server owns DHCP on a
segment of its own (`dnsmasq --interface=` bound to one NIC, no way to leak onto
the house LAN), or, for the Pis specifically, the firmware skips DHCP entirely:

> "If `TFTP_IP` and the following options are set then DHCP is skipped and the
> static IP configuration is applied." — `eeprom-bootloader.adoc`, *Static IP
> address configuration* (`CLIENT_IP`, `SUBNET`, `GATEWAY`)

and `TFTP_IP` is documented for exactly this case ("useful on home networks
because tftpd-hpa can be used instead of dnsmasq where broadband router is the
DHCP server"). With DHCP skipped, the option-43 `"Raspberry Pi Boot"` match
(`PXE_OPTION43`) never comes up either. A second DHCP server on the family LAN
is therefore never necessary. UEFI boot on moose does need DHCP, so moose's
segment has to be one the server owns.

## Storage: what the image is

Netboot fixes how a machine *starts*; where its root lives is separate, and the
two consumers want different answers. Phase it:

1. **Network root, for profiling.** The image is a directory on the server,
   editable in place — the fastest iteration loop while the mechanism is being
   proven. Good enough for `wk profile`, which wants an unsandboxed machine and
   a writable `perf_event_paranoid`, not perf-stable storage.
2. **RAM root, for benchmarking.** A small initramfs pulls a squashfs over HTTP
   into RAM and pivots into it. 16 GB on the rpi5 and 115 GB free on moose make
   this realistic on both, and it removes storage from the measurement
   entirely — the point of `bench_host=image`.

Do not TFTP a multi-gigabyte root: firmware TFTP is slow, and `boot_ramdisk`'s
`boot.img` is capped at 180 MB anyway (it is the *boot* filesystem, not the
rootfs). Unsigned `boot.img` is explicitly fine for netboot if wanted later
(`config_txt/boot.adoc`, `boot_ramdisk`).

### How big is the image — measured on moose, 2026-08-19

Everything below is measured on this machine (aarch64) unless it says estimate.
The method matters for the ones marked *closure*: `ldd` against the built
`MiniBrowser`, resolved and de-duplicated, excluding anything inside the build
tree.

| piece | size | notes |
|---|---|---|
| Ubuntu base rootfs | 103 MB (24.04) / 131 MB (26.04) compressed | ~350 MB unpacked; the starting point |
| dpkg `required`+`important` set | 155 MB | what "a working Linux" costs |
| GTK MiniBrowser library closure | **294 MB**, 187 libs | *closure*; WPE is within noise of this |
| fonts | 50 MB | needed for any rendering benchmark |
| mesa / EGL / GL / gbm / vulkan | 188 MB | gpu-class only |
| weston + wayland | ~50 MB with deps | gpu-class only |
| NVIDIA userspace (moose) | 515 MB of 901 MB installed | `gl` 393 + firmware 101 + decode 20; the 320 MB CUDA `compute` package is not needed, and a prebuilt kernel module matching the image's kernel is |
| tailscale (static Go binaries) | ~40 MB | same two binaries `wk pi setup` pushes |

Which totals:

- **cpu-class image** (jsc shell, no compositor, no GPU): ~650 MB unpacked →
  **~250–300 MB squashfs**.
- **gpu-class image** (MiniBrowser + weston + mesa): ~1.15 GB unpacked →
  **~450–550 MB squashfs**.
- **gpu-class on moose** (add the NVIDIA render stack): ~1.7 GB unpacked →
  **~800 MB–1 GB squashfs**.
- **Pi TFTP stage** (firmware, kernel, DTB, initramfs): 30–60 MB, and this is
  the only part that crosses the slow path.
- **RAM at boot**, keeping the squashfs compressed under an overlay with a tmpfs
  upper: ~1–1.5 GB resident worst case. Nothing on a 16 GB Pi, nothing on moose.

For scale, the two things *not* to ship: `wkdev-sdk` is **14.6 GB** and
`wkdev-sysroot` is **1.87 GB**. The image is an order of magnitude smaller than
the SDK because it only has to *run* WebKit, and it is smaller than the sysroot
because a sysroot carries headers and static archives a running machine never
opens.

**Keep the benchmark payloads out of the image.** Seeded, they are 810 MB
(JetStream3 690 MB, Speedometer3 119 MB, MotionMark 1.5 MB), they are pinned per
run by `wk bench`, and baking them in would rebuild the image every time a
payload is re-pinned. They belong beside the build products.

**The payload partition** wants 8–16 GB, and 32 GB if a few build generations
should coexist: WebKit's *runtime* products are GTK 422 MB (bin 170 + lib 252),
WPE 570 MB (bin 323 + lib 247), JSCOnly 74 MB — against 1.8–1.9 GB for the whole
build tree, which is why only `bin` and `lib` get copied.

**The MBP is the one that needs real media, and 32 GB will not do it.** A macOS
install is ~20–25 GB before APFS snapshot headroom, and the Apple-port build
products go beside it, so **120–128 GB is the practical floor** — and it wants
to be an SSD, not a flash drive: USB-flash IO would poison any benchmark that
touches disk, which is the same reason the second-internal-volume option is
worth costing. `docs/HANDOFF-benchmarking.md`'s "ideally a 32 GB flash drive
works" holds only for the Linux images, and those need no media at all.

**The payload partition.** A WebKit build with debug info is gigabytes and does
not belong in the image on either phase — it changes every build; the image
should not. Give it a dedicated local partition on each machine, mounted by the
image, written by the transfer helper. That keeps the image small and central
while the thing that is actually rebuilt lands on local storage at local speed,
and it answers the benchmarking handoff's "what drive size is required" for the
netboot tier: none, no external media.

What is *not* mounted: the workstation install's own filesystems. If the image
depends on the installed OS's state, the two roles stop being separable.

## The sysroot equivalence — why cross-compile gets cheap

The image rootfs and the cross-build sysroot should be **the same tree**, built
once and used two ways: exported as a sysroot for `wk build --sysroot` on moose,
and booted on the target. Same libraries, same versions, by construction — so
"does the cross-built binary match the target's ABI" stops being a question
anyone has to test, and the slim image's *lack* of an SDK becomes the test
rather than an obstacle. That is what makes the GTK MiniBrowser run a real check
of `docs/HANDOFF-cross-compile.md` and not a rehearsal.

## One-command reproducible, everywhere — required 2026-08-19

The user's rule for this whole substrate: **every part of it is reproducible by
one command.** Nothing here may end up as a wiki recipe, a hand-edited
`dnsmasq.conf`, a manual `rpi-eeprom-config` session or a remembered `dd`. That
is the same principle `docs/HANDOFF-claude.md` applies to skills — an instruction
an agent has to *perform* is a defect; the deterministic verb is the deliverable —
and it applies with more force here, because these steps touch firmware and
physical media where a half-remembered variant is expensive.

Concretely, the verbs this step owes, each idempotent and each with `--dry-run`
(the pattern `wk build` already established):

| verb | does | notes |
|---|---|---|
| `wk image build <profile>` | builds the slim rootfs + squashfs from a spec **in this repo** | profiles: `stock`/`oc`, cpu-class/gpu-class, per-arch. Reproducible from a clean checkout, no interactive steps |
| `wk image stage <machine>` | pushes products + payloads to the machine's local storage | the transfer path `docs/HANDOFF-cross-compile.md` also needs |
| `wk serve <segment>` | brings up DHCP/TFTP/HTTP and claims the service alias IP | idempotent; refuses if a bench run is live on this host |
| `wk boot <machine> [--image P] [--back]` | arms the one-shot: `vcmailbox` on the rpi5, `reboot "0 tryboot"` for a partition, EEPROM order for rpi4/rpi3, BMC virtual media for moose, the staged gate for macOS | one verb, five drivers behind it |
| `wk pi netboot-enable <host>` | writes the client-side firmware config (`BOOT_ORDER`, `TFTP_IP`, static IP) | **the EEPROM is never hand-edited**; this is the only writer |
| `wk pi flash <image> [device]` | image → SD/USB, with the removable-device refusal | already scoped in `docs/HANDOFF-sdcard.md`; consume it, do not duplicate |
| `wk verify <machine>` | proves a machine is in the expected state (which image, which kernel, which profile, which root device) | extends the existing `wk verify`; this is what makes "reproducible" checkable rather than claimed |

Two rules that follow from "one command" and are easy to lose:

- **The spec lives in the repo, not on a machine.** Rootfs package lists, the
  image's `config.txt`, the fan policy, the dnsmasq/TFTP layout, the partition
  map — all files under version control, applied by the verbs above. A machine
  is then disposable in the same sense a workspace is, which is what makes a Pi
  wipe survivable (`docs/HANDOFF-linux-pi.md` notes the rpi5 tree is currently
  the one part of a wipe nothing recreates — do not add a second such tree).
- **Reproducible includes the reverse.** `--back` and `wk pi flash` of the
  recovery image are part of the deliverable, not afterthoughts: the reason the
  one-shot mechanisms were chosen over permanent EEPROM changes is that undo is
  a command rather than a trip to the device.

## What to do

Topology is settled (see below), so this is an execution list now. Each item is
a verb from the table above, not a procedure.

0. **Unblock SSH to the rpi5.** As of 2026-08-19 `tailscale ping rpi5` pongs
   while TCP 22 times out, so the board is up and a path exists but something
   refuses the connection — `sshd` on the board, or more likely the tailnet ACL.
   Nothing below can start without it: every arming mechanism is an SSH command.
   Leading hypothesis to test first: **moose is itself a tagged node** (SETUP.md
   notes `tag:server` covers moose), and a tagged source does not match
   `autogroup:member`, so the SETUP.md grant set — `tag:wk → tag:wk` plus
   `autogroup:member → *` — would never authorise moose → rpi5. If that is it,
   the same gap will block `wk pi setup` reaching the rpi4/rpi3 later, so fix it
   in the ACL rather than per-device.
1. **`wk image build stock`** for aarch64: slim rootfs, **stock kernel**, fan
   policy, `os_check=0`, the image's own `config.txt`. Reproducible from a clean
   checkout — spec in the repo, no interactive steps.
2. **The rpi5 first, because it needs no server.** `wk pi flash` the image onto a
   small USB SSD, then `wk boot rpi5` → `vcmailbox 0x0003808b 4 4 0xf64` +
   reboot lands in the image; a plain reboot lands back in the workstation. Prove
   the negative too: with the USB device absent, the same command falls through
   to the NVMe rather than hanging.
3. **Profiling in that image** — `perf_event_paranoid` and the JIT-dump
   environment are ours to set outright there, which is the whole point and the
   reason profiling leads (`docs/HANDOFF-profile.md`).
4. **`wk serve`** on the rpi5's `eth0` (or moose's `igb`): DHCP with option 43,
   TFTP, HTTP, service alias IP, bound to that interface only. Refuses while a
   bench run is live on the same host.
5. **rpi4, over that segment or the LAN.** `wk pi netboot-enable rpi4` writes
   `BOOT_ORDER`/`TFTP_IP`/static IP — the EEPROM's only writer — then netboot a
   test image. Measured runs move to local storage: USB stick now, SSD later,
   recorded either way.
6. **rpi3 on the direct cable**, the one client that needs real DHCP with option
   43. Netboot to test images; measure from the SD card, because its Ethernet is
   behind the USB controller.
7. **moose**: UEFI HTTP boot to a RAM root, and BMC virtual media via `moosebmc`
   as the path that survives a dead LAN. Served by the rpi5 or the MBP — never by
   moose itself.
8. **Benchmarking**: `wk bench` learns to drive a machine that is not this one,
   records `bench_host=image` plus the new `kernel_provenance`/`profile`/
   `root_device` fields, and refuses a measured run whose content origin is not
   loopback.
9. **The MBP, separately**: second internal APFS volume, the shared data volume,
   the staging gate, and the one-command switch tested for real (`bless --mount
   --setBoot`) with the four-click fallback documented if it fails.

`wk claude` must refuse against an image throughout, for the same reason it
refuses on remote targets: there is no sandbox in there.

## Topology, fixed 2026-08-19 — and the one conflict in it

The user's assignment: servers all on moose; **rpi3 on a direct Ethernet cable
to moose**; **rpi4 on the LAN**; **rpi5 staying on WiFi**; macOS on a second
internal volume. Three of the four work as stated. The fourth cannot, and the
replacement is better:

### The rpi5 cannot netboot on WiFi — use a local one-shot instead

Netboot is wired-only, on every Pi ("Booting over wireless LAN is not
supported, nor is booting from any other wired network device",
`boot-net.adoc`), so a WiFi-only rpi5 is not a netboot client at any price. It
does not need to be: the same one-shot semantics exist for a *local* alternate
boot, verified in the docs 2026-08-19.

`autoboot.txt` selects a boot partition and honours a `[tryboot]` filter:

```ini
[all]
tryboot_a_b=1
boot_partition=2      # the workstation
[tryboot]
boot_partition=3      # the perf image
```

`sudo reboot "0 tryboot"` boots partition 3 once; a normal reboot is back on 2.
`tryboot_a_b=1` makes the switch partition-level, so no `tryboot.txt`/
`tryboot.img` files are needed. Simpler still, the partition can be named
directly as a reboot parameter — `sudo reboot 3` — which `autoboot.adoc`
documents as overriding `boot_partition` for that boot only.

Two ways to lay it out, and the second is the recommendation:

1. **Extra partitions on the workstation NVMe.** Bootable partitions must be
   FAT12/16/32 containing `config.txt` (on Pi 5), with the rootfs elsewhere — so
   a FAT boot partition plus an ext4 root, on top of the two the workstation
   already has. That is 4 of 4 MBR partitions, and whether the firmware will
   read a GPT NVMe here needs checking before anything is repartitioned.
2. **The perf image on a small USB SSD**, selected one-shot with
   `vcmailbox 0x0003808b 4 4 0xf64` (USB-MSD first, then NVMe, then loop). No
   repartitioning of the workstation disk at all, nothing at risk, and the image
   is pushed over WiFi by rsync while the board is a workstation — a
   ~500 MB squashfs over WiFi is seconds. **Preferred.**

This is strictly better than netboot for this board: no server involved, no
network traffic during boot *or* run, the same one-shot-and-revert behaviour,
and the workstation install untouched. It also means moose only ever serves the
rpi3 and rpi4.

### The rpi3's direct cable is exactly what its boot ROM needs

The rpi3 is the one device that *requires* a real DHCP reply carrying option 43
`"Raspberry Pi Boot"`, because its network boot lives in the boot ROM rather
than an EEPROM bootloader — the one case where the "no second DHCP server" rule
would have been a problem. A dedicated cable to moose removes it entirely:
`dnsmasq` bound to that one interface owns a private segment, hands out DHCP
with option 43 and serves TFTP, and nothing touches the house LAN. Use moose's
`igb` port (`enP2p3s0`, plain 1GbE) rather than either `bnxt_en`.

The trap on this board is *not* booting, it is measuring: **on the Pi 3 the
Ethernet is behind the USB controller** (LAN7515 on the 3B+), so network root
and USB storage contend for the same ~300 Mbps of shared bus. Netboot the rpi3
to *test* an image; run measured benchmarks from the SD card (or a RAM root if
its 1 GB allows, which for a browser benchmark it mostly will not). The
`rpi3` skill's OOM-and-swap notes are the same constraint seen from the other
end.

### rpi4 on the LAN, at 2 GB

Confirmed 2026-08-19 as the 2 GB model, which settles the open question from the
section below: **do not RAM-root measured runs on this board.** A ~500 MB
squashfs plus a tmpfs overlay is a quarter of its memory, and on 2 GB a browser
benchmark is already close enough to memory pressure that this would show up in
the numbers rather than beside them. RAM root stays fine for *testing* a freshly
built image, where nothing is being measured.

So the rpi4 keeps the arrangement below: netboot as the test channel, USB SSD as
the root and payload store for anything measured.

### Network reality check, 2026-08-19 — the rpi5 is not on the LAN

Scanned from three vantage points before assuming anything:

| vantage | segment | result |
|---|---|---|
| moose (`wlo1`) | 192.168.1.0/24 | `nmap -sn` plus an ARP-forcing sweep of all 254 addresses: **6 hosts, no Raspberry Pi OUI** (no `b8:27:eb`/`dc:a6:32`/`d8:3a:dd`/`e4:5f:01`/`2c:cf:67`) |
| the PVE host | 192.168.4.0/24 | 5 hosts, no Pi |
| librem5-oob (`bmc0`) | 10.99.0.0/24 | one lease: `9c:6b:00:75:5a:d7` → `10.99.0.2`, hostname `altrad8ud2-1l2q` — that is **moose's own BMC**, not a Pi. Useful side-discovery: the BMC's real address is 10.99.0.2 on the Librem 5's OOB segment, so the `moosebmc` → 192.168.1.41 entry in `~/.ssh/config` is stale |

**Those scans prove nothing about the rpi5's subnet, and an earlier draft of this
file wrongly said they did.** Every one of them ran *after* the board dropped off
the tailnet at 15:37, so its absence from an ARP table is expected regardless of
where it lives. The netmap carries `Addrs: None` for it — no advertised
endpoints — so its LAN address cannot be recovered from moose either. The
"NATing second AP" theory that draft proposed was answering the wrong question:
a NATed client still appears on tailscale, which is exactly what this one did.
The scans were still worth running — they are how the BMC's real address turned
up — but they are not evidence about the Pi.

What the peer record *does* say, and this is the useful part:

```
RxBytes 0    TxBytes 0    LastHandshake 0001-01-01 (never)
LastWrite 09:50 (our attempts)    LastSeen 15:37    Online false
```

**moose and the rpi5 have never completed a WireGuard handshake, and not one byte
has ever flowed between them.** A degraded or NATed path would show attempts and
some traffic; zero of both does not. And the pong that made the board look
reachable proves less than it appears to — `tailscale ping`'s own help says it
"does not inject packets into either side's TUN devices", so it exercises the
disco layer, which is not subject to ACLs or host firewalls, and never touches
the data path.

So the signature is: disco-reachable, zero tunnel traffic, silent drops on every
TCP port (22 and 9999 behaved identically, and a reachable host with nothing
listening would have sent a RST). That is packets being dropped **before the
tunnel** — the ACL's source-side filter, or a firewall on the Pi — which brings
it back to the tag problem: both boards carry `tag:workstation`, and tagged
sources never match `autogroup:member`, so nothing in the SETUP.md §7 grant set
ever authorised this pair. Not proven, but it is the only hypothesis all the
numbers fit.

**The clean discriminator, to run the moment the board is back:**

```sh
tailscale ping rpi5          # disco layer: bypasses ACLs and firewalls
tailscale ping --icmp rpi5   # real ICMP through the tunnel: subject to both
```

If the first pongs and the second does not, it is the ACL or the Pi's firewall,
and the ACL is the one to fix first because the same gap will block `wk pi setup`
from reaching the rpi4 and rpi3 later. If both work, the SSH failure was
transient and the real problem is only that the board keeps dropping off the
network.

Separately and still true: the board went **offline at 15:37** and stayed there,
which is its own fault to chase — and until it is reliably online, the rpi5's
local one-shot (USB SSD, no server involved) is the only part of its plan that
needs nothing from the network.

### The rpi5's Ethernet is now free, so it can be the server

A consequence of the rpi5 booting locally: its Ethernet port is no longer needed
for its own boot, so it can serve the other two. That is the "any free device
serves" requirement satisfied with hardware that is already there, and it takes
moose out of the loop entirely.

Three arrangements, depending on how much cable is acceptable:

- **rpi3 direct to the rpi5's `eth0`.** dnsmasq bound to that one interface owns
  a private segment and can hand out DHCP with option 43, which is the thing the
  rpi3's boot ROM insists on. The rpi5 keeps reaching the LAN over WiFi, so
  nothing is lost by giving its only port away.
- **A cheap switch on the rpi5's `eth0`, with rpi3 and rpi4 both on it.** The
  tidiest end state: one isolated benchmark segment, one DHCP+TFTP server, no
  DHCP on the house LAN at all, and it is also the "isolated guest network"
  SETUP.md §7 wants before `wk pi setup`.
- **rpi4 over the house LAN instead**, served by the rpi5 over its WiFi link.
  Works because the rpi4 skips DHCP entirely (`TFTP_IP` + static IP), so the
  server only has to be routable, and the transfer is boot-time only.

Whichever is used, the serving side is interchangeable with moose — same
daemons, same files, and the service alias IP means the client's firmware does
not care which machine is behind it.

## Perf risks in this arrangement — asked 2026-08-19

Grouped by whether they can change a number.

### Two that would silently corrupt results

**The perf image must carry the tuning that lives in the workstation install.**
The role split (`host/linux/rpi5/HANDOFF.md`) put fan-max and the NUMA kernel on
the "stability, installed OS" side and the overclock on the "perf, image" side —
and that split is wrong in two places for an image that boots on its own:

- **Fan control — the image needs its own copy.** `fan-max.service` lowers the
  trip points and pins pwm=255 on the *installed* OS. This one survives the
  stock-kernel rule because it is **measurement hygiene rather than tuning**: a
  board that drops into thermal throttle partway through a run produces a number
  that is not repeatable, which is a different problem from being
  unrepresentative. Keeping the SoC out of throttle makes runs comparable with
  each other; it does not make the silicon faster than a customer's. Record the
  fan policy with the result and say so.

  The same question lands on the **overclock**, and the two decisions pull
  against each other: "what customers ship" argues for stock clocks, while the
  earlier role split assigned the overclock to the image. Recommended resolution,
  not yet confirmed by the user: the image carries **two profiles** — `stock`
  (the default: stock kernel, stock clocks, fan policy for hygiene) and `oc` (an
  explicit opt-in) — with the profile recorded in every result so the two never
  merge into one series. That keeps the shipping-configuration principle intact
  while leaving the overclock available for headroom experiments.
- **The kernel — decided the other way, 2026-08-19: the image runs a STOCK
  kernel.** The rule the user set is that perf results represent *what customers
  ship*, and customers do not ship `CONFIG_NUMA_EMU`. So the custom
  `7.0.6-numa` kernel is a **workstation/dev convenience, not a benchmark
  configuration**, and the image must not carry it. Three consequences to hold
  onto, because the earlier draft of this file argued the opposite:

  - Image numbers on this board will be **lower** than tuned-workstation numbers
    on memory-bandwidth-bound work, and that is the correct result, not a
    regression. The tuned workstation was measuring a configuration nobody
    ships.
  - Any historical rpi5 number taken on the numa kernel is **not** the baseline
    going forward. Start the series again on the image.
  - `SDRAM_BANKLOW=1` is EEPROM state shared with the workstation, so the
    *banking* stays even on a stock kernel; a stock kernel simply does not act
    on it. Leave the EEPROM alone (that rule has not changed) and record the
    kernel with the result, which `cmd/bench` already does.

**Never serve a boot from a machine that is measuring.** Serving is only
boot-time traffic, so it cannot touch a client's run — but it absolutely touches
the *server's* own run: dnsmasq, TFTP, HTTP and the page cache all land on a
machine that is supposed to be quiet. Encode it rather than remember it:
`wk boot` refuses to serve from a host with a live bench run, and `wk bench`
refuses to start on a host currently serving one. Same shape as the existing
preflight refusals.

### Where the network can still get into a measurement

- **The benchmark content must be served from localhost on the device under
  test**, not from the driving machine. If `run-benchmark`'s HTTP server lives on
  the runner and the browser fetches over WiFi, WiFi jitter is in the score. The
  payload is on local storage for exactly this reason; make sure the harness uses
  it there.

  **Required 2026-08-19: this is coded, not documented.** The user's instruction
  is that the benchmarks themselves avoid WiFi jitter, so it becomes a preflight
  refusal in the same family as the existing environment checks:

  1. the run executes on the device under test, with the driver only issuing
     start/collect — no cross-machine fetch inside a measured iteration;
  2. preflight asserts the content origin resolves to **loopback**, and refuses
     otherwise;
  3. preflight asserts nothing in the run path is a network mount (no NFS root,
     no network-mounted payload) — which also catches the phase-1 profiling
     layout being used for a measured run by accident;
  4. the transport used for control is recorded with the result, so a run driven
     over WiFi is distinguishable from one driven over Ethernet even though
     neither should matter.
- **Control traffic is fine.** ssh and result collection are low-bandwidth and
  happen around the run, not inside it.
- **The rpi3 is the exception and cannot be fixed.** Its Ethernet is behind the
  USB controller, so *any* network activity contends with storage on the same
  ~300 Mbps bus. Netboot to test; measure from the SD card; do not netboot-root a
  measured run on that board.

### Per-machine, the rest

- **rpi5.** Perf image on USB SSD: the Pi 5's USB and Ethernet both hang off
  RP1's PCIe link, which has ample headroom, and the workstation NVMe is idle
  during an image run. WiFi is the only route to the runner while `eth0` serves
  the rpi3 — a reliability risk, not a perf one, and the WiFi-stability half of
  the rpi5 tuning is what covers it.
- **rpi4 (2 GB).** Memory pressure is the whole story: local root, no RAM root
  for measured runs. Its Ethernet is a separate GENET MAC, so disk and network do
  not contend the way they do on the rpi3.

  **Storage, 2026-08-19: a USB stick to start, an SSD once purchased.** The stick
  is a real substrate for bringing the flow up and a poor one for numbers — cheap
  flash has no DRAM cache, no TRIM and erratic random IO, which lands as variance
  rather than as a bias you can subtract. So while the stick is in use: record the
  root device (model, link speed, rotational/TRIM flags) in the run environment
  alongside kernel and governor, mark those runs provisional, and **do not compare
  stick numbers with SSD numbers** once the SSD arrives — same rule as
  `bench_host`. If a run must be defensible before the SSD arrives, prefer a RAM
  root and accept the 2 GB memory cost, or measure cpu-class only.
- **moose.** A RAM root is free here (115 GB), but gpu-class runs need the
  matching NVIDIA kernel module *and* userspace in the image or they degrade to
  software rendering — which `wk bench` will refuse, correctly, and which is the
  most likely reason a first moose image run fails.
- **macOS.** The second volume is on the same physical SSD, so storage is not a
  variable — but two things on that volume are: **Spotlight indexing and Time
  Machine local snapshots** should be off on both the perf volume and the shared
  data volume, or they add IO mid-run. And it is a laptop: sustained MotionMark
  or Speedometer rounds on an M4 will thermally throttle, so runs need a cooldown
  between them and the thermal state recorded with the result.

### Comparability

`bench_host=image` versus `container` is already known to be incomparable
(`cmd/bench` warns). Add one: **an rpi5 image run is not comparable with an rpi5
workstation run either** — different kernel, different root, different tuning.
Switching the board to image-based benchmarking starts a new baseline rather than
continuing the old series.

## The rpi4: netboot as the test channel, not the run substrate

Asked 2026-08-19: is netboot realistic on the rpi4 without hurting its numbers?
Yes, if netboot is used for the *image-test loop* and not for measured runs. The
two roles have opposite requirements and the hardware answers them differently.

What the Pi 4 can and cannot do:

- **Network boot: yes**, same EEPROM bootloader, same `BOOT_ORDER` nibble 2, and
  the same `TFTP_IP`+static-IP keys — so the DHCP-free arrangement above works
  here too, which matters more on this board than on the rpi5 because the Pi 4's
  DHCP path is the one with the documented five-tries/25-second timeout.
- **One-shot arming: no.** `set_reboot_order` is Raspberry Pi 5 only. But the
  rpi4 is a *dedicated test device*, not a workstation, so it does not need
  one-shot semantics: set `BOOT_ORDER` to network-first-then-local permanently
  and arming becomes a server-side act — serve the files and it netboots, empty
  the TFTP root and it falls through to its local image. No access to the device
  is needed to arm a run at all, which is strictly better than what the rpi5
  gets.
- **USB is fast enough, if it is an SSD and not a stick.** The Pi 4's USB 3.0
  host sits behind a VL805 on PCIe Gen2 x1 (~3.4 Gbps ceiling shared across the
  ports), which in practice means ~300–380 MB/s with a UASP SSD, against
  ~45–90 MB/s for the SD card. Crucially the Pi 4's Ethernet is a separate GENET
  MAC, *not* on the USB bus the way the Pi 3B+'s was, so disk and network do not
  contend. A cheap flash stick is the thing to avoid: its random-IO behaviour is
  what would show up in a benchmark.

So the shape, refining the user's proposal:

1. **USB SSD holds root + WebKit payload**, and the Pi 4 can boot from it
   outright (USB-MSD in `BOOT_ORDER`), so it is also the boot volume.
2. **SD card holds a recovery image** — flashed once, never in the measured
   path. Worth having precisely because it is the thing that makes a wedged
   netboot recoverable without a trip to the device.
3. **Netboot is how a freshly built yocto image gets tried** before it is
   committed to the USB SSD. A WPE/cog yocto image is small (hundreds of MB), so
   netbooting it into a RAM root is cheap and touches no local storage at all.
4. **Measured runs read from the USB SSD or from RAM, never from the network.**
   That is the whole answer to "without impacting perf": the network is used
   between runs, not during them.

Two things to check on the actual board when it is up: **how much RAM it has**
(a ~500 MB squashfs plus a tmpfs overlay is nothing on 8 GB and a quarter of a
2 GB board, which would itself be a benchmark variable), and the bootloader
date, since network and USB boot both want a reasonably recent EEPROM.

The rpi3 is the awkward one and should not be assumed to follow: its network
boot is in the *boot ROM*, not an EEPROM bootloader, so it needs a real DHCP
reply carrying option 43 `"Raspberry Pi Boot"` — the one case where the
no-second-DHCP-server rule does not hold. `bootcode.bin` on an SD card is the
documented workaround for exactly this, and `boot-net.adoc`'s "Known problems"
list (25-second DHCP timeout, ARP mid-transfer, no DHCPREQUEST) is all Pi 3
material.

## The macOS image: how big, and on what

Measured/derived 2026-08-19 from what this repo already knows:

- A bare macOS install is ~30 GB (SETUP.md's prepared macOS + Xcode image is
  **~69 GB compressed** to pull and `macos-runner:tahoe` is **520 GB on disk**,
  but the benchmark image needs *no Xcode* — it runs WebKit, it does not build
  it).
- Apple-port build products go beside it, a few GB per configuration; the
  checkout and the tens-of-GB build tree stay on the workstation side.
- Plus APFS snapshot and log headroom.

So **~50 GB used, and a 128 GB volume is comfortable**. It fits a 128 GB USB
stick *by capacity* — and that is the wrong way to choose it. A flash stick's
random-IO behaviour is a benchmark variable, and macOS is far more sensitive to
random IO than the Linux images are; the M4 has 32 GB of RAM (SETUP.md §5) so
paging is rare, but app launch, framework loading and any disk touch during a
run all land on that device. In order of preference:

1. **A second internal APFS system volume** in the existing container — same
   storage the machine normally runs on, so nothing about the measurement
   changes, and no media to lose. Needs ~150 GB free.
2. **An external NVMe SSD** over USB 3.2 or Thunderbolt (500–3000 MB/s). Fine.
3. **A USB flash stick.** Fits, and should not be used for numbers.

### The disk budget, and whether it fits

Decided 2026-08-19: **a second internal volume**, which is the option that keeps
the measurement honest. The arithmetic matters because this Mac is the tightest
machine in the fleet — `docs/HANDOFF-mac-minibrowser.md` measured **~149 GB
free**, against a Tart guest disk provisioned at `WK_VM_DISK_GB=320` (sparse) and
a `macos-runner:tahoe` image that was rejected precisely because 520 GB does not
fit.

The fact that makes this affordable: **APFS volumes in one container share free
space and preallocate nothing.** A second *volume* is not a partition — it grows
as it is used, so the question is headroom, not carving.

| new consumer | size | note |
|---|---|---|
| macOS install, no Xcode | ~30 GB | it runs WebKit, it does not build it |
| Command Line Tools (python3 for `run-benchmark`/`compare-results`) | 1.5–3 GB | **verify it is needed at all** before budgeting it |
| Apple-port products, one configuration | 2–4 GB, estimated | measure a real `mac-release` and replace this number |
| benchmark payloads | ~1 GB | 810 MB seeded today |
| APFS snapshots, logs, paging headroom | 15–20 GB | |
| **total** | **~50–60 GB** | against ~149 GB free |

So it fits, with roughly 90 GB of slack — but the slack is shared with the Tart
guest's sparse disk, which grows every time a build runs inside it. Two rules
follow: **measure before installing** (`df -h /System/Volumes/Data`,
`diskutil apfs list`) and treat a floor as a refusal condition rather than a
hope, the way `cmd/bench`'s preflight already does for environment. The
recoverable space, if it comes to it, is stale Tart images and clones and old
build trees inside the guest.

Not in this budget, deliberately: the WebKit checkout and the build tree. Those
stay on the building side — ~19 GB and tens of GB respectively — and the perf
volume never sees them.

### The build gate: nothing can be built after the reboot

The user's constraint, and it is the sharpest one on this machine: **moose cannot
build for macOS**, so the Mac is the only builder, and the moment it reboots into
the perf install it stops being one — that install has no Xcode by design. A
missing configuration discovered after the switch costs a reboot back, a build,
and another authenticated switch.

So the products must be staged *before* the switch, and the switch must refuse if
they are not:

- **A shared APFS data volume** in the same container, mounted by both installs,
  holds products, payloads and run output. Free space is shared, so it costs
  nothing extra, and it means the perf install reads what the workstation install
  produced without copying twice.
- **Products come out of the Tart guest**, since `wk build mac-release` runs
  inside a VM — rsync over the vmnet bridge onto the shared volume, which is the
  same path `docs/HANDOFF-mac-minibrowser.md` already uses for seeding.
- **`wk boot mac --perf` preflights and refuses**, on: every requested config
  present and complete (a manifest plus the WebKit sha it was built from);
  payloads staged; the perf install reachable (Remote Login and tailscale
  configured); and the perf install's macOS major version matching what the
  products were built against — a framework built against one SDK and run on a
  different major version is a dyld failure at best and a silent behavioural
  difference at worst. Same philosophy as `cmd/bench`: check the environment
  before the run, do not explain it afterwards.
- **A `--stage` verb** so the gate is normally already satisfied, and a dry run
  that lists exactly what would be measured.

One thing to verify early, because it decides whether Command Line Tools are in
the budget: what `Tools/Scripts/run-benchmark` and `compare-results` actually
need at *runtime* on a machine with no Xcode — python3 certainly, possibly
`xcrun`. If a stock macOS python is enough, the perf install stays smaller and
simpler.

### "One command, then leave it"

The requirement is looser than automation and that helps: one command at the
Mac, then the machine is remotely controlled for the run. The flow that
satisfies it without depending on anything unproven:

- The perf install has Remote Login and tailscale enabled at build time, so once
  the Mac is *in* it, everything after that is remote — including `reboot`,
  which stays in the perf install because that install is the selected startup
  disk. Runs, re-runs and a full benchmark sweep need nobody in the room.
- Getting into it is the authenticated step, and it is once per session, not
  once per run.

Whether that authenticated step can be literally one command is **untested and
must be tested on the machine** (lane B): `sudo bless --mount /Volumes/<perf>
--setBoot` is the candidate, and the documentation is genuinely ambiguous —
`--setBoot` with explicit boot paths is documented as unsupported on Apple
silicon, while the `folder` option is documented as supported *only* for
external media, which is precisely this case. Community reports go both ways and
some say neither `bless` nor `systemsetup` still works. So: try it, and if it
fails, the fallback is System Settings → Startup Disk → authenticate → Restart,
which is four clicks rather than one command. Do not build AppleScript UI
automation around it; that is a fragile answer to a once-per-session problem.

One Intel-era belief to drop while doing this: Apple silicon has no "allow
booting from external media" toggle in Startup Security Utility — that was T2.
Each bootable volume group carries its own LocalPolicy, created when the volume
is installed or first selected from that Mac.

## Traps

**The rpi5 has no `cmdline.txt`.** Boot args come from
`/proc/device-tree/chosen/bootargs`, injected by firmware
(`host/linux/rpi5/rpi5-numa-README.md`). A netboot config that assumes it can
edit `cmdline.txt` to add `ip=`/`nfsroot=` will find no such file on the
installed OS — the image supplies its own boot medium over TFTP, so put them
there, and check what the firmware already injected before adding
`numa_policy`/`numa=fake` by hand.

**`os_check=0`.** Pi 5 firmware rejects locally-built kernels for lacking
Ubuntu's trailer, and marked the NUMA kernel's tryboot 'bad' until `os_check=0`
was set in `config.txt` (same README). Any kernel served over TFTP is
locally-built by that definition. Put `os_check=0` in the *image's* `config.txt`.

**A power cycle is not the same as a reboot.** The Pi's one-shot register is
reset-safe, so it survives a warm reboot on purpose. If a run wedges the machine
hard enough to need the plug pulled, confirm which image it landed in on the way
back rather than assuming.

**Do not put the overclock in the EEPROM.** `SDRAM_BANKLOW` and `BOOT_ORDER` are
firmware state shared by both roles; an overclock written there overclocks the
workstation too, which is the exact split this design exists to preserve.

**The Mac's boot volume cannot be switched by script.** If a plan depends on
"reboot the MBP into the benchmark install remotely", the plan is wrong — see
the tier-2 note above. Say so in the docs instead of building it.
