#!/usr/bin/env bash
# rpi5-verify.sh — confirm tuning took effect. Run with: sudo bash rpi5-verify.sh
echo "-- CPU max freq (expect ~2900000) --"
cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq
echo "-- governor (expect performance) --"
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor
echo "-- NUMA emulation (expect ON: >1 node + interleave policy; 8 nodes on this 16GB Pi 5) --"
if grep -q '^CONFIG_NUMA_EMU=y' "/boot/config-$(uname -r)" 2>/dev/null; then
  echo "   kernel : CONFIG_NUMA_EMU=y ($(uname -r))"
else
  echo "   kernel : CONFIG_NUMA_EMU NOT set ($(uname -r)) -> emulation impossible; build a -numa kernel (rpi5-numa-README.md)"
fi
echo "   EEPROM : SDRAM_BANKLOW=$(rpi-eeprom-config 2>/dev/null | sed -n 's/^SDRAM_BANKLOW=//p' | grep . || echo 'bootloader-default(1 on 2712)')"
echo "   args   : $(grep -oE 'numa[_a-z]*=[^ ]+' /proc/cmdline | tr '\n' ' ')"
nodes=$(numactl --hardware 2>/dev/null | grep -c '^node[[:space:]][0-9]* cpus')
dmesg 2>/dev/null | grep -iE "policy overridden to 'interleave" | sed 's/^/   dmesg  : /'
if [ "${nodes:-0}" -gt 1 ] && grep -q 'numa_policy=interleave' /proc/cmdline; then
  echo "   RESULT : NUMA ON — $nodes nodes, interleave policy active ✅"
else
  echo "   RESULT : NUMA OFF/degraded — ${nodes:-0} node(s) (expected 8). See rpi5-numa-README.md ❌"
fi
echo "-- GPU V3D clock (expect ~1200000000) --"
vcgencmd measure_clock v3d
echo "-- NVMe link speed (expect 8.0 GT/s = Gen3) --"
for f in /sys/class/pci_bus/*/device/*/current_link_speed; do
  printf '%s : %s\n' "$f" "$(cat "$f")"
done
echo "-- temp under idle --"
vcgencmd measure_temp
echo "-- throttle flags (want 0x0; nonzero = power/thermal issue) --"
vcgencmd get_throttled
echo "-- swap (expect empty) --"
swapon --show || echo "swap off"
echo "-- NVMe sequential read (needs: sudo apt install hdparm) --"
command -v hdparm >/dev/null && hdparm -t --direct /dev/nvme0n1 || echo "hdparm not installed"
