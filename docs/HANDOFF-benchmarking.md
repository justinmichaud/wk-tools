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

**What is not written, deliberately.** The runner in the benchmark role. Two
things have to be read out of a real checkout before it can be written
honestly, and neither is guessable:

1. **Does `run-benchmark` drive from a partial tree?** The staged payload is
   `Tools/` plus `WebKitBuild/<config>`; webkitdirs finds a checkout root by
   looking for files that a partial tree may not have. If it does not, the
   answer is a checkout on the volume — cloned once, `git fetch` per run, with
   only the product staged each time — and that is a decision to take with the
   measurement in hand, not before.
2. **Which browser driver name is the Mac MiniBrowser?** The Linux ones are
   `minibrowser-wpe` and `minibrowser-gtk` and are named in `cmd/bench`
   already; the Apple one has to be read from
   `webkitpy/benchmark_runner/browser_driver/`.

`wk bench stage` says all of this in its own output rather than implying the
loop is closed.

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
