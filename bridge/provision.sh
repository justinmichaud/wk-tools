#!/bin/sh
# Provision a postmarketOS phone as a tailnet bridge. Runs on the phone, as
# root, from `wk bridge setup` -- which copies this whole directory to
# /usr/local/lib/wk-bridge first, so the spec lives in the repository and the
# phone holds only a copy.
#
#   house WiFi --wlan0-- [ phone ] --lan0-- the segment behind it
#                            |
#                    tailscaled advertises $BR_SEGMENT
#
# The rule that shapes everything: this file is the only source of truth, and
# it is idempotent. Its predecessor lived *on* the phone -- one hand-built
# Librem 5, a 31 KB script, and a README saying "this directory is the only
# copy; moose was wiped; consider pushing it to git". Re-provisioning a lost
# bridge meant recovering that tarball first. So: edit this, re-run
# `wk bridge setup`, never hand-edit /etc on the device.
#
# POSIX sh, no bash: pmOS is Alpine and /bin/sh is busybox ash. Nothing here
# sources lib/common.sh either -- the phone gets this directory, not the repo.
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
#   BR_AUTHKEY    a tagged tailscale auth key, only when the node needs one
set -eu

: "${BR_NAME:?BR_NAME is not set (run this through 'wk bridge setup')}"
: "${BR_HOSTNAME:?BR_HOSTNAME is not set}"
: "${BR_SEGMENT:?BR_SEGMENT is not set}"
: "${BR_ROUTER:?BR_ROUTER is not set}"
BR_DEVICE="${BR_DEVICE:-unknown}"
BR_TAG="${BR_TAG:-tag:bridge}"
BR_IF="${BR_IF:-lan0}"
BR_POOL="${BR_POOL:-}"
BR_LEASES="${BR_LEASES:-}"
BR_EGRESS="${BR_EGRESS:-none}"
BR_CAMERA="${BR_CAMERA:-off}"
BR_LAN_MAC="${BR_LAN_MAC:-}"

# Everything except joining the tailnet. The tailnet step is the only one that
# needs a credential a person has to fetch, and it is the only one that is not
# idempotent in the cheap sense -- `tailscale up --advertise-tags` can only be
# set at login. So it is separable: the whole role can be applied and verified
# without it, which is what makes the rest of this script something a run can
# iterate on. Set BR_NO_TAILNET=1 to stop short; the daemon is still enabled and
# started, so `wk bridge tailnet <name>` afterwards is one command.
BR_NO_TAILNET="${BR_NO_TAILNET:-}"

# The auth key arrives in a 0600 file on tmpfs rather than in the environment
# or on a command line: argv is world readable in /proc, so a
# `tailscale up --authkey=...` is a credential handed to anyone with a shell on
# the phone. Read once, deleted after use, gone on reboot either way.
BR_AUTHKEY="${BR_AUTHKEY:-}"
AUTHKEY_FILE=/run/wk-bridge-authkey
if [ -z "$BR_AUTHKEY" ] && [ -f "$AUTHKEY_FILE" ]; then
    BR_AUTHKEY=$(cat "$AUTHKEY_FILE")
fi

HERE=$(cd "$(dirname "$0")" && pwd)
LEASEFILE=/var/lib/misc/dnsmasq.leases
CHANGES=0
# Generated files are built here and then handed to write_file by redirect.
# Not by pipeline: a shell function on the right of a `|` runs in a subshell,
# so its change count -- the one number that says whether this run did anything
# -- would be discarded exactly for the two biggest files.
TMPD=$(mktemp -d)
trap 'rm -rf "$TMPD"' EXIT INT TERM

step()    { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info()    { printf '    %s\n' "$*"; }
warn()    { printf '\033[1;33m    WARN: %s\033[0m\n' "$*"; }
die()     { printf '\033[1;31m    ERROR: %s\033[0m\n' "$*"; exit 1; }
changed() { CHANGES=$((CHANGES + 1)); printf '\033[1;32m    changed:\033[0m %s\n' "$*"; }

# Write a file only when its content differs, and say which. The count at the
# end is what makes a re-run readable: "0 changes" is the answer that says the
# phone already matches the repository.
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

# The name a service actually has here. Packagings disagree (networkmanager vs
# NetworkManager, tailscale vs tailscaled) and a `rc-service` aimed at a name
# that does not exist fails in a way that reads like the service is broken.
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

# A service file, from the repository copy to /etc/init.d, only when it differs.
#
# Real files under bridge/init.d rather than heredocs in here: they are static
# (everything variable about a bridge is in /etc/wk-bridge.conf, which the
# scripts read at runtime), and a service file you can read, grep and review in
# a diff is worth more than one assembled at provision time.
install_service() {
    local src
    src="$HERE/init.d/$1"
    [ -f "$src" ] || die "bridge/init.d/$1 is missing from the copy on this phone"
    if ! cmp -s "$src" "/etc/init.d/$1"; then
        install -m 0755 "$src" "/etc/init.d/$1"
        changed "/etc/init.d/$1"
    fi
}

# Add to a runlevel only when it is not there. rc-update is idempotent by
# itself, but silent, and "what did this change" is the question a re-run has
# to answer.
enable_svc() {
    if rc-update show default 2>/dev/null | awk '{print $1}' | grep -x "$1" >/dev/null 2>&1; then
        return 0
    fi
    rc-update add "$1" default >/dev/null 2>&1 || { warn "could not enable $1"; return 0; }
    changed "enabled $1"
}

# Force OpenRC to re-read the init scripts.
#
# OpenRC caches the dependency tree and regenerates it only when a script is
# *newer* than the cache. This phone has no RTC, so it boots at 1970 and every
# file this script writes is stamped 1970 -- and then chrony steps the clock
# forward to now, at which point the cache looks decades newer than the scripts
# and OpenRC never looks at them again. The services are enabled, the symlinks
# are right, `rc-update show` lists them, and `rc-status` does not: they are
# invisible to the dependency tree and so they never start at boot.
#
# The result is a phone that provisions cleanly, passes its health check, and
# comes back from a reboot with all four bridge services stopped. `mtime` is not a safe clock on a device whose
# clock is not safe either, so the cache is rebuilt explicitly rather than left
# to a timestamp comparison.
refresh_deptree() {
    rc-update -u >/dev/null 2>&1 \
        || warn "could not refresh OpenRC's dependency cache; services may not start at boot"
}

# --- 0. is this the machine this script is for? ------------------------------
#
# Refusing beats half-applying. The Librem 5 still runs the hand-built
# PureOS/systemd configuration this role replaces, and a run of this script
# against it would install Alpine service files onto a Debian derivative,
# succeed at about a third of them, and leave a bridge that is neither.
[ "$(id -u)" -eq 0 ] || die "run as root"
command -v apk >/dev/null 2>&1 || die "this is not an Alpine/postmarketOS system (no apk).
    The bridge role is pmOS-only on purpose: one provisioner for both phones.
    Reflash the device with postmarketOS first -- 'wk help bridge'."
command -v rc-update >/dev/null 2>&1 || die "no OpenRC here (rc-update is missing).
    A pmOS image built with systemd is not supported by this role."

step "Provisioning $BR_NAME ($BR_DEVICE) on $(cat /etc/hostname 2>/dev/null)"
info "segment $BR_SEGMENT via $BR_IF, router $BR_ROUTER, egress $BR_EGRESS"

# Two lists, because failure means different things. Without the required set
# there is no bridge at all; the optional set makes it survive longer unattended
# and its absence is a warning, not the end of the run -- an apk index that has
# moved on must not be able to stop a re-provision of a device that is already
# working.
REQUIRED="tailscale dnsmasq nftables chrony jq iw ethtool openssh networkmanager"
OPTIONAL="logrotate zram-init v4l-utils"
[ "$BR_CAMERA" = off ] || OPTIONAL="$OPTIONAL ffmpeg"

# --- 1. stop it disappearing -------------------------------------------------
#
# This goes first, before anything that takes time, and the ordering is the
# whole point. Later in the sequence -- after the package check, say -- an
# unprovisioned phone suspends on idle, so a run loses the device it is
# provisioning partway through and leaves a half-applied role behind: the phone
# answers, work starts, phosh idles
# it off the network, and every later step failed against a host that was no
# longer there.
#
# Applying it first costs one file write and makes the rest of the run possible.
# The GUI stays. It is the recovery path: if WiFi and ssh are both gone, the
# phone's own screen and touchscreen are what is left, and masking phosh to
# save a few tens of megabytes trades the last way in for nothing that matters
# on a device whose job is to be reachable.
#
# What must go is every reason it might sleep or act on a button. A bridge that
# suspends is a bridge that is down.
step "Never sleep"
if [ -d /etc/elogind ]; then
    write_file /etc/elogind/logind.conf.d/10-wk-bridge.conf <<'EOF'
# A bridge must not suspend, and must not act on its own buttons: the power
# button on a device wedged in a drawer is as likely to be a pocket as a person.
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

# Kernel-mode tailscale needs /dev/net/tun, and a subnet router needs
# kernel mode: userspace networking forwards nothing for anyone else, which is
# the entire job here. Said now rather than discovered as "the bridge is up and
# the segment is unreachable".
[ -c /dev/net/tun ] || warn "/dev/net/tun is missing -- tailscale cannot route a
    subnet without it. Check that the kernel has tun (modprobe tun), or this
    bridge will come up healthy-looking and forward nothing."

# --- 3. the scripts, and the fact file they all read -------------------------
#
# One file on the device says what this bridge is, and every on-device script
# reads it instead of being generated with the values baked in. That is what
# makes `wk bridge status` truthful after a conf change that has not been
# applied yet: the scripts describe the running configuration, not the one in
# the repository.
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
# What this bridge is. Written by bridge/provision.sh -- edit the conf in
# wk-tools and re-run 'wk bridge setup $BR_NAME' instead of editing this.
#
# Sourced by /bin/sh, so keys are lowercase and values are unquoted-safe.
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
# 127.0.1.1, not 127.0.0.1: sudo and anything else resolving its own name wants
# an answer, and the loopback entry is the one that is always there.
if ! grep "$BR_HOSTNAME" /etc/hosts >/dev/null 2>&1; then
    printf '127.0.1.1\t%s\n' "$BR_HOSTNAME" >> /etc/hosts
    changed "/etc/hosts entry for $BR_HOSTNAME"
fi

# --- 5. the uplink -----------------------------------------------------------
step "WiFi"
write_file /etc/NetworkManager/conf.d/99-wk-bridge.conf <<'EOF'
# Power save is the single most reliable way to turn a working uplink into one
# that answers every third ping, and both phones' parts do it.
[connection]
wifi.powersave=2

# A stable MAC keeps the DHCP lease, and any AP-side reservation, stable. A
# bridge that appears at a new address every association is a bridge you cannot
# find from the LAN when the tailnet is the thing that is broken.
[device]
wifi.scan-rand-mac-address=no
EOF

# UDP GRO forwarding, re-applied on every interface that comes up. It is worth
# roughly a factor on tailscale forwarding throughput and it is not sticky
# across a link bounce, which is why it is a dispatcher hook and not a one-shot.
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
# The adapter is found by being on USB and not being WiFi. Autodetected rather
# than declared, because the answer is a property of the dock in use and a
# wrong MAC in a conf file is a bridge that comes up with no segment at all.
if [ -z "$BR_LAN_MAC" ]; then
    for n in /sys/class/net/*; do
        d=$(basename "$n")
        # usb* is excluded because on a phone that is the USB *gadget*
        # interface -- what the port presents while it is in device mode, which
        # is the state this whole section exists to get out of. A real adapter
        # in host mode enumerates as eth0 or enp*, or as $BR_IF once the udev
        # rule below has been applied.
        case "$d" in lo|wlan*|tailscale*|dummy*|usb*) continue ;; esac
        [ -e "$n/device/subsystem" ] || continue
        case "$(readlink -f "$n/device/subsystem")" in
            */usb)
                # The *permanent* address, and that distinction is the whole
                # reason this is three lines instead of one.
                #
                # pmOS ships NetworkManager with
                # `ethernet.cloned-mac-address=stable`
                # (/usr/lib/NetworkManager/conf.d/50-random-mac.conf), so a
                # managed interface is running on a synthetic MAC within seconds
                # of appearing -- observed here as lan0 answering to
                # ae:bf:97:99:88:46 while `ethtool -P` said 00:00:00:00:03:88.
                #
                # The udev rule below matches at ACTION=="add", when the
                # interface still has its hardware address. So reading the
                # *current* address writes a rule that matches nothing, and the
                # rename silently stops working at the next boot -- while
                # everything looks perfect until then, because the interface was
                # already renamed by the previous, correct rule. Caught by
                # re-running setup and noticing the rule had changed.
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
    # A udev rule, not a systemd .link file: pmOS has eudev and no
    # systemd-networkd. Renaming at all is what makes every other file here
    # device-independent -- dnsmasq, nftables and the address all name $BR_IF,
    # so swapping the dock for one with a different chipset changes this one
    # line and nothing else.
    write_file /etc/udev/rules.d/70-wk-bridge-net.rules <<EOF
# The bridge's downstream NIC, by MAC. Written by bridge/provision.sh.
SUBSYSTEM=="net", ACTION=="add", ATTR{address}=="$(printf '%s' "$BR_LAN_MAC" | tr 'A-Z' 'a-z')", NAME="$BR_IF"
EOF

    # NetworkManager owns the devices on a pmOS image, so the address is a
    # keyfile connection rather than an /etc/network/interfaces stanza -- two
    # things configuring one interface is how an address ends up appearing and
    # disappearing on its own. never-default and ignore-auto-dns because this
    # leg is a segment, not a way out: a default route learned here would send
    # the phone's own traffic at a BMC.
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
# `ethernet.cloned-mac-address=stable`. Two reasons, and neither is taste:
# the udev rule above matches the permanent MAC, so an interface running on a
# synthetic one is an interface whose identity disagrees with the rule that
# named it; and a subnet router's segment address is a thing other machines key
# on -- an ARP entry, a reservation on the far side -- which a per-connection
# hash quietly makes wrong. The phone's *uplink* randomization is a privacy
# feature and is deliberately left alone; this pins one interface.
[ethernet]
cloned-mac-address=permanent
EOF
    nmcli connection reload >/dev/null 2>&1 || true

    # A udev NAME= applies when the device *appears*, and an interface that is
    # already up cannot be renamed underneath itself. So the first run against a
    # plugged-in dock leaves the adapter under its kernel name, everything named
    # $BR_IF matches nothing, and the bridge looks provisioned but dead.
    #
    # Saying so and stopping -- "re-plug the dock, or reboot the phone, then
    # re-run this" -- asks for a hand at the phone for something the kernel will
    # do on request. A downed link can be renamed, so it is renamed here
    # and the udev rule above is what makes it survive the next boot. The whole
    # point of a bridge is being reachable without going to it.
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
            # NetworkManager will not let go of a device it is managing, and a
            # rename under it is refused; handing the device back afterwards is
            # what lets the keyfile above claim it under its new name.
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
# dnsmasq wants a netmask in dhcp-range, and the segment is written as a prefix
# length -- the two spellings of the same fact, so it is derived rather than
# declared twice in the conf.
case "${BR_SEGMENT#*/}" in
    16) NETMASK=255.255.0.0 ;;
    24) NETMASK=255.255.255.0 ;;
    *)  NETMASK=255.255.255.0
        warn "prefix /${BR_SEGMENT#*/} is not one this understands -- assuming $NETMASK" ;;
esac
{
    printf '# DHCP for the bridge segment. Written by bridge/provision.sh.\n'
    if [ "$BR_EGRESS" = nat ]; then
        # DNS on, because a device that may reach the internet needs to resolve
        # names to use it -- and dnsmasq forwarding to whatever the phone's own
        # resolver learned is the whole configuration.
        printf 'domain-needed\nbogus-priv\n'
    else
        # DNS off entirely (port=0) and not advertised (option 6 empty). A
        # segment with no egress has nothing to resolve, and a resolver is a
        # service that can be attacked from the least trusted device here.
        printf 'port=0\n'
    fi
    printf 'interface=%s\n' "$BR_IF"
    # bind-dynamic, not bind-interfaces: the downstream NIC comes and goes with
    # the dock, and bind-interfaces would have dnsmasq fail to start whenever
    # it is out.
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
    # Reservations. On a one-address segment this is the difference between
    # "the BMC has the address" and "whatever asked first has it forever" --
    # which, once, was the host's own onboard NIC.
    for l in $BR_LEASES; do
        printf 'dhcp-host=%s,infinite\n' "$l"
    done
} > "$TMPD/dnsmasq.conf"
write_file /etc/dnsmasq.d/wk-bridge.conf < "$TMPD/dnsmasq.conf"

mkdir -p "$(dirname "$LEASEFILE")"

# Our own service rather than the packaged one, and the reason is a scar: the
# distribution's dnsmasq unit reads /etc/dnsmasq.conf and a conf *directory*,
# so `port=0` in our file never reached the daemon, which then fought the
# system resolver for port 53. One daemon, one conf file, named explicitly.
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
#
# Its own table, and never `flush ruleset`. Tailscale keeps its ts-input and
# ts-forward chains in the same namespace, and Alpine's packaged nftables
# service would take them out with it: /etc/nftables.nft begins with
# `flush ruleset` (line 6 on 3.24), and the service's own `reset` and `panic`
# commands flush as well. So that service stays disabled and this table stands
# alone, loaded by a service of ours that only ever adds and deletes its own.
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
    # Default deny, because of what is one hop away. On the BMC bridge that is
    # a machine's out-of-band console; on the generic bridge it is a board that
    # trusts anything on its cable.
    printf '        type filter hook input priority 0; policy drop;\n\n'
    printf '        iif "lo" accept\n'
    printf '        ct state established,related accept\n'
    # ICMPv6 is not optional: dropping neighbour discovery breaks IPv6 in ways
    # that look like random packet loss rather than like a firewall.
    printf '        meta l4proto ipv6-icmp accept\n\n'
    # Access control for the tailnet lives in the tailnet policy, which can
    # express "humans and workstations, not containers"; nftables cannot.
    printf '        iifname "tailscale0" accept\n\n'
    printf '        # The uplink: only what the bridge itself must answer.\n'
    printf '        iifname "%s" tcp dport 22 accept\n' "$UPLINK"
    printf '        iifname "%s" udp dport 41641 accept\n' "$UPLINK"
    printf '        iifname "%s" udp sport 67 udp dport 68 accept\n' "$UPLINK"
    # Unicast mDNS from the uplink, so the bridge stays findable by name on the
    # LAN. Not decoration: this file drops multicast a few lines down, and
    # without this the phone becomes invisible to the one mechanism that finds
    # it when the tailnet is not up -- which is exactly the state a bridge is in
    # while it is being provisioned, and the state it is in when the thing that
    # is broken *is* the tailnet. Found by provisioning a phone and then being
    # unable to locate it, having just applied the rules that hid it.
    printf '        iifname "%s" udp dport 5353 accept\n' "$UPLINK"
    printf '        iifname "%s" icmp type { echo-request, echo-reply, destination-unreachable, time-exceeded } accept\n\n' "$UPLINK"
    printf '        # The segment is untrusted: what it needs from us and nothing else.\n'
    printf '        iifname "%s" udp dport { 67, 123 } accept\n' "$BR_IF"
    if [ "$BR_EGRESS" = nat ]; then
        printf '        iifname "%s" udp dport 53 accept\n' "$BR_IF"
        printf '        iifname "%s" tcp dport 53 accept\n' "$BR_IF"
    fi
    printf '        iifname "%s" icmp type { echo-request, echo-reply, destination-unreachable, time-exceeded } accept\n\n' "$BR_IF"
    # A home LAN delivers mDNS, IGMP and SSDP constantly. Logged, they bury the
    # drops that mean something.
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

# The packaged service, off. Named explicitly rather than left alone, because
# "enabled by a later apk add" is exactly how the tailnet would disappear.
if [ -x /etc/init.d/nftables ]; then
    if rc-update show default 2>/dev/null | awk '{print $1}' | grep -x nftables >/dev/null 2>&1; then
        rc-update del nftables default >/dev/null 2>&1 || true
        changed "disabled the packaged nftables service (it flushes the ruleset)"
    fi
fi

# --- 10. DNS that resolves ---------------------------------------------------
#
# The image can arrive with an /etc/resolv.conf that NetworkManager is not
# managing -- a plain file left over from whatever built the rootfs. This one
# held a systemd-resolved stub pointing at 127.0.0.53, on an OpenRC system with
# no systemd-resolved on it at all, so every lookup got "connection refused"
# while routing was perfect. That reads as "no internet" and is not: DHCP was
# offering three working nameservers the whole time.
#
# Nothing downstream survives it. chrony cannot resolve its pool, so the clock
# stays at 1970; TLS then fails everywhere; `tailscale up` cannot log in. So this
# runs before NTP, and hands resolv.conf to NetworkManager, which knows the
# servers DHCP offered.
step "DNS"
write_file /etc/NetworkManager/conf.d/98-wk-bridge-dns.conf <<'EOF'
# NetworkManager owns /etc/resolv.conf. Without this an unmanaged file shadows
# the servers DHCP offered, and every lookup fails while ping works.
[main]
dns=default
rc-manager=file
EOF

# A resolv.conf NetworkManager did not write is one it will not correct, so it
# goes. NM recreates it on the next reload; the reload is in the services step.
if [ -f /etc/resolv.conf ] && grep -q '127\.0\.0\.53' /etc/resolv.conf 2>/dev/null; then
    rm -f /etc/resolv.conf
    changed "removed the stale systemd-resolved stub /etc/resolv.conf"
fi

# --- 11. NTP for the segment -------------------------------------------------
#
# Because a BMC's clock resets with its configuration, and a bare board has no
# RTC at all. Both then produce logs and TLS errors dated 1970, which is a day
# of debugging the wrong thing.
step "NTP"
write_file /etc/chrony/conf.d/wk-bridge.conf <<EOF
allow $BR_SEGMENT
rtcsync

# Step the clock, however far out it is, however many times.
#
# This phone has no working RTC: it boots at 1970-01-01, and chrony's default is
# to *slew* rather than step, which will not close a fifty-six year gap in any
# useful time. A clock at 1970 fails every TLS certificate check there is, so
# nothing that matters works -- \`tailscale up\` cannot reach its coordination
# server, apk cannot fetch, and the failure reads as "no internet" while ping
# and DNS are both fine.
#
# -1 rather than a count: the offset recurs on every boot, so a limit of "the
# first few updates" would fix the first boot and no other.
makestep 1.0 -1
EOF
# Alpine already ships `confdir /etc/chrony/conf.d` (verified on 3.24), so this
# edit normally does not happen at all -- it is the fallback for an image that
# reads only chrony.conf. Both spellings count, and only an *active* line does:
# matching a commented mention would skip the edit and leave the file above
# unread, serving the segment no time at all, silently.
if [ -f /etc/chrony/chrony.conf ] \
   && ! grep -E '^[[:space:]]*(include|confdir)[[:space:]]+/etc/chrony/conf\.d' \
          /etc/chrony/chrony.conf >/dev/null 2>&1; then
    printf '\n# wk bridge: serve the segment (bridge/provision.sh)\ninclude /etc/chrony/conf.d/*.conf\n' \
        >> /etc/chrony/chrony.conf
    changed "chrony.conf includes conf.d"
fi
CHRONY_SVC=$(svc chronyd chrony) && enable_svc "$CHRONY_SVC" || warn "chrony has no init script here"

# --- 12. ssh -----------------------------------------------------------------
#
# Key-only. The account password is left alone deliberately: it is the console
# login on the phone's own screen, which is the last recovery path, and it
# cannot be used over the network once this is in place.
step "SSH"
write_file /etc/ssh/sshd_config.d/10-wk-bridge.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
PermitEmptyPasswords no
EOF
# Alpine ships `Include /etc/ssh/sshd_config.d/*.conf` at the top of
# sshd_config already (verified on 3.24, line 15, above every directive), so
# this is the fallback for an image that does not. It prepends rather than
# appends because OpenSSH takes the *first* value it sees for a keyword: at the
# bottom, the stock defaults would win and the drop-in above would do nothing.
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
#
# The phone's eMMC is the only storage it has and there is no way to replace it
# without opening the device. Swap on it and verbose logs to it are the two
# things that wear it out on a machine that is up for months.
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

# /proc/swaps rather than `swapon --show`: that spelling is util-linux's and
# this is busybox, where the flag does not exist and the check would silently
# report "no swap" on a phone that is swapping to eMMC.
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
#
# The failure this catches is the one nothing else can: a kernel that stops
# scheduling. Everything else here recovers a service or a link, and all of it
# assumes the machine is still running.
step "Watchdog"
# No /dev/watchdog may mean a kernel without the driver, or a kernel whose
# driver is a module nobody has loaded -- and those look identical from here
# while having completely different answers. So the driver is asked for before
# the verdict is reached.
#
# wk-bridge-watchdog-load is that attempt, and it is a separate script because
# the watchdog service runs it too: a boot must not depend on udev's coldplug
# having autoloaded the driver. It identifies the driver by the device's own
# modalias, so no module name appears anywhere in this role.
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

# --- 17. tailscale -----------------------------------------------------------
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
    # Already a member, already tagged: re-assert what can be changed without
    # re-authenticating, and leave the identity alone.
    tailscale set --advertise-routes="$BR_SEGMENT" --accept-dns=false --ssh=true >/dev/null 2>&1 \
        || warn "could not re-assert the advertised route"
    rm -f "$AUTHKEY_FILE"
    info "already on the tailnet, tags $TS_TAGS"

    # And the half that `set` alone cannot do, which is the whole reason this
    # block is more than one line.
    #
    # `autoApprovers` is evaluated when a node *advertises* a route. The
    # advertisement above is a no-op whenever the value has not changed -- which
    # it never has on a re-run -- so a route first advertised before the policy
    # existed stays unapproved no matter how many times this runs, and the
    # bridge sits there healthy with nothing behind it reachable. That is
    # exactly what a pasted policy produces: every check green except the one
    # that matters, and `PrimaryRoutes` null.
    #
    # Withdrawing and re-advertising forces the evaluation. Conditional on the
    # route not already being primary, because on a working bridge this would
    # drop the segment for a second for no reason.
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
    # A key handed over in the environment (a hand-run of this script) is put
    # into the same file, so there is one code path into `tailscale up` and it
    # is the one that keeps the key off the command line.
    if [ ! -f "$AUTHKEY_FILE" ]; then
        ( umask 077; printf '%s\n' "$BR_AUTHKEY" > "$AUTHKEY_FILE" )
    fi
    # --advertise-tags is what makes the node non-expiring, and it is only
    # settable at login: an untagged bridge works for 180 days and then goes
    # dark, which is the failure mode this device exists to prevent.
    #
    # --accept-dns=false because a subnet router must keep its own resolver;
    # --accept-routes=false because it publishes routes, it does not consume
    # them; --ssh because the phone's own screen should not be the only way
    # back in when the LAN address has moved.
    # `--auth-key file:` and not `--authkey=<key>`: the second spelling puts a
    # credential in the phone's process table, where anyone with a shell there
    # can read it out of /proc. The file is 0600 on tmpfs and deleted below.
    # (The file: form needs tailscale >= 1.38; if this fails claiming the key is
    # invalid, that is the version, not the key.)
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

# --- 18. start everything ----------------------------------------------------
#
# The dependency cache is rebuilt first: everything below is started by name,
# and a service OpenRC cannot see in its tree is one it will neither start now
# nor at the next boot.
step "Services"
refresh_deptree
for s in wk-bridge-nftables wk-bridge-usb-host wk-bridge-dhcp wk-bridge-netwatch wk-bridge-watchdog wk-bridge-camera; do
    [ -x "/etc/init.d/$s" ] || continue
    if rc-service "$s" status >/dev/null 2>&1; then
        # Restart the ones whose configuration this run may have rewritten, so
        # a re-provision takes effect without a reboot. The watchdog and the
        # netwatch loop read their configuration at start, so they are included.
        rc-service "$s" restart >/dev/null 2>&1 || warn "$s did not restart"
    else
        rc-service "$s" start >/dev/null 2>&1 || warn "$s did not start"
    fi
done
[ -n "${CHRONY_SVC:-}" ] && { rc-service "$CHRONY_SVC" restart >/dev/null 2>&1 || true; }

step "Done -- $CHANGES change(s)"
info "health: wk-bridge-healthcheck   (or 'wk bridge status $BR_NAME' from the workstation)"
