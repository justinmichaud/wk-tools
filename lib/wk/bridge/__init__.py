"""A bridge phone, read: `wk machine status [<bridge>]`, `wk doctor`'s battery row, and finding its ssh
destination (`resolve`). The phone has no python3, so its health check only gathers facts and this module
judges them. `wk machine setup|tailnet|rm <bridge>` is wk.bridge.role."""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from wk import act, fleet, reach
from wk.kv import kv
from wk.machine import Local, Ssh

HEALTHCHECK = "/usr/local/sbin/wk-bridge-healthcheck"
SSH_PROBE_WORKERS = 32
# A fresh image brings an unverifiable key, and root on a bridge is whatever is behind it: accepted once, then held.
ACCEPT_NEW = ["-o", "StrictHostKeyChecking=accept-new"]
BATTERY_SCRIPT = '''set -u
conf=/etc/wk-bridge-battery.conf
node=""; limit=""
if [ -f "$conf" ]; then
    node=$(sed -n 's/^node=//p' "$conf")
    limit=$(sed -n 's/^limit=//p' "$conf")
fi
pct=""; state=""
for d in /sys/class/power_supply/*/capacity; do
    [ -f "$d" ] || continue
    pct=$(cat "$d" 2>/dev/null)
    state=$(cat "$(dirname "$d")/status" 2>/dev/null)
    break
done
cur=""
[ -n "$node" ] && [ -f "$node/charge_control_end_threshold" ] \\
    && cur=$(cat "$node/charge_control_end_threshold" 2>/dev/null)
printf 'percent=%s\\n' "${pct:-?}"
printf 'status=%s\\n'  "${state:-?}"
printf 'limit=%s\\n'   "${limit:-?}"
printf 'current=%s\\n' "${cur:-?}"
'''


class Unreachable(Exception):
    """No ssh route to the phone, with the explanation `resolve`'s caller shows."""


class NeedsPassword(Unreachable):
    pass


def devices(root):
    out = {}
    with open(os.path.join(str(root), "bridge", "devices.tsv")) as f:
        for line in f:
            if line.strip() and not line.startswith("#"):
                dev, note, kill = line.rstrip("\n").split("\t")
                out[dev] = (note, kill)
    return out


def classify_ssh_error(stderr):
    """A reflash changes the phone's host key; reporting that as unreachable would aim the
    diagnosis at the radio instead of at known_hosts."""
    if "IDENTIFICATION HAS CHANGED" in stderr or "Host key verification failed" in stderr:
        return "key-changed"
    return "unreachable"


def segment_state(facts):
    """down: the interface exists (the dock enumerated fine) but nothing answers on the wire --
    check the cable. off: the interface itself never appeared, so the dock or the port is the
    question, not the far end. `unit bridge.segment_down_vs_off`."""
    if facts.get("seg_iface_exists") != "yes":
        return "off"
    return "up" if facts.get("seg_carrier") == "1" else "down"


def ssh_is_phone(machine, ip, user, hostname):
    """`ip` if what answers ssh there calls itself `hostname`, as root or the phone's user."""
    if not machine.run(["nc", "-z", ip, "22"], timeout=4).ok:
        return None
    for u in ("root", user):
        r = Ssh("%s@%s" % (u, ip), opts=ACCEPT_NEW, timeout=4, via=machine).run(["sh", "-c", "hostname"], timeout=10)
        if r.ok and r.out.strip().rstrip("\r") == hostname:
            return ip
    return None


class BridgeConf:
    """A bridge's conf as the fleet fills it: every key BRIDGE_DEFAULTS names is set."""

    def __init__(self, name, conf):
        self.name = name
        self.conf = conf

    def ssh(self):
        return self.conf["BR_SSH"]

    def hostname(self):
        return self.conf["BR_HOSTNAME"]

    def user(self):
        return self.conf["BR_USER"]

    @property
    def device(self):
        return self.conf.get("BR_DEVICE", "")

    @property
    def segment(self):
        return self.conf.get("BR_SEGMENT", "")

    @property
    def iface(self):
        return self.conf["BR_IF"]

    @property
    def egress(self):
        return self.conf["BR_EGRESS"]

    @property
    def camera(self):
        return self.conf["BR_CAMERA"]

    @property
    def note(self):
        return self.conf.get("BR_NOTE", "")


class Report:
    """One `wk machine status <bridge>`: rows of (level, text) with level in ok|bad|note|hdr; `failed` is
    true once any `bad` is added."""

    def __init__(self):
        self.rows = []
        self.failed = False

    def hdr(self, text):
        self.rows.append(("hdr", text))

    def ok(self, text):
        self.rows.append(("ok", text))

    def bad(self, text):
        self.rows.append(("bad", text))
        self.failed = True

    def note(self, text):
        self.rows.append(("note", text))


def judge(facts, bc):
    """The health check's raw facts, judged here: the phone has no python3 to judge them in."""
    r = Report()
    r.hdr("Uplink")
    wifi = facts.get("wifi_iface") or "wlan0"
    if facts.get("wifi_addr"):
        r.ok("%s %s" % (wifi, facts["wifi_addr"]))
    else:
        r.bad("%s has no IPv4 address" % wifi)
    if facts.get("wifi_ssid"):
        r.note("SSID %s, signal %s" % (facts["wifi_ssid"], facts.get("wifi_signal", "?")))
    if facts.get("wifi_power_save") == "off":
        r.ok("wifi power save off")
    else:
        r.bad("wifi power save is ON (%s) -- causes dropped links" % (facts.get("wifi_power_save") or "?"))

    r.hdr("Segment %s" % bc.segment)
    state = segment_state(facts)
    if state in ("up", "down"):
        r.ok("%s %s" % (bc.iface, facts.get("seg_addr", "")))
        if state == "up":
            r.ok("cable connected")
            speed = facts.get("seg_usb_speed", "")
            if speed in ("480", "5000", "10000", "20000"):
                r.ok("USB %s Mbit/s" % speed)
            elif speed:
                r.bad("USB %s Mbit/s -- the NIC is on a full-speed controller" % speed)
                r.note("wk-bridge-usb-host rebinds the EHCI companion to recover 480")
        else:
            r.bad("no carrier on %s -- check the cable" % bc.iface)
    else:
        r.bad("%s missing -- nothing on the segment" % bc.iface)
        role = facts.get("seg_typec_role", "")
        if facts.get("seg_renamed_candidates"):
            r.note("the adapter is here and working -- %s -- it is just not called %s"
                   % (facts["seg_renamed_candidates"], bc.iface))
            r.note("wk machine setup %s renames it in place; no re-plug" % bc.name)
        elif facts.get("seg_unclaimed"):
            r.note("something on the bus looks like a network adapter with no driver bound (%s)"
                   % facts["seg_unclaimed"])
        elif "[host]" in role:
            r.note("the port is in host mode; the bus enumerated%s" %
                   (" -- " + facts["seg_downstream"] if facts.get("seg_downstream") else " nothing at all"))
        else:
            r.note("the port is not in host mode (%s) -- run wk-bridge-usb-host" % (role or "no typec port"))

    r.hdr("DHCP")
    if facts.get("leases_file_nonempty") == "yes":
        r.ok("leases recorded")
    else:
        r.note("no lease recorded -- not a fault by itself; the pings below are what matters")
    for entry in facts.get("leases_ping", "").split(";"):
        if not entry:
            continue
        ip, name, up = (entry.split(",") + ["", ""])[:3]
        label = "%s (%s)" % (ip, name or "reserved")
        (r.ok if up == "up" else r.bad)("%s %s" % (label, "responds" if up == "up" else "unreachable"))

    r.hdr("Tailnet")
    (r.ok if facts.get("ts_backend") == "Running" else r.bad)(
        "tailscaled running" if facts.get("ts_backend") == "Running" else "tailscale backend is %s" % facts.get("ts_backend", "?"))
    (r.ok if facts.get("ts_online") == "true" else r.bad)("online" if facts.get("ts_online") == "true" else "reported offline")
    tags = facts.get("ts_tags", "none")
    (r.ok("tags %s" % tags) if tags != "none" else r.bad("UNTAGGED -- the node key will expire"))
    routes = facts.get("ts_routes", "none")
    if routes != "none":
        r.ok("routes %s" % routes)
        if bc.segment not in routes:
            r.bad("%s is advertised but not approved -- nothing behind this bridge is reachable" % bc.segment)
    else:
        r.bad("no approved subnet route -- nothing behind this bridge is reachable")

    r.hdr("Services")
    for s in ("wk-bridge-dhcp", "wk-bridge-nftables", "wk-bridge-netwatch", "wk-bridge-usb-host"):
        st = facts.get("svc_" + s, "missing")
        r.ok(s) if st == "running" else r.bad("%s is not running" % s if st == "stopped" else "%s is not installed" % s)
    for label, key in (("sshd", "svc_sshd"), ("chrony", "svc_chrony"), ("networkmanager", "svc_nm"), ("tailscale", "svc_tailscale")):
        st = facts.get(key, "missing")
        r.ok(label) if st == "running" else r.bad("%s is not running" % label if st == "stopped" else "%s is not installed" % label)
    r.ok("nftables table loaded") if facts.get("nft_table") == "yes" else r.bad("nftables table inet wkbridge missing")

    r.hdr("Routing")
    r.ok("ipv4 forwarding on") if facts.get("ip_forward") == "1" else r.bad("ipv4 forwarding is off")
    if bc.egress == "nat":
        r.ok("egress: NAT to the uplink") if facts.get("nft_masquerade") == "yes" \
            else r.bad("egress is declared nat but no masquerade rule is loaded")
    else:
        r.ok("egress: none (nothing behind this bridge reaches the internet)")

    r.hdr("Name resolution and time")
    if facts.get("resolv_exists") == "yes":
        if "127.0.0.53" in facts.get("resolv_first", ""):
            r.bad("resolv.conf points at 127.0.0.53 -- a systemd-resolved stub, and there is none here")
        elif int(facts.get("resolv_nameservers") or 0) > 0:
            r.ok("resolv.conf: %s nameserver(s)" % facts["resolv_nameservers"])
        else:
            r.bad("resolv.conf lists no nameserver")
    else:
        r.bad("no /etc/resolv.conf at all")
    r.ok("DNS resolves") if facts.get("dns_resolves") == "yes" \
        else r.bad("DNS does not resolve -- tailscale cannot reach its coordination server")
    year = int(facts.get("clock_year") or 0)
    r.ok("clock sane (%s)" % facts.get("clock_iso", "")) if year >= 2024 \
        else r.bad("clock is %s -- every TLS check fails, so tailscale cannot log in" % (facts.get("clock_year") or "?"))

    r.hdr("Hardening")
    r.ok("SSH password auth disabled") if facts.get("sshd_password_auth") == "no" \
        else r.bad("SSH password auth is ENABLED (%s)" % (facts.get("sshd_password_auth") or "?"))
    if facts.get("watchdog_device") == "yes":
        r.ok("hardware watchdog fed") if facts.get("watchdog_fed") == "yes" \
            else r.bad("/dev/watchdog exists but nothing is feeding it -- a hang stays hung")
    else:
        r.bad("no /dev/watchdog -- a kernel hang needs a person at the phone")
        if facts.get("watchdog_dt_node"):
            r.note("the device tree declares %s (%s) -- 'wk machine setup' loads it by modalias"
                   % (facts["watchdog_dt_node"], facts.get("watchdog_dt_compatible", "")))
    r.ok("swap is zram only") if int(facts.get("swap_nonzram") or 0) == 0 else r.bad("swap on a non-zram device (eMMC wear)")

    if bc.camera != "off":
        r.hdr("Camera")
        if facts.get("camera_device") == "yes":
            r.ok("streaming") if facts.get("camera_streaming") == "yes" \
                else r.bad("a capture device exists but the stream is not running")
        else:
            r.note("no capture device -- the camera kill switch is off, so nothing streams")

    r.hdr("System")
    crashed = [c for c in facts.get("crashed", "").split(",") if c]
    r.ok("no crashed services") if not crashed else r.bad("crashed: %s" % " ".join(crashed))
    if facts.get("uptime"):
        r.note("uptime %s" % facts["uptime"])
    for entry in facts.get("battery", "").split(";"):
        if entry:
            dev, pct, st = (entry.split(",") + ["", ""])[:3]
            r.note("battery %s%% %s (%s)" % (pct, st, dev))
    return r


def render(report, out):
    for level, text in report.rows:
        if level == "hdr":
            out.write("\n%s\n" % text)
        elif level == "note":
            out.write("        %s\n" % text)
        else:
            out.write("  %-5s %s\n" % (level.upper(), text))
    out.write("\n%s\n" % ("Problems found -- see FAIL lines above." if report.failed else "All checks passed."))


def render_ls(rows, out):
    out.write("%-28s %-10s %-16s %-12s %s\n" % ("NAME", "DEVICE", "SEGMENT", "STATE", "NOTE"))
    for row in rows:
        out.write("%-28s %-10s %-16s %-12s %s\n" % (row["name"], row["device"] or "?", row["segment"] or "?", row["state"], row["note"]))
    if not rows:
        out.write("(no bridges declared -- machines/*.conf with KIND=bridge)\n")
    out.write("\n"
              "  bare         answers ssh, nothing of this role on it yet\n"
              "  provisioned  'wk machine setup' has run against it\n"
              "  key-changed  answered, with a host key that is not the one on record.\n"
              "               A reflash does this: drop the old line from known_hosts\n"
              "               (ssh-keygen -R <name>). Anything else is worth reading\n"
              "               before you do.\n"
              "  unreachable  did not answer in 5s -- off, or not flashed yet\n")


class Bridge:
    def __init__(self, root, env=None, machine=None, phones=None):
        self.root = str(root)
        self.env = os.environ if env is None else env
        self.machine = machine or Local()
        self.fleet = fleet.Fleet(self.root, self.env)
        self._phones = phones

    def phone(self, dest):
        return self._phones(dest) if self._phones else Ssh(dest, opts=ACCEPT_NEW, timeout=8, via=self.machine)

    def names(self):
        return self.fleet.names(("bridge",))

    def conf(self, name):
        c = self.fleet.load(name)
        if c is None or c.get("KIND") != "bridge":
            raise LookupError("'%s' is not a declared bridge. Declared bridges: %s"
                               % (name, ", ".join(self.names()) or "(none)"))
        return BridgeConf(name, c)

    def reaches(self, dest, timeout=8):
        return Ssh(dest, opts=ACCEPT_NEW, timeout=timeout, via=self.machine).run(["true"], timeout=timeout + 12).ok

    def discover(self, bc):
        """The phone on a segment this machine sits on: reach's one enumeration, then each neighbour's own hostname."""
        swept = list(reach.Reach(self.machine, self.env).swept())
        if not [rows for _seg, rows in swept if rows is not None]:
            act.warn("this machine swept none of its segments (reach needs ip(8) and nmap), so %s is looked for\n"
                     "  by its conf name only; --at <address> names it outright" % bc.name)
            return None
        ips = [ip for _seg, rows in swept for ip, _mac, _state in rows or ()]
        with ThreadPoolExecutor(max_workers=SSH_PROBE_WORKERS) as pool:
            for hit in pool.map(lambda ip: ssh_is_phone(self.machine, ip, bc.user(), bc.hostname()), ips):
                if hit:
                    return hit
        act.log("  nothing on %s answers ssh as %s" % (", ".join(seg for seg, _rows in swept), bc.hostname()))
        return None

    def _no_route_message(self, bc):
        return ("cannot reach %s.\n"
                "    Tried the name in its conf and the segments this machine sits on, and nothing answered.\n\n"
                "    Two things silence an unprovisioned phone, and the image and the role both turn them off,\n"
                "    since a phone that needs them off to answer ssh cannot be sent that over ssh:\n"
                "      - WiFi power save. The RTL8723CS powers its RF side down when idle and\n"
                "        misses frames aimed at it, so the phone reaches its router while\n"
                "        answering nothing, ARP included. Proving it takes one command on the\n"
                "        phone: iw dev wlan0 set power_save off\n"
                "      - suspend. phosh idles it off the network after a few minutes.\n\n"
                "    If you know the address:\n\n"
                "        wk machine setup %s --at <address>   (tailnet, rm and status take it too)\n\n"
                "    Otherwise the phone is off, on another subnet, or was never installed;\n"
                "    'wk machine setup %s --disk <machine>:<device>' writes its system first." % (bc.name, bc.name, bc.name))

    def resolve(self, name, at=None, names_only=False):
        bc = self.conf(name)
        if at:
            for user in ("root", bc.user()):
                dest = "%s@%s" % (user, at)
                if self.reaches(dest):
                    return dest
            raise Unreachable(
                "--at %s does not answer ssh as root or '%s'.\n"
                "    An address given on the command line is taken at its word rather than worked\n"
                "    around, because the alternative is provisioning something else. Drop --at to\n"
                "    try the phone's names instead." % (at, bc.user()))

        host = bc.ssh().split("@")[-1]
        if self.reaches("root@%s" % host):
            return "root@%s" % host
        if self.reaches(bc.ssh()):
            return bc.ssh()
        if names_only:
            raise Unreachable(self._no_route_message(bc))

        found = self.discover(bc)
        if not found:
            raise Unreachable(self._no_route_message(bc))
        for user in ("root", bc.user()):
            dest = "%s@%s" % (user, found)
            if self.reaches(dest):
                act.log("found %s at %s (it identifies itself as %s) -- a DHCP lease, not written down"
                         % (bc.name, found, bc.hostname()))
                return dest
        raise Unreachable("%s says it is %s but does not accept ssh as root or '%s'." % (found, bc.hostname(), bc.user()))

    def priv_prefix(self, name, ssh, dest):
        uid = ssh.run(["id", "-u"], timeout=15)
        if not uid.ok:
            raise Unreachable("cannot ssh to %s." % dest)
        if uid.out.strip() == "0":
            return []
        if ssh.run(["sh", "-c", "command -v doas"], timeout=15).ok:
            prefix = ["doas", "-n"]
        elif ssh.run(["sh", "-c", "command -v sudo"], timeout=15).ok:
            prefix = ["sudo", "-n"]
        else:
            raise Unreachable("ssh to %s lands on uid %s and the phone has neither doas nor sudo." % (dest, uid.out.strip()))
        if not ssh.run(prefix + ["true"], timeout=15).ok:
            raise NeedsPassword("'%s' on %s needs a password, and this is a read-only check with none to give.\n"
                              "    'wk machine setup %s' gives root the same key, once."
                              % (" ".join(prefix), dest, name))
        return prefix

    def ls_row(self, name):
        bc = self.conf(name)
        dest = bc.ssh()
        phone = Ssh(dest, timeout=5, opts=ACCEPT_NEW, via=self.machine)
        r = phone.run(["true"], timeout=8)
        state = "up" if r.ok else classify_ssh_error(r.err)
        if state == "up":
            state = "provisioned" if phone.exists("/etc/wk-bridge.conf") else "bare"
        return {"name": name, "device": bc.device, "segment": bc.segment, "state": state, "note": bc.note}

    def ls(self, out=None):
        out = out or sys.stdout
        render_ls([self.ls_row(n) for n in self.names()], out)
        return 0

    def status(self, name, at=None, out=None):
        out = out or sys.stdout
        bc = self.conf(name)
        dest = self.resolve(name, at=at)
        ssh = self.phone(dest)
        if not ssh.run(["test", "-x", HEALTHCHECK], timeout=15).ok:
            raise Unreachable("%s answers but has no bridge role on it.\n"
                              "    'wk machine setup %s' provisions it." % (dest, name))
        prefix = self.priv_prefix(name, ssh, dest)
        facts = kv(ssh.run(prefix + [HEALTHCHECK], timeout=30).out)
        out.write("%s (%s)\n" % (bc.name, bc.device))
        report = judge(facts, bc)
        render(report, out)
        return 1 if report.failed else 0

    def ts_status(self, ssh, prefix):
        r = ssh.run(prefix + ["tailscale", "status", "--json", "--peers=false"], timeout=30)
        try:
            return json.loads(r.out) if r.ok else {}
        except ValueError:
            return {}

    def battery(self, name):
        dest = self.resolve(name, names_only=True)
        facts = kv(self.phone(dest).run(["sh", "-s"], input=BATTERY_SCRIPT, timeout=15).out)
        return "".join("%s=%s\n" % (k, facts.get(k, "?")) for k in ("percent", "status", "limit", "current"))
