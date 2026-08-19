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

A fallback worth costing before ruling it out: a second internal-volume install
on the same Mac (a separate APFS system volume in the same container), which
avoids the external-media performance question entirely — an external SSD over
Thunderbolt is not the same storage the machine normally runs on, and for
benchmarks that touch disk that is itself a variable.

## Open research questions

- What drive speed/size is required — ideally a 32 GB flash drive works.
- The BMC image-boot option for moose (RAM should be ample) and netboot are
  both candidates for media-less booting.
- Who serves TFTP/NFS for the Pis: the BMC (which already does DHCP on that
  network) or moose.
- Whether `bless --setBoot` makes the macOS half remotely bootable, or whether
  macOS perf runs are inherently hands-on.
- Where the benchmark runner lives once the machine under test is the whole
  machine. `wk bench` today assumes a container workspace on the local host;
  an image run needs it to drive a remote machine and record
  `bench_host=image`.
