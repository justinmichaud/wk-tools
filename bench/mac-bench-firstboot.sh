#!/bin/bash
# The benchmark install configuring itself once, at first boot. A LaunchDaemon, not a package postinstall: startosinstall's package runs against a volume that is not running, so an account could only be made by hand-editing dslocal. Idempotent throughout.

set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

PAYLOAD=/usr/local/share/wk-bench
LOG=/var/log/wk-bench-firstboot.log
DAEMON=/Library/LaunchDaemons/com.wk.bench-firstboot.plist
SELF=/usr/local/libexec/wk-bench-firstboot.sh

BENCH_USER="${WK_BENCH_USER:-bench}"
PROFILE=perf-macos-tolken

exec >>"$LOG" 2>&1
echo "=== wk-bench first boot: $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

say() { echo "[wk-bench] $*"; }

if [ -r "$PAYLOAD/password" ]; then
    PW=$(cat "$PAYLOAD/password") || PW=""
else
    PW="${WK_BENCH_PASSWORD:-benchbench}"
    say "no password in the payload; using the constant default"
fi

if id -u "$BENCH_USER" >/dev/null 2>&1; then
    say "user $BENCH_USER already exists"
else
    say "creating $BENCH_USER"
    sysadminctl -addUser "$BENCH_USER" -fullName "wk bench" -password "$PW" -admin \
        || say "WARNING: sysadminctl -addUser failed"
fi

# Without the account, .AppleSetupDone leaves a login window offering nothing and Recovery as the only way back in, so the marker comes back off.
if ! id -u "$BENCH_USER" >/dev/null 2>&1; then
    say "FAILSAFE: '$BENCH_USER' does not exist after creation; restoring Setup Assistant"
    rm -f /var/db/.AppleSetupDone
    say "  the next boot will run Setup Assistant so this machine stays reachable"
    say "  the first-boot daemon stays installed and will try again after that"
    exit 1
fi

# Cattle, and a password prompt in the unattended `wk quiesce` is a hang.
if [ ! -f /etc/sudoers.d/wk-bench ]; then
    printf '%s ALL=(ALL) NOPASSWD: ALL\n' "$BENCH_USER" > /etc/sudoers.d/wk-bench
    chmod 0440 /etc/sudoers.d/wk-bench
    if visudo -c -f /etc/sudoers.d/wk-bench >/dev/null 2>&1; then  # a malformed file locks root out of sudo
        say "sudoers.d/wk-bench installed"
    else
        rm -f /etc/sudoers.d/wk-bench
        say "WARNING: sudoers snippet did not parse; removed"
    fi
fi

# A browser driven over ssh with no console session has nowhere to draw.
if [ -n "$PW" ]; then
    # `dscl . -passwd` needs the old password and drifts the account from the login keychain, whose unlock panel can sit on screen through an entire A/B; both tools exit 0 unacted.
    sysadminctl -resetPasswordFor "$BENCH_USER" -newPassword "$PW" >/dev/null 2>&1 \
        || dscl . -passwd "/Users/$BENCH_USER" "$PW" >/dev/null 2>&1 \
        || true
    if dscl . -authonly "$BENCH_USER" "$PW" >/dev/null 2>&1; then
        say "password verified for $BENCH_USER (authonly succeeded)"
    else
        say "WARNING: $BENCH_USER's password is NOT what this script set."
        say "  Autologin will raise a keychain/auth panel, and that panel sits on"
        say "  top of the benchmark where lsappinfo cannot see it. Every number"
        say "  from this install is suspect until this line reads 'verified'."
    fi

    home=$(dscl . -read "/Users/$BENCH_USER" NFSHomeDirectory 2>/dev/null | awk '{print $2}') || home=""  # reset to match: macOS recreates an empty one at next login
    if [ -n "$home" ] && [ -d "$home/Library/Keychains" ]; then
        rm -rf "$home/Library/Keychains" \
            && say "reset $BENCH_USER's login keychain (it drifts from the account password" \
            && say "  on a re-run, and the unlock prompt lands on top of the benchmark)" \
            || say "WARNING: could not reset the login keychain; expect an unlock prompt"
    fi

    # `sysadminctl -autologin set` logs `SACSetAutoLoginPassword error:22` and exits 0, so /etc/kcpassword (XOR Apple's fixed key, NUL-padded to a multiple of 12) is written here.
    /usr/bin/python3 - "$PW" <<'KCP' 2>/dev/null || say "WARNING: could not write /etc/kcpassword"
import sys, os
KEY = bytes([0x7D,0x89,0x52,0x23,0xD2,0xBC,0xDD,0xEA,0xA3,0xB9,0x1F])
pw  = sys.argv[1].encode()
pad = 12 - (len(pw) % 12) if len(pw) % 12 else 12
buf = pw + b"\x00" * pad
out = bytes(c ^ KEY[i % len(KEY)] for i, c in enumerate(buf))
with open("/etc/kcpassword", "wb") as f:
    f.write(out)
os.chmod("/etc/kcpassword", 0o600)
os.chown("/etc/kcpassword", 0, 0)
KCP
    defaults write /Library/Preferences/com.apple.loginwindow autoLoginUser "$BENCH_USER" 2>/dev/null \
        || say "WARNING: could not set autoLoginUser"

    if [ -f /etc/kcpassword ] \
       && [ "$(defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser 2>/dev/null)" = "$BENCH_USER" ]; then
        say "autologin set for $BENCH_USER (kcpassword written, autoLoginUser set)"
    else
        say "WARNING: autologin did NOT take."
        say "  the console will ask for a password; it is '$PW'"
    fi
else
    say "autologin left alone (no password on hand for an existing user)"
fi

# `systemsetup -setremotelogin on` wants Full Disk Access, which a fresh-install LaunchDaemon cannot ask for; Remote Login is a launchd override, so set that instead.
launchctl enable system/com.openssh.sshd 2>/dev/null || true
launchctl bootstrap system /System/Library/LaunchDaemons/ssh.plist 2>/dev/null || true
systemsetup -setremotelogin on >/dev/null 2>&1 || true
if launchctl print-disabled system 2>/dev/null | grep -q '"com.openssh.sshd" => enabled'; then
    say "remote login on"
else
    say "WARNING: remote login is still off -- this machine cannot be driven"
fi

if [ -f "$PAYLOAD/authorized_keys" ]; then
    home=$(dscl . -read "/Users/$BENCH_USER" NFSHomeDirectory 2>/dev/null | awk '{print $2}') || home=""
    if [ -n "$home" ] && [ -d "$home" ]; then
        install -d -m 0700 -o "$BENCH_USER" "$home/.ssh" \
            && install -m 0600 -o "$BENCH_USER" "$PAYLOAD/authorized_keys" "$home/.ssh/authorized_keys" \
            && say "authorized_keys installed for $BENCH_USER" \
            || say "WARNING: could not install authorized_keys for $BENCH_USER"
    else
        say "WARNING: no home directory for $BENCH_USER yet; authorized_keys not installed"
    fi
fi

# No tailnet identity: every macOS Tailscale build tunnels through NetworkExtension, whose "would like to add VPN configurations" panel only a person can answer, and pkgs.tailscale.com publishes no darwin daemon to run instead (measured 2026-09-07). An unattended first boot therefore installs none, and this install is unreachable while it runs: `wk bench mac-ab` reads the volume from host mode afterwards.

if [ -r "$PAYLOAD/wifi.conf" ]; then
    # shellcheck disable=SC1090
    . "$PAYLOAD/wifi.conf"
    if [ -n "${WIFI_SSID:-}" ]; then
        dev=$(networksetup -listallhardwareports 2>/dev/null \
                | awk '/Hardware Port: Wi-Fi/{getline; print $2; exit}') || dev=""
        dev="${dev:-en0}"
        networksetup -setairportpower "$dev" on >/dev/null 2>&1 || true
        networksetup -setairportnetwork "$dev" "$WIFI_SSID" "${WIFI_PSK:-}" >/dev/null 2>&1 || true
        for _t in 1 2 3 4 5 6 7 8 9 10; do  # verified by an address: this exits 0 even on "Could not find network"
            ip=$(ipconfig getifaddr "$dev" 2>/dev/null) && [ -n "$ip" ] && break
            sleep 3
        done
        if [ -n "${ip:-}" ]; then
            say "network: $dev joined '$WIFI_SSID' as $ip"
        else
            say "WARNING: could not join '$WIFI_SSID' -- this machine will be unreachable"
        fi
    fi
else
    say "WARNING: no wifi.conf in the payload; if this Mac is on Wi-Fi it will"
    say "  have no network and nothing will be able to drive it"
fi

install -d -o "$BENCH_USER" -g staff -m 0755 /var/wk 2>/dev/null \
    && say "staging root /var/wk ready, owned by $BENCH_USER" \
    || say "WARNING: could not create /var/wk -- staging will fail from host mode"

if [ ! -f /etc/wk-image ]; then
    printf 'id=%s-%s\nprofile=%s\n' "$PROFILE" "$(date -u +%Y-%m)" "$PROFILE" > /etc/wk-image
    say "wrote /etc/wk-image"
fi

QUIET_HOSTS=/usr/local/libexec/wk-bench-quiet-hosts.sh

# The one fetch a benchmark install makes: without the Command Line Tools there is no working /usr/bin/python3 and run-benchmark is python. softwareupdate offers them only while that in-progress file exists, and the update denial goes back on below.
if [ -x /Library/Developer/CommandLineTools/usr/bin/python3 ]; then
    say "command line tools present"
elif [ -r "$QUIET_HOSTS" ]; then
    # shellcheck disable=SC1090
    . "$QUIET_HOSTS"
    wk_bench_hosts_remove /etc/hosts || say "WARNING: could not lift the update denial"
    say "installing the Command Line Tools"
    touch /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress
    label=$(softwareupdate -l 2>/dev/null \
              | sed -n 's/^ *\* Label: \(Command Line Tools.*\)$/\1/p' | tail -1) || label=""
    if [ -n "$label" ]; then
        softwareupdate -i "$label" >/dev/null 2>&1 \
            && say "  installed: $label" \
            || say "  WARNING: '$label' did not install"
    else
        say "  WARNING: softwareupdate offers no Command Line Tools, so this install"
        say "  has no python3 and can measure nothing. Check its network."
    fi
    rm -f /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress
    xcode-select --switch /Library/Developer/CommandLineTools 2>/dev/null || true
fi

QUIET_DESKTOP=/usr/local/libexec/wk-bench-quiet-desktop.sh
if [ -r "$QUIET_DESKTOP" ]; then
    # shellcheck disable=SC1090
    . "$QUIET_DESKTOP"
    wk_quiet_desktop_system || say "WARNING: the machine-wide quieting did not fully take (above)"
    wk_quiet_desktop_user "$BENCH_USER" || say "WARNING: $BENCH_USER's desktop is not fully quiet (above)"
    _probe=$(wk_quiet_desktop_probe "$BENCH_USER")
    { wk_quiet_desktop_findings "$_probe"; wk_quiet_cpu_findings "$_probe"; } \
        | while IFS="$(printf '\t')" read -r _state _what _rest; do
              say "  $_state  $_what"
          done || true
else
    say "WARNING: $QUIET_DESKTOP missing from the payload; this install is not quieted"
fi
say "filevault: $(fdesetup status 2>&1 | head -1)"

if [ -r "$QUIET_HOSTS" ]; then
    # shellcheck disable=SC1090
    . "$QUIET_HOSTS"
    if wk_bench_hosts_present /etc/hosts; then
        say "hosts: update endpoints already denied"
    elif wk_bench_hosts_apply /etc/hosts; then
        say "hosts: update endpoints denied"
    else
        say "WARNING: could not deny update endpoints in /etc/hosts"
    fi
else
    say "WARNING: $QUIET_HOSTS missing from the payload; update endpoints not denied"
fi

if [ -x "$PAYLOAD/wk-tools/admin/wk-quiesce-priv" ]; then
    install -d -o root -m 0755 /usr/local/libexec 2>/dev/null || true
    if install -o root -m 0755 "$PAYLOAD/wk-tools/admin/wk-quiesce-priv" \
                               /usr/local/libexec/wk-quiesce-priv; then
        say "quiesce helper installed"
    else
        say "WARNING: could not install the quiesce helper; 'wk quiesce' will refuse to run"
    fi
fi

if [ -d "$PAYLOAD/wk-tools" ]; then
    home=$(dscl . -read "/Users/$BENCH_USER" NFSHomeDirectory 2>/dev/null | awk '{print $2}') || home=""
    if [ -n "$home" ] && [ -d "$home" ]; then
        install -d -o "$BENCH_USER" "$home/Development" \
            && /usr/bin/rsync -a --delete "$PAYLOAD/wk-tools/" "$home/Development/wk-tools/" \
            && chown -R "$BENCH_USER" "$home/Development/wk-tools" \
            && say "wk-tools placed at $home/Development/wk-tools" \
            || say "WARNING: could not place wk-tools"
    fi
fi

PYOBJC=/usr/local/libexec/wk-bench-pyobjc.sh
if [ -r "$PYOBJC" ]; then
    # shellcheck disable=SC1090
    . "$PYOBJC"
    # As $BENCH_USER through `su -l`, not as root: `pip install --user` installs into the running user's home, the browser is driven as $BENCH_USER, and a LaunchDaemon has no HOME to install into anyway.
    if su -l "$BENCH_USER" -c ". $PYOBJC; wk_pyobjc_install" >&2; then
        say "pyobjc $WK_PYOBJC_VERSION installed for $BENCH_USER"
    else
        say "PYOBJC MISSING -- run-benchmark cannot size the screen or warp the cursor,"
        say "  and nothing can keep MiniBrowser frontmost, so every number this install"
        say "  produces is a throttled browser's. It needs a route to PyPI."
    fi
else
    say "WARNING: $PYOBJC missing from the payload; this install cannot run a benchmark"
fi

# Not `launchctl bootout` on this daemon's own label: that kills this script before the rm.
say "removing the first-boot daemon"
rm -f "$DAEMON" "$SELF" || true
if [ -f "$DAEMON" ]; then
    say "WARNING: $DAEMON is still there -- provisioning will run again next boot"
else
    say "  removed $DAEMON"
fi
say "=== first boot provisioning complete ==="

say "rebooting so autologin takes effect"
shutdown -r +1 &
exit 0
