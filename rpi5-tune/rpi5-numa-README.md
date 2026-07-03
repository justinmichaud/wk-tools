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
```bash
sudo apt-get update
sudo apt-get build-dep -y linux-raspi              # or: linux-source + build tools
sudo apt-get install -y libncurses-dev flex bison libssl-dev bc kmod cpio
mkdir -p ~/kbuild && cd ~/kbuild
apt-get source linux-raspi                         # needs deb-src in sources
cd linux-raspi-*/
# enable the flag in the raspi flavour config:
scripts/config --file debian.raspi/config/annotations 2>/dev/null || true
# simplest: set it in the running-config-derived .config and rebuild
cp /boot/config-$(uname -r) .config
scripts/config --enable NUMA --enable NUMA_EMU --enable NUMA_MEMBLKS
make olddefconfig
grep CONFIG_NUMA_EMU .config                       # confirm =y
make -j$(nproc) bindeb-pkg                          # ~1-2h on the Pi
sudo dpkg -i ../linux-image-*.deb ../linux-headers-*.deb
# ensure /boot/firmware kernel + DTBs updated per Ubuntu raspi packaging, then reboot
```

## After a NUMA_EMU kernel is running
```bash
NUMA_FAKE=4 bash ~/rpi5-tune/rpi5-setup.sh    # adds numa=fake=4 to cmdline.txt (guarded)
sudo reboot
# verify:
dmesg | grep -i numa        # expect: interleave policy overridden to 'interleave:0-3'
numactl --hardware          # expect 4 nodes
# benchmark:
numactl --interleave=all <your workload>
```
Start with `numa=fake=4`; `8` sometimes scores higher — benchmark both.
