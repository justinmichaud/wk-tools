# HANDOFF — next agent, read this first

**Context:** jmichaud (Igalia) re-installed **Ubuntu 26.04 LTS "Resolute Raccoon"** (kernel 7.0)
on this Raspberry Pi 5 (16GB, NVMe). This folder was restored from backup after the wipe.
It holds the reproducible performance-tuning setup built over a prior session.

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
