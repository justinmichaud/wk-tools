#!/bin/bash
# The benchmark install configuring itself once, at first boot. A LaunchDaemon, not a package postinstall: startosinstall's package runs against a volume that is not running, so an account could only be made by hand-editing dslocal. Idempotent throughout.
# A LaunchDaemon inherits no environment, so the password comes from $PAYLOAD/password; WK_BENCH_USER and WK_BENCH_PASSWORD are for running this by hand.

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

# The standalone/macsys package, whose LaunchDaemon runs before login -- not the App Store build, which is sandboxed and needs a session. Without a tailnet name the LAN address changes across reboots and the ssh alias resolves to the host install.
TS_CLI=/Applications/Tailscale.app/Contents/MacOS/Tailscale
TS_PKG=$(ls "$PAYLOAD"/Tailscale-*macos.pkg 2>/dev/null | head -1) || TS_PKG=""

if [ -r "$PAYLOAD/tailscale-authkey" ] && [ -n "$TS_PKG" ]; then
    say "installing Tailscale (standalone/macsys) from $(basename "$TS_PKG")"
    if installer -pkg "$TS_PKG" -target / >/dev/null 2>&1; then
        if launchctl print system/io.tailscale.ipn.macsys.tssentineld >/dev/null 2>&1; then  # verified by asking launchd
            say "  tailscale system daemon is loaded"
        else
            say "  WARNING: the tailscale daemon did not load; giving it a moment"
            sleep 10
        fi
        if [ -x "$TS_CLI" ]; then
            # `file:` not the key itself: argv is world readable. Tagged nodes never key-expire.
            "$TS_CLI" up --auth-key "file:$PAYLOAD/tailscale-authkey" \
                --advertise-tags=tag:wk \
                --hostname tolken-bench --accept-dns=false >/dev/null 2>&1 || true
            ts_ip=$("$TS_CLI" ip -4 2>/dev/null | head -1) || ts_ip=""  # the property, not the exit status
            if [ -n "$ts_ip" ]; then
                say "tailscale: up as tolken-bench at $ts_ip"
                say "  this install is now reachable by name across a reboot, which is"
                say "  the thing that makes it observable at all"
            else
                say "WARNING: tailscale up did not take; no tailnet address."
                say "  The install will only be reachable at whatever DHCP address it gets,"
                say "  and not at all from a driver that reaches this Mac over the tailnet."
            fi
        else
            say "WARNING: $TS_CLI is missing after a successful install"
        fi
    else
        say "WARNING: installer failed on $TS_PKG"
    fi
elif [ -r "$PAYLOAD/tailscale-authkey" ]; then
    say "WARNING: an auth key is staged but no Tailscale package is."
    say "  Put Tailscale-<ver>-macos.pkg in the payload:"
    say "    curl -LO https://pkgs.tailscale.com/stable/Tailscale-1.102.3-macos.pkg"
    say "  No compiler is needed -- see the comment here."
else
    say "no tailscale auth key in the payload; this install will have no tailnet"
    say "  identity, so nothing that reaches this Mac over the tailnet can reach it"
fi

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

mdutil -i off -a >/dev/null 2>&1 || say "WARNING: spotlight still indexing"
softwareupdate --schedule off >/dev/null 2>&1 || true
defaults write /Library/Preferences/com.apple.SoftwareUpdate AutomaticCheckEnabled -bool false 2>/dev/null || true
pmset -a lowpowermode 0 sleep 0 displaysleep 0 disksleep 0 >/dev/null 2>&1 || true
tmutil disablelocal >/dev/null 2>&1 || true
say "quieted: spotlight=$(mdutil -a -s 2>&1 | tr '\n' ' ' | sed 's/  */ /g')"
say "updates schedule: $(softwareupdate --schedule 2>&1 | tail -1)"
say "filevault: $(fdesetup status 2>&1 | head -1)"

QUIET_HOSTS=/usr/local/libexec/wk-bench-quiet-hosts.sh
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

if /usr/bin/python3 -c 'import objc' >/dev/null 2>&1; then
    say "pyobjc: present"
else
    say "PYOBJC MISSING -- Command Line Tools are not installed on this volume."
    say "  run-benchmark's prepare_env will fail. From a console here:"
    say "    xcode-select --install"
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
