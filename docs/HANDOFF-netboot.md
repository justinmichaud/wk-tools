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

## State as of 2026-08-20 — the rpi4 netboots, and it cost a power cycle

Read this before the two sections below it, which it supersedes wherever they
overlap. The headline of 2026-08-19 — "the rpi4 is off or unplugged, and that is
the whole of what blocks step 3" — is out of date: it is on, it is found, its
EEPROM is written, and the firmware fetches boot files from `wk serve` over the
LAN. The remaining blocker moved from the board to the root filesystem, which is
where this file always predicted it would end up.

**The board.** `raspberrypi4-64`, Pi 4 Model B Rev 1.5 (2 GB), wired on the house
LAN, **not** on any isolated guest network — consistent with the 2026-08-19
finding that no such segment exists. It runs the **WebKit Dev@CI Yocto image**
(scarthgap 5.0.2, kernel 6.6.22-v8), not the buildroot image `cmd/pi` and
`docs/HANDOFF-linux-pi.md` assumed; both have been corrected. Root on
`mmcblk0p2`, and a 29.5 GB USB stick at `/dev/sda` carrying one ext4 partition
labelled `WebKit` — so `MACH_DEVICE=/dev/sda` in `boot/machines.sh` points at
real data, and writing an image there destroys it.

Its address is not stable. Three different DHCP leases were observed in one
afternoon (`.160` from Linux, `.163` from the firmware, `.159` later), because
the firmware's DHCP client and the kernel's present different client
identifiers. **mDNS is the only fixed name**: `raspberrypi4-64.local`, which is
what `Host rpi4-test` in `dotfiles/ssh/config` uses. It has no tailnet identity
— `wk pi setup` has still not run against it.

**`Host rpi4` is gone from `~/.ssh/config`, and `./setup` now keeps it gone.**
It named `rpi4-compilers-0`, a shared build box behind a ProxyJump, so every wk
verb that takes an ssh destination was aimed at the wrong machine — the hazard
`boot/machines.sh` flagged and worked around by defaulting `MACH_SSH` to
`rpi4-test`. `host/dotfiles.sh` now owns the whole of `~/.ssh/config` rather
than just its first line: hand-written entries move to `~/.ssh/config.d/local`
(machine-local, never committed), and any stanza naming a **fleet machine** is
dropped, because that is a shadow rather than a host. The original is kept once
at `~/.ssh/config.wk-backup`.

**The EEPROM, on a board with no eeprom tooling.** `wk pi netboot-enable`
required `rpi-eeprom-config` on the device and refused without it — and the
Yocto image has `vcgencmd` and nothing else, so the writer refused on the one
board the fleet most wants to netboot. `boot/rpi-eeprom.sh` adds the way in that
needs nothing on the board: build the update image *here*, stage
`pieeprom.upd` + `pieeprom.sig` + `recovery.bin` on the board's FAT partition,
and let the ROM apply it on the next boot. Three files pinned by sha256 from
`raspberrypi/rpi-eeprom@86759b0` — not the release tarball, which is 106 MB for
660 KB of content.

Two things it does that the on-board path does not, both said out loud at the
confirm prompt: it replaces the **whole** bootloader image rather than the
config section (there is no way to edit the running image without reading it
back, which is the part the board cannot do), and it takes effect on the *next*
boot. On this board that upgraded the bootloader from 2023-01-11 to 2026-05-17.
`recovery.bin` verifies the image against the signature before writing anything,
so a corrupt transfer flashes nothing.

Done on hardware. `vcgencmd bootloader_config` now reports `BOOT_ORDER=0xf412`
and `TFTP_IP=192.168.1.40`, and re-running `wk pi netboot-enable rpi4-test`
reports "already says this" — the landed code and the by-hand flash agree byte
for byte.

**Two bugs the first real netboot exposed, both fixed:**

- **`wk pi netboot-enable` was writing the workstation boot order to a test
  device.** It defaults the network position from `machine_load "$HOST"`, but
  `$HOST` is an *ssh* name and `rpi4-test` is not a fleet name, so the lookup
  never matched and it fell to `last`. `machine_by_ssh` in `boot/machines.sh`
  resolves the destination back to its machine; the rpi4 now correctly gets
  network **first**.
- **`b_arm` could not read the BOOT_ORDER it exists to check.** It asked
  `rpi-eeprom-config`, got nothing on this board, and took the
  `warn "assuming it netboots"` branch — about the very board whose boot order
  was in question. Both it and `b_evidence` now read `vcgencmd
  bootloader_config`, which is firmware and answers on any Pi. `wk boot rpi4
  --status` prints real evidence.

**The netboot conversation works.** With an empty TFTP root, the firmware
DHCP'd, reached `192.168.1.40:69`, and asked for `7da7aee3/start4.elf` — the
serial-number prefix — then fell back to the root, missed everything, and fell
through to the SD card as designed. Boot cycle with a full miss: about 135 s.

### The power cycle, and the asymmetry it taught

Serving the real `rpi4-perf` boot files went further and ended badly. The
firmware fetched `start4.elf`, `fixup4.dat` and `config.txt`, executed the
second stage, read `os_prefix=current/`, asked for `7da7aee3/current/vmlinuz`
— and got NOT FOUND. **The board halted.** It has been silent since: no ICMP,
no ARP, no further TFTP. It needs someone to unplug it.

The cause was in `boot/wk-tftpd.py`. Its serial-directory fallback retried a
missed request under `os.path.basename`, which throws away *every* directory
rather than the firmware's prefix, so `<serial>/current/vmlinuz` became
`vmlinuz` — not at the root of a flash-kernel image. Worse,
`<serial>/current/overlays/README` became `README`, and the boot partition has
one, so the firmware was handed a real file that was not the file it asked for.
The fallback now strips only the leading component, and only when that component
is not a directory we actually hold.

**The asymmetry, which is the durable lesson.** This file has always said the
netboot failure mode is benign, and for *not serving* that is exactly right: the
TFTP attempt times out, `BOOT_ORDER` falls through to the local disk, nothing is
lost. It is not right for serving **half**. Once the bootloader has pulled
`start4.elf` over the network and executed it, BOOT_ORDER is spent; a second
stage that cannot find its kernel halts, does not retry, and never looks at the
SD card. Serving an incomplete tree is strictly worse than serving nothing —
the one costs a reboot, the other costs a trip to the device.

So `wk serve` now runs `boot/check-boot-files.py` before it starts anything, and
**`WK_SERVE_ANY_ROOT=1` does not reach it**. That flag trades a reboot loop for
a transfer test, which is a real trade; this would trade a journey, which is not.
The check asks `wk-tftpd`'s own `resolve()` rather than the filesystem, and that
is the whole point: the filesystem would have reported `current/vmlinuz` present
and correct, because it was. The only question worth asking is the one the
firmware asks — *if I request this name, do I get bytes?*

`wk serve` is stopped, so a power cycle lands the board on its SD card.

### What is left

**The root filesystem, and it is blocked on one privileged install.** Everything
above proves the transfer, which is exactly what the 2026-08-19 entry predicted
would be the limit. `rpi4-perf` boots to an initramfs that cannot find
`LABEL=wk-image-root` on any disk the netboot client has; it carries `panic=10`,
so it reboots rather than sitting there, and stopping the server drops it back
to its local disk.

Phase 1 in "Storage: what the image is" is an NFS root, and `nfs-kernel-server`
is not installed on moose. `sudo` here needs a password, so this is the step that
has to be run by a person:

    sudo apt install nfs-kernel-server

It does not belong in `./setup`: serving is a role that floats between machines
and most of them will never hold it, so the install belongs to whichever machine
takes the role rather than to every host's bootstrap.

**A note on moose's sudoers while passing.** `/usr/bin/tee` is NOPASSWD for
root, which is passwordless write access to any file on the system and therefore
equivalent to NOPASSWD root. It was presumably added for a specific write. It is
worth narrowing.

## State as of 2026-08-19, later the same day — the verbs exist

Read this before the section below it, which it supersedes on every point they
both cover. Everything the earlier session proved on hardware still stands; what
changed is that the mechanism is now a command instead of a scratch script.

**Landed, and exercised on the workstation:**

- **`wk image build <profile>`** — `cmd/image`, spec in `image/profiles.sh` plus
  `image/<profile>/`. Builds `rpi5-perf` from a pinned Ubuntu 26.04 preinstalled
  server raspi base and seeds it through cloud-init on the boot partition. 47 s
  from a cached base, and **entirely unprivileged**: mtools writes into the
  image's FAT filesystem at a byte offset taken from the partition table, so
  nothing is mounted and `wk` still never calls sudo on this host. The manifest
  is written last and is the whole publishing protocol — a killed build leaves a
  directory every reader ignores and the next build deletes.
- **`wk image flash <machine>`**, **`wk image ls|show|rm`** — the write happens
  *on* the machine over ssh, where sudo is passwordless, and is verified by
  reading the bytes back and comparing hashes. It refuses any device whose
  transport is not usb, the machine's own root device, and anything mounted.
- **`wk boot <machine> [--status|--keep|--back|--disarm|--dry-run]`** —
  `cmd/boot`, with `boot/machines.sh` (the fleet) and `boot/rpi5-usb.sh` (the
  driver), the same shape `targets/*.sh` uses. Arming refuses unless the boot
  device actually starts with the image being armed, which is checked before the
  firmware call: a one-shot that falls through looks exactly like a firmware
  fault.
- **The role transition is recorded and read back.** One record of intent, on the
  machine, next to the boot mechanism — a copy on the driving workstation would
  be the one that goes stale, because the transition happens over there.
  Everything else is derived: `--status` reports the role from the image's own
  identity marker, the persistent boot order from the EEPROM, and calls a record
  *spent* (not desynced) when the machine has booted since it was armed. The
  reader is `wk boot --status` rather than a line in `wk status` only because
  `cmd/status` belongs to the macOS lane this week.
- **`wk boot <machine> --diag`** — the offline diagnostics channel as a verb.
  The image dumps its network state to its own FAT partition 75 s into every
  boot; this mounts that partition read-only from the *other* role and prints
  it. It is the one channel that works precisely when the image never appeared,
  and last session's scratch version of it is what turned three blind attempts
  into one answer.

**The finding that would have cost another boot attempt.** The board's own
netplan says `renderer: NetworkManager`, and **the Ubuntu base image has no
NetworkManager at all** — its 668-package manifest carries `netplan.io`,
`wpasupplicant` and `systemd`, and nothing else that could render that file.
Copying the board's netplan into the image verbatim, which is what "copy the
network profile from the board" naturally means, produces an image with no
network on a board with no cable: the same failure as attempts 1-3, reached from
a new direction. So `image/netplan-to-networkd.py` carries the *credential*
across and re-renders it for `networkd`, dropping every NM-specific key. This
is safe for the reason that matters: NM drives wpa_supplicant and so does
networkd, so the association machinery on the far side is the one that is
associating with this AP right now.

**Two deliberate departures from the earlier session's conclusions**, both on
its own evidence:

- **The regulatory country is not set.** Conclusion 2 of "Attempts 2 and 3" asks
  for it, but the same section disproves it: the image showed `country 99 /
  DFS-UNSET` and so does the workstation *while connected to this AP on channel
  52*. Setting a country would change the one thing known to work. `rfkill
  unblock all` stays, as insurance that costs nothing.
- **The self-return watchdog is armed in `bootcmd`, not `runcmd`.** runcmd is
  cloud-init's last stage, behind the package install — and "the network did not
  come up" is both the scenario the watchdog exists for and the scenario that
  makes apt slowest. A safety net installed behind the thing it protects against
  fires too late to matter. The unit file is still written and enabled, for every
  boot after the first.

**Done, on hardware, and the whole cycle measured:** arm -> the board boots the
image -> **reachable over WiFi 53 s later** -> `wk boot rpi5 --keep` claims it ->
`wk boot rpi5 --back` hands it back in ~40 s -> `--status` reports the record as
*spent*. On the image: `kernel.perf_event_paranoid = -1`, `kernel.kptr_restrict
= 0`, `perf` installed, the JIT-dump directory and environment in place, its own
root and boot partitions mounted, its own cloud-init seed used, and a persistent
journal. That is the profiling half of this step unblocked.

It took **four attempts and three power cycles**, and the four failures are the
most valuable thing this session produced. Each is recorded below as its own
trap, because each is a thing that will happen again to anyone building an image
for a machine that also runs a workstation install of the same distro.

**The trap that cost the first arming, and would have cost every one after
it: an image and a workstation built from the same distro are filesystem
label-twins.** Ubuntu's preinstalled raspi image labels its partitions
`writable` (ext4) and `system-boot` (FAT), its `cmdline.txt` says
`root=LABEL=writable`, and its `/etc/fstab` mounts both by label. The rpi5's
NVMe workstation came from that same image, so it carries the *same two labels*.
Boot the stick with the NVMe attached — which is the whole point of a perf image
that lives on a permanently-plugged stick — and `root=LABEL=writable` names two
filesystems. Which one the initramfs picks is enumeration order. Both outcomes
are bad and neither is visible: land on the NVMe root with the stick's kernel
and the wifi driver's modules are missing, so the board is up and unreachable
forever; fail to resolve it at all and the initramfs drops to a shell, where
there is no cloud-init, no watchdog, and therefore **no self-return** — the one
safety property the design leans on, defeated before it is ever armed.

`/boot/firmware` is the same hazard with a worse tail: mounted by
`LABEL=system-boot`, the image's kernel updates could land on the
*workstation's* firmware partition.

The fix is in `wk image build` and it is four edits that must agree: `tune2fs -L`
on the ext4, `mlabel` on the FAT, `/etc/fstab` rewritten through `debugfs`, and
`root=LABEL=` in `current/cmdline.txt`. All unprivileged — e2fsprogs accepts
`file?offset=N` on every tool, so the root filesystem is editable inside the
image exactly as the boot partition is, with nothing mounted.

**Trap 2: cloud-init finds its seed by filesystem label too, so relabelling
breaks it.** Ubuntu's raspi base ships `datasource: NoCloud: fs_label:
system-boot`, which means cloud-init does not read `/boot/firmware` -- it goes
looking for a filesystem with that label and mounts whatever it finds. Rename
the stick's boot partition to fix trap 1 and the only `system-boot` left in the
machine is the *workstation's* NVMe. The image then booted correctly from the
stick and configured itself **from the workstation's seed**: no user, no key, no
network configuration, no watchdog -- and cloud-init reported `0 failures` at
every stage, because from its point of view nothing had gone wrong. The tell was
one line in `/var/lib/cloud/data/status.json` on the stick:
`"datasource": "DataSourceNoCloud [seed=/dev/nvme0n1p1]"`.

`wk image build` now writes `/etc/cloud/cloud.cfg.d/99-wk-image.cfg` pointing
`fs_label` at the image's own boot label. **The general rule, of which traps 1
and 2 are both instances: an image and an install made from the same distro are
twins in every namespace, and any mechanism that finds something by name will
find the wrong one. Renaming one namespace is not a fix until every mechanism
that reads that name has been changed with it** -- here fstab, the kernel command
line, and cloud-init's datasource.

**Trap 3, and the expensive one: `systemd-run` in cloud-init's `bootcmd`
deadlocks the boot.** Arming the watchdog as early as possible looked obviously
right -- bootcmd is cloud-init's first stage, runcmd its last -- and it stalled
the machine at `sysinit.target` forever. `systemd-run` without `--no-block`
waits for the start job to complete; the transient unit it creates carries
default dependencies and so waits for `basic.target`; `basic.target` waits for
`sysinit.target`; and `sysinit.target` waits for `cloud-init-local`, which is
sitting inside `systemd-run`. No ssh, no multi-user, no watchdog, and a journal
whose last systemd line is an unmet condition check while wpa_supplicant chatters
on beneath it for another five minutes. **Anything that must run early belongs in
a unit installed in the rootfs, where systemd orders it -- never in a command
that asks systemd to do work while systemd is waiting for that command.**

**Trap 4: a different DHCP client identifier means a different address.**
NetworkManager identifies itself by MAC; systemd-networkd defaults to a DUID.
The image therefore got `192.168.1.156` where the workstation has `.165`, and
was invisible to a driving host looking for the machine it knew. The generated
network configuration now sets `dhcp-identifier: mac`, so the image lands on the
same lease as the workstation and the neighbour-table lookup in
`boot/machines.sh` finds it.

**What actually diagnosed all of this** was not any of the network channels: it
was the **persistent journal on the image's own root partition**, read from the
workstation afterwards with `journalctl -D /mnt/r/var/log/journal`. Making the
journal persistent costs one drop-in and it is the difference between "the board
did not come back" and a timestamped account of exactly how far it got. Put it
in every image, and reach for it before anything else.

**The lesson underneath it, for every future image:** the self-return watchdog
only protects boots that *reach userspace* -- and, as trap 3 showed, only those
that reach the target it is wanted by. It is now installed into the rootfs at
build time and enabled by systemd, not by cloud-init, because the boots that need
a watchdog most are exactly the boots where cloud-init did not run. Anything that
can strand the image in firmware or initramfs — a root= that does not resolve, a missing kernel, a
bad overlay — is outside its reach and costs a trip to the machine. Those
failures are the ones to design out at build time, not to catch at run time.
A serial console (`enable_uart=1` is already in the base) is the only thing
that observes them, and it is worth a cable.

**One correction to the section below, from the base image itself.** `os_prefix=
current/` with `[tryboot] os_prefix=new/` is **Ubuntu's own A/B layout, shipped
in the stock preinstalled image** — not something flash-kernel or the NUMA-kernel
work added, as "Measured on the board" reads. The conclusion it supports is
unchanged and if anything stronger: tryboot is taken on *any* Ubuntu install of
this board, by the distro, so a perf image using it would collide with ordinary
kernel updates. The other reason stands untouched — `p2` fills the 469 GB disk,
so a third partition means shrinking the root. The base also ships
`enable_uart=1`, so the serial console the last session wished for needs only a
cable.

**The server half, added the same day.** `wk serve` (`cmd/serve`) fills a TFTP
root from an image already in the store -- with mtools at a byte offset, so a
netboot client gets the same artifact `wk image flash` writes to a stick -- and
serves it alongside an HTTP root for the root filesystem. Verified over the real
LAN from another machine: `config.txt` and a 14.7 MB kernel, byte-identical, ~23 s
over WiFi at a firmware-realistic 1468-byte block size.

Three decisions in it worth keeping:

- **A stdlib TFTP daemon (`boot/wk-tftpd.py`), not tftpd-hpa.** The serving role
  has to float between machines, the Mac included, and a distro system service
  does not float: different package, different unit, different config file per
  host, and none at all on macOS. One stdlib file runs wherever `wk` runs -- the
  same argument that made the egress proxy stdlib-only.
- **The serial-number directory is solved rather than configured.** Pi firmware
  prefixes every request with a directory named after the board's serial, which
  nobody can know before the board first asks -- the fiddliest part of setting Pi
  netboot up by hand, and the worst one to get wrong from a machine with no
  console. The server retries at the root when that directory is absent, through
  the same containment check, and logs which of the two happened. So
  `TFTP_PREFIX` never has to be written into an EEPROM.
- **Privilege is one syscall wide.** Firmware TFTP clients speak to port 69, so
  something must bind below 1024. The helper binds and then *drops to the
  invoking user before resolving a single path* -- and refuses outright to serve
  with root still held. The difference matters: a root-owned server reading an
  arbitrary `--root` would be a read-only export of the whole filesystem, as
  root, over UDP, with a sudoers rule as the only thing between a home LAN and
  `/etc/shadow`. Installed by `admin/install.sh` beside the quiesce helper, with
  the same copied-to-root-owned-path and re-verified-mode guarantees.

**A container cannot serve this, and the measurement is worth recording so
nobody re-derives it.** Asked 2026-08-19: rootless podman would be the natural
home for the server -- it is what every workspace already is, it works on macOS,
and it would need no sudoers rule. It cannot do it, for one reason: firmware
TFTP clients speak to **port 69**, and rootless containers cannot bind below
1024 either way round. Measured on this machine:

    $ podman run --rm -p 69:69/udp alpine ...
    Error: rootlessport cannot expose privileged port 69, you can add
    'net.ipv4.ip_unprivileged_port_start=69' to /etc/sysctl.conf (currently
    1024), or choose a larger port number (>= 1024)

    $ podman run --rm --network host --cap-add NET_BIND_SERVICE alpine ...
    nc: bind: Permission denied

`--cap-add` does not help: the capability is granted in the container's user
namespace, and the bind is checked against the initial one. Podman's own
suggested workaround is **worse than the helper it would replace** -- lowering
`ip_unprivileged_port_start` to 69 lets *any* local process bind 69 through
1023, permanently, including 80 and 443, where the helper buys one bind(2) on
one path and drops privileges immediately. And on macOS the container path is
worse still: the podman machine sits behind user-mode NAT, TFTP's data transfer
comes from a fresh ephemeral port, and forwarding that through two layers of
NAT is a well-known way to get a protocol that half works.

So the split stands: **everything that can be a container is one; the bind is
not one.** Rootless podman is the right answer for a workspace, and the wrong
answer for a service whose port number is fixed in firmware.

**`wk pi netboot-enable` is written and its read path verified.** It diffs a
board's firmware configuration and refuses to write without confirmation. The
load-bearing detail is where network goes in `BOOT_ORDER`: **last, before the
restart nibble** (`0xf461` -> `0xf2461`), never first. That register is shared by
both of a machine's roles, so a Pi that netboots by default is a Pi that stops
being a workstation the moment a server answers -- and getting it back needs
physical access. An actual netboot is requested per-boot with the same one-shot
`wk boot` uses.

**The remaining gap, and it is the substantial one: netboot has no root yet.**
`wk serve` hands a client its firmware, kernel, DTBs and initramfs correctly --
that half is verified over the real LAN. But the image's `cmdline.txt` says
`root=LABEL=wk-image-root`, a label that exists only on a local disk, so a
netbooted board would fetch the kernel and then sit in an initramfs with nothing
to mount. With `panic=10` in the same cmdline and network first in `BOOT_ORDER`,
that is a **boot loop on a headless board** -- caused by a server reporting
success. `wk serve` now refuses to serve an image whose root is local, and names
`wk image flash` instead, so this is a message rather than a trip to the device.

Closing it means one of the two mechanisms this file already scoped under
"Storage: what the image is" -- an NFS root (needs a server with real
privileges, so it reopens the question above) or an initramfs that pulls a
squashfs into RAM over HTTP (needs the image's initramfs rebuilt, which the
unprivileged build path cannot yet do). **That is the next piece of work on this
step**, and until it exists netboot proves the transfer and nothing further.

Note what this does *not* block: the rpi4 can run a wk image today by exactly
the route the rpi5 took -- `wk image flash` to a local device over ssh, then
boot it -- which needs no server, no root mechanism and no privilege at all.
Netboot is the *test channel* for images not yet committed to local storage,
which is what this file always said it was for.

**And the thing netboot cannot bootstrap itself out of.** `netboot-enable` writes
the EEPROM over ssh, so it needs the board *running*. Netboot removes the second
trip to a device, not the first: a Pi that answers nothing has to be met once,
physically, with an SD card. For the rpi4 specifically that first contact is
probably trivial -- it has a buildroot SD image and comes up on the LAN when
powered -- but as of this session a full 254-address sweep finds no Raspberry Pi
on the LAN but the rpi5, and moose's three wired NICs are all `carrier=0`. The
rpi4 is off or unplugged, and that is the whole of what blocks step 3.

**Two hardware facts corrected while hunting for the rpi4, 2026-08-19 (later).**
Both contradict statements elsewhere in this file, and evidence wins:

- **moose's BMC is up and has a lease.** `docs/HANDOFF-bmc.md` and the section
  below record it as not answering. It is answering: the Librem 5's `bmc0`
  segment (10.99.0.1/24) holds exactly one dnsmasq lease,
  `9c:6b:00:75:5a:d7 -> 10.99.0.2 altrad8ud2-1l2q`, which is moose's own ASPEED
  BMC. That unblocks the moose half of this step -- BMC virtual media is the
  path that survives a dead LAN, and it now has something to talk to.
- **The Librem 5 is up, on the LAN, and is *not* running a guest network.**
  It answers at 192.168.1.151 (`librem5-oob-lan` in the ssh config) and its only
  served segment is `bmc0`. So the "isolated guest network with no route to the
  main LAN" that `cmd/pi` cites as the reason the tailnet is the only path to
  the test devices does not currently exist. There is no hidden segment for a
  Pi to be hiding on.

The rpi4 was searched for from three vantage points -- moose, the rpi5 and the
Librem 5 -- by ICMP sweep of all 254 LAN addresses, IPv6 all-nodes (which finds
a host with no DHCP lease), neighbour tables, dnsmasq leases and an SSH banner
scan of every host that answered. The only Raspberry Pi anywhere is the rpi5.
The `raspberrypi` entry in the ssh config (192.168.1.182) has nothing at it.

**The rpi3's bring-up, scoped 2026-08-19 — and the recommendation is to wait.**
It is the furthest-out client of this step, for reasons that are properties of
the board rather than of the plan:

- **Nothing to configure.** A Pi 3's network boot lives in the SoC boot ROM, not
  an EEPROM bootloader, so there is no `BOOT_ORDER`, no `TFTP_IP`, no static-IP
  keys and no `set_reboot_order`. `wk pi netboot-enable` refuses on it correctly
  -- it checks for `rpi-eeprom-config`, which does not exist there. The *only*
  way to point this board at a server is DHCP.
- **So it needs the one component `wk serve` does not have:** a DHCP reply
  carrying option 43 `"Raspberry Pi Boot"`. dnsmasq in proxy mode
  (`dhcp-range=<net>,proxy`) on the house LAN is the shape to try first -- it
  supplies the PXE bits without assigning addresses, so no second
  address-assigning server appears on a family network. Port 67 is privileged
  too, so it is one more bind for the same helper.
- **An irreversible one-time cost.** Network boot on a Pi 3 needs
  `program_usb_boot_mode=1` written once from a working SD card, and **OTP is a
  one-way door**. Whether this is a 3B or a 3B+ also matters and is unestablished
  -- a plain 3B cannot use a TFTP server on another subnet, among several boot-ROM
  bugs fixed only in the B+.
- **And the payoff is capped.** The Pi 3's Ethernet sits behind the USB
  controller, so netboot is a *test channel* and measured runs come off the SD
  card; with 931 MB and no swap a RAM root is out for anything browser-shaped.

**So: do not burn the OTP yet.** The rpi3's fastest route to being useful is the
one the rpi5 has already proven -- `wk image flash` to its local media over ssh,
then boot it -- which needs no DHCP, no OTP and no server at all. Netboot for
this board is worth doing only after the netboot *root* mechanism exists and the
rpi4 has proven the whole chain; otherwise the one-way door is taken for a
capability that cannot yet be exercised.

**A 32-bit decision is owed before any rpi3 image exists.** It is the fleet's
only armv7l board, and deliberately so -- this repo carries a whole armhf story
and the rpi3 is where 32-bit meets real hardware. The arm64 base every other
profile uses *would* boot on it (the Cortex-A53 is 64-bit capable) and would
quietly retire the fleet's 32-bit coverage. `wk image build rpi3-perf` therefore
refuses and says so rather than guessing.

**Where the rpi3 actually was:** `root@192.168.1.160`, on the house LAN -- which
is one more reason the "isolated guest network" premise in `cmd/pi` needs
retiring. It is powered off as of this session.

**Also owed, and blocked on file ownership rather than on work:** `wk status`
showing the armed transition on the machine's line, `wk`'s help text listing
`image` and `boot`, and their `is_host_only` refusals. All three live in files
the macOS lane holds this week.

## State as of 2026-08-19, end of session — read this first


**Proven on hardware, not just designed:**

- The **one-shot USB boot** on the rpi5: `sudo vcmailbox 0x0003808b 4 4 0xf64`
  then reboot. The stick booted (machine-id written, six ssh host keys generated,
  NetworkManager state, cloud-init logs — all stamped at the boot minute). No
  EEPROM write, nothing touched on the workstation's NVMe.
- The **revert**, both ways: a power cycle and a self-reboot each land back on the
  NVMe workstation, because `BOOT_ORDER=0xf461` reaches NVMe (6) before USB (4).
  The perf stick can therefore stay plugged in permanently.
- The **self-return watchdog**: fired at 420 s and handed the board back with
  nobody touching it. This is what makes remote arming safe.
- The **offline diagnostics channel**: a unit dumping radio state to the FAT
  partition is what turned three blind attempts into one answer, and it is the
  reason anything above is known rather than guessed.

**Not working, and deliberately parked:** WiFi on the Raspberry Pi OS *test*
image. Association failed for reasons specific to that distro's
NetworkManager/netplan secret handling. The real image is Ubuntu-based, matching
the workstation that connects to this AP on channel 52 with this exact radio, so
the chase was stopped rather than finished.

**Two claims made during this session and later disproven** — recorded so nobody
re-derives them: the tailnet ACL was *not* blocking SSH to the rpi5 (that was
WiFi range; `tailscale ping --icmp`, which is ACL-subject, pongs), and the
regulatory domain was *not* blocking association (the workstation shows the
identical `phy#0 country 99: DFS-UNSET` while connected on channel 52).

**Hardware state at session end:** rpi5 is the workstation on `/dev/nvme0n1p2`,
reachable over the tailnet, WiFi only, `eth0` down with no cable, and the test
stick still attached with its image intact and armable. moose is WiFi-only with
all three wired NICs at `carrier=0`. rpi4 and rpi3 are not on the tailnet. moose's
BMC lives at **10.99.0.2** on the Librem 5's `bmc0` segment, not the stale
192.168.1.41 in `~/.ssh/config`.

**Next three actions, in order** (1 is built but not yet booted; see the newer
state section above):

1. `wk image build` on an **Ubuntu** base (the only path to a reachable rpi5 perf
   image, since that board is never on the LAN — see the topology section). It
   must bake in: the driving machine's ssh key, the network profile, sshd enabled,
   an identity marker, the self-return watchdog, the diag dump, **and no
   first-boot resize-and-reboot step** (that step spends the one-shot).
2. Re-run the one-shot with that image and confirm it comes up reachable; then
   `perf_event_paranoid` and the JIT-dump environment, which is the profiling half
   this whole step exists to unblock.
3. `wk serve` plus `wk pi netboot-enable rpi4`, trying **proxy DHCP on the LAN**
   before any cable.

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
- **Tier 2 — the MBP. As of 2026-08-20 this has a driver**
  (`boot/mac-volume.sh`) and a third arming model, `hands-on`: the driver
  checks what it can, records the intent, prints the ritual, and reboots
  nothing. `wk boot mbp --status` reports it as armed *and waiting for a
  person*, which is the honest reading of a transition no software can make.
  The machine also drives itself (`MACH_LOCAL`), because it is the only Apple
  Silicon machine here. See docs/HANDOFF-benchmarking.md for the staging that
  goes with it.
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
  reachable as `moosebmc` — at 10.99.0.2 on the Librem 5's `bmc0` segment, ssh
  on 2200; the 192.168.1.41 in `dotfiles/ssh/config` is stale — so an image can be
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
drives its display chip; ssh on `moosebmc`, 10.99.0.2:2200 on the Librem 5's
`bmc0` segment — the 192.168.1.41 in `dotfiles/ssh/config` is stale — though it
did not answer on 2026-08-19). That path needs nothing from DHCP and works when the
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

`wk boot` is a role transition, and the fleet status must see it:
per `docs/HANDOFF-workspace-state.md` ("`wk status` walks the fleet"),
arming writes a small record of intent (image, who, when) next to the boot
mechanism, the firmware's own one-shot state is read back as the evidence
where the platform allows, `wk status` on any workstation shows the armed
transition on the machine's line, and mutating commands against an armed
machine warn or refuse. `--back` and a completed boot clear the record; a
record that outlives its transition is reported as desync.

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

**Status, 2026-08-19 (later):** 0, 1, 2 and 3 are **done** — ssh unblocked, the
image built by `wk image build rpi5-perf`, the rpi5 booted into it over the USB
one-shot and returned, and `perf_event_paranoid`/`kptr_restrict`/the JIT-dump
environment set and verified on the board. 4 onward (the server, and the
rpi4/rpi3 netboot clients) are untouched — they need a wire and a machine that is
currently not on the tailnet at all.

0. **Unblock SSH to the rpi5.** *(Cleared: ssh to the board works, over
   Tailscale SSH, which authenticates with no key at all. The ACL hypothesis
   below was already disproven in the same session that raised it — the cause
   was WiFi range.)* As of 2026-08-19 `tailscale ping rpi5` pongs
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
3. **Profiling in that image** — *done 2026-08-19*: `kernel.perf_event_paranoid
   = -1` and `kernel.kptr_restrict = 0` measured on the booted image, `perf`
   installed, and the JSC JIT-dump wall written to `/etc/wk-perf-env` rather
   than exported globally (every one of those variables changes what JSC does,
   `useConcurrentJIT=0` most of all, so a shell that set them for all processes
   would silently change every measurement taken on the machine). What
   `docs/HANDOFF-profile.md` still owes is the `wk profile` verb itself; the
   machine it needs now exists.
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

### Measured on the board, 2026-08-19 — the USB route is the only sane one

With SSH working, the board's own state settles the layout question and retires
the partition option outright:

```
Raspberry Pi 5 Model B Rev 1.1, 15 GiB usable, kernel 7.0.6-1-numa, 8 NUMA nodes
bootloader          2025/12/08 (recent; set_reboot_order available)
BOOT_ORDER          0xf461      -> SD(1), NVMe(6), USB(4), restart(f)
EEPROM              SDRAM_BANKLOW=1, BOOT_UART=1, NET_INSTALL_AT_POWER_ON=1
config.txt          os_check=0 already set; os_prefix=current/, [tryboot] os_prefix=new/
autoboot.txt        [all] tryboot_a_b=1
nvme0n1             p1 512M FAT /boot/firmware (366M free), p2 469G ext4 / (219G free)
eth0                DOWN (no cable)      wlan0 192.168.1.165/24
sudo                passwordless
```

Four consequences:

- **`tryboot` is already taken on this board.** `config.txt` carries
  `os_prefix=current/` with `[tryboot] os_prefix=new/`, and `autoboot.txt` sets
  `tryboot_a_b=1` — that is flash-kernel's `pi-try` kernel staging, installed by
  the NUMA-kernel work. A perf image that also used tryboot would collide with
  kernel updates. **So the partition-plus-tryboot option is out**, and the USB
  route is not merely preferred, it is the one that does not fight the board's
  existing configuration.
- **Repartitioning was never cheap anyway**: `p2` fills the disk, so a third
  partition means shrinking a 469 GB root.
- **The perf USB can stay plugged in permanently.** `BOOT_ORDER=0xf461` reaches
  the NVMe (6) before USB (4), so a normal boot always lands on the workstation
  even with a bootable USB attached. Nothing needs unplugging between runs — arm
  with a one-shot `0xf64` (USB, then NVMe, then loop) when a perf boot is wanted.
- **The mechanism is confirmed live**, not just documented:
  `sudo vcmailbox 0x0003808b 4 4 0xf461` — deliberately the *current* order, so
  the next boot is unchanged — returned `0x80000000` (request succeeded) with the
  tag marked processed. Passwordless sudo means `wk boot rpi5` can arm it over
  SSH with no prompt.

**The image must come up reachable, or a run cannot be driven at all** (raised by
the user 2026-08-19, and it is a real gap in the plan as written). On this board
`eth0` has no cable, so an image with no WiFi credentials boots into total
isolation — no ssh, no runner, no result collection, and no way to reboot it back
except a power cycle. So `wk image build` bakes in, as part of the spec rather
than as manual post-flash surgery:

- **the authorised key** that the driving machine already uses, so ssh works on
  first boot;
- **the network profile** — for the rpi5 that is the WiFi credential, which the
  build should copy from the target board's own NetworkManager profile *on the
  board*, so the PSK never travels through a driver's logs or an agent's context;
- **sshd enabled explicitly**, not left to a distro's first-boot flag file;
- **no first-boot resize-and-reboot step.** This one is subtle and specific to the
  one-shot: a distro image that resizes its root and reboots itself spends the
  one-shot on the *first* boot, so the self-reboot lands back on the workstation
  and the image never finishes coming up. Pre-size the image instead.
- **an identity marker** — hostname plus a file such as `/etc/wk-image` naming the
  profile and build — so "which OS am I talking to" is never a guess.

Two things the image build has to account for, from the same dump: `os_check=0`
is already set on the workstation but the image needs its own copy, and
`/boot/firmware` has only 366 MB free — so images are staged on `/` (219 GB
free), never in the firmware partition.

This is strictly better than netboot for this board: no server involved, no
network traffic during boot *or* run, the same one-shot-and-revert behaviour,
and the workstation install untouched. It also means moose only ever serves the
rpi3 and rpi4.

### The rpi3 needs a real option-43 DHCP reply — but not necessarily a cable

The rpi3 is the one device that *requires* a DHCP reply carrying option 43
`"Raspberry Pi Boot"`, because its network boot lives in the boot ROM rather than
an EEPROM bootloader, and it cannot be told to skip DHCP the way the Pi 4/5
bootloader can. That does not force a private segment: `dnsmasq` in *proxy*
mode supplies the PXE bits without assigning addresses, which the boot ROM's
flow explicitly allows, so the house LAN works with no second
address-assigning DHCP server. A private segment (a cable from the rpi5's
`eth0`, or moose's `igb` port `enP2p3s0` — the plain 1GbE, not either
`bnxt_en`) is the fallback if the boot ROM's DHCP quirks defeat proxy mode.
Details and the rpi3's two prerequisites are in "The rpi5's Ethernet is for
the private segment, never the LAN" below.

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

**Resolved 2026-08-19: it was WiFi range.** The board had been placed too far
from the AP. Moved closer, and everything works: `tailscale ping` 6 ms direct
over IPv6, `tailscale ping --icmp` (which *is* subject to ACLs and host
firewalls, unlike the plain disco ping) pongs, and SSH connects normally.

**So the ACL hypothesis was wrong**, and so was the reading of the scans. Two
mistakes worth naming so neither is repeated:

- The Pi *was* in the very first ARP sweep, at **192.168.1.165** with MAC
  `88:a2:9e:07:1c:92` — the same 192.168.1.0/24 as moose. It was dismissed
  because the OUI list being grepped for was incomplete: `88:a2:9e` is a
  Raspberry Pi Ltd prefix, confirmed from the board's own kernel cmdline
  (`smsc95xx.macaddr=88:A2:9E:07:1C:91`). Grep for the OUI list *and* eyeball
  the hosts that answer.
- Zero `RxBytes`/`TxBytes` and no handshake were read as "packets dropped before
  the tunnel". They are equally consistent with "the link is too lossy to
  complete a handshake", which is what it was. A marginal link and a filter look
  identical from one end; the discriminator is `--icmp`, and it should have been
  run before naming a cause.

The ACL note added to SETUP.md §7 has been rewritten accordingly: the general
caveat about tagged sources not matching `autogroup:member` is real and worth
keeping, but the live policy already permits this traffic.

### The rpi5's Ethernet is for the private segment, never the LAN

Clarified by the user 2026-08-19, and it is a constraint rather than a
preference: **the rpi5 is never on the LAN.** Its WiFi is its only path to the
house network and the tailnet, and its `eth0` exists to serve the other boards on
a private segment. Only the rpi3 and rpi4 have LAN drops available.

So the arrangement is:

- **rpi4 over the house LAN**, served by the rpi5 across its WiFi link. The rpi4
  skips DHCP entirely (`TFTP_IP` + static IP in its EEPROM bootloader config), so
  the server only has to be reachable — and since the AP bridges the wireless and
  wired halves of 192.168.1.0/24, server and client are on one L2 anyway. The
  transfer is boot-time only. moose can serve this equally well; the roles are
  interchangeable.
- **rpi3: try proxy DHCP on the LAN first, and keep the private segment as the
  fallback.** A private segment is *not* the only way (an earlier draft of this
  file claimed it was). The boot ROM's documented flow includes an "(optional)
  Receive DHCP proxy reply" step (`boot-net.adoc`), so `dnsmasq` in proxy mode
  (`dhcp-range=<net>,proxy`) can supply the PXE bits and option 43 while the house
  router keeps handing out addresses — no second address-assigning DHCP server,
  no rogue-DHCP objection, no cable, and the rpi5 need not serve at all. If the
  boot ROM's DHCP quirks defeat that, *then* fall back to a direct cable from the
  rpi5's `eth0` with dnsmasq bound to that interface only.

Two prerequisites specific to the rpi3, both worth knowing before it is powered
up rather than after:

- **Network boot on a Pi 3 needs an OTP bit programmed, and OTP is
  irreversible.** `boot-net.adoc`: add `program_usb_boot_mode=1` to `config.txt`,
  reboot from a working SD card, then confirm `vcgencmd otp_dump | grep 17:`
  reads `3020000a`. A one-way door on that board — decide deliberately.
- **A plain 3B cannot use a TFTP server on a different subnet** ("Fixed in
  Raspberry Pi 3 Model B+"), along with broken DHCP relay and several other boot
  ROM bugs fixed only in the B+. Same-subnet serving satisfies either model — the
  LAN and a private segment both qualify — but **which model this board is has
  not been established**, and it decides how much of `boot-net.adoc`'s "Known
  problems" list applies.

Whichever is used, the serving side is interchangeable with moose — same daemons,
same files, and the service alias IP means the client's firmware does not care
which machine is behind it.

Two consequences that follow directly, and the first one is the sharp one:

- **The rpi5's perf image must work over WiFi. There is no wired fallback for
  that board, ever.** So "the image comes up reachable" is not satisfiable with a
  cable here, and image WiFi is a first-class requirement of `wk image build`
  rather than a convenience — which also means the RPi OS WiFi failure could not
  have been sidestepped by plugging something in. The way through is the Ubuntu
  base whose network stack is already proven on this exact board and AP. Until an
  image's WiFi is proven, the only console independent of the network is the
  UART: `BOOT_UART=1` is already set in this board's EEPROM.
- **A serving board may have to route, not just serve.** Netboot itself needs
  nothing beyond the private segment, but `wk pi setup` installs tailscale on the
  served device, and that needs egress — so the rpi5 needs IP forwarding and NAT
  from its `eth0` segment out over WiFi for the rpi3 to reach the tailnet at all.
  Worth building into `wk serve` as an explicit flag rather than discovering it
  when `wk pi setup rpi3` cannot reach the control plane.

### First mechanism test, 2026-08-19 — and the lesson it taught

Ran the one-shot for real on the rpi5: RPi OS Lite (trixie) written to a 29 GB
Verbatim stick, preseeded to be reachable (driving machine's ed25519 key in
`/root/.ssh/authorized_keys`, the board's own WiFi profile copied on-device so
the PSK never left it, `ssh.service` and NetworkManager explicitly enabled,
hostname `rpi5-perftest` plus an `/etc/wk-test-image` marker), then
`vcmailbox 0x0003808b 4 4 0xf64` and a reboot.

Result: **the workstation went away and did not come back, and the stick OS never
appeared on the network.** tailscale shows the board offline since the reboot;
the LAN has no host at its address and no host with its MAC. So the board is
sitting in *something* with no network, and one-shot semantics mean a power cycle
returns it to the workstation — which needs hands, because nothing on the board
is reachable to reboot it.

The most likely cause is not the boot mechanism but the radio: **Raspberry Pi OS
soft-blocks WiFi until a WLAN country is set** (rfkill), which the Imager's
first-run script normally does and which this preseed did not. A copied
NetworkManager profile cannot help if the radio is blocked.

**Proven by forensics on the stick afterwards, which is the important half:** the
board *did* boot the stick — `/etc/machine-id` populated, six ssh host keys
generated, NetworkManager state written, cloud-init logs, all stamped 10:48. And
a power cycle brought the workstation back on the NVMe, unaided. **So the
one-shot USB mechanism works in both directions**; only the network failed.

Why it failed, found in the stick's own `cloud-init.log`: **Raspberry Pi OS
trixie is cloud-init driven** (v25.2) and reads its network configuration from
`/boot/firmware/network-config`, so the NetworkManager profile dropped into
`/etc` was the wrong lever and was never consulted. `wlan0` shows `Up: False`
throughout. Compounding it, this AP runs on **channel 52 — 5 GHz DFS** — which
does not exist for a radio with no regulatory domain set, and RPi OS leaves the
radio rfkill-blocked until a WLAN country is configured.

So the reproducible preseed for any RPi OS-derived image is three files on the
FAT partition and no rootfs surgery at all: `user-data` (users, keys, `bootcmd`
for `rfkill unblock` and the country), `network-config` (netplan v2 wifi with
SSID and PSK), and `meta-data` (whose `instance-id` must change, or cloud-init
sees an instance it has already configured and skips every module).

### Attempts 2 and 3, and where the WiFi chase ended

Attempt 2 (cloud-init seed: `user-data` + `network-config` + bumped
`instance-id`) worked as a *mechanism*: cloud-init ran every module, `bootcmd`
succeeded, `write_files` installed the watchdog, and it rendered netplan with
`renderer: NetworkManager`. `wlan0` still never associated.

Attempt 3 added the thing that should have existed first — **an offline
diagnostics channel**: a unit that dumps `rfkill`, `iw reg`, `nmcli`
device/connection/scan state, `ip`, and the NetworkManager journal to
`/boot/firmware/wk-diag.txt` 75 s into the boot, where the host can read it even
though the board is unreachable. It worked, and it ended the guessing:

- **rfkill: not blocked.** Soft and hard both `no`.
- **The regulatory theory was wrong.** The image had `global country CA` with
  `phy#0 country 99: DFS-UNSET` — and so does **the workstation, which is
  connected to this AP on channel 52 right now**. Identical regulatory state,
  one works. So neither the country nor DFS was ever the blocker, and
  `cfg80211.ieee80211_regdom=CA` in `cmdline.txt` was a fix for a non-problem.
- **The AP was visible the whole time** — `Ducky0138` on channel 52 (signal 67)
  and again on 100, so scanning was never the issue.
- **What actually failed was association, twice over, for two different
  reasons.** NM had *two* competing profiles for the one SSID: the keyfile
  copied from the workstation, which failed with `no secrets: No agents were
  available for this request`, and cloud-init's netplan-rendered one, which had
  its secret ("secrets exist. No new secrets needed") and then looped
  `associating -> disconnected` until "association took too long".

The keyfile failure is a finding worth keeping: **on Debian/RPi OS trixie an NM
keyfile is not authoritative.** NM's netplan integration rewrote the dropped-in
profile as `/etc/netplan/90-NM-<new-uuid>.yaml` under a *different* uuid, and the
secret did not survive the round trip. Copying a workstation's
`.nmconnection` into an image is therefore not a reliable way to give it
credentials on this distro; the cloud-init `network-config` seed is.

**Chase stopped there, deliberately.** The remaining candidates — netplan/NM
secret round-tripping, or the Pi 5 nvram variant this image selects — are
properties of *Raspberry Pi OS as a test vehicle*, and the real image will be
built from the same Ubuntu base as the workstation, which demonstrably drives
this radio onto this AP. Two conclusions for the deliverable instead: prefer
**wired** for anything that must be reachable mid-test, and build the perf image
from the base whose network stack is already proven on the hardware.

Three things this bought, all worth more than the test itself:

1. **An image that cannot be reached must return the machine by itself** — and
   this is now proven, not just proposed. The self-return watchdog fired at 420 s
   on attempt 3 and handed the board back to the NVMe workstation with nobody
   touching it. Every image gets one: a oneshot that reboots after N minutes
   unless a marker (`/run/wk-keep-running`) has been created by whoever logged
   in. Note the version that does *not* work: with systemd's default
   `TimeoutStartSec=90`, a `Type=oneshot` unit sleeping longer than that is
   killed before it can fire — set `TimeoutStartSec=infinity`.
2. **`wk image build` must set the WLAN regulatory country** and `rfkill unblock`
   before NetworkManager starts, not rely on a distro's first-run wizard.
3. **A console is the only ground truth when the network is the thing that
   failed.** HDMI, or better, the UART — `BOOT_UART=1` is already set in this
   board's EEPROM, so a serial console needs only a cable and would have answered
   "did it boot the stick or hang at firmware" in one look. Worth having before
   the next attempt.

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

## Appendix: the mechanism test, reproducible from this file alone

Recorded because the scripts that ran it were scratch files, and because the next
person needs the *shape* rather than the scripts — `wk image build` and
`wk pi flash` are the reproducible versions, and these are what they must
encapsulate.

**1. Write a throwaway image to the stick** (on the board itself; `/` has 219 GB
free, `/boot/firmware` has only 366 MB and is the wrong place):

```sh
cd /var/tmp
curl -fL -o rpios.img.xz https://downloads.raspberrypi.com/raspios_lite_arm64_latest
sudo umount /run/media/$USER/*            # the stick automounts
xz -dc rpios.img.xz | sudo dd of=/dev/sda bs=4M conv=fsync
sudo partprobe /dev/sda                   # -> sda1 vfat bootfs, sda2 ext4 rootfs
```

**2. Seed it through cloud-init**, which is what Raspberry Pi OS trixie actually
honours — three files on the FAT partition, no rootfs surgery. `/boot/firmware/`
is the NoCloud seed dir:

- `network-config` — netplan v2 (`version: 2`, `wifis: wlan0: access-points:` with
  SSID and PSK, plus `ethernets: eth0: dhcp4: true` so a cable Just Works).
  Read the SSID and PSK out of the board's own
  `/run/NetworkManager/system-connections/*.nmconnection` **on the board**, so the
  credential never travels through a log or an agent's context.
- `user-data` — `#cloud-config` with a user carrying the driving machine's public
  key, `hostname`, and `write_files` for the two units below.
- `meta-data` — **bump `instance-id`**, and `rm -rf /var/lib/cloud` on the rootfs.
  Without both, cloud-init recognises an instance it has already configured and
  skips every module.

**3. The two units every image needs.** Self-return, so an unreachable image hands
the machine back by itself — note `TimeoutStartSec=infinity`, because systemd's
default 90 s start timeout kills a `Type=oneshot` that sleeps longer than that
before it can ever fire:

```ini
[Service]
Type=oneshot
ExecStart=/bin/sh -c 'sleep 420; [ -f /run/wk-keep-running ] || systemctl reboot'
TimeoutStartSec=infinity
```

And the diagnostics dump, written where the host can read it offline — this is
the single highest-value thing in the whole test:

```ini
[Unit]
After=NetworkManager.service
[Service]
Type=oneshot
TimeoutStartSec=300
ExecStart=/bin/sh -c 'sleep 75; { date -Is; rfkill list; iw reg get; \
  nmcli -f DEVICE,TYPE,STATE,CONNECTION dev; nmcli -f NAME,TYPE,DEVICE con show; \
  nmcli -f SSID,CHAN,SIGNAL,SECURITY dev wifi list; ip -br addr; \
  journalctl -u NetworkManager --no-pager | tail -60; } \
  > /boot/firmware/wk-diag.txt 2>&1; sync'
```

Make the journal persistent too (`mkdir -p /var/log/journal` plus
`Storage=persistent`); the stock image's journal is volatile, so the first
attempt's logs were simply gone.

**4. Arm and go.** The mailbox reply is the confirmation — `0x80000000` in the
second word means the request succeeded, and `0x80000004` marks the tag processed:

```sh
sudo vcmailbox 0x0003808b 4 4 0xf64      # USB(4) -> NVMe(6) -> restart(f), one-shot
sudo systemd-run --on-active=3 --unit=oneshot-reboot /sbin/reboot
```

Arming with the *current* `BOOT_ORDER` (`0xf461` here) is a safe no-op that proves
the call works without changing what the next boot does.

**5. Verify without needing the board reachable.** After it returns, mount the
stick from the workstation and look for artifacts only a real boot produces:

```sh
sudo mount /dev/sda2 /mnt/r
sudo cat /mnt/r/etc/machine-id            # populated  = systemd ran
sudo ls /mnt/r/etc/ssh/ | grep -c ssh_host_   # 6       = first-boot services ran
sudo mount /dev/sda1 /mnt/b && sudo cat /mnt/b/wk-diag.txt
```

That sequence is what proved the one-shot works while the board was unreachable,
and it is why the design does not depend on the network to tell you whether the
network failed.
