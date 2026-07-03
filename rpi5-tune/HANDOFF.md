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

## Step 2 — START THE NUMA PROCESS (the main ask)
Goal: enable NUMA emulation for the ~6–18%+ gain. **Full procedure in `rpi5-numa-README.md`.**
Quick status check first:
```bash
grep CONFIG_NUMA_EMU /boot/config-$(uname -r)   # stock 26.04 = "not set"
```
- If it's **not set** (expected): do **Path A** (Launchpad request to enable `CONFIG_NUMA_EMU`
  in linux-raspi — Igalia authored the feature) and/or **Path B** (config-only kernel rebuild;
  safe thanks to 26.04 A/B boot). See README.
- Once a `CONFIG_NUMA_EMU=y` kernel is running: `NUMA_FAKE=4 bash ~/rpi5-tune/rpi5-setup.sh` → reboot → verify with `dmesg | grep -i numa` and `numactl --hardware`.

## Known-good tuning summary (validated on the prior 24.04 install)
2.8GHz CPU (3.0 was UNSTABLE — SIGILL), v3d=1200 GPU, PCIe Gen3, perf governor, swap OFF,
fan 100% via fan-max.service (trip-lowering + pwm=255), de-snapped, Flatpak Chromium,
apport off + systemd-coredump, indexer+Evolution masked, fstab discard→defaults.
