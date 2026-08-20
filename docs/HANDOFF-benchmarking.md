# HANDOFF — bootable benchmark images

A managed image, deployed automatically to external media (or netboot), that a
machine boots into for benchmarking: pre-configured with tailscale (like the
rpi5 provisioning), no sandboxing inside, driven remotely by a benchmark runner
on another computer, for maximum perf stability.

Targets: macOS and Linux workstations, rpi5 (Ubuntu), rpi5/4/3 (yocto).

**Vocabulary, fixed 2026-08-18 when `wk bench` learned it:** every run records
`bench_host`, and everything `wk bench` can reach today is
`bench_host=container` — a workspace, with cgroup limits and a desktop
underneath it. What this handoff builds is `bench_host=image`, and the two are
not comparable; `wk bench compare` warns across them rather than producing a
confident p-value for a machine that was doing two different things. Two other
axes were fixed at the same time and matter here: `class` (gpu-class needs a
real compositor on a real GPU; cpu-class does not and is not treated as
degraded for lacking one) and `runner` (`browser` via run-benchmark, or `jsc`
via the benchmark's own cli.js in the jsc shell). The image runner must set
`bench_host=image` and record the same `class`/`runner`/`arch` fields — see the
header of `cmd/bench`.

## Fields the image runner has to record (added 2026-08-19)

Three decisions taken while designing `docs/HANDOFF-netboot.md` add fields to the
run environment, on top of the `class`/`runner`/`host` axes `cmd/bench` already
records:

- **`kernel_provenance`** — stock or custom. Perf results represent what
  customers ship, so the image runs a **stock kernel**; the rpi5's custom
  `7.0.6-numa` kernel is workstation-only, and any older number taken on it is
  not a baseline for the image series.
- **`profile`** — `stock` (stock clocks) or `oc` (opt-in overclock), plus the fan
  policy in force. Fan-max is measurement hygiene, not tuning: it keeps a run out
  of thermal throttle so runs are repeatable. The two profiles must never merge
  into one series.
- **`kernel_arch`** — the width of the *kernel* the run was made under, beside
  the `arch` of the build that `cmd/bench` already records. They are not the
  same fact, and only recording the second makes two incomparable runs look
  like one series: an armhf build on an armhf kernel and an armhf build on an
  arm64 kernel both report `arch=armhf`. The fleet has boards where both are
  physically possible (the rpi5's kernel is `CONFIG_COMPAT=y`), so this is a
  real confusion rather than a hypothetical one. Runs whose `kernel_arch`
  differs are two series — the same rule as `bench_host` and `root_device`.
  See `docs/HANDOFF-vocabulary.md`, "32-bit and 64-bit", for why a 32-bit run
  is a 32-bit *system* rather than a 32-bit process.
- **`root_device`** — model, link speed, TRIM/rotational. The rpi4 starts on a
  USB stick with an SSD to follow, and cheap flash contributes variance rather
  than a subtractable bias, so stick runs are provisional and are not comparable
  with SSD runs — the same rule as `bench_host`.

Plus one refusal: the content origin for a measured run must resolve to
**loopback**, and nothing in the run path may be a network mount. See the
"Where the network can still get into a measurement" section of the netboot
handoff — that check is code, not documentation.

## The rpi5 model (decided 2026-08-18, confirmed 2026-08-19)

This supersedes the earlier rpi5-as-tuned-test-device plan.

- **The rpi5 is a full workstation, now** — not after the benchmark image
  exists. Its own `./setup`, full tailnet privileges, Claude sandboxed in
  podman workspaces exactly like moose. Its workstation identity is never in
  `pi-hosts`: an unrestricted tailnet node reachable from inside a workspace
  would defeat the boundary.
- **Benchmarking boots an image instead.** The image carries the perf tuning
  that used to live on the installed OS — overclock, v3d, perf governor, swap
  off (see `host/linux/rpi5/`) — plus the quiesce environment, and joins the
  tailnet under its *own* identity, tagged `tag:wk`. That identity is what goes
  in `pi-hosts` and what workspaces and the remote benchmark runner reach.
- **The stability half of the rpi5 tuning** (fan always 100%, WiFi stability,
  fstab/indexer, the NUMA kernel) applies to the rpi5 in every role and stays
  on the installed OS.
- rpi4/rpi3 are unchanged: plain test devices via `wk pi setup`.
- The gap this leaves is real and accepted: between the workstation conversion
  and a working image, the rpi5 is not a benchmark device. Nothing else was
  benchmarking on it.

## Booting the image: three machines, three mechanisms, one of them missing

"Netboot" is one word for three different things here, and only two of the
three machines can actually do it. This is the design question to settle before
building the image, because it decides what the image even is — a network root,
a RAM disk, or an installed volume.

The shared requirement: **no sandboxing inside**. The image is the whole
machine for the duration of a run — no podman, no Tart, no workspace — and the
benchmark runner drives it from another computer over the tailnet. That is the
opposite of every other environment in this repo, and it is why it is an image
rather than a mode.

### rpi5 — real netboot, and a one-shot primitive that fits perfectly

**Superseded in detail by `docs/HANDOFF-netboot.md` (2026-08-19), which is now
lane A's first step.** What it settles, so the guesses below can be read as
history: the one-shot primitive is `set_reboot_order` via `vcmailbox`, not
`tryboot` — `tryboot` picks a config on the medium that is already booting,
which leaves the medium unchosen. moose serves, not the BMC. And the DHCP
problem the section below worries about is avoidable outright: the bootloader
skips DHCP entirely when `TFTP_IP` and the static-IP keys are set.

The Pi bootloader supports network boot natively (a `BOOT_ORDER` nibble
selecting network: TFTP for firmware and kernel, NFS or an initramfs for the
root). Two things make this the easiest of the three:

- The isolated guest network already has a machine that could serve it. The
  Librem 5 BMC is described in `docs/HANDOFF-bmc.md` as doing DHCP, routing and
  tailscale proxying for that network; a TFTP root and an NFS export are the
  same box's job. Worth confirming before designing around it — the alternative
  is moose serving it, which means the Pis' network has to reach moose.
- **`tryboot` is the mechanism to reach for.** `sudo reboot '0 tryboot'` boots
  the alternate configuration *once* and falls back on the next reboot. That is
  exactly the semantics wanted here — become a benchmark machine for one run,
  come back a workstation — and it needs no EEPROM rewrite per run and no
  physical access when a run wedges the machine.

Open: whether the perf tuning in `host/linux/rpi5/` survives being baked into
an image rather than applied to an install. The EEPROM half (`SDRAM_BANKLOW`,
`BOOT_ORDER`, the overclock in `config.txt`) is firmware state, not image
state, and firmware state is shared between the two roles. Overclocking the
workstation permanently to benchmark it occasionally is the thing this split
was meant to avoid, so the answer is probably that the overclock stays in
`config.txt` on the boot medium the image supplies, not in the EEPROM.

### rpi4 — netboot is the wrong lane for this, on the hardware's own evidence

Scoped 2026-08-20, after the first real netboot of an image onto the fleet's
rpi4. The goal it is scoped against: **build an image for the rpi4 and benchmark
it end to end with no human in the room, and without the delivery mechanism
showing up in the numbers.** Those two constraints between them rule netboot out
for this lane, and neither is a matter of taste.

**No human in the room rules out netboot's failure mode.** Once the bootloader
has pulled `start4.elf` over the network and executed it, `BOOT_ORDER` is spent.
A second stage that cannot then find its kernel *halts* — no retry, no
fall-through to the SD card, nothing reachable over the wire. That is not a
theoretical tail: it happened twice on 2026-08-20 and cost two power cycles, the
first from a resolver bug and the second from a stale copy of the fixed
resolver. Both causes are now guarded (`boot/check-boot-files.py`,
`check_helper_current`), but the *class* is not closable: a server that dies
mid-transfer, or a dropped packet at the wrong moment, lands in the same place.
An unattended lane cannot have a state whose only exit is a hand on the power
supply.

Local boot fails better rather than never. A kernel that cannot find its root
panics, and these images carry `panic=10`, so the board reboots; if the boot
medium is not bootable at all the firmware moves to the next `BOOT_ORDER` entry
and lands on the SD card, reachable. Both exits are automatic.

**Not distorting the numbers rules out the network root.** "Storage: what the
image is" in `docs/HANDOFF-netboot.md` already phases this correctly — a network
root for *profiling*, a RAM root for *benchmarking* — and the reason is exactly
this constraint: an NFS root does I/O, over the same interface, during the
measurement.

But the RAM root is not available here either. It is realistic on the rpi5 (16
GB) and on moose (115 GB free); **the rpi4 has 2 GB**, and a browser benchmark
is the workload. The rpi3, at 931 MB, already needs swap to finish Speedometer.
A squashfs held in RAM plus an overlay would leave something like a gigabyte for
the thing being measured, and a run that swaps is a run whose number means
nothing.

So for the rpi4, and for the rpi4 specifically, both network options are out and
what is left is the one the netboot handoff already noted was never blocked:
**write the image to a local device over ssh and boot it.** No server, no root
mechanism, no privilege — and no network in the boot path or the measurement.

**The shape, and the one thing it needs that does not exist yet.**

- The image goes on the **USB stick** (`/dev/sda`, 29.5 GB, USB 3.0 — faster and
  far more consistent than the SD card, which matters for a benchmark). The
  **SD card stays the rescue role**: a Yocto image that boots, joins the LAN and
  answers ssh.
- `BOOT_ORDER` becomes **USB → SD → restart** (`0xf14`). Today it is `0xf412`
  — network, SD, USB — which was right for the netboot experiment and is wrong
  for this: the SD is tried before the USB, so an image on the stick would never
  boot. This is one `wk pi` write, and `boot/rpi-eeprom.sh` can do it without
  eeprom tooling on the board.
- **Arming is the stick's boot partition**, the way arming is the server's
  content for netboot. The firmware will not boot a device with no `start4.elf`,
  so renaming that one file is a complete, instant, reversible arm — and the
  disarm can be done from either role, since the rescue role can mount the
  stick's FAT partition.
- **The one-shot comes back for free.** The image renames its own `start4.elf`
  away on boot, so any later reboot falls through to the SD card. That is the
  same "reverts by itself unless claimed" property `wk boot <machine> --keep`
  relies on for the rpi5, reached without the Pi 5's `set_reboot_order` — which
  is the primitive `boot/pi-netboot.sh` correctly says the Pi 4 does not have.
  It wants a driver of its own (`boot/pi-usb.sh`); `pi-netboot.sh` stays where
  it is, for the profiling lane.

**What netboot is still for.** Nothing above wastes it. Profiling wants a root
that is a directory on the server, editable in place, and does not care about
storage noise; that is phase 1 of "Storage: what the image is" and netboot is
how it gets there. The two lanes want opposite things and should stop sharing a
mechanism.

**Already landed toward this, 2026-08-20:** `wk image write` refuses an image
whose boot partition cannot get the firmware as far as a kernel
(`image_check_boot_files`), checked from the image file before anything is
written. That is the pre-flight that makes an unattended local boot safe, and it
is the same check `wk serve` runs — firmware asks a disk the same questions it
asks a TFTP server.

**Built 2026-08-20, all of it offline — the board was halted at the time and
none of this has met hardware yet:**

- **`wk pi boot-order <host> <order>`**, with `netboot-first`, `netboot-last`,
  `usb-first` and `local`. One writer still: `netboot-enable` is now a spelling
  of the same function rather than a second implementation, because BOOT_ORDER
  is firmware state shared by every role and two commands that can both write
  it is two places for a wrong value to come from. The orders are derived from
  whatever is already there rather than written as constants, so a board with a
  nibble this does not know about keeps it, and applying one twice is a no-op.
- **`boot/pi-usb.sh`**, and a third arming model to go with it. The other two
  put the intent in firmware (`one-shot`) or in the server's content
  (`server`); `medium` puts it on the disk the machine boots from — the only
  one of the three that is both readable and writable from the other role, so
  `--status` reports the arming as evidence rather than as a claim.
- **The self-disarm**, which is what makes a file-based arming behave like a
  one-shot. `wk image build` installs it as a `sysinit.target` unit, so the
  image parks its own `start4.elf` before it does anything else and every later
  reboot — clean, panic, watchdog or power cut — lands on the SD card.
- **`force_turbo=1` with `arm_freq=arm_freq_min=1500` and `arm_boost=0`**, in
  `image/rpi4-perf/config.txt.append`. Pinned rather than fast: 1800 needs the
  boost clock, and a Pi 4 that throttles partway through a run has produced two
  measurements and reported one.
- **`wk-no-swap.service`**, which is a guard and not a change — the Ubuntu base
  ships no swap, and the point is that swap arriving later would otherwise show
  up as variance nobody could explain.

**A bug found while building it, and worth knowing about beyond this lane.**
`user_data()` in `cmd/image` is an *unquoted* heredoc, because it interpolates
the profile's values — so its lines are shell input, including the ones that
look like YAML comments. Backticked command names in prose were being executed
on the build host: three `systemd-run` invocations per build, and the comment
explaining the cloud-init deadlock had lost the name of the thing that caused
it. Escaped, and `wk selftest` now refuses an unescaped backtick in that
heredoc. `install_unit` also grew a target argument in the same pass: nothing
ever runs `systemctl enable` here, so a unit's own `[Install] WantedBy=` is
inert and a unit asking for `sysinit.target` was being linked from
`multi-user.target.wants` — an ordering systemd cannot satisfy.

### On hardware, 2026-08-20 — the lane runs, and the disarm was wrong twice

The whole chain was exercised on the board: `wk pi boot-order rpi4-test
usb-first` wrote `0xf14` through recovery.bin (bootloader 2023-01-11 ->
2026-05-17, `TFTP_IP` removed), `wk image write` put a 4.6 GB image on the stick
and verified it by read-back, `wk boot rpi4` armed and rebooted, and **the image
came up on `/dev/sda2` with its own identity marker**. `wk boot rpi4 --status`
reported `image rpi4-perf-...` from evidence. The self-disarm had run.

Four facts corrected against hardware, and two of them matter beyond this board:

- **The rpi4 is a 4 GB Pi 4B Rev 1.5, not 2 GB.** `boot/machines.sh` said 2 GB
  and this file argued from it. The USB-local decision does not move -- it rests
  on netboot's halt, which is independent of RAM -- but the RAM-root option is
  more plausible than it was written up as, and should be re-costed if the
  profiling lane ever wants it.
- **`force_turbo` works**: `measure_clock arm` reads 1500345728 at idle, and
  `get_throttled` was `0x0` at 48.7 C through the run.
- **The perf sysctls were never taking effect, and this affects the rpi5 image
  too.** `systemd-sysctl` ran at 18:33:04; cloud-init wrote
  `/etc/sysctl.d/90-wk-perf.conf` at 18:33:21. Seventeen seconds too late, so
  `perf_event_paranoid` was still 4 on an image whose entire purpose is that it
  is -1 -- and on a one-shot image, "takes effect on the second boot" means
  never. The file is installed into the rootfs at build time now (`install_file`),
  which is the lesson the watchdog already taught and this had not learned.
  **The rpi5's `-1` should be re-checked**: this file records it as confirmed,
  and the same ordering applies there.
- **`image_addr` could not find the image.** It looks the board up by MAC in the
  neighbour table and the rpi4 had no `MACH_MAC`; its fallback is the image's
  mDNS name, which cloud-init installs and so is absent for the first several
  minutes of exactly the boot worth watching. The MAC is declared now, and
  `image_addr` prefers a REACHABLE neighbour entry -- this board takes a fresh
  DHCP lease per boot and was observed holding three at once, two of them dead.

**And the disarm was wrong, in the same way twice.** The first version renamed
`start4.elf` aside, reasoning that a device the firmware cannot boot is a device
it skips. The observation behind that was real but was of a *different state*: a
stick carrying one **ext4** partition and no FAT at all, which does skip. A
stick with a valid FAT boot partition that is merely missing `start4.elf`
**halts the firmware** -- the netboot failure again, reached from a third
direction, and it cost the board another power cycle.

The disarm now reproduces the state that was watched skipping: partition 1's MBR
type byte flips 0x0c -> 0x83, so the firmware finds no boot filesystem on the
device at all. One byte at offset 450, written with `dd conv=notrunc` rather
than through `sfdisk`, because the self-disarm runs from that same disk with
partition 2 mounted as root -- a tool that rewrites the table asks the kernel to
re-read it, and a surgical write asks nothing of anybody. `wk selftest` checks
the offset, the round trip, and that the write does not truncate the device.

**The rule this board has now taught three times, worth stating once:** Pi
firmware that finds a boot medium and then cannot complete from it *halts*. It
only falls through when the medium is not bootable at all. Every arming
mechanism here has to produce the second state, never the first.

### The lane closed, 2026-08-20 evening

With the partition-type disarm in place the whole cycle ran clean and with no
hands on the board:

    wk image build rpi4-perf                        2.8G, 47 s from a cached base
    wk image write <id> --disk rpi4:/dev/sda        4642 MB, verified by read-back,
                                                    left disarmed
    wk boot rpi4                                    armed, rebooted
    ... image up on /dev/sda2, self-disarmed ...
    wk boot rpi4 --back                             back on the SD card in 84 s

and then the same cycle again with nothing touching it at all, which is the
property the lane exists for: armed 18:53, image up 18:55:30, the 900 s
self-return watchdog fired at ~19:11:30, and the board was back in its rescue
role at 19:12:20. No command, no console, no hands.

Verified inside the running image, which is the point of all of it:

    kernel.perf_event_paranoid = -1      (was 4 before install_file)
    kernel.kptr_restrict = 0
    scaling_governor = performance
    measure_clock arm = 1500345728       pinned, at idle
    get_throttled = 0x0                  at 51.1 C
    swap: none
    / on /dev/sda2                       no network anywhere in the boot path
    /dev/sda partition 1 type = 0x83     the image disarmed itself on the way up
    start4.elf still present             the disarm destroys nothing

Two more bugs found in the process, both of the same shape -- a mechanism that
looked right and had never been exercised twice:

- **`i_ssh` pinned the image's host key, and an image's host key changes every
  build.** `wk image build` makes a fresh rootfs, ssh generates a fresh key in
  it, and the next image boots at the same address. `accept-new` accepts keys
  it has never seen, not keys that have moved -- so the *second* image ever
  booted on a machine was refused, and `wk boot --status` called a running,
  pingable, healthy board unreachable. Nothing is pinned on that channel now:
  a key with no continuity across builds cannot authenticate anything, and
  `/etc/wk-image` says *which* image answered, which is the stronger statement.
  **The rpi5 has the same channel and will hit this on its second image.**
- **The self-disarm command was interpolated into a single-quoted systemd
  `ExecStart` and contained single quotes of its own**, which close that string
  early. Found by reading the unit out of a built image; the only other way it
  was going to surface was a board that did not come back. `wk selftest`
  asserts the absence now.

**The residual hands-on case, stated rather than papered over.** An image that
hangs *between kernel start and sysinit* never reaches its self-disarm, so the
stick stays armed and a power cycle re-enters it. `panic=10` covers kernel-level
failure and the watchdog covers anything after multi-user, but that window is
real. It is far narrower than netboot's — which halts on any transfer hiccup, on
every boot — and it is the one place this design is weaker than the rpi5's, whose
one-shot is consumed by the boot *attempt* rather than by the boot succeeding.
Closing it properly would mean disarming from the initramfs.

**Still to build:** `bench_host=image` itself, which none of this has touched —
the runner, the fields in "Fields the image runner has to record", and getting a
WebKit build onto a booted image without reimaging it. Then the remaining
measurement question this lane has not answered: the Pi 4 throttles under
sustained load and nothing yet reads the firmware's throttled flags, so a run
that thermally degraded is currently reported as a run.

### moose — UEFI network boot, or the BMC's virtual media

An Ampere workstation with a BMC has two routes, and they differ in what has to
be true of the network:

- **UEFI HTTP/PXE boot**, with the image as a RAM root so nothing on the NVMe
  is touched or even mounted. RAM is ample here (115 GB free measured during
  this session), so a full image in RAM is realistic and removes every question
  about disk state affecting a benchmark.
- **BMC virtual media** — mount the image over KVM-over-IP and boot it. Already
  named as a candidate in `docs/HANDOFF-bmc.md` ("the BMC image-boot option for
  moose"). Slower to load, but it needs nothing from the network's DHCP and it
  works when the machine is otherwise unreachable, which is also the recovery
  story that handoff wants.

Either way the run is remote-driven, and the machine that drives it cannot be
moose. That is a new constraint: today moose is the machine that runs
`wk bench`. An image run needs a *second* machine to hold the runner — the MBP,
or the rpi5 in its workstation role.

### macOS — there is no netboot, and this is the hard one

**Apple Silicon cannot boot from the network at all.** NetBoot and NetInstall
were Intel-era features and are gone; the Apple Silicon boot chain requires a
LocalPolicy held in the machine's own secure storage, so there is no equivalent
to hand the machine an image over the wire. The MBP here is an M4 (SETUP.md
section 8), so this is not a "which macOS version" question — it is structural.

What exists instead:

- **A personalised external volume.** An external SSD with a full macOS
  install can be booted, but the boot policy lives in the Mac's own secure
  storage: the volume has to be personalised for each machine that boots it
  (installed or blessed from that machine), not merely copied to. So "build one
  image, boot it anywhere" does not hold here the way it does for the Pi — it
  is an install per Mac, maintained per Mac, even if the contents are identical.
- **Selecting it.** Holding the power button gives the startup manager, which
  is physical access. Whether `bless --setBoot` can set an external APFS system
  volume as the default from a running Apple Silicon macOS — making
  "reboot into the benchmark install" scriptable and remote — is the single
  question worth answering first, because everything else about the macOS half
  depends on it. If it works, the flow is symmetrical with `tryboot`; if it
  does not, macOS perf runs need someone in the room, and the honest thing is
  to say so in the docs rather than build an automation that cannot exist.

  **Answered 2026-08-19: it does not, so macOS perf runs are hands-on.** Boot
  volume selection on Apple Silicon goes through LocalPolicy, changed only via
  System Settings → Startup Disk or Startup Security Utility in Recovery — both
  authenticated user actions. `bless --setBoot` is superseded for this purpose;
  its `folder` option survives only for external media. Screen Sharing makes the
  switch remote-ish, never automatic. Recorded in `docs/HANDOFF-netboot.md` as
  tier 2, alongside the second-internal-volume alternative.

### The macOS shape, decided and half-built 2026-08-20

**Build inside the VM, run outside on the metal.** Both halves are necessary
and neither is negotiable. A guest cannot produce a number worth keeping — it
shares a CPU with a desktop, its GPU is paravirtualised, its scheduler is
somebody else's — and the benchmark install cannot build, because an install
carrying Xcode and a checkout is no longer a benchmark install: it is a second
workstation, drifting from the first. So the build stays where builds are
cheap and reproducible, and what crosses the boundary is the product.

The thing that makes this cheap on a Mac, and that has no equivalent on the
Pis: **while this machine is in its normal role the benchmark volume is simply
mounted**, so staging a build onto it is a copy rather than a transfer, and
reading the results back afterwards is the same. The transition is the only
manual part, and it is two clicks.

Built and verified as far as the hardware allows (there is no benchmark volume
on this machine yet — exercised against a disposable `hdiutil` APFS volume):

- **`boot/mac-volume.sh`**, the fleet's third arming model. `one-shot` (rpi5)
  and `server` (rpi4/rpi3) are joined by **`hands-on`**: the driver checks what
  it can, records the intent, and prints the ritual; nothing reboots the
  machine, because the role it is going to is chosen at the startup manager.
  `wk boot mbp --status` then reports it as *armed and waiting for a person*
  rather than as something that will move by itself, and `--disarm` says which
  half a person still has to undo if they used the sticky route.
- The machine drives itself (`MACH_LOCAL` in `boot/machines.sh`): there is one
  Apple Silicon machine here, so the transition is arranged from inside the
  role being left, by a shell that is about to be rebooted out from under
  itself. That also answers the open question below about where the runner
  lives — for the Mac it is the machine itself, writing its results onto the
  volume, which the normal role reads back the moment it returns.
- Boot identity comes from `kern.boottime`, which does the same job for "has
  this arming been spent" that Linux's random `boot_id` does, and is a clock
  reading as well.
- **`wk bench stage <ws> --to mbp`** copies the product out of the guest onto
  the volume: `WebKitBuild/<config>`, `Tools/` (run-benchmark, webkitpy and
  the plans — the smallest tree that can drive a run), and a `stage.json`
  written last, carrying workspace, sha, config, wk-tools tree hash and
  `bench_host=image`. Provenance is decided *here*, where it is all still
  known: the other role cannot ask a guest anything.
- **`t_pull_dir`** joins `t_pull` in the driver contract, since a build tree is
  tens of thousands of files: `podman cp` for a container, rsync over ssh for
  a guest and for a build machine.

### The rehearsal: a guest standing in for the benchmark role

Everything above except the *number* can be exercised without a reboot and
without a disk, by making the benchmark machine a second macOS guest:
`boot/mac-guest.sh`, machine `benchvm`. Build in one guest, stage across the
machine boundary into another, run there, read the result back — which is the
whole design, with the one manual step replaced by `wk vm start`.

It proves the mechanism and cannot prove the measurement: a guest shares a CPU
with a desktop and its GPU is paravirtualised, which is precisely why the real
role is bare metal. What it *does* answer, in minutes rather than in a
half-hour trip to the keyboard, is whether the staged tree carries everything
run-benchmark needs, whether the payload survives the crossing, whether the
role is recognised on the other side, and whether the record is complete.

Three things fell out of building it, all of which apply to the real machine
too:

- **Staging is products, not the build tree.** An Apple `WebKitBuild/<config>`
  is ~39 GB and nearly all of it — `Intermediates.noindex`, the compilation
  cache, the module cache, `DerivedData`, `*.dSYM` — is of no use to a machine
  that will not compile anything. `t_pull_dir` grew `--exclude` for it, and the
  container driver *refuses* excludes rather than silently copying everything
  (`podman cp` has no filter).
- **A machine that is off is a normal state.** The guest driver's probes ran
  under `set -o pipefail` and `wk boot benchvm --status` exited 1 in silence
  when the guest was not running. `wk selftest --quick` now asserts that every
  driver answers `--status` with *something* and exits 0/2/3.
- **`wk boot rpi5 --status` could not run on the Mac at all.** `date -u -d
  @<epoch>` is GNU-only; BSD date answers "illegal option -- d". The fleet is
  meant to be drivable from either workstation and one of them could not read a
  machine's boot time — `epoch_to_utc`/`utc_to_epoch` in `lib/common.sh` now.

The payload is pinned the same way in both: `wk bench stage --payload <dir>`
copies a benchmark checkout in beside the build, so the run needs no network
and cannot silently measure a different revision than the last run did.

**Prepare the benchmark install by hand, once.** `wk quiesce` already covers
the per-run half on macOS (a `caffeinate`, the analysis daemons paused, and the
privileged helper switching Spotlight, automatic updates and low power mode
off), and as of 2026-08-20 it also *measures* the result rather than trusting
it — including the two things the helper does not touch, a configured Time
Machine destination and the thermal state. What is left is what the install
itself has to be:

- a full macOS install on the volume, *personalised for this Mac* (installed or
  blessed from it — copying an image onto a disk does not boot), named
  `WK Bench` (or `WK_BENCH_VOLUME`);
- Spotlight indexing off for that volume, Time Machine off, automatic updates
  off, sleep and screen saver off, Siri and analytics off, no login items;
- an `/etc/wk-image` with an `id=` line — that marker is the only thing that
  tells `wk boot` which role answered, and without it the benchmark role
  reports itself as a workstation;
- ssh in, so the run can be driven from a terminal rather than a keyboard.

**The runner: `wk bench staged`, written 2026-08-20** — and the two questions
it was waiting on were answered by reading and running WebKit's own code (a
`Tools/Scripts` tree out of a base snapshot, on this Mac), not by guessing:

1. **run-benchmark drives from a partial tree.** `Tools/Scripts` alone is
   enough — `--list-plans` exits 0 with no checkout root, no `Source/`, no
   `WebKitBuild`. Plan files come from `BenchmarkRunner.plan_directory()`,
   which is relative to webkitpy itself, and so do the patches a plan applies
   (`get_path_from_project_root` resolves against the `benchmark_runner`
   package). So the staged tree never has to grow into a clone.
2. **The browser is `--browser minibrowser --platform osx`,** and
   `--build-directory` is what makes the partial tree work: with one, the
   driver launches `MiniBrowser.app/Contents/MacOS/MiniBrowser` itself with
   `DYLD_FRAMEWORK_PATH` set. Without one it goes through
   `Tools/Scripts/run-minibrowser`, which is the path that *would* have needed
   a checkout. It insists on a `*.framework` in the build directory.

Two more things were found the same way, and both would otherwise have been
discovered in the benchmark role with the machine already rebooted:

3. **It needs a python with PyObjC.** The driver's `prepare_env` imports
   `webkitpy.autoinstalled.pyobjc_frameworks`, whose first line is a bare
   `import objc` — pyobjc-core is *not* autoinstalled. Apple's
   `/usr/bin/python3` has it (3.9.6, pyobjc 11.1); the Homebrew python3 first
   on PATH does not. The runner names the interpreter explicitly.
4. **Everything else webkitpy installs itself**, into the staged tree
   (`Tools/Scripts/libraries/autoinstalled`), on first use — so either the
   benchmark install has the network once, or the tree is staged from a guest
   where it has already happened and carries the packages with it.

**The run happens in the benchmark role or it does not happen.** That is the
one refusal here that `--force` does not open, at the user's direction and for
the reason the whole design exists: a run on the workstation shares the machine
with a desktop, a podman VM and a browser, and produces a result of exactly the
same shape — same command, same plan, same build, same JSON. Nothing tells the
two apart afterwards except the refusal, so there is no "record it as a
workstation run" escape hatch. `--dry-run` still describes the whole thing from
the workstation role, because describing is not measuring.

`WK_IMAGE_MARKER` exists so the benchmark role's code path can be exercised
without a reboot; a run that uses it is announced loudly and recorded as
`role_marker_overridden`, and `wk bench compare` warns about it. The record
cannot be made to lie by an override nobody remembers setting.

**What the run records**, in the same shape a container run records so
`wk bench compare` can put one against the other: the three axes
(`class` / `runner` / `bench_host=image`), the role, the staged payload, the
workspace and sha it came from, and the machine — model, cores, memory, macOS
version, AC or battery, `CPU_Speed_Limit`, and the display size, because both
Speedometer and MotionMark scale with the surface being drawn.

**Preflight, all of it measured**: the role, run-benchmark in the staged tree,
`MiniBrowser.app` plus a framework in the build, PyObjC in the interpreter that
will be used, a console session (a browser driven over ssh with nobody logged
in at the screen has nowhere to draw), AC power, and the machine's own quietness
through the same `macos_noise` that `wk quiesce` uses.

**Verified end to end against a simulated role** (a stub browser, a disposable
APFS volume, a trivial payload): payload build, http server, driver creation,
`prepare_env`, browser launch, the timeout path, the recorded failure with the
real exception surfaced, and the record on the volume. What has *not* run is a
measurement — that needs a real `mac-release` build, which needs the golden
base guest this machine does not have yet.

One side effect worth knowing about, found by running it: **a run that dies
leaves the Dock's launch animation off.** webkitpy turns it off in
`prepare_env` and restores it only on a clean exit; a timed-out run does not
reach the restore. `wk bench staged` puts it back afterwards, and only when it
is actually off.

A fallback worth costing before ruling it out: a second internal-volume install
on the same Mac (a separate APFS system volume in the same container), which
avoids the external-media performance question entirely — an external SSD over
Thunderbolt is not the same storage the machine normally runs on, and for
benchmarks that touch disk that is itself a variable.

## Open research questions

- What drive speed/size is required — ideally a 32 GB flash drive works.
  **Answered for the rpi5 (2026-08-19): none.** It netboots, and the build
  payload lands on a dedicated NVMe partition — see `docs/HANDOFF-netboot.md`,
  "Storage". Still open for moose and the MBP.
- The BMC image-boot option for moose (RAM should be ample) and netboot are
  both candidates for media-less booting.
- ~~Who serves TFTP/NFS for the Pis~~ — **answered 2026-08-19: moose.** Serving
  a multi-gigabyte root is not a phone's job, and the BMC is offline more often
  than moose. Its DHCP role on the guest network is unaffected.
- ~~Whether `bless --setBoot` makes the macOS half remotely bootable~~ —
  **answered 2026-08-19: it does not.** macOS perf runs are hands-on, and the
  `hands-on` arming model above is what that looks like once it is admitted
  rather than worked around.
- ~~Where the benchmark runner lives once the machine under test is the whole
  machine~~ — **answered for the Mac, 2026-08-20: the machine itself.** It is
  the only Apple Silicon machine here, so nothing else can drive it; the
  payload is staged onto the volume before the transition and the results are
  read back off it afterwards. Still open for the Pis, where the runner is on
  the machine that serves the image.
- Still open: **nothing runs `wk quiesce` on the benchmark volume for you**,
  and nothing checks the install *before* a run rather than during it. The
  measurement exists now (`wk quiesce status` on macOS reports Spotlight, Time
  Machine, updates, sleep, low power and the thermal limit from evidence); what
  is missing is a preflight in the benchmark role that refuses a run on an
  install that is still indexing itself.
