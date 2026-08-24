# HANDOFF — the rpi5's tuning tree

This directory is the rpi5's performance-tuning setup, restored from backup
after a wipe and re-applied by hand. The board is a **workstation** (its own
`./setup`, full tailnet privileges, podman workspaces like moose), so this tree
splits in two:

- the **stability half** — fan, WiFi, fstab/indexer, the NUMA kernel — applies
  to the installed OS, i.e. host mode;
- the **perf half** — overclock, v3d, perf governor, swap off — belongs to the
  bench system the board boots for a run (`perf-linux-rpi5`).

Governor and swap-off are already baked into every system by `cmd/sysimage`.

## Remaining

- **The overclock has not moved to the bench system.**
  `image/perf-linux-rpi5/config.txt.append` says so in place: when an `oc`
  profile arrives it sets `arm_freq`/`over_voltage` in *that* file, per image.
  `image/perf-linux-rpi4/config.txt.append` is the worked example
  (`arm_freq=1500`, `force_turbo`). Note `docs/HANDOFF-benchmarking.md` requires
  a run to record `profile=stock|oc` and `cmd/bench` records neither — one piece
  of work, not two.
  **Never write the overclock to the EEPROM**: `SDRAM_BANKLOW` and `BOOT_ORDER`
  are firmware state shared by both modes, so it would overclock the workstation
  too.
- **Nothing recreates this tree.** `rpi5-setup.sh` applies fan, wifi powersave,
  the BSSID pin, fstab and the NUMA kernel with inline `sudo`, and neither
  `./setup` nor `wk backup` reproduces any of it — so a rebuild of this board
  loses all of it. The sharpest live example for
  `docs/HANDOFF-settings-audit.md` and for cattle-not-pets' obligation 2.
- **Re-flashing this board from nothing** still needs another provisioned
  machine, pending `wk sysimage flash --reader` (`docs/HANDOFF-sdcard.md`).
- **Path A is unfiled** — the Launchpad request to enable `CONFIG_NUMA_EMU` in
  stock linux-raspi, so the custom kernel is not needed long-term (Igalia
  authored the feature). Path B has validated the approach. An upstreaming item
  in `docs/HANDOFF-architecture-review.md`.
- **The 26.04 re-check list has never been walked**: are
  `/boot/firmware/config.txt` and `cmdline.txt` still the right paths (A/B boot
  may relocate them), the root fstab label (was `writable`) and its `discard`
  option, the GNOME 50 indexer names (`localsearch`/`tinysparql` — the script
  handles this dynamically), and whether swap exists by default.

## Re-applying the tuning

Idempotent, run as the user, **not** with sudo:

```bash
bash ~/rpi5-tune/rpi5-setup.sh
sudo reboot
sudo bash ~/rpi5-tune/rpi5-verify.sh     # clocks / gen3 / fan
sudo bash ~/rpi5-tune/rpi5-stress.sh     # 2.8GHz CPU stability
```

Then validate the GPU (v3d=1200) with a sustained glmark2-wayland load and
`dmesg | grep -i v3d`. Tunables: `ARM_FREQ`, `V3D_FREQ`, `OVER_VOLTAGE_DELTA`,
`BROWSER`.

**Known-good**: 2.8 GHz CPU (3.0 was UNSTABLE — SIGILL), v3d=1200, PCIe Gen3,
perf governor, swap off, fan 100% via fan-max.service (trip-lowering +
pwm=255), de-snapped, Flatpak Chromium, apport off + systemd-coredump, indexer
and Evolution masked, fstab `discard`→`defaults`.

## NUMA — done, and workstation-only

The custom **`7.0.6-numa`** kernel (`CONFIG_NUMA_EMU=y`, built by
`rpi5-numa-kernel.sh`) is installed: 8 nodes, `mempolicy interleave:0-7`,
`SDRAM_BANKLOW=1`. Confirm with `rpi5-verify.sh` (RESULT line) or `numactl
--hardware`; `rpi5-numa-README.md` → "Best configuration" has the detail.

**It stays out of bench systems by decision**: perf results represent what
customers ship, and customers do not ship `CONFIG_NUMA_EMU`, so a bench system
runs a stock kernel. Consequences: image numbers on this board will be lower
than tuned-workstation numbers on memory-bandwidth-bound work (correct, not a
regression), and historical numbers taken on the numa kernel are not the
going-forward baseline. `SDRAM_BANKLOW=1` stays in the EEPROM because it is
shared firmware state; a stock kernel simply does not act on it.

**NUMA is firmware-driven here and there is no `cmdline.txt`** — boot args come
from `/proc/device-tree/chosen/bootargs`, into which the firmware injects
`numa_policy=interleave` and `numa=fake=8`. `NUMA_FAKE=auto` lets the firmware
pick the node count; **do not hardcode 4** — that was 8 GB-era guidance, and 8
is correct for this 16 GB board.
