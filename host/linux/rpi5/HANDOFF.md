# HANDOFF — next agent, read this first

**Context:** jmichaud (Igalia) re-installed **Ubuntu 26.04 LTS "Resolute Raccoon"** (kernel 7.0)
on this Raspberry Pi 5 (16GB, NVMe). This folder was restored from backup after the wipe.
It holds the reproducible performance-tuning setup built over a prior session.

**Role decision (2026-08-18, confirmed 2026-08-19; see
`docs/HANDOFF-benchmarking.md`):** this tree splits, and the workstation half
is not conditional any more -- the rpi5 is provisioned as a regular
workstation now, with its own `./setup` and podman workspaces like moose. The
stability half here (fan-max, WiFi stability, fstab/indexer, NUMA kernel) keeps
applying to the installed OS in every role. The perf half (overclock, v3d, perf
governor, swap off) belongs to the benchmark image the machine boots for a run,
and the image does not exist yet -- so between now and then this board is not a
benchmark device, which is accepted rather than overlooked.

**Update 2026-08-19:** the image now has a design and is lane A's first step,
not its last -- `docs/HANDOFF-netboot.md`. Two consequences for this tree. The
perf half has a destination: the image's own `config.txt`, served over TFTP,
which the board fetches on a one-shot netboot (`vcmailbox 0x0003808b 4 4
0xf4612` + reboot) and forgets on the next. And the stability half became a
prerequisite rather than a peer -- the netboot is armed over SSH, so this board
has to be up, reachable and provisioned as a workstation before any of it
starts. As of this date it is offline (6 days on the tailnet).

Watch the firmware boundary while moving the perf half: the EEPROM
(`SDRAM_BANKLOW`, `BOOT_ORDER`) and `config.txt` are shared by both roles, so
an overclock written to the EEPROM overclocks the workstation too. The image
should carry its own `config.txt` on the boot medium.

Everything below still applies as written for the installed OS.

## Step 1 — re-apply the tuning (idempotent, run as the user, NOT sudo)
```bash
bash ~/rpi5-tune/rpi5-setup.sh
sudo reboot
sudo bash ~/rpi5-tune/rpi5-verify.sh     # confirm clocks/gen3/fan
sudo bash ~/rpi5-tune/rpi5-stress.sh     # validate 2.8GHz CPU stability
```
Then validate the GPU (v3d=1200) with a sustained glmark2-wayland load + `dmesg | grep -i v3d`.
Adjust via env vars if needed: `ARM_FREQ`, `V3D_FREQ`, `OVER_VOLTAGE_DELTA`, `BROWSER`.
(The setup script also installs the bundled `id_ed25519` SSH key into `~/.ssh/` automatically.)

### Things to re-check on 26.04 (may have shifted from 24.04):
- Paths `/boot/firmware/config.txt` and `/boot/firmware/cmdline.txt` still correct? (A/B boot may relocate.)
- Root fstab label (was `writable`) and the `discard` mount option.
- GNOME 50 indexer is `localsearch`/`tinysparql` (script already handles this dynamically).
- swapfile unit name / whether swap exists by default.

**Decided 2026-08-19 — the NUMA kernel is workstation-only.** Perf results must
represent what customers ship, and customers do not ship `CONFIG_NUMA_EMU`, so
the benchmark image runs a **stock kernel** and the custom `7.0.6-numa` kernel
below is a dev/workstation convenience from here on. Two consequences: image
numbers on this board will be lower than the tuned-workstation numbers on
memory-bandwidth-bound work (correct, not a regression), and historical numbers
taken on the numa kernel are not the going-forward baseline. `SDRAM_BANKLOW=1`
stays in the EEPROM because it is shared firmware state; a stock kernel simply
does not act on it. See `docs/HANDOFF-netboot.md`.

## Step 2 — NUMA: DONE ✅ (Path B completed 2026-07-04)
The custom **`7.0.6-numa`** kernel (`CONFIG_NUMA_EMU=y`, built via `rpi5-numa-kernel.sh`) is
installed and running. NUMA is **ON and optimal**: **8 nodes**, `mempolicy interleave:0-7`,
`SDRAM_BANKLOW=1` (bootloader default). Confirm any time with `sudo bash rpi5-verify.sh`
(RESULT line) or `numactl --hardware`.

Key facts for the next agent (see `rpi5-numa-README.md` → "Best configuration"):
- NUMA is **firmware-driven** here. There is **no `cmdline.txt`** — boot args come from
  `/proc/device-tree/chosen/bootargs`, into which the firmware injects `numa_policy=interleave`
  + `numa=fake=8`. `rpi5-setup.sh` Section 10 now pins `SDRAM_BANKLOW=1` in the EEPROM and, on
  boxes that *do* have a `cmdline.txt`, `numa_policy=interleave`. `NUMA_FAKE=auto` (default) lets
  the firmware pick the optimal node count — **do not hardcode 4** (that was 8GB-era guidance;
  8 is correct for this 16GB board).
- Still open (optional): **Path A** — Launchpad request to enable `CONFIG_NUMA_EMU` in stock
  linux-raspi so the custom kernel isn't needed long-term (Igalia authored the feature).

## Known-good tuning summary (validated on the prior 24.04 install)
2.8GHz CPU (3.0 was UNSTABLE — SIGILL), v3d=1200 GPU, PCIe Gen3, perf governor, swap OFF,
fan 100% via fan-max.service (trip-lowering + pwm=255), de-snapped, Flatpak Chromium,
apport off + systemd-coredump, indexer+Evolution masked, fstab discard→defaults.
