#!/usr/bin/env bash
# rpi5-verify.sh — confirm tuning took effect. Run with: sudo bash rpi5-verify.sh
echo "-- CPU max freq (expect ~2800000) --"
cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq
echo "-- governor (expect performance) --"
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor
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
