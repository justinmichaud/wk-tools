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
# CPU baseline: 2900 @ +50mV validated STABLE via 10-min stress-ng --verify torture
# (2026-07-04, worst 76.8C, throttled 0x0). 3.0GHz is UNSTABLE on this specific chip
# even at +50mV — and +50mV already pins VDD_CORE at the ~1.0V hardware cap (measured
# 1.000V under load), so more over_voltage_delta buys nothing. 2.9GHz is this unit's wall.
ARM_FREQ="${ARM_FREQ:-2900}"             # 2900 = validated stable ceiling for this chip
V3D_FREQ="${V3D_FREQ:-1200}"             # 960 stock; 1000 current; 1200 ran earlier — re-test w/ glmark2 before raising
OVER_VOLTAGE_DELTA="${OVER_VOLTAGE_DELTA:-50000}"  # µV; 50mV = at the ~1.0V core cap; higher adds no real voltage
# NUMA emulation is FIRMWARE-DRIVEN on Pi 5: with SDRAM_BANKLOW set, the bootloader banks the
# SDRAM and auto-appends the OPTIMAL numa=fake=N (RPi rule: log2(N)=high-bank-bits → 8 on this
# 16GB dual-rank board) whenever numa_policy is present. So the real levers are the EEPROM bank
# split + the interleave policy, NOT a hardcoded node count. Validated ON here: 8 nodes,
# interleave:0-7 (2026-07-04, kernel 7.0.6-numa). The oft-quoted "numa=fake=4 → +6%/+18%" was
# early 8GB testing; 8 is correct for 16GB. Requires a CONFIG_NUMA_EMU kernel — see rpi5-numa-README.md.
NUMA_FAKE="${NUMA_FAKE:-auto}"            # auto = let the bootloader pick optimal N; a number forces numa=fake=N; 0/off disables
NUMA_POLICY="${NUMA_POLICY:-interleave}"  # round-robin allocations across nodes — the actual memory-bandwidth win
SDRAM_BANKLOW="${SDRAM_BANKLOW:-1}"       # Pi5 EEPROM memory banking (Pi4=3). Enables NUMA auto-split + best mem perf. Empty = leave EEPROM as-is
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
#
# The logic lives in a standalone script rather than inline in ExecStart: systemd
# performs its OWN ${...} expansion on ExecStart lines before running them, which
# mangles bash param-expansions like ${t%_temp} (invalid systemd var name) and
# silently broke the trip-lowering loop, leaving the fan governor-controlled.
sudo tee /usr/local/sbin/rpi5-fan-max >/dev/null <<'EOF'
#!/bin/bash
# Pin thermal state to max (lower all active trips) + full PWM. Run by fan-max.service.
n=1
for t in /sys/class/thermal/thermal_zone*/trip_point_*_temp; do
  ty="${t%_temp}_type"
  [ "$(cat "$ty" 2>/dev/null)" = active ] && { echo $((n*1000)) > "$t" 2>/dev/null; n=$((n+1)); }
done
for h in /sys/class/hwmon/hwmon*; do
  [ "$(cat "$h/name" 2>/dev/null)" = pwmfan ] && { echo 1 > "$h/pwm1_enable"; echo 255 > "$h/pwm1"; }
done
exit 0
EOF
sudo chmod 755 /usr/local/sbin/rpi5-fan-max
sudo tee /etc/systemd/system/fan-max.service >/dev/null <<'EOF'
[Unit]
Description=Force PWM fan to 100% (pin thermal state to max + full PWM)
After=multi-user.target
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/rpi5-fan-max
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now fan-max.service >/dev/null 2>&1 || true
ok "fan-max.service (100%) via /usr/local/sbin/rpi5-fan-max"

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
log "4  Disable unneeded services + AUTOMATIC UPDATES (keeping WiFi/NetworkManager; Bluetooth OFF)"
# attempt disable; `systemctl disable` returns non-zero only if the unit doesn't
# exist (sysv units redirect & succeed) — pipefail-safe, no grep pipe.
#
# What we now kill here (background CPU/IO noise not covered before):
#  - ua-timer (Ubuntu Pro/ESM poll), sysstat-collect/rotate/summary (sadc every
#    10 min — NOT used by any script in this repo), dpkg-db-backup, anacron
#    (fires cron.daily/weekly maintenance mid-benchmark), update-notifier-motd,
#    apport-autoreport: all periodic wake-ups that can spike load during a run.
#  - systemd-oomd: the *proactive*, pressure-based OOM killer. On this 16GB
#    swap-off box it can kill a heavy-but-healthy benchmark under transient memory
#    pressure. Disabling it leaves the in-kernel OOM killer as the real safety net,
#    so a genuine runaway is still reaped — we just stop the daemon second-guessing
#    a legitimately memory-hungry workload. Verified safe for browser perf work:
#    oomd only exposes org.freedesktop.oom1 (a KILL API), it does NOT broadcast
#    memory-pressure warnings to apps. WebKit/Chrome read pressure themselves from
#    PSI (/proc/pressure/memory) / cgroup memory.pressure, which stay available.
#  - bluetooth: not needed on this box (WiFi via NetworkManager is kept).
#  - anacron runs cron.daily/weekly (apport, apt-compat, dpkg backup, man-db
#    reindex, logrotate) — none load-bearing here (logrotate also has its own
#    systemd timer). Crucially, SSD TRIM is NOT a cron job: it runs via
#    fstrim.timer (step 7), so trim is UNAFFECTED by disabling anacron.
disable_sys(){
  if sudo systemctl disable --now "$1" >/dev/null 2>&1; then ok "disabled $1"
  else skip "$1 absent"; fi; }
for u in bluetooth.service \
         NetworkManager-wait-online.service ModemManager.service switcheroo-control.service \
         kerneloops.service cups.service cups-browsed.service cups.socket \
         gnome-remote-desktop.service avahi-daemon.service avahi-daemon.socket \
         fwupd-refresh.timer apport.service \
         rsyslog.service \
         unattended-upgrades.service apt-daily.timer apt-daily-upgrade.timer \
         man-db.timer motd-news.timer update-notifier-download.timer \
         update-notifier-motd.timer apport-autoreport.timer \
         ua-timer.timer dpkg-db-backup.timer anacron.timer anacron.service \
         sysstat.service sysstat-collect.timer sysstat-rotate.timer sysstat-summary.timer \
         systemd-oomd.service; do disable_sys "$u"; done
# Units a plain `disable` can't stop because they're static or get dbus/bus-
# reactivated on demand — mask them so nothing can pull them in and spike CPU/IO
# mid-benchmark. Manual updates still work any time:
#   sudo apt update && sudo apt full-upgrade
#
# power-profiles-daemon: `disable` leaves it enabled=disabled but it comes back
#   active because GNOME's SettingsDaemon.Power dbus-activates it. It's redundant
#   here (CPU governor is already pinned to `performance` above) and can fight the
#   governor, so it must be MASKED, not just disabled.
# colord: `static` (can't be disabled) colour-management daemon, dbus-activated by
#   GNOME — useless on a headless/perf box.
for m in apt-daily.service apt-daily-upgrade.service packagekit.service \
         packagekit-offline-update.service motd-news.service \
         power-profiles-daemon.service colord.service; do
  sudo systemctl mask "$m" >/dev/null 2>&1 && ok "masked $m" || skip "$m n/a"
done
sudo touch /etc/cloud/cloud-init.disabled 2>/dev/null && ok "cloud-init disabled" || skip "no cloud-init"

# Never suspend/sleep/hibernate — a perf box must stay awake for long runs and be
# reachable over SSH. Masking the sleep targets hard-blocks every path into them
# (GNOME idle, logind lid/idle, `systemctl suspend`), which is stronger and more
# robust than only flipping the GNOME gsettings timeout.
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null 2>&1 \
  && ok "suspend/sleep/hibernate masked (box stays awake)" || skip "sleep targets n/a"
# Belt-and-suspenders: also tell GNOME (if a user session exists) not to auto-sleep.
if systemctl --user show-environment >/dev/null 2>&1; then
  gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing' 2>/dev/null || true
  gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-battery-type 'nothing' 2>/dev/null || true
fi

#-------------------------------------------------------------------------------
log "5  apport OFF but keep core dumps (systemd-coredump)"
sudo sed -i 's/^enabled=1/enabled=0/' /etc/default/apport 2>/dev/null || true
if ! dpkg -l systemd-coredump 2>/dev/null | grep -q '^ii'; then
  sudo apt-get install -y systemd-coredump >/dev/null 2>&1 || true; fi
ok "core_pattern: $(cat /proc/sys/kernel/core_pattern)"

#-------------------------------------------------------------------------------
log "6  Disable GNOME bloat (tracker indexer + Evolution + gvfs/SettingsDaemon helpers)"
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

  # gvfs volume monitors + SettingsDaemon helpers for hardware this box won't use.
  # These are dbus-activated user services that idle-poll for hotplug (phones,
  # cameras, MTP, online accounts) or manage absent hardware (smartcard, cellular,
  # printers, Wacom) — background wake-ups + RAM with zero benefit here. Masking
  # them does NOT affect browser/WebKit perf testing or WiFi. NVMe/USB storage still
  # mounts (udisks2 kept); this only drops the phone/camera/MTP *monitors*.
  for u in gvfs-afc-volume-monitor.service gvfs-gphoto2-volume-monitor.service \
           gvfs-mtp-volume-monitor.service gvfs-goa-volume-monitor.service \
           org.gnome.SettingsDaemon.Smartcard.service org.gnome.SettingsDaemon.Wwan.service \
           org.gnome.SettingsDaemon.PrintNotifications.service org.gnome.SettingsDaemon.Wacom.service; do
    systemctl --user stop "$u" 2>/dev/null || true
    systemctl --user mask "$u" 2>/dev/null || true
  done
  ok "gvfs volume monitors + unused SettingsDaemon helpers masked"
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
log "10  NUMA emulation (Pi 5: EEPROM SDRAM banking + interleave; firmware picks optimal N)"
CL=/boot/firmware/cmdline.txt; [ -f "$CL" ] || CL=/boot/cmdline.txt
KCONF=/boot/config-$(uname -r)
# Idempotently ensure a single-token kernel arg is on the (single-line) cmdline.txt.
ensure_cmdline_token(){ # $1 = token e.g. numa_policy=interleave  (key = part before '=')
  local tok="$1" key="${1%%=*}"
  grep -qw -- "$tok" "$CL" && return 0
  if grep -qE "(^|[[:space:]])$key=" "$CL"; then
    sudo sed -i -E "s#(^|[[:space:]])$key=[^[:space:]]+#\1$tok#" "$CL"   # replace existing value
  else
    sudo sed -i "s/\brootwait\b/$tok rootwait/" "$CL"                     # else insert before rootwait
  fi
  ok "cmdline: $tok"
}
numa_nodes(){ numactl --hardware 2>/dev/null | grep -c '^node[[:space:]][0-9]* cpus' || true; }

if [ "$NUMA_FAKE" = 0 ] || [ "$NUMA_FAKE" = off ]; then
  skip "NUMA disabled (NUMA_FAKE=$NUMA_FAKE)"
elif ! grep -q '^CONFIG_NUMA_EMU=y' "$KCONF" 2>/dev/null; then
  skip "kernel $(uname -r) lacks CONFIG_NUMA_EMU — NOT enabling. Build a -numa kernel first (rpi5-numa-kernel.sh / rpi5-numa-README.md)."
else
  ok "kernel supports NUMA emulation (CONFIG_NUMA_EMU=y, $(uname -r))"

  # (a) EEPROM SDRAM banking — the ROOT lever. On Pi 5 the bootloader only splits the SDRAM into
  #     banks AND auto-appends the optimal numa=fake=N when SDRAM_BANKLOW is set (1 = best perf on
  #     BCM2712; regresses only NON-numa kernels' SW H264 decode, which doesn't apply here). Pin it
  #     explicitly so a future bootloader default change can't silently undo NUMA. Idempotent:
  #     once written, the value matches and we skip. --apply schedules the reflash for next reboot.
  if [ -n "$SDRAM_BANKLOW" ] && command -v rpi-eeprom-config >/dev/null; then
    cur_bl="$(sudo rpi-eeprom-config 2>/dev/null | sed -n 's/^SDRAM_BANKLOW=//p' || true)"
    if [ "$cur_bl" = "$SDRAM_BANKLOW" ]; then
      ok "EEPROM SDRAM_BANKLOW=$SDRAM_BANKLOW already pinned"
    else
      conf="$(mktemp)"; sudo rpi-eeprom-config > "$conf" 2>/dev/null || true
      if grep -q '^SDRAM_BANKLOW=' "$conf"; then
        sed -i "s/^SDRAM_BANKLOW=.*/SDRAM_BANKLOW=$SDRAM_BANKLOW/" "$conf"
      else
        printf 'SDRAM_BANKLOW=%s\n' "$SDRAM_BANKLOW" >> "$conf"
      fi
      if sudo rpi-eeprom-config --apply "$conf" >/dev/null 2>&1; then
        ok "EEPROM SDRAM_BANKLOW=$SDRAM_BANKLOW scheduled (was '${cur_bl:-bootloader-default}'; applies on reboot)"
      else
        skip "could not write EEPROM (bootloader default is banklow=1 on 2712 — NUMA still works)"
      fi
      rm -f "$conf"
    fi
  else
    skip "SDRAM_BANKLOW empty or rpi-eeprom-config absent — relying on bootloader default (banklow=1 on 2712)"
  fi

  # (b) Boot args. If a cmdline.txt exists, pin numa_policy (this is what triggers the bootloader's
  #     optimal-N auto-add) and, only when a specific count was requested, an explicit numa=fake=N.
  #     On THIS box there is no cmdline.txt — the Pi firmware assembles /chosen/bootargs itself and
  #     already injects numa_policy=interleave + numa=fake=8, so there is nothing to edit; we verify (c).
  if [ -f "$CL" ]; then
    sudo cp -a "$CL" "$CL.bak-$(date +%Y%m%d-%H%M%S)"
    ensure_cmdline_token "numa_policy=$NUMA_POLICY"
    if [ "$NUMA_FAKE" != auto ]; then ensure_cmdline_token "numa=fake=$NUMA_FAKE"; fi
  else
    skip "no cmdline.txt — boot args come from Pi firmware (/proc/device-tree/chosen/bootargs)"
  fi

  # (c) Report the EFFECTIVE state on the running kernel (levers above are firmware/EEPROM, take
  #     effect on reboot). This is the authoritative on/off; rpi5-verify.sh checks the same.
  n="$(numa_nodes)"
  if grep -q 'numa_policy=' /proc/cmdline || grep -q 'numa=fake' /proc/cmdline; then
    ok "active now: ${n:-?} NUMA node(s) — boot args: $(grep -oE 'numa[_a-z]*=[^ ]+' /proc/cmdline | tr '\n' ' ')"
  else
    skip "NUMA not yet in effect on the running kernel — reboot, then: sudo bash rpi5-verify.sh"
  fi
fi

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
