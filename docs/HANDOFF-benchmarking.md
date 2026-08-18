# HANDOFF — bootable benchmark images

A managed image, deployed automatically to external media (or netboot), that a
machine boots into for benchmarking: pre-configured with tailscale (like the
rpi5 provisioning), no sandboxing inside, driven remotely by a benchmark
runner on another computer, for maximum perf stability.

Targets: macOS and Linux workstations, rpi5 (Ubuntu), rpi5/4/3 (yocto).

Open research questions:

- What drive speed/size is required — ideally a 32 GB flash drive works.
- The BMC image-boot option for moose (RAM should be ample) and netboot are
  both candidates for media-less booting.

## The rpi5 model (decided 2026-08-18)

This supersedes the earlier rpi5-as-tuned-test-device plan. End state:

- **The rpi5 is a full workstation.** Its own `./setup`, full tailnet
  privileges, Claude sandboxed in podman workspaces exactly like moose. Its
  workstation identity is never in `pi-hosts` — an unrestricted node
  reachable from inside a workspace would defeat the boundary.
- **Benchmarking boots the image built here** (netboot or USB). The image
  carries the perf tuning that used to live on the installed OS — overclock,
  v3d, perf governor, swap off (see `host/linux/rpi5/`) — plus the quiesce
  environment, and joins the tailnet under its own identity, tagged `tag:wk`.
  That identity goes in `pi-hosts` and is what workspaces and the remote
  benchmark runner reach.
- **The stability half of the rpi5 tuning** (fan always 100%, WiFi
  stability, fstab/indexer, the NUMA kernel) applies to the rpi5 in every
  role and stays on the installed OS.
- rpi4/rpi3 are unchanged: plain test devices via `wk pi setup`.
- Sequencing: the rpi5 stays a tuned test device until this image exists
  (HANDOFF.md lane A, after step 7 — this consumes step 7's flashing flow),
  then converts to a workstation. No gap in benchmarking capability.
