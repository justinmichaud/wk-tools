#!/bin/sh
# Provision a postmarketOS phone as a tailnet bridge. Runs on the phone, as
# root, from `wk bridge setup`, which copies this whole directory to
# /usr/local/lib/wk-bridge first, so the spec lives in the repository and
# the phone holds only a copy.
#
#   house WiFi --wlan0-- [ phone ] --lan0-- the segment behind it
#                            |
#                    tailscaled advertises $BR_SEGMENT
#
# This file is the only source of truth, and it is idempotent: edit this,
# re-run `wk bridge setup`, never hand-edit /etc on the device. POSIX sh, no
# bash -- pmOS is Alpine and /bin/sh is busybox ash -- and nothing here
# sources lib/common.sh either, since the phone gets this directory, not
# the repo.
#
# Inputs, all from the environment, all set by cmd/bridge from the host conf:
#
#   BR_NAME       the bridge's name, which is also its conf file's name
#   BR_DEVICE     pinephone | librem5 (bridge/devices.sh)
#   BR_HOSTNAME   what the phone calls itself and how it appears on the tailnet
#   BR_TAG        the tailnet tag it must hold -- tagged nodes never key-expire
#   BR_SEGMENT    the subnet it routes, e.g. 10.99.1.0/24
#   BR_ROUTER     its own address on that subnet
#   BR_IF         what the downstream NIC is renamed to, e.g. lan0
#   BR_POOL       dnsmasq's range: "first,last" (equal for a one-address segment)
#   BR_LEASES     reservations: "mac,ip[,name] mac,ip[,name] ..."
#   BR_EGRESS     none | nat -- whether the segment may reach the internet
#   BR_CAMERA     off | http -- stream the camera over the tailnet
#   BR_LAN_MAC    the USB Ethernet adapter's MAC; autodetected when empty
#   BR_BATTERY    the power_supply node exposing charge_control_end_threshold;
#                 autodetected when empty (bridge/hosts/<name>.conf)
#   BR_BATTERY_LIMIT  the charge cap, in percent (cmd/bridge defaults it to 80)
#   BR_AUTHKEY    a tagged tailscale auth key, only when the node needs one
set -eu

: "${BR_NAME:?BR_NAME is not set (run this through 'wk bridge setup')}"
: "${BR_HOSTNAME:?BR_HOSTNAME is not set}"
: "${BR_SEGMENT:?BR_SEGMENT is not set}"
: "${BR_ROUTER:?BR_ROUTER is not set}"
: "${BR_BATTERY_LIMIT:?BR_BATTERY_LIMIT is not set (run this through 'wk bridge setup')}"
BR_DEVICE="${BR_DEVICE:-unknown}"
BR_TAG="${BR_TAG:-tag:bridge}"
BR_IF="${BR_IF:-lan0}"
BR_POOL="${BR_POOL:-}"
BR_LEASES="${BR_LEASES:-}"
BR_EGRESS="${BR_EGRESS:-none}"
BR_CAMERA="${BR_CAMERA:-off}"
BR_LAN_MAC="${BR_LAN_MAC:-}"
BR_BATTERY="${BR_BATTERY:-}"

# Everything except joining the tailnet: that needs a credential a person
# fetches, and `--advertise-tags` can only be set at login, so it is
# separable. BR_NO_TAILNET=1 stops short; `wk bridge tailnet <name>` finishes it.
BR_NO_TAILNET="${BR_NO_TAILNET:-}"

# The auth key arrives in a 0600 file on tmpfs: argv is world readable in
# /proc, so `--authkey=...` on a command line hands it to anyone with a shell.
BR_AUTHKEY="${BR_AUTHKEY:-}"
AUTHKEY_FILE=/run/wk-bridge-authkey
if [ -z "$BR_AUTHKEY" ] && [ -f "$AUTHKEY_FILE" ]; then
    BR_AUTHKEY=$(cat "$AUTHKEY_FILE")
fi

HERE=$(cd "$(dirname "$0")" && pwd)
LEASEFILE=/var/lib/misc/dnsmasq.leases
CHANGES=0
# Built here, handed to write_file by redirect, not pipeline: a function
# right of `|` runs in a subshell, silently discarding its change count.
TMPD=$(mktemp -d)
trap 'rm -rf "$TMPD"' EXIT INT TERM

step()    { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info()    { printf '    %s\n' "$*"; }
warn()    { printf '\033[1;33m    WARN: %s\033[0m\n' "$*"; }
die()     { printf '\033[1;31m    ERROR: %s\033[0m\n' "$*"; exit 1; }
changed() { CHANGES=$((CHANGES + 1)); printf '\033[1;32m    changed:\033[0m %s\n' "$*"; }

# Write a file only when its content differs, and say which: "0 changes" is
# what makes a re-run readable as "already matches the repository".
write_file() {
    # `local` is not POSIX but both ash and dash have it, and pmOS is busybox
    # ash. Without it these leak into the caller, which matters here: `tmp` is
    # also the name the sshd_config edit uses.
    local dst mode tmp
    dst="$1"; mode="${2:-0644}"
    tmp=$(mktemp)
    cat > "$tmp"
    if [ -f "$dst" ] && cmp -s "$tmp" "$dst"; then
        chmod "$mode" "$dst"
        rm -f "$tmp"
        return 0
    fi
    mkdir -p "$(dirname "$dst")"
    chmod "$mode" "$tmp"
    mv "$tmp" "$dst"
    changed "$dst"
}

# The name a service actually has: packagings disagree (networkmanager vs
# NetworkManager, tailscale vs tailscaled), and the wrong name reads as "broken".
svc() {
    local n
    for n in "$@"; do
        # `if` rather than `[ ... ] && { ... }` for readability only -- the
        # explicit `return 1` below is what makes the no-match case safe under
        # `set -e`, and it has to stay there.
        if [ -x "/etc/init.d/$n" ]; then
            printf '%s' "$n"
            return 0
        fi
    done
    return 1
}

# Real files under bridge/init.d rather than heredocs here: everything
# variable is in /etc/wk-bridge.conf, read at runtime, and a service file
# you can read, grep and diff is worth more than one assembled at provision time.
install_service() {
    local src
    src="$HERE/init.d/$1"
    [ -f "$src" ] || die "bridge/init.d/$1 is missing from the copy on this phone"
    if ! cmp -s "$src" "/etc/init.d/$1"; then
        install -m 0755 "$src" "/etc/init.d/$1"
        changed "/etc/init.d/$1"
    fi
}

# rc-update is idempotent by itself but silent, and "what did this change"
# is the question a re-run has to answer.
enable_svc() {
    if rc-update show default 2>/dev/null | awk '{print $1}' | grep -x "$1" >/dev/null 2>&1; then
        return 0
    fi
    rc-update add "$1" default >/dev/null 2>&1 || { warn "could not enable $1"; return 0; }
    changed "enabled $1"
}

# Force OpenRC to re-read the init scripts: it regenerates the dependency
# tree only when a script is *newer* than the cache, but this phone boots at
# 1970 (no RTC) and chrony then steps the clock forward, making the cache
# look decades newer than scripts that are enabled, symlinked and listed by
# `rc-update show` yet invisible to `rc-status` and never started.
refresh_deptree() {
    rc-update -u >/dev/null 2>&1 \
        || warn "could not refresh OpenRC's dependency cache; services may not start at boot"
}

# --- 0. is this the machine this script is for? ------------------------------
# Refusing beats half-applying: running this against a non-Alpine phone
# would install service files that fit no init system, succeed partway, and
# leave a bridge that is neither.
[ "$(id -u)" -eq 0 ] || die "run as root"
command -v apk >/dev/null 2>&1 || die "this is not an Alpine/postmarketOS system (no apk).
    The bridge role is pmOS-only on purpose: one provisioner for both phones.
    Reflash the device with postmarketOS first -- 'wk help'."
command -v rc-update >/dev/null 2>&1 || die "no OpenRC here (rc-update is missing).
    A pmOS image built with systemd is not supported by this role."

step "Provisioning $BR_NAME ($BR_DEVICE) on $(cat /etc/hostname 2>/dev/null)"
info "segment $BR_SEGMENT via $BR_IF, router $BR_ROUTER, egress $BR_EGRESS"

# Two lists: without the required set there is no bridge; the optional
# set's absence is a warning, not a stop, so a moved-on apk index cannot
# block re-provisioning a device that already works.
REQUIRED="tailscale dnsmasq nftables chrony jq iw ethtool openssh networkmanager"
OPTIONAL="logrotate zram-init v4l-utils"
[ "$BR_CAMERA" = off ] || OPTIONAL="$OPTIONAL ffmpeg"

# --- 1. stop it disappearing -------------------------------------------------
# First, before anything slow: an unprovisioned phone suspends on idle, so
# provisioning later loses the device mid-run. The GUI stays, the recovery
# path if WiFi and ssh are both gone, but every reason to sleep must go.
step "Never sleep"
if [ -d /etc/elogind ]; then
    write_file /etc/elogind/logind.conf.d/10-wk-bridge.conf <<'EOF'
# A power button wedged in a drawer is as likely to be a pocket as a person.
[Login]
HandlePowerKey=ignore
HandleSuspendKey=ignore
HandleHibernateKey=ignore
HandleLidSwitch=ignore
IdleAction=ignore
EOF
else
    warn "no /etc/elogind -- cannot pin the power/suspend keys; check what this image uses"
fi


# --- 2. packages -------------------------------------------------------------
step "Packages"
missing=""
for p in $REQUIRED $OPTIONAL; do
    apk info -e "$p" >/dev/null 2>&1 || missing="$missing $p"
done

if [ -z "$missing" ]; then
    info "all packages present -- apk not contacted"
else
    info "missing:$missing"
    apk update >/dev/null 2>&1 || warn "apk update failed -- installing from the cached index"
    for p in $REQUIRED; do
        apk info -e "$p" >/dev/null 2>&1 && continue
        apk add --no-progress "$p" >/dev/null || die "could not install $p, which the bridge cannot work without"
        changed "installed $p"
    done
    for p in $OPTIONAL; do
        apk info -e "$p" >/dev/null 2>&1 && continue
        if apk add --no-progress "$p" >/dev/null 2>&1; then
            changed "installed $p"
        else
            warn "could not install $p (optional) -- see what depends on it below"
        fi
    done
fi

# Kernel-mode tailscale needs /dev/net/tun; userspace networking forwards
# nothing for anyone else, the entire job here.
[ -c /dev/net/tun ] || warn "/dev/net/tun is missing -- tailscale cannot route a
    subnet without it. Check that the kernel has tun (modprobe tun), or this
    bridge will come up healthy-looking and forward nothing."

# --- 3. the scripts, and the fact file they all read -------------------------
# One file says what this bridge is; every on-device script reads it, so
# `wk bridge status` describes the running configuration, not the repository.
step "Scripts"
mkdir -p /usr/local/sbin
for b in "$HERE"/bin/*; do
    [ -f "$b" ] || continue
    n=$(basename "$b")
    if ! cmp -s "$b" "/usr/local/sbin/$n"; then
        install -m 0755 "$b" "/usr/local/sbin/$n"
        changed "/usr/local/sbin/$n"
    fi
done

write_file /etc/wk-bridge.conf 0644 <<EOF
# Written by bridge/provision.sh; edit the conf in wk-tools instead. Sourced
# by /bin/sh, so keys are lowercase and values are unquoted-safe.
name=$BR_NAME
device=$BR_DEVICE
hostname=$BR_HOSTNAME
tag=$BR_TAG
segment=$BR_SEGMENT
router=$BR_ROUTER
iface=$BR_IF
pool=$BR_POOL
leases="$BR_LEASES"
egress=$BR_EGRESS
camera=$BR_CAMERA
leasefile=$LEASEFILE
EOF

# --- 4. hostname -------------------------------------------------------------
step "Hostname"
if [ "$(cat /etc/hostname 2>/dev/null)" != "$BR_HOSTNAME" ]; then
    printf '%s\n' "$BR_HOSTNAME" > /etc/hostname
    hostname "$BR_HOSTNAME"
    changed "hostname $BR_HOSTNAME"
fi
# 127.0.1.1, not 127.0.0.1: sudo and anything resolving its own name wants
# the loopback entry that is always there.
if ! grep "$BR_HOSTNAME" /etc/hosts >/dev/null 2>&1; then
    printf '127.0.1.1\t%s\n' "$BR_HOSTNAME" >> /etc/hosts
    changed "/etc/hosts entry for $BR_HOSTNAME"
fi

# --- 5. the uplink -----------------------------------------------------------
step "WiFi"
write_file /etc/NetworkManager/conf.d/99-wk-bridge.conf <<'EOF'
# Power save turns a working uplink into one that answers every third ping.
[connection]
wifi.powersave=2

# A stable MAC keeps the DHCP lease and any AP-side reservation stable.
[device]
wifi.scan-rand-mac-address=no
EOF

# UDP GRO forwarding, re-applied on every interface that comes up: worth
# roughly a factor on tailscale throughput, and not sticky across a link
# bounce, hence a dispatcher hook rather than a one-shot.
write_file /etc/NetworkManager/dispatcher.d/50-wk-bridge-gro 0755 <<'EOF'
#!/bin/sh
IFACE="$1"
ACTION="$2"
[ "$ACTION" = up ] || exit 0
case "$IFACE" in
    lo|tailscale0) exit 0 ;;
esac
ethtool -K "$IFACE" rx-udp-gro-forwarding on rx-gro-list off 2>/dev/null
exit 0
EOF

NM_SVC=$(svc networkmanager NetworkManager) || die "NetworkManager has no init script here"
enable_svc "$NM_SVC"

# --- 6. USB host mode, and the downstream NIC --------------------------------
step "USB host mode"
install_service wk-bridge-usb-host
enable_svc wk-bridge-usb-host
/usr/local/sbin/wk-bridge-usb-host || true

step "Downstream NIC ($BR_IF)"
# Found by being on USB and not WiFi, autodetected rather than declared: the
# answer is a property of the dock in use.
if [ -z "$BR_LAN_MAC" ]; then
    for n in /sys/class/net/*; do
        d=$(basename "$n")
        # usb* excluded: on a phone that is the USB *gadget* interface (device
        # mode), not a real adapter in host mode (eth0/enp*, or $BR_IF once
        # the udev rule below applies).
        case "$d" in lo|wlan*|tailscale*|dummy*|usb*) continue ;; esac
        [ -e "$n/device/subsystem" ] || continue
        case "$(readlink -f "$n/device/subsystem")" in
            */usb)
                # The *permanent* address, not the current one: pmOS ships
                # NetworkManager with `ethernet.cloned-mac-address=stable`, so
                # a managed interface runs on a synthetic MAC within seconds
                # of appearing. The udev rule below matches at ACTION=="add",
                # against the hardware address, so a rule built from the
                # current (synthetic) address matches nothing and the rename
                # silently stops working at the next boot.
                perm=$(ethtool -P "$d" 2>/dev/null | sed -n 's/^Permanent address: //p')
                case "$perm" in
                    ""|00:00:00:00:00:00) BR_LAN_MAC=$(cat "$n/address") ;;
                    *) BR_LAN_MAC="$perm" ;;
                esac
                info "found USB ethernet: $d ($BR_LAN_MAC)"
                break ;;
        esac
    done
fi

if [ -z "$BR_LAN_MAC" ]; then
    warn "no USB ethernet adapter is enumerated right now."
    warn "  The rest of this run applies, but this step does *not*: the udev"
    warn "  rename and the address are written from the adapter's own MAC, so"
    warn "  with no adapter there is nothing to match and neither file is"
    warn "  written. An adapter plugged in later comes up unnamed and"
    warn "  unaddressed until 'wk bridge setup $BR_NAME' is run again."
    warn "  (Nothing is written that could match the interface by MAC when it"
    warn "  appears, so waiting for it achieves nothing.)"
else
    # A udev rule, not a systemd .link file: pmOS has eudev, no
    # systemd-networkd. Renaming is what makes every other file here
    # device-independent -- dnsmasq, nftables and the address all name
    # $BR_IF -- so swapping the dock changes this one line and nothing else.
    write_file /etc/udev/rules.d/70-wk-bridge-net.rules <<EOF
# The bridge's downstream NIC, by MAC. Written by bridge/provision.sh.
SUBSYSTEM=="net", ACTION=="add", ATTR{address}=="$(printf '%s' "$BR_LAN_MAC" | tr 'A-Z' 'a-z')", NAME="$BR_IF"
EOF

    # NetworkManager owns the devices here, so this is a keyfile connection,
    # not an /etc/network/interfaces stanza -- two things configuring one
    # interface is how an address appears and disappears on its own.
    # never-default/ignore-auto-dns: this leg is a segment, not a way out --
    # a default route learned here would send the phone's own traffic at a BMC.
    write_file "/etc/NetworkManager/system-connections/wk-bridge-$BR_IF.nmconnection" 0600 <<EOF
[connection]
id=wk-bridge-$BR_IF
type=ethernet
interface-name=$BR_IF
autoconnect=true
autoconnect-retries=0

[ipv4]
method=manual
address1=$BR_ROUTER/${BR_SEGMENT#*/}
never-default=true
ignore-auto-dns=true

[ipv6]
method=ignore

# Keep the hardware address on this leg, against pmOS's global
# `ethernet.cloned-mac-address=stable`: the udev rule above matches the
# permanent MAC, and this segment address is a thing other machines key on
# (an ARP entry, a far-side reservation) that a synthetic MAC would break.
# The phone's *uplink* randomization is a privacy feature, left alone.
[ethernet]
cloned-mac-address=permanent
EOF
    nmcli connection reload >/dev/null 2>&1 || true

    # A udev NAME= applies when the device *appears*; an interface already up
    # cannot be renamed underneath itself, so the first run against a
    # plugged-in dock leaves it under its kernel name. Renamed here instead of
    # asking for a hand at the phone -- the whole point of a bridge is being
    # reachable without going to it -- and the udev rule makes it survive the
    # next boot.
    if [ ! -e "/sys/class/net/$BR_IF" ]; then
        cur=""
        for n in /sys/class/net/*; do
            d=$(basename "$n")
            case "$d" in lo|wlan*|tailscale*|dummy*|usb*) continue ;; esac
            [ "$(cat "$n/address" 2>/dev/null)" = "$BR_LAN_MAC" ] || continue
            cur="$d"; break
        done
        if [ -z "$cur" ]; then
            warn "$BR_IF does not exist and no interface holds $BR_LAN_MAC --"
            warn "  the adapter has gone between detection and now. Re-run this."
        else
            info "renaming $cur to $BR_IF in place"
            # NetworkManager refuses to rename a device it manages; handing it
            # back afterwards lets the keyfile above claim the new name.
            nmcli device set "$cur" managed no >/dev/null 2>&1 || true
            ip link set "$cur" down 2>/dev/null || true
            if ip link set "$cur" name "$BR_IF" 2>/dev/null; then
                ip link set "$BR_IF" up 2>/dev/null || true
                nmcli device set "$BR_IF" managed yes >/dev/null 2>&1 || true
                nmcli connection up "wk-bridge-$BR_IF" >/dev/null 2>&1 || true
                CHANGES=$((CHANGES + 1))
            else
                nmcli device set "$cur" managed yes >/dev/null 2>&1 || true
                warn "could not rename $cur to $BR_IF. Re-plug the dock or reboot"
                warn "  the phone -- udev applies the rule when it next appears."
            fi
        fi
    fi
fi

# --- 7. DHCP for the segment -------------------------------------------------
step "DHCP"
# dnsmasq wants a netmask, the segment is written as a prefix length -- the
# same fact twice, so it is derived rather than declared twice in the conf.
case "${BR_SEGMENT#*/}" in
    16) NETMASK=255.255.0.0 ;;
    24) NETMASK=255.255.255.0 ;;
    *)  NETMASK=255.255.255.0
        warn "prefix /${BR_SEGMENT#*/} is not one this understands -- assuming $NETMASK" ;;
esac
{
    printf '# DHCP for the bridge segment. Written by bridge/provision.sh.\n'
    if [ "$BR_EGRESS" = nat ]; then
        # DNS on: a device that may reach the internet needs to resolve names.
        printf 'domain-needed\nbogus-priv\n'
    else
        # DNS off (port=0, empty option 6): a segment with no egress has
        # nothing to resolve, and a resolver is attackable from here.
        printf 'port=0\n'
    fi
    printf 'interface=%s\n' "$BR_IF"
    # bind-dynamic, not bind-interfaces: the NIC comes and goes with the
    # dock, and bind-interfaces fails to start whenever it is out.
    printf 'bind-dynamic\ndhcp-authoritative\nlog-dhcp\n'
    printf 'dhcp-leasefile=%s\n' "$LEASEFILE"
    [ -n "$BR_POOL" ] && printf 'dhcp-range=%s,%s,infinite\n' "$BR_POOL" "$NETMASK"
    printf 'dhcp-option=option:router,%s\n' "$BR_ROUTER"
    printf 'dhcp-option=option:ntp-server,%s\n' "$BR_ROUTER"
    if [ "$BR_EGRESS" = nat ]; then
        printf 'dhcp-option=option:dns-server,%s\n' "$BR_ROUTER"
    else
        printf 'dhcp-option=6\n'
    fi
    # Reservations: on a one-address segment, the difference between "the
    # BMC has the address" and "whatever asked first has it forever".
    for l in $BR_LEASES; do
        printf 'dhcp-host=%s,infinite\n' "$l"
    done
} > "$TMPD/dnsmasq.conf"
write_file /etc/dnsmasq.d/wk-bridge.conf < "$TMPD/dnsmasq.conf"

mkdir -p "$(dirname "$LEASEFILE")"

# Our own service, not the packaged one: the distribution's dnsmasq unit
# reads /etc/dnsmasq.conf plus a conf *directory*, so `port=0` here never
# reached the daemon, which then fought the system resolver for port 53.
install_service wk-bridge-dhcp
enable_svc wk-bridge-dhcp

# --- 8. forwarding -----------------------------------------------------------
step "Forwarding"
write_file /etc/sysctl.d/99-wk-bridge.conf <<'EOF'
net.ipv4.ip_forward=1
net.ipv6.conf.all.forwarding=1
EOF
sysctl -p /etc/sysctl.d/99-wk-bridge.conf >/dev/null 2>&1 || warn "could not apply sysctl now (it applies at boot)"

# --- 9. firewall -------------------------------------------------------------
# Its own table, never `flush ruleset`: Alpine's packaged nftables service
# (`flush ruleset` at the top, plus reset/panic) would take tailscale's own
# chains out too, so it stays disabled and this table stands alone.
step "Firewall"
UPLINK=$(nmcli -t -f DEVICE,TYPE device status 2>/dev/null | awk -F: '$2 == "wifi" { print $1; exit }')
[ -n "$UPLINK" ] || UPLINK=wlan0
info "uplink $UPLINK, segment interface $BR_IF"

{
    printf '#!/usr/sbin/nft -f\n\n'
    printf '# The bridge firewall. Written by bridge/provision.sh -- do not edit here.\n'
    printf '# create-then-delete makes the file idempotent.\n'
    printf 'table inet wkbridge\ndelete table inet wkbridge\n\n'
    printf 'table inet wkbridge {\n'
    printf '    chain input {\n'
    # Default deny: one hop away is a machine's out-of-band console, or a
    # board that trusts anything on its cable.
    printf '        type filter hook input priority 0; policy drop;\n\n'
    printf '        iif "lo" accept\n'
    printf '        ct state established,related accept\n'
    # ICMPv6 is not optional: dropping neighbour discovery reads as random
    # packet loss, not a firewall.
    printf '        meta l4proto ipv6-icmp accept\n\n'
    # Access control for the tailnet lives in the tailnet policy; nftables
    # cannot express "humans and workstations, not containers".
    printf '        iifname "tailscale0" accept\n\n'
    printf '        # The uplink: only what the bridge itself must answer.\n'
    printf '        iifname "%s" tcp dport 22 accept\n' "$UPLINK"
    printf '        iifname "%s" udp dport 41641 accept\n' "$UPLINK"
    printf '        iifname "%s" udp sport 67 udp dport 68 accept\n' "$UPLINK"
    # Unicast mDNS from the uplink, so the bridge stays findable by name:
    # this file drops multicast below, and without this the phone is
    # invisible to the one mechanism that finds it when the tailnet is down.
    printf '        iifname "%s" udp dport 5353 accept\n' "$UPLINK"
    printf '        iifname "%s" icmp type { echo-request, echo-reply, destination-unreachable, time-exceeded } accept\n\n' "$UPLINK"
    printf '        # The segment is untrusted: what it needs from us and nothing else.\n'
    printf '        iifname "%s" udp dport { 67, 123 } accept\n' "$BR_IF"
    if [ "$BR_EGRESS" = nat ]; then
        printf '        iifname "%s" udp dport 53 accept\n' "$BR_IF"
        printf '        iifname "%s" tcp dport 53 accept\n' "$BR_IF"
    fi
    printf '        iifname "%s" icmp type { echo-request, echo-reply, destination-unreachable, time-exceeded } accept\n\n' "$BR_IF"
    # A home LAN delivers mDNS, IGMP and SSDP constantly; logged, they bury
    # the drops that mean something.
    printf '        meta pkttype { multicast, broadcast } counter drop\n'
    printf '        limit rate 10/minute log prefix "wkbridge-input-drop " counter drop\n'
    printf '    }\n\n'
    printf '    chain forward {\n'
    printf '        type filter hook forward priority 0; policy drop;\n\n'
    printf '        ct state established,related accept\n\n'
    printf '        # Tailnet -> segment. Tailscale SNATs it, so nothing behind\n'
    printf '        # the bridge needs a route back.\n'
    printf '        iifname "tailscale0" oifname "%s" accept\n' "$BR_IF"
    if [ "$BR_EGRESS" = nat ]; then
        printf '\n        # ... and out, because a test board has to be able to fetch things.\n'
        printf '        iifname "%s" oifname "%s" accept\n' "$BR_IF" "$UPLINK"
    fi
    printf '\n        limit rate 10/minute log prefix "wkbridge-forward-drop " counter drop\n'
    printf '        counter drop\n'
    printf '    }\n'
    if [ "$BR_EGRESS" = nat ]; then
        printf '\n    chain postrouting {\n'
        printf '        type nat hook postrouting priority srcnat; policy accept;\n'
        printf '        oifname "%s" ip saddr %s masquerade\n' "$UPLINK" "$BR_SEGMENT"
        printf '    }\n'
    fi
    printf '}\n'
} > "$TMPD/wk-bridge.nft"
write_file /etc/nftables.d/wk-bridge.nft < "$TMPD/wk-bridge.nft"

install_service wk-bridge-nftables
enable_svc wk-bridge-nftables

# The packaged service, off, named explicitly: "enabled by a later apk add"
# is exactly how the tailnet would disappear.
if [ -x /etc/init.d/nftables ]; then
    if rc-update show default 2>/dev/null | awk '{print $1}' | grep -x nftables >/dev/null 2>&1; then
        rc-update del nftables default >/dev/null 2>&1 || true
        changed "disabled the packaged nftables service (it flushes the ruleset)"
    fi
fi

# --- 10. DNS that resolves ---------------------------------------------------
# The image can arrive with an /etc/resolv.conf NetworkManager is not
# managing -- a leftover systemd-resolved stub pointing at 127.0.0.53 on an
# OpenRC system with no systemd-resolved, so every lookup gets "connection
# refused" while routing is perfect and DHCP is offering working nameservers
# the whole time. Nothing downstream survives it (chrony cannot resolve its
# pool, TLS fails, `tailscale up` cannot log in), so this runs before NTP.
step "DNS"
write_file /etc/NetworkManager/conf.d/98-wk-bridge-dns.conf <<'EOF'
# Without this an unmanaged /etc/resolv.conf shadows what DHCP offered.
[main]
dns=default
rc-manager=file
EOF

# A resolv.conf NetworkManager did not write, it will not correct; NM
# recreates it on the next reload, in the services step.
if [ -f /etc/resolv.conf ] && grep -q '127\.0\.0\.53' /etc/resolv.conf 2>/dev/null; then
    rm -f /etc/resolv.conf
    changed "removed the stale systemd-resolved stub /etc/resolv.conf"
fi

# --- 11. NTP for the segment -------------------------------------------------
# A BMC's clock resets with its configuration, and a bare board has no RTC
# at all; both then produce logs and TLS errors dated 1970.
step "NTP"
write_file /etc/chrony/conf.d/wk-bridge.conf <<EOF
allow $BR_SEGMENT
rtcsync

# Step the clock, however far out, however many times: this phone has no
# RTC, boots at 1970-01-01, and chrony's default is to *slew*, which will
# not close a fifty-six year gap in useful time -- and a clock at 1970 fails
# every TLS check, so \`tailscale up\` and apk both fail while ping and DNS
# are fine. -1, not a count: the offset recurs on every boot.
makestep 1.0 -1
EOF
# Alpine ships `confdir /etc/chrony/conf.d` (verified on 3.24), so this is
# the fallback for an image that reads only chrony.conf. Only an *active*
# line counts: matching a commented mention would skip the edit and leave
# the file above unread, silently.
if [ -f /etc/chrony/chrony.conf ] \
   && ! grep -E '^[[:space:]]*(include|confdir)[[:space:]]+/etc/chrony/conf\.d' \
          /etc/chrony/chrony.conf >/dev/null 2>&1; then
    printf '\n# wk bridge: serve the segment (bridge/provision.sh)\ninclude /etc/chrony/conf.d/*.conf\n' \
        >> /etc/chrony/chrony.conf
    changed "chrony.conf includes conf.d"
fi
CHRONY_SVC=$(svc chronyd chrony) && enable_svc "$CHRONY_SVC" || warn "chrony has no init script here"

# --- 12. ssh -----------------------------------------------------------------
# Key-only. The account password is left alone deliberately: it is the
# console login on the phone's own screen, the last recovery path, and it
# cannot be used over the network once this is in place.
step "SSH"
write_file /etc/ssh/sshd_config.d/10-wk-bridge.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
PermitEmptyPasswords no
EOF
# Alpine ships `Include /etc/ssh/sshd_config.d/*.conf` at the top already
# (verified on 3.24), so this is the fallback. Prepends rather than appends:
# OpenSSH takes the *first* value for a keyword, so at the bottom the stock
# defaults would win.
if ! grep '^Include /etc/ssh/sshd_config.d/' /etc/ssh/sshd_config >/dev/null 2>&1; then
    tmp=$(mktemp)
    printf '# wk bridge: first-match-wins, so this include is first.\nInclude /etc/ssh/sshd_config.d/*.conf\n\n' > "$tmp"
    cat /etc/ssh/sshd_config >> "$tmp"
    cat "$tmp" > /etc/ssh/sshd_config
    rm -f "$tmp"
    changed "/etc/ssh/sshd_config includes sshd_config.d"
fi
enable_svc sshd
if sshd -t 2>/dev/null; then
    rc-service sshd reload >/dev/null 2>&1 || rc-service sshd restart >/dev/null 2>&1 || true
else
    warn "sshd -t rejects the configuration -- NOT reloading it; fix this before rebooting"
fi

# --- 13. flash and memory ----------------------------------------------------
# The phone's eMMC cannot be replaced without opening the device; swap and
# verbose logs are what wear it out on a machine up for months.
step "Flash and memory"
if [ -d /etc/logrotate.d ]; then
    write_file /etc/logrotate.d/wk-bridge <<'EOF'
/var/log/messages {
    size 8M
    rotate 3
    compress
    missingok
    notifempty
    copytruncate
}
EOF
fi

# /proc/swaps, not `swapon --show`: that flag is util-linux's, and busybox
# would silently report "no swap" on a phone swapping to eMMC.
nonzram=$(awk 'NR > 1 && $1 !~ /zram/ { c++ } END { print c + 0 }' /proc/swaps 2>/dev/null)
if [ "${nonzram:-0}" -gt 0 ]; then
    warn "swap is on something other than zram -- that is eMMC wear on a device"
    warn "  that must last years. Left alone rather than switched off under you:"
    warn "  'swapoff' it and remove it from /etc/fstab when you are at the phone."
fi
if [ -x /etc/init.d/zram-init ]; then
    enable_svc zram-init
elif [ "${nonzram:-0}" -eq 0 ] && [ "$(awk 'NR > 1 { c++ } END { print c + 0 }' /proc/swaps 2>/dev/null)" = 0 ]; then
    warn "no swap at all and no zram-init -- a memory spike will OOM the router"
fi

# --- 14. the hardware watchdog -----------------------------------------------
# The failure this catches is the one nothing else can: a kernel that stops
# scheduling. Everything else here recovers a service or a link, assuming
# the machine is still running.
step "Watchdog"
# No /dev/watchdog may mean no driver, or a driver module nobody loaded --
# indistinguishable from here, so the driver is asked for first.
# wk-bridge-watchdog-load is a separate script since the watchdog service
# runs it too: boot must not depend on udev's coldplug having loaded it. It
# identifies the driver by the device's own modalias, no module name here.
/usr/local/sbin/wk-bridge-watchdog-load || true

if [ -e /dev/watchdog ]; then
    install_service wk-bridge-watchdog
    enable_svc wk-bridge-watchdog
else
    warn "no /dev/watchdog on this device -- a kernel hang needs a person"
fi

# --- 15. the network watchdog ------------------------------------------------
step "Network watchdog"
install_service wk-bridge-netwatch
enable_svc wk-bridge-netwatch

# --- 16. the camera ----------------------------------------------------------
step "Camera"
if [ "$BR_CAMERA" = off ]; then
    if [ -x /etc/init.d/wk-bridge-camera ]; then
        rc-service wk-bridge-camera stop >/dev/null 2>&1 || true
        rc-update del wk-bridge-camera default >/dev/null 2>&1 || true
        rm -f /etc/init.d/wk-bridge-camera
        changed "removed the camera service (BR_CAMERA=off)"
    else
        info "off for this bridge"
    fi
else
    install_service wk-bridge-camera
    enable_svc wk-bridge-camera
    info "the kill switch is the access control: switched off, there is no"
    info "capture device and the service idles."
fi

# --- 17. battery charge limit -------------------------------------------------
# A phone left on a charger at 100% every day swells its cell over months;
# there is no kill switch for this the way there is for the radios, so
# capping the charge in software is the one thing worth doing. One code path
# for both phones: charge_control_end_threshold is a standard Linux
# power_supply attribute, so the node exposing it is found by globbing for
# the attribute itself rather than naming a chip (axp20x, bq2589x, ...) --
# BR_BATTERY pins a specific node only if a phone ever exposes more than one.
#
# The threshold does not survive a reboot on its own (the driver resets it to
# the hardware default at every power-on), so a service applies it again at
# every boot -- the same shape as wk-bridge-usb-host, which re-asserts a
# setting the kernel also does not remember.
step "Battery charge limit (${BR_BATTERY_LIMIT}%)"
BATT_NODE=""
if [ -n "$BR_BATTERY" ]; then
    [ -f "/sys/class/power_supply/$BR_BATTERY/charge_control_end_threshold" ] \
        && BATT_NODE="/sys/class/power_supply/$BR_BATTERY"
else
    for f in /sys/class/power_supply/*/charge_control_end_threshold; do
        [ -f "$f" ] || continue
        BATT_NODE=$(dirname "$f")
        break
    done
fi

if [ -z "$BATT_NODE" ]; then
    warn "no power_supply node here exposes charge_control_end_threshold --"
    warn "  this kernel/driver has no charge limit to set. (BR_BATTERY in the"
    warn "  conf pins a specific node if this device ever exposes more than one.)"
else
    write_file /etc/wk-bridge-battery.conf <<EOF
# Written by bridge/provision.sh; applied at every boot by the
# wk-bridge-battery service, since the threshold itself does not survive one.
node=$BATT_NODE
limit=$BR_BATTERY_LIMIT
EOF
    install_service wk-bridge-battery
    enable_svc wk-bridge-battery
    /usr/local/sbin/wk-bridge-battery || true

    # Verified by reading the node back, not assumed from the write's exit
    # status: a sysfs write can succeed against a value the driver clamps.
    cur=$(cat "$BATT_NODE/charge_control_end_threshold" 2>/dev/null || echo '?')
    if [ "$cur" = "$BR_BATTERY_LIMIT" ]; then
        info "$BATT_NODE now caps at ${cur}%"
    else
        warn "$BATT_NODE reads ${cur}%, not ${BR_BATTERY_LIMIT}% -- the write did not take"
    fi
fi

# --- 18. tailscale -----------------------------------------------------------
step "Tailscale"
TS_SVC=$(svc tailscale tailscaled) || die "the tailscale package has no init script here"
enable_svc "$TS_SVC"
rc-service "$TS_SVC" status >/dev/null 2>&1 || rc-service "$TS_SVC" start >/dev/null 2>&1 || true

# Wait for the daemon's socket rather than sleeping a guessed number of seconds.
i=0
while [ "$i" -lt 30 ]; do
    tailscale status >/dev/null 2>&1 && break
    tailscale status 2>&1 | grep -iE 'logged out|NeedsLogin' >/dev/null 2>&1 && break
    i=$((i + 1)); sleep 1
done

TS_STATE=$(tailscale status --json --peers=false 2>/dev/null | jq -r '.BackendState // "unknown"')
TS_TAGS=$(tailscale status --json --peers=false 2>/dev/null | jq -rc '.Self.Tags // "none"')

if [ -n "$BR_NO_TAILNET" ] && { [ "$TS_STATE" != Running ] || [ "$TS_TAGS" = none ]; }; then
    info "daemon enabled and running; not joining the tailnet (BR_NO_TAILNET)"
    info "everything else in this role is applied. To finish:"
    info "    wk bridge tailnet $BR_NAME"
elif [ "$TS_STATE" = Running ] && [ "$TS_TAGS" != none ]; then
    # Already tagged: re-assert what can change without re-authenticating.
    tailscale set --advertise-routes="$BR_SEGMENT" --accept-dns=false --ssh=true >/dev/null 2>&1 \
        || warn "could not re-assert the advertised route"
    rm -f "$AUTHKEY_FILE"
    info "already on the tailnet, tags $TS_TAGS"

    # `autoApprovers` is evaluated only when a node *advertises* a route, and
    # the advertisement above is a no-op when the value has not changed (never,
    # on a re-run) -- so a route first advertised before the policy existed
    # stays unapproved forever, healthy-looking with nothing behind it
    # reachable. Withdrawing and re-advertising forces the evaluation,
    # conditional on the route not already being primary so a working bridge
    # does not drop the segment for no reason.
    if [ "$(tailscale status --json 2>/dev/null | jq -rc '.Self.PrimaryRoutes // "none"')" = none ]; then
        info "the route is advertised but not approved -- re-advertising so"
        info "  autoApprovers is evaluated (a paste alone does not trigger it)"
        tailscale set --advertise-routes= >/dev/null 2>&1 || true
        sleep 2
        tailscale set --advertise-routes="$BR_SEGMENT" >/dev/null 2>&1 || true
        sleep 5
        if [ "$(tailscale status --json 2>/dev/null | jq -rc '.Self.PrimaryRoutes // "none"')" = none ]; then
            warn "still not approved. Either the policy is not in place yet, or"
            warn "  its autoApprovers names a tag this node does not hold"
            warn "  ($TS_TAGS). 'wk bridge setup' prints the stanza to paste."
        else
            info "approved: $BR_SEGMENT is live"
            CHANGES=$((CHANGES + 1))
        fi
    fi
elif [ -n "$BR_AUTHKEY" ]; then
    # A key handed over in the environment goes into the same file, so there
    # is one code path into `tailscale up`, off the command line.
    if [ ! -f "$AUTHKEY_FILE" ]; then
        ( umask 077; printf '%s\n' "$BR_AUTHKEY" > "$AUTHKEY_FILE" )
    fi
    # --advertise-tags is only settable at login and makes the node
    # non-expiring; untagged, a bridge goes dark after 180 days.
    # --accept-dns=false: a subnet router keeps its own resolver.
    # --accept-routes=false: it publishes routes, does not consume them.
    # --ssh: the phone's own screen should not be the only way back in.
    # `--auth-key file:`, not `--authkey=<key>`: the latter puts the
    # credential in the process table, readable from /proc by anyone with a
    # shell (needs tailscale >= 1.38; a rejected key here means the version).
    tailscale up \
        --auth-key="file:$AUTHKEY_FILE" \
        --advertise-routes="$BR_SEGMENT" \
        --advertise-tags="$BR_TAG" \
        --hostname="$BR_HOSTNAME" \
        --accept-dns=false \
        --accept-routes=false \
        --ssh=true \
        || { rm -f "$AUTHKEY_FILE"; die "tailscale up failed"; }
    rm -f "$AUTHKEY_FILE"
    changed "joined the tailnet as $BR_HOSTNAME ($BR_TAG)"
else
    warn "this node is not on the tailnet and no auth key was given."
    warn "  Re-run 'wk bridge setup $BR_NAME' with a tagged key ($BR_TAG) to join."
fi

# --- 19. start everything ----------------------------------------------------
# Dependency cache rebuilt first: a service OpenRC cannot see in its tree
# starts neither now nor at the next boot.
step "Services"
refresh_deptree
for s in wk-bridge-nftables wk-bridge-usb-host wk-bridge-dhcp wk-bridge-netwatch wk-bridge-watchdog wk-bridge-camera wk-bridge-battery; do
    [ -x "/etc/init.d/$s" ] || continue
    if rc-service "$s" status >/dev/null 2>&1; then
        # Restart what this run may have rewritten, so re-provisioning takes
        # effect without a reboot (watchdog and netwatch read config at start).
        rc-service "$s" restart >/dev/null 2>&1 || warn "$s did not restart"
    else
        rc-service "$s" start >/dev/null 2>&1 || warn "$s did not start"
    fi
done
[ -n "${CHRONY_SVC:-}" ] && { rc-service "$CHRONY_SVC" restart >/dev/null 2>&1 || true; }

step "Done -- $CHANGES change(s)"
info "health: wk-bridge-healthcheck   (or 'wk bridge status $BR_NAME' from the workstation)"
