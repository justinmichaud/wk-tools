#!/usr/bin/env bash
BROWSER="${BROWSER:-flatpak-chromium}"   # flatpak-chromium | vivaldi | brave | none
REMOVE_FIREFOX="${REMOVE_FIREFOX:-yes}"  # yes | no
# 2900 passes stress-ng torture but hard-locks in real use; +50mV pins VDD_CORE at its cap.
ARM_FREQ="${ARM_FREQ:-2800}"             # 2800 = stable; 2900 hard-locks in real use
V3D_FREQ="${V3D_FREQ:-1200}"             # 960 stock; 1000 current; 1200 ran earlier — re-test w/ glmark2 before raising
OVER_VOLTAGE_DELTA="${OVER_VOLTAGE_DELTA:-50000}"  # µV; 50mV = at the ~1.0V core cap; higher adds no real voltage
# NUMA is firmware-driven: with SDRAM_BANKLOW set the bootloader banks the SDRAM
# and appends the optimal numa=fake=N when numa_policy is on the cmdline.
NUMA_FAKE="${NUMA_FAKE:-auto}"            # auto = let the bootloader pick optimal N; a number forces numa=fake=N; 0/off disables
NUMA_POLICY="${NUMA_POLICY:-interleave}"  # round-robin allocations across nodes — the actual memory-bandwidth win
SDRAM_BANKLOW="${SDRAM_BANKLOW:-1}"       # Pi5 EEPROM memory banking (Pi4=3). Enables NUMA auto-split + best mem perf. Empty = leave EEPROM as-is
set -euo pipefail
log(){ printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok(){  printf '   \033[32m✓\033[0m %s\n' "$*"; }
skip(){ printf '   \033[33m-\033[0m %s\n' "$*"; }

[ "$(id -u)" -ne 0 ] || { echo "Run as your normal user (not root/sudo)."; exit 1; }
command -v sudo >/dev/null || { echo "sudo is required."; exit 1; }
CFG=/boot/firmware/config.txt; [ -f "$CFG" ] || CFG=/boot/config.txt

ensure_pi5_line(){ # $1 = exact line
  grep -qxF "$1" "$CFG" && return 0
  sudo sed -i "/^arm_freq=/i $1" "$CFG"; ok "added: $1"
}

log "1  Overclock / PCIe / GPU / fan in $CFG"
if [ -f "$CFG" ]; then
  if ! grep -q "rpi5-tune" "$CFG"; then
    sudo cp -a "$CFG" "$CFG.bak-$(date +%Y%m%d-%H%M%S)"
    sudo tee -a "$CFG" >/dev/null <<EOF

# --- Performance tuning (rpi5-tune) ---
[pi5]
dtparam=pciex1_gen=3
arm_freq=$ARM_FREQ
v3d_freq=$V3D_FREQ
[all]
EOF
    ok "appended tuning block"
  else
    skip "tuning block present; topping up individual lines"
  fi
  ensure_pi5_line "dtparam=pciex1_gen=3"
  grep -q '^v3d_freq=' "$CFG" || sudo sed -i "/^arm_freq=/i v3d_freq=$V3D_FREQ" "$CFG"
  sudo sed -i "s/^v3d_freq=.*/v3d_freq=$V3D_FREQ/" "$CFG"; ok "v3d_freq=$V3D_FREQ"
  sudo sed -i "s/^arm_freq=.*/arm_freq=$ARM_FREQ/" "$CFG"; ok "arm_freq=$ARM_FREQ"
  if [ "$OVER_VOLTAGE_DELTA" != "0" ]; then ensure_pi5_line "over_voltage_delta=$OVER_VOLTAGE_DELTA"; fi
else skip "no config.txt (not Pi firmware layout)"; fi

log "2  CPU governor = performance"
sudo tee /etc/systemd/system/cpu-performance.service >/dev/null <<'EOF'
[Unit]
Description=Set CPU governor to performance
# A headless boot stalls multi-user.target indefinitely (step 4c).
After=basic.target
[Service]
Type=oneshot
ExecStart=/bin/bash -c 'for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance > "$g"; done'
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now cpu-performance.service >/dev/null 2>&1 || true
ok "governor -> $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null)"

# Firmware ignores config.txt fan_temp, and the thermal governor reclaims a bare
# PWM write; a standalone script because systemd would expand ${t%_temp} itself.
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
After=basic.target
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

log "3  Swap OFF (16GB box)"
sudo swapoff -a || true
sudo systemctl disable --now swapfile.swap 2>/dev/null || true
sudo systemctl mask swapfile.swap 2>/dev/null || true
sudo rm -f /swapfile
sudo sed -i '/^[^#].*\sswap\s/s/^/#/' /etc/fstab || true
ok "swap devices active: $(swapon --show | grep -c /)"

log "4  Disable unneeded services + AUTOMATIC UPDATES (keeping WiFi/NetworkManager; Bluetooth OFF)"
# systemd-oomd kills a healthy benchmark under transient pressure on a swap-off box.
disable_sys(){
  if sudo systemctl disable --now "$1" >/dev/null 2>&1; then ok "disabled $1"
  else skip "$1 absent"; fi; }
for u in bluetooth.service \
         ModemManager.service switcheroo-control.service \
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
# power-profiles-daemon returns via GNOME's SettingsDaemon.Power and fights the governor.
for m in apt-daily.service apt-daily-upgrade.service packagekit.service \
         packagekit-offline-update.service motd-news.service \
         power-profiles-daemon.service colord.service; do
  sudo systemctl mask "$m" >/dev/null 2>&1 && ok "masked $m" || skip "$m n/a"
done
sudo touch /etc/cloud/cloud-init.disabled 2>/dev/null && ok "cloud-init disabled" || skip "no cloud-init"

sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null 2>&1 \
  && ok "suspend/sleep/hibernate masked (box stays awake)" || skip "sleep targets n/a"
if systemctl --user show-environment >/dev/null 2>&1; then
  gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing' 2>/dev/null || true
  gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-battery-type 'nothing' 2>/dev/null || true
fi

if command -v timedatectl >/dev/null; then
  sudo timedatectl set-ntp true 2>/dev/null \
    && ok "automatic date/time ON (NTP=$(timedatectl show -p NTP --value 2>/dev/null))" \
    || skip "could not enable NTP via timedatectl"
else skip "timedatectl absent — leaving clock config untouched"; fi

# No battery-backed RTC: the clock resets to 1970, chrony's NTS then fails
# certificate validation, and nothing over HTTPS works. fake-hwclock breaks that.
if ! dpkg -l fake-hwclock 2>/dev/null | grep -q '^ii'; then
  sudo apt-get install -y fake-hwclock >/dev/null 2>&1 || true
fi
if dpkg -l fake-hwclock 2>/dev/null | grep -q '^ii'; then
  # The bare "fake-hwclock" alias is a masked SysV shim; enable -load/-save instead.
  sudo systemctl enable fake-hwclock-load.service fake-hwclock-save.service \
                        fake-hwclock-save.timer >/dev/null 2>&1 || true
  sudo fake-hwclock save 2>/dev/null || true
  ok "fake-hwclock installed + enabled (clock survives reboot → NTS/TLS time sync auto-recovers)"
else
  skip "fake-hwclock unavailable (apt offline?) — install later so the clock survives reboots"
fi

log "4b  WiFi stability (disable power-save + roaming, pin BSSID, wait for network at boot)"
# brcmfmac powersave stops the radio re-associating after idle (NM defaults 3; 2 is off).
if [ -d /etc/NetworkManager ]; then
  sudo install -d /etc/NetworkManager/conf.d
  printf '[connection]\nwifi.powersave = 2\n' \
    | sudo tee /etc/NetworkManager/conf.d/wifi-powersave-off.conf >/dev/null
  ok "wifi.powersave=2 (off) drop-in written"
  sudo systemctl reload NetworkManager >/dev/null 2>&1 || true
else
  skip "no /etc/NetworkManager — WiFi powersave drop-in not written"
fi
# The CYW43455 roam engine scans off-channel and wedges the radio: associated but
# passing nothing, then ASSOC-REJECT status_code=16 for 10-15 min. This AP is DFS.
printf '%s\n' 'options brcmfmac roamoff=1' \
  | sudo tee /etc/modprobe.d/brcmfmac-roamoff.conf >/dev/null
ok "brcmfmac roamoff=1 written (takes effect on driver reload / reboot)"

# Pin the BSSID, not the channel: a DFS radar-vacate changes the channel, not the BSSID.
_conf_dir=$(cd "$(dirname "$0")" && pwd)
# shellcheck disable=SC1091
[ -f "$_conf_dir/rpi5.conf" ] && . "$_conf_dir/rpi5.conf"
WIFI_SSID="${WIFI_SSID:-$(nmcli -t -f NAME,TYPE connection show --active 2>/dev/null \
                          | awk -F: '$2 ~ /wireless/ {print $1; exit}')}"
WIFI_PIN_BSSID="${WIFI_PIN_BSSID-auto}"

if [ -z "$WIFI_SSID" ]; then
  skip "no active WiFi connection and no WIFI_SSID in rpi5.conf — BSSID not pinned"
elif ! nmcli -g name connection show 2>/dev/null | grep -qx "$WIFI_SSID"; then
  skip "no '$WIFI_SSID' connection — BSSID not pinned"
else
  _scan=$(nmcli -t -f SSID,SIGNAL,BSSID dev wifi list --rescan yes 2>/dev/null | sed 's/\\:/-/g')
  _pin=""
  case "$WIFI_PIN_BSSID" in
    '')   ;;   # explicitly disabled in rpi5.conf
    auto) _pin=$(printf '%s\n' "$_scan" \
                 | awk -F: -v ssid="$WIFI_SSID" '$1==ssid {print $3, $2}' \
                 | sort -k2,2nr | awk 'NR==1 {print $1}' | tr -- '-' ':') ;;
    *)    if printf '%s\n' "$_scan" | tr -- '-' ':' | grep -qiF "$WIFI_PIN_BSSID"; then
            _pin="$WIFI_PIN_BSSID"
          else
            skip "configured BSSID $WIFI_PIN_BSSID is not on the air — clearing the pin instead"
          fi ;;
  esac
  if [ -n "$_pin" ]; then
    sudo nmcli connection modify "$WIFI_SSID" 802-11-wireless.bssid "$_pin" \
      && ok "connection '$WIFI_SSID' pinned to BSSID $_pin" \
      || skip "could not pin BSSID on '$WIFI_SSID'"
  else
    sudo nmcli connection modify "$WIFI_SSID" 802-11-wireless.bssid "" 2>/dev/null \
      && ok "no BSS to pin — '$WIFI_SSID' left unpinned (roaming allowed)" \
      || skip "could not clear the BSSID pin on '$WIFI_SSID'"
  fi
fi

sudo systemctl enable --now NetworkManager-wait-online.service >/dev/null 2>&1 \
  && ok "NetworkManager-wait-online enabled (boot waits for network)" \
  || skip "NetworkManager-wait-online n/a"

sudo tee /usr/local/sbin/rpi5-wifi-watchdog >/dev/null <<'WDEOF'
#!/bin/bash
# NetworkManager's own reconnect first (~100s), then escalate. No 'nmcli device
# disconnect' — it blocks autoconnect and bounces the link every ~2min.
IFACE="${IFACE:-wlan0}"; INTERVAL="${INTERVAL:-20}"; THRESHOLD="${THRESHOLD:-5}"; COOLDOWN="${COOLDOWN:-40}"
connected() { local gw; gw="$(ip route | awk '/^default/{print $3; exit}')"; [ -n "$gw" ] && ping -c1 -W2 "$gw" >/dev/null 2>&1; }
fails=0; step=0
while true; do
  if connected; then
    fails=0; step=0
  else
    fails=$((fails+1))
    if [ "$fails" -ge "$THRESHOLD" ]; then
      step=$((step+1))
      case "$step" in
        1) logger -t wifi-watchdog "down ~$((fails*INTERVAL))s - nudge: reactivate $IFACE"; nmcli device connect "$IFACE" >/dev/null 2>&1 ;;
        2) logger -t wifi-watchdog "still down - networking off/on"; nmcli networking off >/dev/null 2>&1; sleep 3; nmcli networking on >/dev/null 2>&1 ;;
        *) logger -t wifi-watchdog "still down - reloading brcmfmac (last resort)"; modprobe -r brcmfmac brcmutil >/dev/null 2>&1; sleep 3; modprobe brcmfmac >/dev/null 2>&1; step=0 ;;
      esac
      fails=0; sleep "$COOLDOWN"
    fi
  fi
  sleep "$INTERVAL"
done
WDEOF
sudo chmod 755 /usr/local/sbin/rpi5-wifi-watchdog
sudo tee /etc/systemd/system/rpi5-wifi-watchdog.service >/dev/null <<'WDSVC'
[Unit]
Description=Wi-Fi watchdog: re-associate wlan0 when the network drops (brcmfmac flakiness)
# After NetworkManager, not multi-user.target, which the plymouth stall gates.
After=NetworkManager.service
Wants=network.target
[Service]
Type=simple
ExecStart=/usr/local/sbin/rpi5-wifi-watchdog
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
WDSVC
sudo systemctl daemon-reload
sudo systemctl enable --now rpi5-wifi-watchdog.service >/dev/null 2>&1 || true
ok "wifi watchdog installed + enabled (auto re-associate wlan0 on drop)"

log "4c  Boot reliability: bound plymouth-quit-wait (headless must not stall multi-user.target)"
# plymouth-quit-wait is Before=multi-user.target with no timeout, and headless nothing quits it.
sudo install -d /etc/systemd/system/plymouth-quit-wait.service.d
printf '[Service]\nTimeoutStartSec=20s\n' \
  | sudo tee /etc/systemd/system/plymouth-quit-wait.service.d/10-timeout.conf >/dev/null
sudo systemctl daemon-reload
ok "plymouth-quit-wait bounded to 20s (boot works headless AND with a display)"

log "5  apport OFF but keep core dumps (systemd-coredump)"
sudo sed -i 's/^enabled=1/enabled=0/' /etc/default/apport 2>/dev/null || true
if ! dpkg -l systemd-coredump 2>/dev/null | grep -q '^ii'; then
  sudo apt-get install -y systemd-coredump >/dev/null 2>&1 || true; fi
ok "core_pattern: $(cat /proc/sys/kernel/core_pattern)"

log "6  Disable GNOME bloat (tracker indexer + Evolution + gvfs/SettingsDaemon helpers)"
if systemctl --user show-environment >/dev/null 2>&1; then
  for u in $(systemctl --user list-unit-files --no-legend 2>/dev/null | awk '{print $1}' \
             | grep -iE 'tracker|localsearch|tinysparql|evolution-(addressbook|calendar|source-registry|user-prompter)'); do
    systemctl --user stop "$u" 2>/dev/null || true
    systemctl --user mask "$u" 2>/dev/null || true
  done
  for sch in org.freedesktop.Tracker3.Miner.Files org.freedesktop.LocalSearch3.Miner.Files; do
    gsettings writable "$sch" enable-monitors >/dev/null 2>&1 && {
      gsettings set "$sch" enable-monitors false 2>/dev/null || true
      gsettings set "$sch" index-recursive-directories "[]" 2>/dev/null || true
      gsettings set "$sch" index-single-directories "[]" 2>/dev/null || true; }
  done
  (localsearch3 reset -s -r || tracker3 reset --filesystem || tracker reset --hard) >/dev/null 2>&1 || true
  ok "file indexer + Evolution masked"

  for u in gvfs-afc-volume-monitor.service gvfs-gphoto2-volume-monitor.service \
           gvfs-mtp-volume-monitor.service gvfs-goa-volume-monitor.service \
           org.gnome.SettingsDaemon.Smartcard.service org.gnome.SettingsDaemon.Wwan.service \
           org.gnome.SettingsDaemon.PrintNotifications.service org.gnome.SettingsDaemon.Wacom.service; do
    systemctl --user stop "$u" 2>/dev/null || true
    systemctl --user mask "$u" 2>/dev/null || true
  done
  ok "gvfs volume monitors + unused SettingsDaemon helpers masked"
else skip "no user session bus — run this from inside the GNOME session"; fi

log "7  Storage: drop continuous 'discard', use weekly fstrim.timer"
if grep -qE '\s/\s+ext4\s+[^#]*discard' /etc/fstab; then
  sudo cp -a /etc/fstab /etc/fstab.bak-$(date +%Y%m%d-%H%M%S)
  sudo sed -i -E '/\s\/\s+ext4\s/ s/\bdiscard\b/defaults/' /etc/fstab; ok "removed 'discard'"
else skip "no continuous 'discard'"; fi
sudo systemctl enable --now fstrim.timer >/dev/null 2>&1 || true; ok "fstrim.timer active"

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

log "10  NUMA emulation (Pi 5: EEPROM SDRAM banking + interleave; firmware picks optimal N)"
CL=/boot/firmware/cmdline.txt; [ -f "$CL" ] || CL=/boot/cmdline.txt
KCONF=/boot/config-$(uname -r)
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

  # With no cmdline.txt the Pi firmware assembles /chosen/bootargs instead.
  if [ -f "$CL" ]; then
    sudo cp -a "$CL" "$CL.bak-$(date +%Y%m%d-%H%M%S)"
    ensure_cmdline_token "numa_policy=$NUMA_POLICY"
    if [ "$NUMA_FAKE" != auto ]; then ensure_cmdline_token "numa=fake=$NUMA_FAKE"; fi
  else
    skip "no cmdline.txt — boot args come from Pi firmware (/proc/device-tree/chosen/bootargs)"
  fi

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
