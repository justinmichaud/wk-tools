#!/usr/bin/env bash
#===============================================================================
# rpi5-setup.sh — Reproduce ALL performance tuning for a Raspberry Pi 5 (16GB)
#                 running Ubuntu 24.04 desktop (GNOME/Wayland) on NVMe.
#
# IDEMPOTENT: safe to run repeatedly and on a fresh install.
# RUN AS YOUR NORMAL USER (must have sudo). Do NOT prefix with sudo:
#     bash rpi5-setup.sh
# A reboot is required afterwards (overclock / PCIe / fan / fstab).
#
# Tunables (override via env, e.g.  BROWSER=vivaldi bash rpi5-setup.sh):
BROWSER="${BROWSER:-flatpak-chromium}"   # flatpak-chromium | vivaldi | brave | none
REMOVE_FIREFOX="${REMOVE_FIREFOX:-yes}"  # yes | no
ARM_FREQ="${ARM_FREQ:-2800}"             # 2800 safe / 3000 needs good silicon+cooling
V3D_FREQ="${V3D_FREQ:-1200}"             # GPU: 960 stock / 1100 proven / 1200 conservative try
OVER_VOLTAGE_DELTA="${OVER_VOLTAGE_DELTA:-0}"  # µV; set 25000/50000 if 3.0GHz unstable
NUMA_FAKE="${NUMA_FAKE:-0}"               # 0=off; 4 or 8 = emulated NUMA nodes (needs CONFIG_NUMA_EMU kernel — see rpi5-numa-README.md)
#===============================================================================
set -euo pipefail
log(){ printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok(){  printf '   \033[32m✓\033[0m %s\n' "$*"; }
skip(){ printf '   \033[33m-\033[0m %s\n' "$*"; }

[ "$(id -u)" -ne 0 ] || { echo "Run as your normal user (not root/sudo)."; exit 1; }
command -v sudo >/dev/null || { echo "sudo is required."; exit 1; }
CFG=/boot/firmware/config.txt; [ -f "$CFG" ] || CFG=/boot/config.txt

# ensure a `key=value` line exists inside the [pi5] tuning block (idempotent)
ensure_pi5_line(){ # $1 = exact line
  grep -qxF "$1" "$CFG" && return 0
  sudo sed -i "/^arm_freq=/i $1" "$CFG"; ok "added: $1"
}

#-------------------------------------------------------------------------------
log "1  Overclock / PCIe / GPU / fan in $CFG"
if [ -f "$CFG" ]; then
  if ! grep -q "rpi5-tune" "$CFG"; then
    sudo cp -a "$CFG" "$CFG.bak-$(date +%Y%m%d-%H%M%S)"
    sudo tee -a "$CFG" >/dev/null <<EOF

# --- Performance tuning (rpi5-tune) ---
[pi5]
# NVMe PCIe Gen 3 (default Gen 2). Requires a good PSU.
dtparam=pciex1_gen=3
# CPU overclock (stock 2.4GHz). Requires active cooling.
arm_freq=$ARM_FREQ
# GPU (V3D) overclock (stock 960).
v3d_freq=$V3D_FREQ
[all]
EOF
    ok "appended tuning block"
  else
    skip "tuning block present; topping up individual lines"
  fi
  # idempotent per-line top-up (handles upgrades / partial blocks)
  ensure_pi5_line "dtparam=pciex1_gen=3"
  # arm_freq / v3d_freq: ensure exactly one line, then set value (no duplicates)
  grep -q '^v3d_freq=' "$CFG" || sudo sed -i "/^arm_freq=/i v3d_freq=$V3D_FREQ" "$CFG"
  sudo sed -i "s/^v3d_freq=.*/v3d_freq=$V3D_FREQ/" "$CFG"; ok "v3d_freq=$V3D_FREQ"
  sudo sed -i "s/^arm_freq=.*/arm_freq=$ARM_FREQ/" "$CFG"; ok "arm_freq=$ARM_FREQ"
  if [ "$OVER_VOLTAGE_DELTA" != "0" ]; then ensure_pi5_line "over_voltage_delta=$OVER_VOLTAGE_DELTA"; fi
else skip "no config.txt (not Pi firmware layout)"; fi

#-------------------------------------------------------------------------------
log "2  CPU governor = performance"
sudo tee /etc/systemd/system/cpu-performance.service >/dev/null <<'EOF'
[Unit]
Description=Set CPU governor to performance
After=multi-user.target
[Service]
Type=oneshot
ExecStart=/bin/bash -c 'for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance > "$g"; done'
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now cpu-performance.service >/dev/null 2>&1 || true
ok "governor -> $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null)"

# Fan always 100% — config.txt fan_temp params are ignored by firmware, and a
# bare PWM write gets reclaimed by the thermal governor on trip crossings. The
# reliable method: lower all *active* fan trips so the governor pins max state,
# then set full PWM. Critical (110C) trip is left intact for CPU safety.
sudo tee /etc/systemd/system/fan-max.service >/dev/null <<'EOF'
[Unit]
Description=Force PWM fan to 100% (pin thermal state to max + full PWM)
After=multi-user.target
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/bash -c 'n=1; for t in /sys/class/thermal/thermal_zone*/trip_point_*_temp; do ty="${t%_temp}_type"; [ "$(cat "$ty" 2>/dev/null)" = active ] && { echo $((n*1000)) > "$t" 2>/dev/null; n=$((n+1)); }; done; for h in /sys/class/hwmon/hwmon*; do [ "$(cat "$h/name" 2>/dev/null)" = pwmfan ] && { echo 1 > "$h/pwm1_enable"; echo 255 > "$h/pwm1"; }; done'
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now fan-max.service >/dev/null 2>&1 || true
ok "fan-max.service (100%)"

#-------------------------------------------------------------------------------
log "3  Swap OFF (16GB box)"
sudo swapoff -a || true
# mask is harmless even if the unit is absent (creates a /dev/null symlink)
sudo systemctl disable --now swapfile.swap 2>/dev/null || true
sudo systemctl mask swapfile.swap 2>/dev/null || true
sudo rm -f /swapfile
sudo sed -i '/^[^#].*\sswap\s/s/^/#/' /etc/fstab || true
ok "swap devices active: $(swapon --show | grep -c /)"

#-------------------------------------------------------------------------------
log "4  Disable unneeded services (keeping bluetooth, unattended-upgrades, NM)"
# attempt disable; `systemctl disable` returns non-zero only if the unit doesn't
# exist (sysv units redirect & succeed) — pipefail-safe, no grep pipe.
disable_sys(){
  if sudo systemctl disable --now "$1" >/dev/null 2>&1; then ok "disabled $1"
  else skip "$1 absent"; fi; }
for u in NetworkManager-wait-online.service ModemManager.service switcheroo-control.service \
         kerneloops.service cups.service cups-browsed.service cups.socket \
         gnome-remote-desktop.service avahi-daemon.service avahi-daemon.socket \
         fwupd-refresh.timer apport.service power-profiles-daemon.service \
         rsyslog.service colord.service; do disable_sys "$u"; done
sudo touch /etc/cloud/cloud-init.disabled 2>/dev/null && ok "cloud-init disabled" || skip "no cloud-init"

#-------------------------------------------------------------------------------
log "5  apport OFF but keep core dumps (systemd-coredump)"
sudo sed -i 's/^enabled=1/enabled=0/' /etc/default/apport 2>/dev/null || true
if ! dpkg -l systemd-coredump 2>/dev/null | grep -q '^ii'; then
  sudo apt-get install -y systemd-coredump >/dev/null 2>&1 || true; fi
ok "core_pattern: $(cat /proc/sys/kernel/core_pattern)"

#-------------------------------------------------------------------------------
log "6  Disable GNOME bloat (tracker indexer + Evolution calendar/contacts)"
if systemctl --user show-environment >/dev/null 2>&1; then
  # Version-robust: GNOME <=45 = tracker-*, GNOME 46+/50 (Ubuntu 26.04) renamed
  # the indexer to localsearch-* / tinysparql-*. Discover & mask whatever exists,
  # plus Evolution data-server. Mask is harmless for absent units.
  for u in $(systemctl --user list-unit-files --no-legend 2>/dev/null | awk '{print $1}' \
             | grep -iE 'tracker|localsearch|tinysparql|evolution-(addressbook|calendar|source-registry|user-prompter)'); do
    systemctl --user stop "$u" 2>/dev/null || true
    systemctl --user mask "$u" 2>/dev/null || true
  done
  # turn off indexing via whichever gsettings schema is present (Tracker3 or localsearch)
  for sch in org.freedesktop.Tracker3.Miner.Files org.freedesktop.LocalSearch3.Miner.Files; do
    gsettings writable "$sch" enable-monitors >/dev/null 2>&1 && {
      gsettings set "$sch" enable-monitors false 2>/dev/null || true
      gsettings set "$sch" index-recursive-directories "[]" 2>/dev/null || true
      gsettings set "$sch" index-single-directories "[]" 2>/dev/null || true; }
  done
  (localsearch3 reset -s -r || tracker3 reset --filesystem || tracker reset --hard) >/dev/null 2>&1 || true
  ok "file indexer + Evolution masked"
else skip "no user session bus — run this from inside the GNOME session"; fi

#-------------------------------------------------------------------------------
log "7  Storage: drop continuous 'discard', use weekly fstrim.timer"
if grep -qE '\s/\s+ext4\s+[^#]*discard' /etc/fstab; then
  sudo cp -a /etc/fstab /etc/fstab.bak-$(date +%Y%m%d-%H%M%S)
  sudo sed -i -E '/\s\/\s+ext4\s/ s/\bdiscard\b/defaults/' /etc/fstab; ok "removed 'discard'"
else skip "no continuous 'discard'"; fi
sudo systemctl enable --now fstrim.timer >/dev/null 2>&1 || true; ok "fstrim.timer active"

#-------------------------------------------------------------------------------
log "8  De-snap everything (remove all snaps + snapd, block reinstall)"
if command -v snap >/dev/null; then
  for _ in 1 2 3 4 5 6; do
    left=$(snap list 2>/dev/null | awk 'NR>1 && $1!="snapd"{print $1}'); [ -z "$left" ] && break
    for s in $left; do sudo snap remove --purge "$s" >/dev/null 2>&1 || true; done
  done
  sudo snap remove --purge snapd >/dev/null 2>&1 || true
  sudo apt-get purge -y snapd >/dev/null 2>&1 || true
  sudo apt-get autoremove -y >/dev/null 2>&1 || true
  sudo rm -rf /var/cache/snapd /var/lib/snapd /root/snap ~/snap 2>/dev/null || true
  ok "snapd removed"
else skip "snap already absent"; fi
printf 'Package: snapd\nPin: release a=*\nPin-Priority: -10\n' | sudo tee /etc/apt/preferences.d/nosnap.pref >/dev/null
ok "snapd reinstall blocked (nosnap.pref)"

#-------------------------------------------------------------------------------
log "9  Browser: remove Firefox (optional) + install non-snap browser [$BROWSER]"
if [ "$REMOVE_FIREFOX" = yes ] && dpkg -l firefox 2>/dev/null | grep -q '^ii'; then
  sudo apt-get purge -y firefox >/dev/null 2>&1 || true
  sudo rm -f /etc/apt/sources.list.d/mozilla.list /etc/apt/preferences.d/mozilla
  ok "removed Firefox + Mozilla repo"
fi
case "$BROWSER" in
  flatpak-chromium)
    dpkg -l flatpak 2>/dev/null | grep -q '^ii' || sudo apt-get install -y flatpak >/dev/null 2>&1
    sudo flatpak remote-add --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo >/dev/null 2>&1 || true
    flatpak info org.chromium.Chromium >/dev/null 2>&1 || sudo flatpak install -y flathub org.chromium.Chromium >/dev/null 2>&1
    flatpak info org.chromium.Chromium >/dev/null 2>&1 && ok "Flatpak Chromium installed" || skip "Chromium flatpak install failed (check arm64 availability)" ;;
  vivaldi)
    if ! command -v vivaldi >/dev/null; then
      curl -fsSL https://repo.vivaldi.com/archive/linux_signing_key.pub | sudo gpg --dearmor -o /usr/share/keyrings/vivaldi.gpg
      echo "deb [signed-by=/usr/share/keyrings/vivaldi.gpg arch=arm64] https://repo.vivaldi.com/archive/deb/ stable main" | sudo tee /etc/apt/sources.list.d/vivaldi.list >/dev/null
      sudo apt-get update -qq && sudo apt-get install -y vivaldi-stable >/dev/null 2>&1
    fi
    command -v vivaldi >/dev/null && ok "Vivaldi installed" || skip "Vivaldi install failed" ;;
  brave)
    if ! command -v brave-browser >/dev/null; then
      sudo curl -fsSLo /usr/share/keyrings/brave-browser-archive-keyring.gpg https://brave-browser-apt-release.s3.brave.com/brave-browser-archive-keyring.gpg
      echo "deb [signed-by=/usr/share/keyrings/brave-browser-archive-keyring.gpg arch=arm64] https://brave-browser-apt-release.s3.brave.com/ stable main" | sudo tee /etc/apt/sources.list.d/brave-browser-release.list >/dev/null
      sudo apt-get update -qq && sudo apt-get install -y brave-browser >/dev/null 2>&1
    fi
    command -v brave-browser >/dev/null && ok "Brave installed" || skip "Brave install failed" ;;
  none) skip "no browser install requested" ;;
esac

#-------------------------------------------------------------------------------
log "10  NUMA emulation (numa=fake) — only if the kernel supports it"
CL=/boot/firmware/cmdline.txt; [ -f "$CL" ] || CL=/boot/cmdline.txt
KCONF=/boot/config-$(uname -r)
if [ "$NUMA_FAKE" != 0 ] && [ -f "$CL" ]; then
  if grep -q '^CONFIG_NUMA_EMU=y' "$KCONF" 2>/dev/null; then
    sudo cp -a "$CL" "$CL.bak-$(date +%Y%m%d-%H%M%S)"
    if grep -q 'numa=fake=' "$CL"; then
      sudo sed -i -E "s/numa=fake=[0-9]+/numa=fake=$NUMA_FAKE/" "$CL"
    else
      sudo sed -i "s/\brootwait\b/numa=fake=$NUMA_FAKE rootwait/" "$CL"
    fi
    ok "numa=fake=$NUMA_FAKE set in cmdline.txt (reboot, then: dmesg | grep -i numa)"
  else
    skip "kernel lacks CONFIG_NUMA_EMU — NOT enabling. See rpi5-numa-README.md to build a kernel with it."
  fi
else skip "NUMA_FAKE=0 (off). Ubuntu stock raspi kernels (incl. 26.04's 7.0) ship CONFIG_NUMA_EMU OFF — see rpi5-numa-README.md"; fi

log "11  Notes (informational)"
skip "EEPROM: keep current (benefits NVMe boot); update via: sudo rpi-eeprom-update -a"
skip "zswap in cmdline.txt is inert while swap is off (harmless)"

#-------------------------------------------------------------------------------
log "12  SSH key: install to ~/.ssh (if present alongside this script)"
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$HERE/id_ed25519" ]; then
  mkdir -p ~/.ssh && chmod 700 ~/.ssh
  cp -f "$HERE/id_ed25519" "$HERE/id_ed25519.pub" ~/.ssh/
  chmod 600 ~/.ssh/id_ed25519; chmod 644 ~/.ssh/id_ed25519.pub
  ok "installed id_ed25519 -> ~/.ssh ($(ssh-keygen -lf ~/.ssh/id_ed25519.pub 2>/dev/null | awk '{print $2}'))"
else skip "no id_ed25519 in $HERE — skipping"; fi

log "DONE — reboot to apply overclock/PCIe/fan/fstab:  sudo reboot"
echo "   Validate 2.8GHz stability after reboot:  sudo bash \"$(dirname "$0")/rpi5-stress.sh\""
