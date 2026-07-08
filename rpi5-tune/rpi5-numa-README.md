# Enabling NUMA emulation on Raspberry Pi 5 (Ubuntu 26.04 "Resolute Raccoon")

## Why
Splitting the Pi 5's single memory controller into fake NUMA nodes (`numa=fake=N`)
lets the kernel interleave allocations across memory banks → measured ~6% single /
~18% multi-core Geekbench, and much more on memory-bound workloads. Feature is
**Igalia's** upstream arm64 work, in mainline since 6.12.

## The one blocker on stock Ubuntu 26.04
Kernel 7.0 has all the plumbing ENABLED except the emulation switch:
```
CONFIG_NUMA=y  CONFIG_NUMA_MEMBLKS=y  CONFIG_GENERIC_ARCH_NUMA=y  CONFIG_OF_NUMA=y
# CONFIG_NUMA_EMU is not set        <-- this is what numa=fake needs
```
So `numa=fake=4` is ignored until a kernel with `CONFIG_NUMA_EMU=y` is installed.
Check on any system:  `grep CONFIG_NUMA_EMU /boot/config-$(uname -r)`

## Path A (recommended, low-risk): ask Ubuntu to flip the flag
It is config-only (code already upstream in 7.0). File a linux-raspi kernel
request on Launchpad to set `CONFIG_NUMA_EMU=y`. Igalia authored the feature, so
we have the standing to push it. If accepted, enabling NUMA becomes just:
`NUMA_FAKE=4 bash rpi5-setup.sh` + reboot — no custom kernel to maintain.

## Path B (do-it-now): rebuild the raspi kernel with the flag on
No patching needed on 26.04 — just toggle one Kconfig. 26.04's **A/B boot** auto-
reverts a bad kernel, so this is far safer than it was on 24.04.

**Turnkey:** run the bundled script (idempotent, resumable, verifies the flag
*before* the compile so you never waste hours on a bad build):
```bash
bash ~/rpi5-tune/rpi5-numa-kernel.sh      # prompts for sudo; builds a -numa kernel .debs
```
It: enables `deb-src` (26.04 ships it off), installs the toolchain, fetches the
real `linux-raspi` kernel source (resolving past the `linux-meta-raspi` decoy),
runs `debian.raspi/reconstruct` + fixes the arm64 dts-overlays symlink (else
`make dtbs` fails), derives `.config` from the running kernel, hard-checks
`CONFIG_NUMA_EMU=y`, builds `bindeb-pkg`, and (optionally) `dpkg -i` the result.
The stock kernel stays as the A/B fallback.

Two env knobs, both **on by default**:
- `LEAN=yes` — `make localmodconfig`: build only currently-loaded modules
  (~130 vs thousands) → **~10-20 min instead of 1-2h**. Boots on *this* Pi;
  drivers for hardware not plugged in at build time are omitted. `LEAN=no` for a
  full driver-parity kernel (slow).
- `MAXPERF=yes` — throughput tuning: `HZ=100`, default cpufreq governor =
  performance, `-O2`. Preemption is left at **`PREEMPT_LAZY`** (the modern
  throughput model): on arm64 `ARCH_HAS_PREEMPT_LAZY=y` makes `PREEMPT_NONE`
  (needs `ARCH_NO_PREEMPT`) and `PREEMPT_VOLUNTARY` unbuildable, and forcing the
  choice only yields full `PREEMPT` (worse). Also keeps **BTF + `SCHED_CLASS_EXT`
  (sched_ext)** so Meta's SCX schedulers (`scx_lavd`, `scx_bpfland`,
  `scx_rusty`/`scx_layered`) and `bpftrace` work — do **not** set `DEBUG_INFO_NONE`,
  which strips BTF and breaks all of that.

<details><summary>What it does, by hand</summary>

```bash
sudo apt-get update
sudo apt-get build-dep -y linux-raspi
sudo apt-get install -y build-essential fakeroot dpkg-dev libncurses-dev flex bison libssl-dev bc kmod cpio libelf-dev dwarves zstd
mkdir -p ~/kbuild && cd ~/kbuild
apt-get source linux-raspi                         # needs deb-src in sources
cd linux-raspi-*/
cp /boot/config-$(uname -r) .config
# LOCALVERSION MUST carry a NUMERIC ABI: "-1-numa", never "-numa". Ubuntu boots
# via flash-kernel's pi-try; its latest-kernel filter (include_only_flavors)
# only recognises <version>-<ABInum>-<flavour>, so a bare "-numa" name is
# silently dropped, flash-kernel calls the stock kernel "latest", prints
# "Ignoring old or unknown version ...", and NEVER stages ours into
# /boot/firmware/new/ — every reboot lands back on stock.
scripts/config --enable NUMA --enable NUMA_EMU --enable NUMA_MEMBLKS --set-str LOCALVERSION "-1-numa"
make olddefconfig
grep CONFIG_NUMA_EMU .config                       # MUST show =y before building
make -j$(nproc) bindeb-pkg                          # ~1-2h on the Pi
sudo dpkg -i ../linux-image-*-numa_*.deb ../linux-headers-*-numa_*.deb
# Pi 5 firmware rejects locally-built kernels ("...OS does not support...") because
# they lack the trailer Ubuntu's official images carry. Disable os_check or the
# tryboot below is marked 'bad' and falls back to stock:
grep -qE '^os_check=0' /boot/firmware/config.txt || sudo sed -i '1i os_check=0' /boot/firmware/config.txt
KVER=$(ls /boot/vmlinuz-*-numa | sed 's|.*/vmlinuz-||' | tail -1)
sudo flash-kernel "$KVER"                          # STAGES into /boot/firmware/new/
sudo flash-kernel "$KVER" 2>&1 | grep -q 'Ignoring old or unknown' && echo "FAIL: bad kernel name — see LOCALVERSION note above"
# next reboot tryboots new/; if it boots+validates it's promoted to current/, else auto-reverts to stock
sudo reboot
```
</details>

## Best configuration (researched 2026-07-04) — let the firmware pick, don't hardcode 4
NUMA on the Pi 5 is **firmware-driven**, and the running box is already at the optimum:
```
8 NUMA nodes, mempolicy 'interleave:0-7', SDRAM_BANKLOW=1 (bootloader default on 2712)
```
- **Node count:** the RPi bootloader auto-selects the optimal `numa=fake=N` — the rule is
  `log2(N) = number of high SDRAM bank bits`. On this **16GB dual-rank** BCM2712 with
  `SDRAM_BANKLOW=1` that is **8**, and it chose 8. The widely-quoted "`numa=fake=4` → +6%/+18%
  Geekbench" figure was **early 8GB testing** — 4 would *under-split* a 16GB board and leave
  memory bandwidth unused. Do **not** hardcode a count; `NUMA_FAKE=auto` lets the firmware decide.
- **The two real levers** are (1) `SDRAM_BANKLOW=1` in the EEPROM (banks the DRAM + lets the
  bootloader auto-add `numa=fake=N`; regresses only *non-NUMA* kernels' SW-H264 decode, N/A here),
  and (2) `numa_policy=interleave` on the cmdline (round-robin allocs across banks — the actual
  bandwidth win). `rpi5-setup.sh` pins both; on a box with no `cmdline.txt` the firmware already
  injects `numa_policy=interleave` + `numa=fake=8` into `/proc/device-tree/chosen/bootargs`.

## After a NUMA_EMU kernel is running
```bash
bash ~/rpi5-tune/rpi5-setup.sh            # NUMA_FAKE=auto default: pins SDRAM_BANKLOW=1, verifies interleave
sudo reboot                               # only needed if it scheduled an EEPROM/cmdline change
sudo bash ~/rpi5-tune/rpi5-verify.sh      # RESULT: NUMA ON — 8 nodes, interleave policy active ✅
# manual spot-checks:
numactl --hardware                        # expect 8 nodes
dmesg | grep -i numa                      # expect: default policy overridden to 'interleave:0-7'
# benchmark a workload pinned to interleaved memory:
numactl --interleave=all <your workload>
```
To force a specific split for A/B benchmarking: `NUMA_FAKE=4 bash rpi5-setup.sh` (needs a
`cmdline.txt`; on firmware-bootargs boxes, set it in the EEPROM cmdline instead). `auto`/8 is the
validated optimum here — only override to measure.
