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
scripts/config --enable NUMA --enable NUMA_EMU --enable NUMA_MEMBLKS --set-str LOCALVERSION "-numa"
make olddefconfig
grep CONFIG_NUMA_EMU .config                       # MUST show =y before building
make -j$(nproc) bindeb-pkg                          # ~1-2h on the Pi
sudo dpkg -i ../linux-image-*-numa_*.deb ../linux-headers-*-numa_*.deb
# confirm /boot/firmware kernel + DTBs updated (sudo flash-kernel if not), then reboot
```
</details>

## After a NUMA_EMU kernel is running
```bash
NUMA_FAKE=4 bash ~/rpi5-tune/rpi5-setup.sh    # adds numa=fake=4 (+ preempt=none) to cmdline.txt (guarded)
sudo reboot
# verify:
dmesg | grep -i numa                     # expect: interleave policy overridden to 'interleave:0-3'
numactl --hardware                        # expect 4 nodes
cat /sys/kernel/debug/sched/preempt       # expect (none) selected — max-perf preemption
# benchmark:
numactl --interleave=all <your workload>
```
Start with `numa=fake=4`; `8` sometimes scores higher — benchmark both.
