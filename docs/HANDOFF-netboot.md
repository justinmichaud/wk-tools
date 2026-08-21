# Handoff: booting a system — the shared substrate

**Moved to the front of lane A on 2026-08-19, at the user's direction**, and
scoped to all three machines the same day: whatever gets built here has to boot
**moose** and the **MBP**, not just the rpi5. It was previously buried inside
`docs/HANDOFF-benchmarking.md` as an open design question ("who serves
TFTP/NFS"); it became its own step, first, because four later steps all consume
it:

| consumer | what it needs from here | status |
|---|---|---|
| profiling (`docs/HANDOFF-profile.md`) | a machine with no sandbox, where `perf_event_paranoid` is ours to set | served: the rpi5's booted system has `-1`, and `wk profile` exists |
| benchmarking (`docs/HANDOFF-benchmarking.md`) | `bench_host=image` — the whole machine, perf-tuned, remote-driven | the boot half is served; the image-lane runner is still owed there |
| cross-compile (`docs/HANDOFF-cross-compile.md`) | a slim distro with no SDK on it, to run a cross-built GTK MiniBrowser | not served yet — see "The sysroot equivalence" |
| yocto + `wk pi setup` (`docs/HANDOFF-yocto.md`, `docs/HANDOFF-linux-pi.md`) | a way to boot a freshly built rpi4 image without touching an SD card | served: `wk sysimage build downstream-yocto-*`, `wk serve`, `wk boot` |

Profiling came first of the four, deliberately: it is the least demanding
consumer (it wants no-sandbox, not perf stability), so it proved the mechanism
without also having to settle the storage question benchmarking raises.

## State as of 2026-08-20 — the verbs exist, and two boards have used them

The mechanism this file was written to design is built, exercised on hardware,
and checked (`docs/TESTING.md` §7 is the ledger; `wk selftest --section
netboot` exercises the resolver and its guards offline):

- **`wk sysimage build|ls|show|disks|write|rm`** (`cmd/sysimage`; profiles in
  `image/profiles.sh` plus `image/<spec>/`). Distro systems build entirely
  unprivileged — mtools and e2fsprogs edit the image at byte offsets, nothing
  is mounted, `wk` still never calls sudo on this host — in ~47 s from a
  cached base. Yocto systems bitbake in a workspace and enter the same store.
  The manifest is written last and is the whole publishing protocol; `write`
  happens *on* the machine over ssh, verified by read-back, and refuses the
  machine's own root device, anything mounted, and any non-usb transport.
- **`wk boot <machine> [--system <id>] [--status|--keep|--back|--disarm|--diag|--dry-run]`**
  (`cmd/boot`, fleet in `boot/machines.sh`, one driver per arming model —
  the same shape `targets/*.sh` uses). Arming leaves a record of intent on the
  machine, next to the boot mechanism; `--status` reports role and mode from
  evidence (the system's own `/etc/wk-image`, the firmware's registers) and
  calls a record *spent* by boot id, never by guessing.
- **`wk serve`** (`cmd/serve`, `boot/wk-tftpd.py`): TFTP for the boot files
  plus HTTP, filled from an image already in the store, refusing to start when
  the installed privileged helper differs from the checkout, when a client
  could be led into a halt (`boot/check-boot-files.py`), when the image's root
  is local-only, or when a bench run is live on this host.
- **`wk pi boot-order <host> <order>`** (`netboot-first|netboot-last|usb-first|local`),
  with `netboot-enable` kept as an older spelling of the same single writer.
  `boot/rpi-eeprom.sh` can flash a board that has no eeprom tooling of its own
  (staged `pieeprom.upd` + `recovery.bin`, pinned by sha256, verified before
  write).

Proven on hardware, both directions: the **rpi5** (2026-08-19: armed over ssh,
system up and reachable over WiFi in 53 s, `--keep`, `--back` in ~40 s, record
reported spent; `perf_event_paranoid=-1`, `kptr_restrict=0`, `perf` installed,
the JIT-dump wall in `/etc/wk-perf-env`) and the **rpi4** (2026-08-20: netboot
transfer end to end, then the whole USB-local lane with no hands on the board —
`docs/HANDOFF-benchmarking.md` records that lane closing, self-disarm and
watchdog return included).

**Still owed, here or nearby:**

- **Netboot has no root.** `wk serve` hands a client firmware, kernel, DTBs
  and initramfs — verified over the real LAN — but the images boot from
  `root=LABEL=wk-image-root`, a label that exists only on local disks, so a
  netbooted board would sit in an initramfs with nothing to mount. `wk serve`
  refuses to serve such an image and names `wk sysimage write` instead, so
  this is a message rather than a boot loop. Closing it means an NFS root
  (phase 1 below — `nfs-kernel-server` is not installed on moose, and `sudo`
  there needs a password, so that one `apt install` is a person's step; it
  does not belong in `./setup`, because serving is a role that floats and most
  machines will never hold it) or a RAM root (phase 2 — needs the initramfs
  rebuilt, which the unprivileged build path cannot yet do). Until one exists,
  netboot proves the transfer and nothing further — which is all the test
  channel needs.
- **The rpi3, entirely** — see its section below; the recommendation to wait
  stands, and the OTP door stays shut.
- **moose as a boot client** — neither UEFI HTTP boot nor BMC virtual media
  has been attempted; both routes are scoped below.
- **`wk status` showing an armed transition on the machine's line**
  (`docs/TESTING.md`, "Still owed here"). `wk`'s help text and the
  `is_host_only` refusals, once owed alongside it, are done.
- **The serving election** (`--server auto`, the service alias IP) is designed
  below and unbuilt; today the server is whichever machine runs `wk serve`.

**A note on moose's sudoers while passing.** `/usr/bin/tee` is NOPASSWD for
root, which is passwordless write access to any file on the system and
therefore equivalent to NOPASSWD root. It was presumably added for a specific
write. It is worth narrowing.

## The headline: "netboot" is not one mechanism, and one machine cannot do it

Five machines, and their last miles do not converge:

| machine | mechanism | remotely armable? |
|---|---|---|
| rpi5 | USB one-shot (`set_reboot_order`); netboot never applied — WiFi-only, and netboot is wired-only | **yes** — one SSH command |
| rpi4 | local USB boot, armed on the medium itself; netboot kept as the test channel | **yes** — one SSH command |
| rpi3 | firmware network boot from the boot ROM (option-43 DHCP), or local media | **yes**, once built |
| moose (Ampere, ASPEED BMC) | UEFI HTTP/PXE boot, **or** BMC virtual media | **yes** — either path |
| MBP (M4) | personalised volume. **Netboot does not exist here.** | **no** — authenticated, hands-on |

**Apple Silicon cannot boot from the network, at all.** NetBoot and NetInstall
were Intel-era; the Apple Silicon boot chain requires a LocalPolicy held in the
machine's own secure storage, so there is nothing to hand an image to over the
wire. Confirmed 2026-08-19: boot volume selection goes through LocalPolicy
(`bputil -d` to inspect), and changing it means System Settings → Startup Disk
or Startup Security Utility in Recovery — *authenticated user actions*, not
scriptable ones. `bless --setBoot` has been superseded for this purpose and its
`folder` option survives on Apple Silicon only for external media.
(eclecticlight.co's LocalPolicy and external-bootable-disk write-ups are the
clearest references; `bless(8)` for what is left of the tool.)

So the honest shape is **two tiers**, and the design must not pretend
otherwise:

- **Tier 1 — armed remotely, no hands.** rpi5, rpi4, rpi3, moose. One command
  puts the machine in the bench system for one boot; the next boot is normal.
- **Tier 2 — the MBP.** A macOS install on another volume, *personalised for
  that Mac* (installed or blessed from it, not merely copied to it), selected
  by an authenticated action. **Accepted by the user 2026-08-19 on the
  condition that a switch is only authentication plus a reboot** — which is
  what it is, once the volume exists. As of 2026-08-20 this has a driver
  (`boot/mac-volume.sh`) and its own arming model, `hands-on`: the driver
  checks what it can, records the intent, prints the ritual, and reboots
  nothing. `wk boot mbp --status` reports it as armed *and waiting for a
  person*, which is the honest reading of a transition no software can make.
  The machine also drives itself (`MACH_LOCAL`), because it is the only Apple
  Silicon machine here. What tier 2 is *not* is automatic — nothing arms a Mac
  the way one SSH command arms a Pi, so a scheduled or unattended macOS run
  does not exist. `docs/HANDOFF-mac-perf-mode.md` is the volume's own task;
  `docs/HANDOFF-benchmarking.md` has the staging that goes with it.

**What actually unifies the machines, then, is not the boot mechanism.** It is
the image store, the runner, and the vocabulary: one place systems are built
and kept, one remote-driven runner, and `bench_host=image` recorded identically
whichever machine it ran on. Behind that sits a per-machine **boot driver** —
exactly the shape `targets/*.sh` already uses, so the pattern needed no
invention. Note the corollary for the Mac: its "system" is a *macOS install*,
not the slim Linux rootfs the others boot. Same store, same runner, same
recorded fields — different contents, built a different way. Pretending one
image serves them all is the thing that would waste the most time here.

## The Pi one-shot primitive, confirmed 2026-08-19

The Pi 5 firmware has exactly the semantics this wants, with no EEPROM write
per run, no local state, and no physical access:

```sh
ssh rpi5 'sudo vcmailbox 0x0003808b 4 4 0xf64 && sudo reboot'
```

`set_reboot_order` passes a `BOOT_ORDER` to the bootloader through a reset-safe
register; "as with `tryboot`, this is a one-time setting and is automatically
cleared after use" (`config_txt/boot.adoc`). The order reads
lowest-nibble-first: USB (4), NVMe (6), restart (f).

Three properties follow, and together they are why this is the mechanism:

- **It is one-shot.** Any later reboot is a normal NVMe boot, so the rpi5 is
  back in host mode without anyone having to remember to put it there.
- **It fails back rather than hanging.** USB is first for one boot only and
  the NVMe is next in the same order, so a run that wedges recovers by itself.
  A forum report has a Pi 5 with network-first in the *EEPROM* hanging
  outright rather than falling through (raspberrypi.com forums t=381280), and
  that failure mode needs a person in the room. Setting `BOOT_ORDER`
  permanently is therefore the thing not to do on a machine with a host mode.
- **It never touches the host install.** No EEPROM overclock, no `config.txt`
  edit, nothing written to the NVMe — the firmware-boundary rule
  `host/linux/rpi5/HANDOFF.md` and `docs/HANDOFF-benchmarking.md` both insist
  on. The perf tuning belongs to the system, which supplies its own
  `config.txt` on its own medium.

`tryboot` is *not* the mechanism, and the benchmarking handoff's guess that it
would be is superseded twice over: `tryboot` selects an alternate config on
whatever medium is already booting, which leaves the medium unchosen — and on
this board it is already taken. The stock Ubuntu preinstalled image ships
`os_prefix=current/` with `[tryboot] os_prefix=new/` and `tryboot_a_b=1`: A/B
kernel staging, used by ordinary kernel updates, on *any* Ubuntu install of
this board. A perf system using tryboot would collide with them.

The rpi4 has the same EEPROM bootloader and the same `BOOT_ORDER` nibble but
**no `set_reboot_order`** — its one-shot is synthesised on the boot medium
instead (`boot/pi-usb.sh`, the `medium` arming model: the system disarms its
own stick on the way up, so every later reboot lands on the SD card). The rpi3
has neither; its network boot lives in the SoC boot ROM.

## moose: two routes, and it cannot serve its own boot

- **UEFI HTTP/PXE boot**, with the system as a RAM root so nothing on the NVMe
  is touched or even mounted. RAM is ample (115 GB free, measured 2026-08-19),
  so a full system in RAM is realistic and removes every question about disk
  state affecting a benchmark. UEFI boot needs DHCP, so moose's boot segment
  has to be one the server owns — and it needs a cable: moose is WiFi-only
  today (all three wired NICs measured `carrier=0` on 2026-08-19), and no
  netboot mechanism anywhere in this file has a WiFi stack.
- **BMC virtual media.** moose has a real ASPEED BMC, reachable as `moosebmc`
  at **10.99.0.2** on the Librem 5's `bmc0` segment, ssh on 2200 (the
  192.168.1.41 in `dotfiles/ssh/config` is stale; the lease was confirmed live
  2026-08-19). An image attached there is presented to the host as USB
  storage. It needs nothing from DHCP and works when the machine is otherwise
  unreachable, which is also `docs/HANDOFF-bmc.md`'s recovery story. Which BMC
  firmware it runs decides whether it is scriptable — virtual media differs
  between OpenBMC and AMI, and only some expose it over Redfish. Check before
  designing on it.

**The constraint that decides the topology:** the boot server cannot be moose,
because moose is one of the clients — an HTTP or nbd export on moose dies the
moment moose reboots into the image it is serving. The driving machine cannot
be moose either: today moose is the machine that runs `wk bench`, so an image
run on moose needs a second machine to hold the runner.

## Where the server goes, and how the role floats — decided 2026-08-19

Two measurements settled the placement:

- **The Proxmox host is out.** It is the ideal always-on server on paper —
  never a boot client — but it sits on 192.168.4.60/24 and is not reachable
  from the house LAN at all; moose only reaches it over the tailnet, and
  firmware cannot use tailscale. It stays useful as an off-machine *image
  store*.
- **Bandwidth stops mattering once the root is in RAM.** A squashfs pulled
  once at boot is a boot-time cost, not a per-run variable — so the server
  does not need to be co-located, on the same switch, or even wired. That is
  what makes "the machines sit in separate areas" a non-problem.

So: **the Pis are served by moose** (over its WiFi link — with `TFTP_IP` and
the static-IP keys set, Pi 4/5-class firmware skips DHCP entirely, so there is
no broadcast-domain requirement and no second DHCP server on the family LAN,
only routability); **moose is served by the rpi5 in host mode or by the MBP**,
never by itself. The Librem 5 stays what it is — DHCP and routing for the BMC
segment — and is not the image server: a phone serving multi-gigabyte roots is
the wrong shape.

The user's requirement on top: *any* idle machine should be able to serve.
That is a constraint on three things, and only one is awkward:

- **Running the daemons anywhere** — solved. `boot/wk-tftpd.py` is a stdlib
  TFTP daemon, chosen over tftpd-hpa precisely because a distro system service
  does not float (different package, unit and config per host, none on macOS);
  one stdlib file runs wherever `wk` runs, the same argument that made the
  egress proxy stdlib-only.
- **Having the image** — one store, and the artifact a netboot client gets is
  byte-identical to what `wk sysimage write` puts on a stick (`wk serve` fills
  its TFTP root with mtools at a byte offset, from the same `disk.img`).
- **The client's server address, baked into EEPROM.** The fix is not to
  re-flash EEPROMs per move — it is a **service alias IP** the serving machine
  claims for the duration (`ip addr add` / `ifconfig alias`): one address in
  firmware, any machine behind it. With an explicit, dumb election on top —
  `wk boot <machine> --server <host>`, `--server auto` walking a priority list
  — there is nothing to debug at 2am. **Neither the alias nor the election is
  built**; they become worth building when a second machine actually serves.

Two decisions inside the server worth keeping:

- **The serial-number directory is solved rather than configured.** Pi
  firmware prefixes every request with a directory named after the board's
  serial, which nobody can know before the board first asks. The server
  retries at the root when that directory is absent, through the same
  containment check, and logs which happened — so `TFTP_PREFIX` never has to
  be written into an EEPROM.
- **Privilege is one syscall wide.** Firmware speaks to port 69, so something
  must bind below 1024. The helper binds and then *drops to the invoking user
  before resolving a single path* — and refuses outright to serve with root
  still held: a root-owned server reading an arbitrary `--root` would be a
  read-only export of the whole filesystem over UDP. Installed by
  `admin/install.sh` beside the quiesce helper, same copied-to-root-owned-path
  guarantees.

**A container cannot serve this, measured 2026-08-19 so nobody re-derives it.**
Rootless podman cannot bind port 69 either way round — `-p 69:69/udp` is
refused (`rootlessport cannot expose privileged port 69`), and `--network host
--cap-add NET_BIND_SERVICE` still gets `bind: Permission denied`, because the
capability lands in the container's user namespace and the bind is checked
against the initial one. Podman's own suggested workaround — lowering
`ip_unprivileged_port_start` to 69 — is worse than the helper it would replace:
it opens 69–1023 to every local process, permanently, where the helper buys one
bind(2) on one path. And on macOS the podman machine sits behind user-mode NAT,
where TFTP's fresh-ephemeral-port data transfer is a known way to get a
protocol that half works. So the split stands: **everything that can be a
container is one; the bind is not one.**

## Storage: what the system is

Booting fixes how a machine *starts*; where its root lives is separate, and the
two consumers want different answers. Phase it:

1. **Network root, for profiling.** The root is a directory on the server,
   editable in place — the fastest iteration loop. Good enough for
   `wk profile`, which wants an unsandboxed machine and a writable
   `perf_event_paranoid`, not perf-stable storage. (This is the phase the NFS
   install above unblocks; it is also the layout the loopback preflight in
   "Perf risks" exists to keep out of measured runs.)
2. **RAM root, for benchmarking.** A small initramfs pulls a squashfs over
   HTTP into RAM and pivots into it. Realistic on the rpi5 (16 GB) and moose
   (115 GB free); it removes storage from the measurement entirely. **Not on
   the rpi4 for measured browser runs** — even at its corrected 4 GB, a
   squashfs plus tmpfs overlay is memory taken from the workload — which is
   half of why that board went to local USB instead
   (`docs/HANDOFF-benchmarking.md`, the rpi4 sections).

Do not TFTP a multi-gigabyte root: firmware TFTP is slow, and `boot_ramdisk`'s
`boot.img` is capped at 180 MB anyway (it is the *boot* filesystem, not the
rootfs). Unsigned `boot.img` is explicitly fine for netboot if wanted later.

### How big is the image — measured on moose, 2026-08-19

Measured on aarch64 unless marked estimate; the closures are `ldd` against the
built `MiniBrowser`, resolved and de-duplicated, excluding the build tree.

| piece | size | notes |
|---|---|---|
| Ubuntu base rootfs | 103 MB (24.04) / 131 MB (26.04) compressed | ~350 MB unpacked |
| dpkg `required`+`important` set | 155 MB | what "a working Linux" costs |
| GTK MiniBrowser library closure | **294 MB**, 187 libs | WPE is within noise of this |
| fonts | 50 MB | any rendering benchmark |
| mesa / EGL / GL / gbm / vulkan | 188 MB | gpu-class only |
| weston + wayland | ~50 MB with deps | gpu-class only |
| NVIDIA userspace (moose) | 515 MB of 901 MB installed | the 320 MB CUDA package is not needed; a kernel module matching the image's kernel is |
| tailscale (static Go binaries) | ~40 MB | if a system ever carries it |

Which totals: **cpu-class** ~650 MB unpacked → ~250–300 MB squashfs;
**gpu-class** ~1.15 GB → ~450–550 MB; **gpu-class on moose** ~1.7 GB →
~800 MB–1 GB; the Pi TFTP stage (firmware, kernel, DTB, initramfs) is
30–60 MB and is the only part that crosses the slow path. RAM at boot, squashfs
compressed under an overlay: ~1–1.5 GB resident worst case.

For scale, the two things *not* to ship: `wkdev-sdk` is 14.6 GB and
`wkdev-sysroot` is 1.87 GB. The image only has to *run* WebKit, and it is
smaller than a sysroot because a sysroot carries headers and static archives a
running machine never opens.

**Keep the benchmark payloads out of the image.** Seeded, they are 810 MB
(JetStream3 690, Speedometer3 119, MotionMark 1.5), they are pinned per run by
`wk bench`, and baking them in would rebuild the image every time a payload is
re-pinned. They belong beside the build products, on **the payload partition**:
8–16 GB, 32 GB if build generations should coexist — WebKit's *runtime*
products are GTK 422 MB, WPE 570 MB, JSCOnly 74 MB, against 1.8–1.9 GB for a
whole build tree, which is why only `bin` and `lib` get copied. A build changes
every day; the image should not. What is *never* mounted: the host install's
own filesystems — if the system depends on the installed OS's state, the two
modes stop being separable.

The MBP is the one machine that needs real capacity, and its budget has its
own section below.

## The sysroot equivalence — why cross-compile gets cheap

The image rootfs and the cross-build sysroot should be **the same tree**, built
once and used two ways: exported as a sysroot for `wk build --sysroot` on
moose, and booted on the target. Same libraries, same versions, by
construction — so "does the cross-built binary match the target's ABI" stops
being a question anyone has to test, and the slim image's *lack* of an SDK
becomes the test rather than an obstacle. That is what makes the GTK
MiniBrowser run a real check of `docs/HANDOFF-cross-compile.md` and not a
rehearsal.

## One-command reproducible, everywhere — required 2026-08-19

The user's rule for this whole substrate: **every part of it is reproducible by
one command.** Nothing here may end up as a wiki recipe, a hand-edited
`dnsmasq.conf`, a manual `rpi-eeprom-config` session or a remembered `dd`. That
is the same principle `docs/HANDOFF-claude.md` applies to skills — an
instruction an agent has to *perform* is a defect; the deterministic verb is
the deliverable — and it applies with more force here, because these steps
touch firmware and physical media where a half-remembered variant is expensive.

The verbs, as they landed (each idempotent, each with `--dry-run`):

| verb | does | status |
|---|---|---|
| `wk sysimage build <profile>` | builds a system from a spec **in this repo** | built; reproducible from a clean checkout, no interactive steps |
| `wk sysimage write <id> --disk <machine>:<device>` | system → the machine's removable disk, verified by read-back | built; absorbed the scoped `wk pi flash` — one flashing verb, not two |
| `wk serve` | TFTP + HTTP from the store, helper freshness and boot-file completeness checked first | built; the alias IP and `--server` election are not |
| `wk boot <machine> [--system S] [--back]` | the mode transition: one verb, one driver per arming model | built: `one-shot` (rpi5), `medium` (rpi4), `server` (rpi3), `hands-on` (mbp), `guest` (benchvm) |
| `wk pi boot-order <host> <order>` | the EEPROM's **only** writer (`netboot-enable` is an older spelling of it) | built; orders derived from what is there, so unknown nibbles survive and re-applying is a no-op |
| `wk bench stage <ws> --to <machine>` | pushes products + payloads to where the bench system will read them | built for the Mac; the Pi payload path is still owed |
| `wk verify <machine>` | prove a machine is in the expected state | **not built** — `wk verify` still takes only workspaces; `wk boot --status` covers the mode half from evidence |

`wk boot` is a mode transition, and the fleet status must see it: per
`docs/HANDOFF-workspace-state.md`, arming writes a record of intent (system,
who, when) next to the boot mechanism, the firmware's own one-shot state is
read back as evidence where the platform allows, and a record that outlives its
transition is reported as spent or desynced, never repaired silently. The one
missing piece is `wk status` showing the armed transition on the machine's
line.

Two rules that follow from "one command" and are easy to lose:

- **The spec lives in the repo, not on a machine.** Package lists, the
  system's `config.txt`, the fan policy, the partition map — files under
  version control, applied by the verbs above. A machine is then disposable in
  the same sense a workspace is (`docs/HANDOFF-linux-pi.md` notes the rpi5
  tree is currently the one part of a wipe nothing recreates — do not add a
  second such tree).
- **Reproducible includes the reverse.** `--back` and writing the rescue
  medium are part of the deliverable, not afterthoughts: the reason one-shot
  mechanisms were chosen over permanent firmware changes is that undo is a
  command rather than a trip to the device.

`wk claude` must refuse against a bench system throughout, for the same reason
it refuses on remote targets: there is no sandbox in there.

## Topology, fixed 2026-08-19

The user's assignment: servers on moose; **rpi3 on a direct Ethernet cable to
moose**; **rpi4 on the LAN**; **rpi5 staying on WiFi**; macOS on a second
internal volume. Three of the four work as stated; the rpi5's replacement is
better than what was asked for.

### The rpi5 cannot netboot on WiFi — the USB one-shot instead

Netboot is wired-only on every Pi ("Booting over wireless LAN is not
supported", `boot-net.adoc`), and **the rpi5 is never on the LAN** — clarified
by the user 2026-08-19 as a constraint, not a preference: its WiFi is its only
path to the house network and the tailnet, and its `eth0` exists to serve the
other boards on a private segment if that is ever needed. Only the rpi3 and
rpi4 have LAN drops.

The board's own state, measured 2026-08-19, settled the mechanism:

```
Raspberry Pi 5 Model B Rev 1.1, 15 GiB usable, kernel 7.0.6-1-numa, 8 NUMA nodes
bootloader          2025/12/08 (recent; set_reboot_order available)
BOOT_ORDER          0xf461      -> SD(1), NVMe(6), USB(4), restart(f)
EEPROM              SDRAM_BANKLOW=1, BOOT_UART=1, NET_INSTALL_AT_POWER_ON=1
config.txt          os_check=0; os_prefix=current/, [tryboot] os_prefix=new/
autoboot.txt        [all] tryboot_a_b=1
nvme0n1             p1 512M FAT /boot/firmware (366M free), p2 469G ext4 / (219G free)
eth0                DOWN (no cable)      wlan0 192.168.1.165/24
sudo                passwordless
```

Four consequences:

- **`tryboot` is already taken** (Ubuntu's A/B kernel staging, above), so the
  partition-plus-tryboot option is out — and repartitioning was never cheap
  anyway, since `p2` fills the disk.
- **The perf USB stays plugged in permanently.** `BOOT_ORDER=0xf461` reaches
  the NVMe before USB, so a normal boot always lands in host mode even with a
  bootable stick attached; arming is a one-shot `0xf64` when a bench boot is
  wanted.
- **The mechanism was confirmed live before anything depended on it**:
  `vcmailbox 0x0003808b 4 4 0xf461` — deliberately the *current* order, a safe
  no-op — returned `0x80000000` with the tag marked processed.
- **The system must come up reachable over WiFi, or a run cannot be driven at
  all** (raised by the user 2026-08-19). There is no wired fallback for this
  board, ever — so the WiFi credential, the authorised key, sshd, an identity
  marker, and **no first-boot resize-and-reboot step** (a self-reboot spends
  the one-shot) are all part of the build spec, not post-flash surgery. Until
  a system's WiFi is proven, the only console independent of the network is
  the UART — `BOOT_UART=1` is already set, so it needs only a cable.

Also from the dump: images are staged on `/` (219 GB free), never in the
366 MB firmware partition; and if a serving board ever feeds the rpi3, it also
has to *route* — `wk pi setup` installs tailscale on the served device, which
needs egress, so NAT from `eth0` out over WiFi would have to be explicit in
`wk serve` rather than discovered when the control plane is unreachable.

### The rpi4 — netboot proven, then retired to the test channel

The rpi4 netbooted for real on 2026-08-20, and the same day's evidence moved
its benchmark lane to local USB boot — the halt-on-incomplete-transfer failure
mode (below) is unacceptable for an unattended device, and a network root
would sit inside the measurement. The full scoping, the `medium` arming model,
and the closed lane live in `docs/HANDOFF-benchmarking.md`; the netboot path
stays exactly what this file always said it was — **the test channel for
images not yet committed to local storage** — and the profiling lane's
editable network root is still its future.

Board facts, corrected on hardware and now in `boot/machines.sh`: a 4 GB Pi 4B
Rev 1.5 (this file argued RAM-root impossibility from "2 GB", which was wrong;
the local-boot decision rests on the halt, not the RAM), wired on the house
LAN, running the WebKit Dev@CI Yocto image (scarthgap 5.0.2, kernel
6.6.22-v8) as its SD rescue system. Its DHCP leases are unstable — the
firmware's client and the kernel's present different identifiers, and three
live leases were observed at once — so `image_addr` finds it by declared MAC
(`MACH_MAC`) preferring REACHABLE neighbour entries, and `raspberrypi4-64.local`
is the only fixed name. Its bootloader was upgraded 2023-01-11 → 2026-05-17 by
`boot/rpi-eeprom.sh`'s staged path (the Yocto image has no eeprom tooling —
build the update here, stage `pieeprom.upd` + `pieeprom.sig` + `recovery.bin`
on the FAT partition, let the ROM apply and verify it; a corrupt transfer
flashes nothing).

One fleet-wide fix fell out of finding it: `Host rpi4` in `~/.ssh/config`
named a shared build box behind a ProxyJump, so every wk verb aimed at "rpi4"
was aimed at the wrong machine. `host/dotfiles.sh` now owns the whole of
`~/.ssh/config`: hand-written entries move to `~/.ssh/config.d/local`, and any
stanza naming a fleet machine is dropped, because that is a shadow rather than
a host.

### The rpi3 — wait, and do not burn the OTP

The rpi3 is the one device that *requires* a DHCP reply carrying option 43
`"Raspberry Pi Boot"`: its network boot lives in the boot ROM, not an EEPROM
bootloader — no `BOOT_ORDER`, no `TFTP_IP`, no way to skip DHCP. That does not
force a private segment: `dnsmasq` in **proxy mode** (`dhcp-range=<net>,proxy`)
supplies the PXE bits without assigning addresses, which the boot ROM's
documented flow explicitly allows — no second address-assigning server on the
family LAN. A direct cable (from the rpi5's `eth0`, or moose's plain-1GbE
`igb` port) is the fallback if the boot ROM's DHCP quirks defeat proxy mode.
Port 67 is privileged too: one more bind for the same helper.

Three prerequisites, all worth knowing before power-up:

- **Network boot needs `program_usb_boot_mode=1` written once from a working
  SD card, and OTP is a one-way door.** Confirm with
  `vcgencmd otp_dump | grep 17:` → `3020000a`. Decide deliberately.
- **A plain 3B cannot use a TFTP server on another subnet**, among several
  boot-ROM bugs fixed only in the B+ — and **which model this board is has not
  been established**.
- **Netboot is a test channel only on this board no matter what**: its
  Ethernet sits behind the USB controller (~300 Mbps shared with storage), so
  measured runs come off local media, and with 931 MB and no swap a RAM root
  is out for anything browser-shaped. The `rpi3` skill's OOM notes are the
  same constraint from the other end.

So: the rpi3's fastest route to being useful is the one the other boards
proved — `wk sysimage write` to local media, boot it — which needs no DHCP, no
OTP and no server. Netboot for it is worth doing only after the netboot root
exists and the rpi4 has proven the chain; otherwise the one-way door is taken
for a capability that cannot be exercised.

The 32-bit question this board carries was decided 2026-08-20:
`wk sysimage build perf-linux-rpi3` **refuses**, permanently — Ubuntu publishes
no armhf raspi image to seed it from, and a 32-bit run must measure a 32-bit
kernel and userspace, not an arm64 system on the fleet's only armv7l board.
Its perf systems are the Yocto profiles, which exist in both widths
(`downstream-yocto-wpe-2.48-rpi3-32` / `-64`); `docs/HANDOFF-vocabulary.md`,
"32-bit and 64-bit", has the whole argument.

Where it physically is: it was at `root@192.168.1.160` on the house LAN — one
more reason the "isolated guest network" premise `cmd/pi` used to cite is
retired (the Librem 5 is on the LAN at .151 and serves only the BMC segment;
there is no hidden segment) — and it is powered off.

## Perf risks in this arrangement — asked 2026-08-19

Grouped by whether they can change a number.

### Two that would silently corrupt results

**The perf system must carry its own copy of the tuning that lives in the host
install.** The old role split put fan-max on the installed OS and the
overclock on the image side, and both halves needed correcting for a system
that boots on its own:

- **Fan control — the image needs its own copy.** Fan-max survives the
  stock-configuration rule because it is **measurement hygiene rather than
  tuning**: a board that drops into thermal throttle partway through a run
  produces a number that is not repeatable, which is a different problem from
  being unrepresentative. It does not make the silicon faster than a
  customer's. Record the fan policy with the result and say so.
- **The kernel — decided 2026-08-19: the image runs a STOCK kernel.** Perf
  results represent *what customers ship*, and customers do not ship
  `CONFIG_NUMA_EMU`. The custom `7.0.6-numa` kernel is a host-mode
  convenience, not a benchmark configuration. Three consequences, because an
  earlier draft argued the opposite: image numbers on this board will be
  **lower** than tuned-host numbers on memory-bandwidth-bound work, and that
  is the correct result; any historical rpi5 number taken on the numa kernel
  is **not** the baseline going forward; and `SDRAM_BANKLOW=1` is shared
  EEPROM state that stays (a stock kernel simply does not act on it) — leave
  the EEPROM alone and record the kernel with the result.

  The overclock resolves the same tension the same way: the image carries two
  profiles — `stock` (default: stock kernel, stock clocks, fan hygiene) and
  `oc` (explicit opt-in) — recorded in every result so the two series never
  merge. (`bench` axis `profile`, `docs/HANDOFF-benchmarking.md`; no `oc`
  profile is built yet, and when one is, it goes in the image's
  `config.txt.append`, never the EEPROM.)

**Never serve a boot from a machine that is measuring.** Serving is boot-time
traffic only, so it cannot touch a client's run — but it absolutely touches
the *server's* own run. Encoded, not remembered: `wk serve` refuses to start
while a bench run is live on the same host.

### Where the network can still get into a measurement

- **The benchmark content must be served from localhost on the device under
  test**, not from the driving machine — otherwise WiFi jitter is in the
  score. Required 2026-08-19 as code, not documentation: the run executes on
  the device under test with the driver only issuing start/collect; preflight
  asserts the content origin resolves to **loopback** and that nothing in the
  run path is a network mount (which also catches the phase-1 profiling layout
  being used for a measured run by accident); and the control transport is
  recorded with the result. (Owed by the image-lane runner —
  `docs/HANDOFF-benchmarking.md`, "Fields the image runner has to record".)
- **Control traffic is fine.** ssh and result collection are low-bandwidth and
  happen around the run, not inside it.
- **The rpi3 is the exception and cannot be fixed** — Ethernet behind the USB
  controller. Netboot to test; measure from local media.

### Per-machine, the rest

- **rpi5.** Perf system on USB: the Pi 5's USB and Ethernet hang off RP1's
  PCIe link with ample headroom, and the host NVMe is idle during a bench
  boot. WiFi is the only route to the runner — a reliability risk, not a perf
  one, covered by the WiFi-stability half of the rpi5 tuning.
- **rpi4.** Memory pressure is the story: local root, no RAM root for measured
  runs. Its Ethernet is a separate GENET MAC (not on the USB bus the way the
  Pi 3B+'s is), and its USB 3.0 host behind the VL805 does ~300–380 MB/s with
  a UASP SSD — so an SSD is a real substrate and a cheap stick is not:
  no DRAM cache, no TRIM, erratic random IO, which lands as **variance rather
  than a subtractable bias**. While the stick is in use, record the root
  device in the run environment, mark those runs provisional, and never
  compare stick numbers with SSD numbers — same rule as `bench_host`.
- **moose.** A RAM root is free here, but gpu-class runs need the matching
  NVIDIA kernel module *and* userspace in the image or they degrade to
  software rendering — which `wk bench` will refuse, correctly, and which is
  the most likely reason a first moose image run fails.
- **macOS.** The second volume is on the same physical SSD, so storage is not
  a variable — but Spotlight indexing and Time Machine local snapshots must be
  off on it, or they add IO mid-run. And it is a laptop: sustained rounds on
  an M4 throttle, so runs want a cooldown between them and the thermal state
  recorded (`wk quiesce status` reads `CPU_Speed_Limit` now).

### Comparability

`bench_host=image` versus `container` is already known to be incomparable
(`cmd/bench` warns). Add one: **an rpi5 image run is not comparable with an
rpi5 host-mode run either** — different kernel, different root, different
tuning. Switching the board to image-based benchmarking starts a new baseline
rather than continuing the old series.

## The MBP: the disk budget, and what it decided

Decided 2026-08-19: **a second internal APFS volume**, the option that keeps
the measurement honest — an external SSD is not the storage the machine
normally runs on, and a USB flash stick's random IO is a benchmark variable in
its own right. (`docs/HANDOFF-mac-perf-mode.md`, the live task, still weighs
the external SSD as the simple alternative for runs where internal storage is
itself a variable to remove.) The arithmetic mattered because this Mac is the
tightest
machine in the fleet (~149 GB free, measured), and the fact that makes it
affordable is that **APFS volumes in one container share free space and
preallocate nothing** — a second volume is headroom, not carving:

| new consumer | size | note |
|---|---|---|
| macOS install, no Xcode | ~30 GB | it runs WebKit, it does not build it |
| Command Line Tools | 1.5–3 GB | needed: Apple's python3 is the one with PyObjC |
| Apple-port products, one configuration | 2–4 GB estimated | a real `mac-release` staged 1.5 GB of products |
| benchmark payloads | ~1 GB | 810 MB seeded today |
| APFS snapshots, logs, headroom | 15–20 GB | |
| **total** | **~50–60 GB** | against ~149 GB free |

The slack is shared with the Tart guest's sparse disk, which grows every time
a build runs inside it — so measure before installing (`diskutil apfs list`)
and treat a floor as a refusal condition, the way `cmd/bench`'s preflight
already does. Not in the budget, deliberately: the checkout and the build
tree. **The benchmark install cannot build by design** — the moment it can, it
is a second workstation drifting from the first — so products are staged
before the switch and the switch-adjacent tooling refuses when they are not.
That gate is built: `wk bench stage` carries the manifest and provenance,
`wk bench staged` preflights everything it will need, and `wk boot mbp` checks
what a driver can check and prints the ritual for the rest
(`docs/HANDOFF-benchmarking.md`, "The macOS shape";
`docs/HANDOFF-mac-perf-mode.md` for the install itself).

The "one command, then leave it" requirement resolved the honest way: the
authenticated step cannot be a command (`bless --setBoot` is superseded on
Apple Silicon — answered 2026-08-19), so it is two clicks at the startup
manager, once per session, and everything after that is remote because the
benchmark install has Remote Login enabled at build time. No AppleScript UI
automation around the Startup Disk pane; that is a fragile answer to a
once-per-session problem. One Intel-era belief to drop: Apple Silicon has no
"allow booting from external media" toggle — that was T2; each bootable volume
group carries its own LocalPolicy.

## What the bring-up taught — the post-mortems

The mechanism took four rpi5 boot attempts, three rpi5 power cycles and three
rpi4 power cycles to prove. The failures are worth more than the transcript,
so each is kept as the rule it taught. (The step-by-step mechanics that used
to be appended here — the cloud-init seed layout, the diag-dump and
self-return units, the offline verification — became `cmd/sysimage` and
`wk boot --diag`, which is where they belong; `docs/TESTING.md` §7 checks
them.)

### Pi firmware halts when a boot medium goes half-missing

The rule this fleet has now taught three times: **firmware that finds a boot
medium and then cannot complete from it *halts*. It only falls through when
the medium is not bootable at all.** Every arming mechanism must produce the
second state, never the first.

- **Serving half a tree is strictly worse than serving nothing.** Not serving
  is benign — the TFTP attempt times out, `BOOT_ORDER` falls through. But once
  the bootloader has pulled `start4.elf` over the network and executed it,
  `BOOT_ORDER` is spent; a second stage that cannot find its kernel halts,
  unreachable, until a person pulls the plug. It happened 2026-08-20: the
  tftpd's serial-directory fallback retried misses under `os.path.basename`,
  so `<serial>/current/vmlinuz` became `vmlinuz` — and worse,
  `<serial>/current/overlays/README` was answered with the *root's own*
  `README`, a real file that was not the file asked for. The fallback now
  strips only the leading component, and only when it is not a directory
  actually held. So `wk serve` runs `boot/check-boot-files.py` before starting
  anything, `WK_SERVE_ANY_ROOT=1` does not bypass it (that flag trades a
  reboot loop for a transfer test; this would trade a journey), and the check
  asks the *resolver* rather than the filesystem — the filesystem would have
  said `current/vmlinuz` was present and correct, because it was. The only
  question worth asking is the one the firmware asks: *if I request this name,
  do I get bytes?*
- **The rpi4's first self-disarm hit the same wall from a third direction.**
  It renamed `start4.elf` aside, on the observation that an unbootable stick
  is skipped — but the state actually watched skipping was a stick with *no
  FAT filesystem at all*. A valid FAT partition merely missing `start4.elf`
  halts the firmware, and cost another power cycle. The disarm now reproduces
  the observed-skipping state: partition 1's MBR type byte flips 0x0c → 0x83,
  one byte at offset 450, written with `dd conv=notrunc` rather than through
  `sfdisk` — the self-disarm runs from that same disk with partition 2 as
  root, and a tool that rewrites the table asks the kernel to re-read it,
  where a surgical write asks nothing of anybody. `wk selftest` checks the
  offset, the round trip, and that the write does not truncate the device.

### A check that reads a different copy of the thing it checks is not a check

The resolver fix above was verified on hardware — and the board halted again,
in the same place, on the same request. `wk serve` binds port 69 through a
**root-owned copy** of the tftpd (`admin/install.sh` installs it; the copy is
right — a sudoers rule must name a file the invoking user cannot write). But
only `./setup` ever refreshed it, and the copy was three hours stale: the
board was served by code nobody had looked at that day. The guard added an
hour earlier — written specifically so an incomplete tree could never reach a
board — **passed**, because it loaded the checkout's resolver and certified a
resolver that was not the one about to run. A second opinion about a third
file.

Three changes, and the ordering between them is the point: `wk serve` compares
the installed helper against `boot/wk-tftpd.py` and refuses on any difference,
**first**, before the image is resolved — "the code about to talk to firmware
is not the code in this checkout" outranks anything wrong with an image;
`check_boot_files_serveable` loads its resolver from whichever binary is about
to run, so the two agree by construction rather than by coincidence; and
`wk selftest --section netboot` replays offline the exact requests that cost
the board its afternoon. This resolver has halted the fleet twice; it cannot
be edited without being exercised.

### An image and an install from the same distro are twins in every namespace

**Any mechanism that finds something by name will find the wrong one, and
renaming one namespace is not a fix until every mechanism that reads that name
has been changed with it.** Four instances, each found the expensive way on
the rpi5:

1. **Filesystem labels.** Ubuntu's raspi image labels its partitions
   `writable`/`system-boot` and mounts by label; the board's NVMe host install
   came from the same base, so `root=LABEL=writable` on the stick named two
   filesystems and the winner was enumeration order. Both outcomes are bad and
   invisible — the stick's kernel on the NVMe root loses its wifi modules (up
   and unreachable forever); an unresolvable root drops to an initramfs shell
   with **no self-return**. `/boot/firmware` by `LABEL=system-boot` is the
   same hazard with a worse tail: the image's kernel updates landing on the
   *host's* firmware partition. The fix in `wk sysimage build` is four edits
   that must agree — `tune2fs -L`, `mlabel`, fstab via `debugfs`, and
   `root=LABEL=` in `cmdline.txt` — all unprivileged (e2fsprogs accepts
   `file?offset=N` everywhere).
2. **cloud-init finds its seed by label too.** The base ships `fs_label:
   system-boot`, so after the relabel the only `system-boot` left was the
   host's NVMe — and the image booted correctly from the stick while
   configuring itself **from the host's seed**, reporting `0 failures`
   throughout. The tell was one line in the stick's `status.json`:
   `seed=/dev/nvme0n1p1`. The build now writes a `99-wk-image.cfg` pointing
   `fs_label` at the image's own boot label.
3. **`systemd-run` in cloud-init's `bootcmd` deadlocks the boot.** Arming the
   watchdog as early as possible looked obviously right and stalled the
   machine at `sysinit.target` forever: `systemd-run` without `--no-block`
   waits for a start job whose default dependencies wait for `basic.target` →
   `sysinit.target` → `cloud-init-local` — which is sitting inside
   `systemd-run`. **Anything that must run early belongs in a unit installed
   in the rootfs, where systemd orders it — never in a command that asks
   systemd for work while systemd is waiting for that command.**
4. **A different DHCP client identifier is a different address.**
   NetworkManager identifies by MAC; systemd-networkd defaults to a DUID, so
   the image landed on a different lease than the host and was invisible to a
   driver looking for the machine it knew. The generated network config sets
   `dhcp-identifier: mac`.

A fifth, close cousin: **a copied network profile is not a credential
transfer.** The board's netplan says `renderer: NetworkManager` and the image
base has no NetworkManager at all — copying the file verbatim produces an
image with no network on a board with no cable. `image/netplan-to-networkd.py`
carries the *credential* across and re-renders for networkd, dropping every
NM-specific key — safe because both NM and networkd drive wpa_supplicant, so
the association machinery on the far side is the one associating with this AP
right now. (On RPi-OS-family distros the keyfile is not even authoritative: NM's
netplan integration rewrites a dropped-in profile under a new uuid and the
secret does not survive the round trip.)

### An image that cannot be reached must return the machine by itself

Proven, not proposed: the **self-return watchdog** fired at 420 s on the third
rpi5 attempt and handed an unreachable board back to host mode with nobody
touching it, and the rpi4's fired at 900 s to close its lane. The version that
does *not* work: a `Type=oneshot` sleeping past systemd's default 90 s
`TimeoutStartSec` is killed before it can fire — set `TimeoutStartSec=infinity`.
And it is installed into the rootfs at build time and enabled by systemd,
never by cloud-init: the boots that need a watchdog most are exactly the boots
where cloud-init did not run.

Its limits, stated: it only protects boots that reach userspace *and* its
target. A hang between kernel start and the disarm/watchdog units leaves an
armed medium armed — narrow on the rpi4 (`panic=10` covers kernel-level
failure; closing it fully means disarming from the initramfs), structural for
anything stranded in firmware or initramfs. Those failures are designed out at
build time (`image_check_boot_files`, the label surgery, pre-sized roots — no
first-boot resize-and-reboot, which would spend a one-shot on the *first*
boot), not caught at run time. A serial console is the only thing that
observes them, and it is worth a cable.

Two more of the same shape — a mechanism that looked right and had never been
exercised twice — are recorded in `docs/HANDOFF-benchmarking.md`'s rpi4
sections: the host-key pinning that refused every second image, and the
self-disarm command whose single quotes closed the unit's `ExecStart` early.
And one ordering bug that silently affects any profile: **cloud-init writes
sysctl files after `systemd-sysctl` has already run**, so
`/etc/sysctl.d/90-wk-perf.conf` arrived seventeen seconds too late and
`perf_event_paranoid` was still 4 on an image whose entire purpose is that it
is -1 — on a one-shot image, "takes effect on the second boot" means never.
Files like it are installed into the rootfs at build time (`install_file`)
now.

### Diagnosis: the channels that worked, and the readings that lied

- **The persistent journal on the image's own root partition** is what
  actually diagnosed the boot failures — `journalctl -D` from host mode
  afterwards, a timestamped account of exactly how far the board got. One
  drop-in per image (the stock journal is volatile, so the first attempt's
  logs were simply gone). Reach for it first.
- **The offline diag dump** (`wk boot --diag`: the system writes its radio and
  network state to its own FAT partition 75 s into every boot, readable from
  host mode) is what turned three blind WiFi attempts into one answer: rfkill
  not blocked, AP visible the whole time, association itself failing — and
  the regulatory theory dead, because the image showed `country 99 /
  DFS-UNSET` *and so does the host install while connected to this AP on
  channel 52*. Identical regulatory state, one works; setting a country would
  have changed the one thing known to work.
- **Forensics on the medium beat guessing about the network**: a populated
  `/etc/machine-id` and six fresh ssh host keys proved the very first stick
  boot had *worked* as a boot while the network failed — so the one-shot
  mechanism was never the problem, and the chase moved to the radio where it
  belonged. The design must never depend on the network to tell you whether
  the network failed.
- Two diagnosis mistakes worth naming so neither is repeated (the board "lost
  to the ACL" for half a day was actually out of WiFi range): the ARP sweep
  *had* found the Pi, dismissed because the OUI list being grepped was
  incomplete — grep for the OUI list *and* eyeball the hosts that answer; and
  zero WireGuard traffic with no handshake was read as "packets dropped
  before the tunnel" when it is equally consistent with "the link is too
  lossy to complete a handshake". A marginal link and a filter look identical
  from one end; the discriminator is `tailscale ping --icmp` (which is
  ACL-subject, unlike the plain disco ping, which "does not inject packets
  into either side's TUN devices" and proves almost nothing), and it should
  be run before naming a cause.
- **Stopped chases are recorded so nobody re-derives them**: Raspberry Pi OS
  as a test vehicle (its cloud-init/netplan/NM secret handling never
  associated this radio; the real image uses the Ubuntu base that
  demonstrably does), and the tailnet-ACL hypothesis (the SETUP.md §7 caveat
  about tagged sources not matching `autogroup:member` is real and kept, but
  the live policy already permits this traffic).

## Traps

**The rpi5 has no `cmdline.txt`.** Boot args come from
`/proc/device-tree/chosen/bootargs`, injected by firmware
(`host/linux/rpi5/rpi5-numa-README.md`). The system supplies its own boot
medium, so its args go there — and check what the firmware already injected
before adding `numa_policy`/`numa=fake` by hand.

**`os_check=0`.** Pi 5 firmware rejects kernels lacking Ubuntu's trailer, and
anything served or built by us is "locally built" by that definition. The
*image's* `config.txt` needs its own copy; the host's does nothing for it.

**A power cycle is not the same as a reboot.** The Pi's one-shot register is
reset-safe on purpose, so it survives a warm reboot. If a run wedges the
machine hard enough to need the plug pulled, confirm which mode it landed in
on the way back rather than assuming.

**Do not put the overclock in the EEPROM.** `SDRAM_BANKLOW` and `BOOT_ORDER`
are firmware state shared by both modes; an overclock written there overclocks
host mode too, which is the exact split this design exists to preserve.

**The Mac's boot volume cannot be switched by script.** If a plan depends on
"reboot the MBP into the benchmark install remotely", the plan is wrong — see
tier 2. Say so in the docs instead of building it.

**First contact with an unreachable Pi is physical.** Every arming mechanism
here is an ssh command, so netboot removes the second trip to a device, never
the first: a board that answers nothing has to be met once, with an SD card.
