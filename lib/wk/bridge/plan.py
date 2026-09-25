"""What `wk machine setup <bridge>` puts on the phone, rendered here from its conf and the facts the phone
reports: the files, the manifest bridge/provision.sh applies, and the bundle that carries them."""

import base64
import gzip
import io
import os
import shlex
import tarfile

LIB = "/usr/local/lib/wk-bridge"
LEASEFILE = "/var/lib/misc/dnsmasq.leases"
AUTHKEY = "/run/wk-bridge-authkey"
NETMASKS = {"16": "255.255.0.0", "24": "255.255.255.0"}
PACKAGED = (("nm", ("networkmanager", "NetworkManager")), ("chrony", ("chronyd", "chrony")),
            ("tailscale", ("tailscale", "tailscaled")))

FACTS = r'''set -u
printf 'uplink=%s\n' "$(nmcli -t -f DEVICE,TYPE device status 2>/dev/null | awk -F: '$2 == "wifi" { print $1; exit }')"
for n in /sys/class/net/*; do
    d=${n##*/}
    case "$d" in lo|wlan*|tailscale*|dummy*|usb*) continue ;; esac
    case "$(readlink -f "$n/device/subsystem" 2>/dev/null)" in */usb) ;; *) continue ;; esac
    perm=$(ethtool -P "$d" 2>/dev/null | sed -n 's/^Permanent address: //p')
    case "$perm" in ""|00:00:00:00:00:00) perm=$(cat "$n/address") ;; esac
    printf 'lan_dev=%s\nlan_mac=%s\n' "$d" "$perm"
    break
done
nodes=""
for f in /sys/class/power_supply/*/charge_control_end_threshold; do
    [ -f "$f" ] && nodes="$nodes${nodes:+,}${f%/*}"
done
printf 'battery_nodes=%s\n' "$nodes"
yn() { if "$@"; then echo yes; else echo no; fi; }
printf 'watchdog=%s\ntun=%s\nelogind=%s\n' "$(yn test -e /dev/watchdog)" "$(yn test -c /dev/net/tun)" "$(yn test -d /etc/elogind)"
printf 'swap_total=%s\n' "$(awk 'NR > 1 { c++ } END { print c + 0 }' /proc/swaps 2>/dev/null)"
printf 'swap_nonzram=%s\n' "$(awk 'NR > 1 && $1 !~ /zram/ { c++ } END { print c + 0 }' /proc/swaps 2>/dev/null)"
printf 'init='
for s in networkmanager NetworkManager chronyd chrony tailscale tailscaled nftables zram-init; do
    [ -x "/etc/init.d/$s" ] && printf '%s ' "$s"
done
echo
'''

LOGIND = """[Login]
HandlePowerKey=ignore
HandleSuspendKey=ignore
HandleHibernateKey=ignore
HandleLidSwitch=ignore
IdleAction=ignore
"""
NM_WIFI = """# Power save turns a working uplink into one that answers every third ping.
[connection]
wifi.powersave=2

# A stable MAC keeps the DHCP lease and any AP-side reservation stable.
[device]
wifi.scan-rand-mac-address=no
"""
GRO = """#!/bin/sh
# UDP GRO forwarding is worth about a factor on tailscale throughput, and a link bounce resets it.
[ "$2" = up ] || exit 0
case "$1" in lo|tailscale0) exit 0 ;; esac
ethtool -K "$1" rx-udp-gro-forwarding on rx-gro-list off 2>/dev/null
exit 0
"""
SYSCTL = "net.ipv4.ip_forward=1\nnet.ipv6.conf.all.forwarding=1\n"
NM_DNS = """# Without this an unmanaged /etc/resolv.conf shadows what DHCP offered.
[main]
dns=default
rc-manager=file
"""
SSHD = "PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin prohibit-password\nPermitEmptyPasswords no\n"
LOGROTATE = "/var/log/messages {\n    size 8M\n    rotate 3\n    compress\n    missingok\n    notifempty\n    copytruncate\n}\n"
CHRONY = """allow %s
rtcsync

# This phone has no RTC and boots at 1970; chrony's default slew never closes that gap, and a
# 1970 clock fails every TLS check. -1: the offset recurs on every boot.
makestep 1.0 -1
"""
NMCONNECTION = """[connection]
id=wk-bridge-%(iface)s
type=ethernet
interface-name=%(iface)s
autoconnect=true
autoconnect-retries=0

# never-default/ignore-auto-dns: a default route learned on this leg would send the phone's own traffic at a BMC.
[ipv4]
method=manual
address1=%(router)s/%(prefix)s
never-default=true
ignore-auto-dns=true

[ipv6]
method=ignore

# The hardware address, against pmOS's `cloned-mac-address=stable`: the udev rule and far-side reservations key on it.
[ethernet]
cloned-mac-address=permanent
"""
ICMP = "icmp type { echo-request, echo-reply, destination-unreachable, time-exceeded } accept"


def wk_bridge_conf(name, c):
    keys = {"name": name, "device": c.get("BR_DEVICE", ""), "hostname": c["BR_HOSTNAME"],
            "tag": c["BR_TAG"], "segment": c["BR_SEGMENT"], "router": c["BR_ROUTER"],
            "iface": c["BR_IF"], "pool": c.get("BR_POOL", ""), "leases": c.get("BR_LEASES", ""),
            "egress": c["BR_EGRESS"], "camera": c["BR_CAMERA"], "leasefile": LEASEFILE}
    return "# Rendered by wk machine setup from machines/%s.conf; sourced by /bin/sh.\n%s" % (
        name, "".join("%s=%s\n" % (k, shlex.quote(v)) for k, v in keys.items()))


def dnsmasq(c, netmask):
    nat, iface, router = c["BR_EGRESS"] == "nat", c["BR_IF"], c["BR_ROUTER"]
    out = ["# DHCP for the bridge segment."]
    # No egress: DNS off (port=0, an empty option 6), since there is nothing to resolve and a resolver is attackable.
    out += ["domain-needed", "bogus-priv"] if nat else ["port=0"]
    # bind-dynamic: the NIC comes and goes with the dock, and bind-interfaces fails to start while it is out.
    out += ["interface=" + iface, "bind-dynamic", "dhcp-authoritative", "log-dhcp", "dhcp-leasefile=" + LEASEFILE]
    if c.get("BR_POOL"):
        out.append("dhcp-range=%s,%s,infinite" % (c["BR_POOL"], netmask))
    out += ["dhcp-option=option:router," + router, "dhcp-option=option:ntp-server," + router]
    out.append("dhcp-option=option:dns-server," + router if nat else "dhcp-option=6")
    out += ["dhcp-host=%s,infinite" % lease for lease in c.get("BR_LEASES", "").split()]
    return "\n".join(out) + "\n"


def nft(c, uplink):
    iface, nat = c["BR_IF"], c["BR_EGRESS"] == "nat"
    up = 'iifname "%s" ' % uplink
    seg = 'iifname "%s" ' % iface
    lines = ["#!/usr/sbin/nft -f", "# create-then-delete makes the file idempotent.",
             "table inet wkbridge", "delete table inet wkbridge", "", "table inet wkbridge {", "    chain input {",
             "        type filter hook input priority 0; policy drop;",
             '        iif "lo" accept', "        ct state established,related accept",
             # Dropping neighbour discovery reads as random loss.
             "        meta l4proto ipv6-icmp accept", '        iifname "tailscale0" accept',
             "        %stcp dport 22 accept" % up, "        %sudp dport 41641 accept" % up,
             "        %sudp sport 67 udp dport 68 accept" % up,
             "        %s%s" % (up, ICMP), "        %sudp dport { 67, 123 } accept" % seg]
    if nat:
        lines += ["        %sudp dport 53 accept" % seg, "        %stcp dport 53 accept" % seg]
    lines += ["        %s%s" % (seg, ICMP),
              # Before the log rule: a home LAN's mDNS/IGMP/SSDP would bury every drop that means something.
              "        meta pkttype { multicast, broadcast } counter drop",
              '        limit rate 10/minute log prefix "wkbridge-input-drop " counter drop', "    }",
              "    chain forward {", "        type filter hook forward priority 0; policy drop;",
              "        ct state established,related accept", '        iifname "tailscale0" oifname "%s" accept' % iface]
    if nat:
        lines.append('        %soifname "%s" accept' % (seg, uplink))
    lines += ['        limit rate 10/minute log prefix "wkbridge-forward-drop " counter drop', "        counter drop", "    }"]
    if nat:
        lines += ["    chain postrouting {", "        type nat hook postrouting priority srcnat; policy accept;",
                  '        oifname "%s" ip saddr %s masquerade' % (uplink, c["BR_SEGMENT"]), "    }"]
    return "\n".join(lines + ["}"]) + "\n"


def packaged(facts, role):
    have = facts.get("init", "").split()
    return next((n for n in dict(PACKAGED)[role] if n in have), "")


def battery_node(c, facts):
    nodes = [n for n in facts.get("battery_nodes", "").split(",") if n]
    pinned = c.get("BR_BATTERY", "")
    if pinned:
        return next((n for n in nodes if os.path.basename(n) == pinned), "")
    return nodes[0] if nodes else ""


class Plan:
    """The files and manifest lines one bundle carries, and what the host has to say about them."""

    def __init__(self, name, conf, facts=None):
        self.name, self.conf = name, conf
        self.files, self.lines, self.warnings = [], [], []
        prefix = conf["BR_SEGMENT"].split("/")[-1]
        self.netmask = NETMASKS.get(prefix, "255.255.255.0")
        if prefix not in NETMASKS:
            self.warnings.append("prefix /%s is not one this understands -- assuming %s" % (prefix, self.netmask))
        self.lan_mac = (conf.get("BR_LAN_MAC") or (facts or {}).get("lan_mac", "")).lower()
        self.battery = battery_node(conf, facts) if facts is not None else ""
        self._common()
        if facts is not None:
            self._role(facts)

    def file(self, path, text, mode="0644"):
        self.files.append((path, mode, text))
        self.lines.append("file %s %s" % (mode, path))

    def _common(self):
        c = self.conf
        self.file("/etc/elogind/logind.conf.d/10-wk-bridge.conf", LOGIND)
        self.file("/etc/wk-bridge.conf", wk_bridge_conf(self.name, c))
        self.file("/etc/NetworkManager/conf.d/99-wk-bridge.conf", NM_WIFI)
        self.file("/etc/NetworkManager/dispatcher.d/50-wk-bridge-gro", GRO, "0755")
        self.file("/etc/NetworkManager/conf.d/98-wk-bridge-dns.conf", NM_DNS)
        self.file("/etc/dnsmasq.d/wk-bridge.conf", dnsmasq(c, self.netmask))
        self.file("/etc/sysctl.d/99-wk-bridge.conf", SYSCTL)
        self.file("/etc/chrony/conf.d/wk-bridge.conf", CHRONY % c["BR_SEGMENT"])
        self.file("/etc/ssh/sshd_config.d/10-wk-bridge.conf", SSHD)
        self.file("/etc/logrotate.d/wk-bridge", LOGROTATE)

    def _role(self, facts):
        c, iface = self.conf, self.conf["BR_IF"]
        uplink = facts.get("uplink") or "wlan0"
        if self.lan_mac:
            self.file("/etc/udev/rules.d/70-wk-bridge-net.rules",
                      'SUBSYSTEM=="net", ACTION=="add", ATTR{address}=="%s", NAME="%s"\n' % (self.lan_mac, iface))
            self.file("/etc/NetworkManager/system-connections/wk-bridge-%s.nmconnection" % iface,
                      NMCONNECTION % {"iface": iface, "router": c["BR_ROUTER"], "prefix": c["BR_SEGMENT"].split("/")[-1]}, "0600")
        else:
            self.warnings.append("no USB ethernet adapter is enumerated right now, so %s has no udev rename and no address;\n"
                                 "  one plugged in later comes up unnamed until 'wk machine setup %s' runs again." % (iface, self.name))
        self.file("/etc/nftables.d/wk-bridge.nft", nft(c, uplink))
        if self.battery:
            self.file("/etc/wk-bridge-battery.conf", "node=%s\nlimit=%s\n" % (self.battery, c["BR_BATTERY_LIMIT"]))
        for role, missing in (("nm", "NetworkManager"), ("tailscale", "the tailscale package")):
            if not packaged(facts, role):
                raise LookupError("%s has no init script on the phone" % missing)
        self.lines += ["enable " + packaged(facts, "nm"), "service wk-bridge-usb-host", "service wk-bridge-dhcp",
                       "service wk-bridge-nftables", "service wk-bridge-netwatch"]
        if "nftables" in facts.get("init", "").split():
            self.lines.append("disable nftables")
        chrony = packaged(facts, "chrony")
        self.lines += ["enable " + chrony, "restart " + chrony] if chrony else []
        self.lines.append("enable sshd")
        self.lines.append("enable zram-init" if "zram-init" in facts.get("init", "").split() else "")
        self.lines.append("service wk-bridge-watchdog" if facts.get("watchdog") == "yes" else "")
        self.lines.append("service wk-bridge-camera" if c["BR_CAMERA"] != "off" else "drop wk-bridge-camera")
        self.lines.append("service wk-bridge-battery" if self.battery else "")
        self.lines += ["enable " + packaged(facts, "tailscale"), "start " + packaged(facts, "tailscale")]
        self.lines = [l for l in self.lines if l]
        self.warnings += self._fact_warnings(facts)

    def _fact_warnings(self, facts):
        out = []
        if facts.get("tun") != "yes":
            out.append("/dev/net/tun is missing -- tailscale cannot route a subnet without it (modprobe tun)")
        if facts.get("elogind") != "yes":
            out.append("no /etc/elogind -- the power and suspend keys are not pinned; check what this image uses")
        if facts.get("watchdog") != "yes":
            out.append("no /dev/watchdog on this device -- a kernel hang needs a person")
        if int(facts.get("swap_nonzram") or 0):
            out.append("swap is on something other than zram (eMMC wear); swapoff it and drop it from /etc/fstab at the phone")
        elif not int(facts.get("swap_total") or 0) and "zram-init" not in facts.get("init", ""):
            out.append("no swap at all and no zram-init -- a memory spike will OOM the router")
        if not self.battery:
            out.append("no power_supply node here exposes charge_control_end_threshold%s, so the charge is not capped"
                       % (" named %s (BR_BATTERY)" % self.conf["BR_BATTERY"] if self.conf.get("BR_BATTERY") else ""))
        return out

    def env(self):
        c = self.conf
        values = (("BR_NAME", self.name), ("BR_HOSTNAME", c["BR_HOSTNAME"]), ("BR_IF", c["BR_IF"]),
                  ("BR_LAN_MAC", self.lan_mac), ("BR_CAMERA", c["BR_CAMERA"]),
                  ("BR_BATTERY_LIMIT", c["BR_BATTERY_LIMIT"]), ("BR_BATTERY_NODE", self.battery))
        return "".join("%s=%s\n" % (k, shlex.quote(v)) for k, v in values)

    def paths(self):
        return [p for p, _m, _t in self.files]


def every_path(name, conf):
    """Every file a setup can leave, whatever the phone reports: what `rm` removes."""
    rich = {"lan_mac": "00:00:00:00:00:01", "battery_nodes": "/sys/class/power_supply/any", "watchdog": "yes",
            "init": "networkmanager chronyd tailscale nftables zram-init"}
    return Plan(name, conf, rich).paths()


def _add(tar, name, data, mode):
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = len(data), mode, 0
    tar.addfile(info, io.BytesIO(data))


def bundle(root, plan):
    """bridge/'s scripts plus the plan, as base64 text for `base64 -d | tar xz` on the phone: byte for byte the
    same for the same inputs, so a dry run and a re-run show the same thing."""
    raw = io.BytesIO()
    with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as tar:
        src = os.path.join(str(root), "bridge")
        for sub in ("bin", "init.d"):
            for n in sorted(os.listdir(os.path.join(src, sub))):
                with open(os.path.join(src, sub, n), "rb") as f:
                    _add(tar, "%s/%s" % (sub, n), f.read(), 0o755)
        with open(os.path.join(src, "provision.sh"), "rb") as f:
            _add(tar, "provision.sh", f.read(), 0o755)
        _add(tar, "role.env", plan.env().encode(), 0o644)
        _add(tar, "manifest", "".join(l + "\n" for l in plan.lines).encode(), 0o644)
        for path, mode, text in plan.files:
            _add(tar, "files" + path, text.encode(), int(mode, 8))
    return base64.encodebytes(raw.getvalue()).decode()
