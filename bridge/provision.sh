#!/bin/sh
# Runs on the phone as root from `wk machine setup <bridge>`, beside what the host rendered for it (lib/wk/bridge/render.py): role.env, manifest and files/. It renders nothing; busybox ash only.
#   provision.sh base   the configuration files, the packages and the scripts, before the phone is asked its facts
#   provision.sh role   every manifest line, then what only the phone can do in place
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/role.env"
CHANGES=0
REQUIRED="tailscale dnsmasq nftables chrony jq iw ethtool openssh networkmanager"
OPTIONAL="logrotate zram-init v4l-utils"
[ "$BR_CAMERA" = off ] || OPTIONAL="$OPTIONAL ffmpeg"

step()    { printf '\n==> %s\n' "$*"; }
info()    { printf '    %s\n' "$*"; }
warn()    { printf '    WARN: %s\n' "$*"; }
die()     { printf '    ERROR: %s\n' "$*"; exit 1; }
changed() { CHANGES=$((CHANGES + 1)); printf '    changed: %s\n' "$*"; }

put() {
    if [ -f "$2" ] && cmp -s "$HERE/files$2" "$2"; then
        chmod "$1" "$2"
        return 0
    fi
    mkdir -p "$(dirname "$2")"
    install -m "$1" "$HERE/files$2" "$2.wk-new"
    mv "$2.wk-new" "$2"
    changed "$2"
}

enabled() { rc-update show default 2>/dev/null | awk '{ print $1 }' | grep -qx "$1"; }

enable() {
    enabled "$1" && return 0
    rc-update add "$1" default >/dev/null 2>&1 || { warn "could not enable $1"; return 0; }
    changed "enabled $1"
}

service() {
    if ! cmp -s "$HERE/init.d/$1" "/etc/init.d/$1"; then
        install -m 0755 "$HERE/init.d/$1" "/etc/init.d/$1"
        changed "/etc/init.d/$1"
    fi
    enable "$1"
}

drop() {
    [ -x "/etc/init.d/$1" ] || return 0
    rc-service "$1" stop >/dev/null 2>&1 || true
    rc-update del "$1" default >/dev/null 2>&1 || true
    rm -f "/etc/init.d/$1"
    changed "removed $1"
}

disable() {
    enabled "$1" || return 0
    rc-update del "$1" default >/dev/null 2>&1 || true
    changed "disabled $1"
}

manifest() {
    while read -r verb a b <&3; do
        case " $* " in *" $verb "*) ;; *) continue ;; esac
        case "$verb" in
            file)    put "$a" "$b" ;;
            service) service "$a" ;;
            enable)  enable "$a" ;;
            disable) disable "$a" ;;
            drop)    drop "$a" ;;
            start)   rc-service "$a" status >/dev/null 2>&1 || rc-service "$a" start >/dev/null 2>&1 || warn "$a did not start" ;;
            restart) rc-service "$a" restart >/dev/null 2>&1 || warn "$a did not restart" ;;
        esac
    done 3< "$HERE/manifest"
}

base() {
    [ "$(id -u)" -eq 0 ] || die "run as root"
    command -v apk >/dev/null 2>&1 || die "this is not postmarketOS (no apk)"
    command -v rc-update >/dev/null 2>&1 || die "no OpenRC here: a pmOS image built with systemd is not supported by this role"
    step "Configuration"
    manifest file
    step "Packages"
    missing=""
    for p in $REQUIRED $OPTIONAL; do
        apk info -e "$p" >/dev/null 2>&1 || missing="$missing $p"
    done
    [ -n "$missing" ] || info "all present -- apk not contacted"
    [ -z "$missing" ] || apk update >/dev/null 2>&1 || warn "apk update failed -- installing from the cached index"
    for p in $missing; do
        if apk add --no-progress "$p" >/dev/null 2>&1; then
            changed "installed $p"
        else
            case " $REQUIRED " in *" $p "*) die "could not install $p, which the bridge cannot work without" ;; esac
            warn "could not install $p (optional)"
        fi
    done
    step "Scripts"
    mkdir -p /usr/local/sbin
    for f in "$HERE"/bin/*; do
        cmp -s "$f" "/usr/local/sbin/${f##*/}" && continue
        install -m 0755 "$f" "/usr/local/sbin/${f##*/}"
        changed "/usr/local/sbin/${f##*/}"
    done
    /usr/local/sbin/wk-bridge-usb-host || true
    /usr/local/sbin/wk-bridge-watchdog-load || true
}

# NAME= applies only when a device appears, and NetworkManager will not rename a device it manages, hence the hand-back.
rename() {
    cur=""
    for n in /sys/class/net/*; do
        [ "$(cat "$n/address" 2>/dev/null)" = "$BR_LAN_MAC" ] || continue
        cur=${n##*/}
        break
    done
    [ -n "$cur" ] || { warn "no interface holds $BR_LAN_MAC -- the adapter went away; re-run"; return 0; }
    info "renaming $cur to $BR_IF in place"
    nmcli device set "$cur" managed no >/dev/null 2>&1 || true
    ip link set "$cur" down 2>/dev/null || true
    if ip link set "$cur" name "$BR_IF" 2>/dev/null; then
        ip link set "$BR_IF" up 2>/dev/null || true
        nmcli device set "$BR_IF" managed yes >/dev/null 2>&1 || true
        nmcli connection up "wk-bridge-$BR_IF" >/dev/null 2>&1 || true
        changed "renamed $cur to $BR_IF"
    else
        nmcli device set "$cur" managed yes >/dev/null 2>&1 || true
        warn "could not rename $cur to $BR_IF; re-plug the dock and udev applies the rule"
    fi
}

role() {
    step "Configuration and services"
    manifest file service enable disable drop
    if [ "$(cat /etc/hostname 2>/dev/null)" != "$BR_HOSTNAME" ]; then
        printf '%s\n' "$BR_HOSTNAME" > /etc/hostname
        hostname "$BR_HOSTNAME"
        changed "hostname $BR_HOSTNAME"
    fi
    if ! grep -q "$BR_HOSTNAME" /etc/hosts 2>/dev/null; then
        printf '127.0.1.1\t%s\n' "$BR_HOSTNAME" >> /etc/hosts
        changed "/etc/hosts entry for $BR_HOSTNAME"
    fi
    nmcli connection reload >/dev/null 2>&1 || true
    [ -z "$BR_LAN_MAC" ] || [ -e "/sys/class/net/$BR_IF" ] || rename
    sysctl -p /etc/sysctl.d/99-wk-bridge.conf >/dev/null 2>&1 || warn "could not apply sysctl now (it applies at boot)"
    if grep -q '127\.0\.0\.53' /etc/resolv.conf 2>/dev/null; then
        rm -f /etc/resolv.conf
        changed "removed the systemd-resolved stub /etc/resolv.conf"
    fi
    if [ -f /etc/chrony/chrony.conf ] && ! grep -Eq '^[[:space:]]*(include|confdir)[[:space:]]+/etc/chrony/conf\.d' /etc/chrony/chrony.conf; then
        printf '\ninclude /etc/chrony/conf.d/*.conf\n' >> /etc/chrony/chrony.conf
        changed "chrony.conf includes conf.d"
    fi
    # OpenSSH takes the first value for a keyword, so the include goes first.
    if ! grep -q '^Include /etc/ssh/sshd_config.d/' /etc/ssh/sshd_config; then
        { printf 'Include /etc/ssh/sshd_config.d/*.conf\n\n'; cat /etc/ssh/sshd_config; } > /etc/ssh/sshd_config.wk-new
        mv /etc/ssh/sshd_config.wk-new /etc/ssh/sshd_config
        changed "/etc/ssh/sshd_config includes sshd_config.d"
    fi
    if sshd -t 2>/dev/null; then
        rc-service sshd reload >/dev/null 2>&1 || rc-service sshd restart >/dev/null 2>&1 || true
    else
        warn "sshd -t rejects the configuration -- NOT reloading it; fix this before rebooting"
    fi
    if [ -n "$BR_BATTERY_NODE" ]; then
        /usr/local/sbin/wk-bridge-battery || true
        cur=$(cat "$BR_BATTERY_NODE/charge_control_end_threshold" 2>/dev/null || echo '?')
        [ "$cur" = "$BR_BATTERY_LIMIT" ] || warn "$BR_BATTERY_NODE reads ${cur}%, not ${BR_BATTERY_LIMIT}% -- the write did not take"
    fi
    # The phone boots at 1970, so OpenRC's dependency cache looks newer than every script until it is refreshed.
    rc-update -u >/dev/null 2>&1 || warn "could not refresh OpenRC's dependency cache; services may not start at boot"
    step "Restarting"
    for s in $(sed -n 's/^service //p' "$HERE/manifest"); do
        rc-service "$s" restart >/dev/null 2>&1 || rc-service "$s" start >/dev/null 2>&1 || warn "$s did not start"
    done
    manifest start restart
}

case "${1:-}" in
    base) base ;;
    role) role ;;
    *) die "usage: provision.sh base|role" ;;
esac
step "Done -- $CHANGES change(s)"
