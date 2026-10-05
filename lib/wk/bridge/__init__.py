"""A bridge phone, read: `wk machine status [<bridge>]`, `wk doctor`'s battery row, and its ssh destination
(`resolve`). `wk machine setup|tailnet|rm <bridge>` is wk.bridge.role."""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from wk import act, fleet, reach
from wk.kv import kv
from wk.machine import HAVE, Local, Ssh

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
    pass


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
    """A reflash changes the phone's host key: that is known_hosts, not the radio."""
    if "IDENTIFICATION HAS CHANGED" in stderr or "Host key verification failed" in stderr:
        return "key-changed"
    return "unreachable"


def segment_state(facts):
    """off: the interface never appeared (the dock or the port); down: no carrier (the cable)."""
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
    def __init__(self, name, conf):
        self.name, self.conf = name, conf
        g = conf.get
        self.ssh, self.hostname, self.user = g("ssh", ""), g("hostname", ""), g("user", "")
        self.device, self.segment, self.iface = g("device", ""), g("segment", ""), g("if", "")
        self.egress, self.camera, self.note = g("egress", ""), g("camera", ""), g("note", "")


class Report:
    """Rows of (level, text), level in ok|bad|note|hdr; `failed` once any is bad."""

    def __init__(self):
        self.rows, self.failed = [], False

    def hdr(self, text):
        self.rows.append(("hdr", text))

    def ok(self, text):
        self.rows.append(("ok", text))

    def note(self, text):
        self.rows.append(("note", text))

    def bad(self, text):
        self.rows.append(("bad", text))
        self.failed = True

    def check(self, cond, good, bad):
        self.ok(good) if cond else self.bad(bad)

    def service(self, label, state):
        self.check(state == "running", label, "%s is not %s" % (label, "running" if state == "stopped" else "installed"))


def judge(facts, bc):
    """The health check's raw facts, judged here: the phone has no python3."""
    r = Report()
    r.hdr("Uplink")
    wifi = facts.get("wifi_iface") or "wlan0"
    r.check(facts.get("wifi_addr"), "%s %s" % (wifi, facts.get("wifi_addr")), "%s has no IPv4 address" % wifi)
    if facts.get("wifi_ssid"):
        r.note("SSID %s, signal %s" % (facts["wifi_ssid"], facts.get("wifi_signal", "?")))
    r.check(facts.get("wifi_power_save") == "off", "wifi power save off",
            "wifi power save is ON (%s) -- causes dropped links" % (facts.get("wifi_power_save") or "?"))

    r.hdr("Segment %s" % bc.segment)
    state = segment_state(facts)
    if state == "off":
        r.bad("%s missing -- nothing on the segment" % bc.iface)
        role = facts.get("seg_typec_role", "")
        if facts.get("seg_renamed_candidates"):
            r.note("the adapter is here and working -- %s -- it is just not called %s"
                   % (facts["seg_renamed_candidates"], bc.iface))
            r.note("wk machine setup %s renames it in place; no re-plug" % bc.name)
        elif facts.get("seg_unclaimed"):
            r.note("something on the bus looks like a network adapter with no driver bound (%s)" % facts["seg_unclaimed"])
        elif "[host]" in role:
            r.note("the port is in host mode; the bus enumerated %s" % (facts.get("seg_downstream") or "nothing at all"))
        else:
            r.note("the port is not in host mode (%s) -- run wk-bridge-usb-host" % (role or "no typec port"))
    else:
        r.ok("%s %s" % (bc.iface, facts.get("seg_addr", "")))
        r.check(state == "up", "cable connected", "no carrier on %s -- check the cable" % bc.iface)
        speed = facts.get("seg_usb_speed", "") if state == "up" else ""
        if speed:
            r.check(speed in ("480", "5000", "10000", "20000"), "USB %s Mbit/s" % speed,
                    "USB %s Mbit/s -- the NIC is on a full-speed controller" % speed)

    r.hdr("DHCP")
    if facts.get("leases_file_nonempty") == "yes":
        r.ok("leases recorded")
    else:
        r.note("no lease recorded -- not a fault by itself; the pings below are what matters")
    for entry in filter(None, facts.get("leases_ping", "").split(";")):
        ip, name, up = (entry.split(",") + ["", ""])[:3]
        label = "%s (%s)" % (ip, name or "reserved")
        r.check(up == "up", label + " responds", label + " unreachable")

    r.hdr("Tailnet")
    try:
        ts = json.loads(facts.get("ts_status_json") or "")
    except ValueError:
        ts = {}
    self_ = ts.get("Self") or {}
    backend = ts.get("BackendState", "?")
    r.check(backend == "Running", "tailscaled running", "tailscale backend is %s" % backend)
    r.check(self_.get("Online"), "online", "reported offline")
    tags = self_.get("Tags") or []
    r.check(tags, "tags %s" % tags, "UNTAGGED -- the node key will expire")
    routes = self_.get("PrimaryRoutes") or []
    r.check(routes, "routes %s" % routes, "no approved subnet route -- nothing behind this bridge is reachable")
    if routes and bc.segment not in routes:
        r.bad("%s is advertised but not approved -- nothing behind this bridge is reachable" % bc.segment)

    r.hdr("Services")
    for s in ("wk-bridge-dhcp", "wk-bridge-nftables", "wk-bridge-netwatch", "wk-bridge-usb-host"):
        r.service(s, facts.get("svc_" + s, "missing"))
    for label, key in (("sshd", "sshd"), ("chrony", "chrony"), ("networkmanager", "nm"), ("tailscale", "tailscale")):
        r.service(label, facts.get("svc_" + key, "missing"))
    r.check(facts.get("nft_table") == "yes", "nftables table loaded", "nftables table inet wkbridge missing")

    r.hdr("Routing")
    r.check(facts.get("ip_forward") == "1", "ipv4 forwarding on", "ipv4 forwarding is off")
    if bc.egress == "nat":
        r.check(facts.get("nft_masquerade") == "yes", "egress: NAT to the uplink",
                "egress is declared nat but no masquerade rule is loaded")
    else:
        r.ok("egress: none (nothing behind this bridge reaches the internet)")

    r.hdr("Name resolution and time")
    if facts.get("resolv_exists") != "yes":
        r.bad("no /etc/resolv.conf at all")
    elif "127.0.0.53" in facts.get("resolv_first", ""):
        r.bad("resolv.conf points at 127.0.0.53 -- a systemd-resolved stub, and there is none here")
    else:
        n = int(facts.get("resolv_nameservers") or 0)
        r.check(n, "resolv.conf: %d nameserver(s)" % n, "resolv.conf lists no nameserver")
    r.check(facts.get("dns_resolves") == "yes", "DNS resolves",
            "DNS does not resolve -- tailscale cannot reach its coordination server")
    r.check(int(facts.get("clock_year") or 0) >= 2024, "clock sane (%s)" % facts.get("clock_iso", ""),
            "clock is %s -- every TLS check fails, so tailscale cannot log in" % (facts.get("clock_year") or "?"))

    r.hdr("Hardening")
    r.check(facts.get("sshd_password_auth") == "no", "SSH password auth disabled",
            "SSH password auth is ENABLED (%s)" % (facts.get("sshd_password_auth") or "?"))
    if facts.get("watchdog_device") == "yes":
        r.check(facts.get("watchdog_fed") == "yes", "hardware watchdog fed",
                "/dev/watchdog exists but nothing is feeding it -- a hang stays hung")
    else:
        r.bad("no /dev/watchdog -- a kernel hang needs a person at the phone")
        if facts.get("watchdog_dt_node"):
            r.note("the device tree declares %s (%s) -- 'wk machine setup' loads it by modalias"
                   % (facts["watchdog_dt_node"], facts.get("watchdog_dt_compatible", "")))
    r.check(not int(facts.get("swap_nonzram") or 0), "swap is zram only", "swap on a non-zram device (eMMC wear)")

    if bc.camera != "off":
        r.hdr("Camera")
        if facts.get("camera_device") == "yes":
            r.check(facts.get("camera_streaming") == "yes", "streaming", "a capture device exists but the stream is not running")
        else:
            r.note("no capture device -- the camera kill switch is off, so nothing streams")

    r.hdr("System")
    crashed = [c for c in facts.get("crashed", "").split(",") if c]
    r.check(not crashed, "no crashed services", "crashed: %s" % " ".join(crashed))
    if facts.get("uptime"):
        r.note("uptime %s" % facts["uptime"])
    for entry in filter(None, facts.get("battery", "").split(";")):
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
    fmt = "%-28s %-10s %-16s %-12s %s\n"
    out.write(fmt % ("NAME", "DEVICE", "SEGMENT", "STATE", "NOTE"))
    for row in rows:
        out.write(fmt % (row["name"], row["device"] or "?", row["segment"] or "?", row["state"], row["note"]))
    if not rows:
        out.write("(no bridges declared -- machines/*.conf with kind=bridge)\n")
    out.write("\n"
              "  bare         answers ssh, nothing of this role on it yet\n"
              "  provisioned  'wk machine setup' has run against it\n"
              "  key-changed  answered with a host key not on record; after a reflash, ssh-keygen -R <name>\n"
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
        if c is None or c.get("kind") != "bridge":
            raise LookupError("'%s' is not a declared bridge. Declared bridges: %s"
                               % (name, ", ".join(self.names()) or "(none)"))
        return BridgeConf(name, c)

    def reaches(self, dest, timeout=8):
        return Ssh(dest, opts=ACCEPT_NEW, timeout=timeout, via=self.machine).run(["true"], timeout=timeout + 12).ok

    def login(self, *dests):
        return next((d for d in dests if self.reaches(d)), None)

    def as_root_or_user(self, bc, host):
        return self.login("root@" + host, "%s@%s" % (bc.user, host))

    def discover(self, bc):
        """The phone on a segment this machine sits on: reach's one enumeration, then each neighbour's own hostname."""
        swept = list(reach.Reach(self.machine, self.env).swept())
        if not [rows for _seg, rows in swept if rows is not None]:
            act.warn("this machine swept none of its segments (reach needs ip(8) and nmap), so %s is looked for\n"
                     "  by its conf name only; --at <address> names it outright" % bc.name)
            return None
        ips = [ip for _seg, rows in swept for ip, _mac, _state in rows or ()]
        with ThreadPoolExecutor(max_workers=SSH_PROBE_WORKERS) as pool:
            for hit in pool.map(lambda ip: ssh_is_phone(self.machine, ip, bc.user, bc.hostname), ips):
                if hit:
                    return hit
        act.log("  nothing on %s answers ssh as %s" % (", ".join(seg for seg, _rows in swept), bc.hostname))
        return None

    def resolve(self, name, at=None, names_only=False):
        bc = self.conf(name)
        if at:
            dest = self.as_root_or_user(bc, at)
            if dest:
                return dest
            raise Unreachable("--at %s does not answer ssh as root or '%s'; drop --at to try the phone's names." % (at, bc.user))
        dest = self.login("root@" + bc.ssh.split("@")[-1], bc.ssh)
        if dest:
            return dest
        found = None if names_only else self.discover(bc)
        if not found:
            raise Unreachable(
                "cannot reach %s by its conf name or on this machine's segments.\n"
                "    An unprovisioned phone goes silent under WiFi power save (iw dev wlan0 set power_save off,\n"
                "    on the phone) or phosh's suspend; the image and the role turn both off.\n"
                "    With its address:  wk machine setup %s --at <address>   (tailnet, rm and status take it too)\n"
                "    Never installed:   wk machine setup %s --disk <machine>:<device>" % (bc.name, bc.name, bc.name))
        dest = self.as_root_or_user(bc, found)
        if not dest:
            raise Unreachable("%s says it is %s but does not accept ssh as root or '%s'." % (found, bc.hostname, bc.user))
        act.log("found %s at %s (it identifies itself as %s) -- a DHCP lease, not written down" % (bc.name, found, bc.hostname))
        return dest

    def priv_prefix(self, name, ssh, dest):
        uid = ssh.run(["id", "-u"], timeout=15)
        if not uid.ok:
            raise Unreachable("cannot ssh to %s." % dest)
        if uid.out.strip() == "0":
            return []
        tool = next((t for t in ("doas", "sudo") if ssh.run(list(HAVE) + [t], timeout=15).ok), None)
        if not tool:
            raise Unreachable("ssh to %s lands on uid %s and the phone has neither doas nor sudo." % (dest, uid.out.strip()))
        if not ssh.run([tool, "-n", "true"], timeout=15).ok:
            raise NeedsPassword("'%s -n' on %s needs a password, and this is a read-only check with none to give.\n"
                                "    'wk machine setup %s' gives root the same key, once." % (tool, dest, name))
        return [tool, "-n"]

    def ls_row(self, name):
        bc = self.conf(name)
        phone = Ssh(bc.ssh, timeout=5, opts=ACCEPT_NEW, via=self.machine)
        r = phone.run(["true"], timeout=8)
        state = classify_ssh_error(r.err) if not r.ok else "provisioned" if phone.exists("/etc/wk-bridge.conf") else "bare"
        return {"name": name, "device": bc.device, "segment": bc.segment, "state": state, "note": bc.note}

    def ls(self, out=None):
        render_ls([self.ls_row(n) for n in self.names()], out or sys.stdout)
        return 0

    def status(self, name, at=None, out=None):
        out = out or sys.stdout
        bc = self.conf(name)
        dest = self.resolve(name, at=at)
        ssh = self.phone(dest)
        if not ssh.run(["test", "-x", HEALTHCHECK], timeout=15).ok:
            raise Unreachable("%s answers but has no bridge role on it; 'wk machine setup %s' provisions it." % (dest, name))
        facts = kv(ssh.run(self.priv_prefix(name, ssh, dest) + [HEALTHCHECK], timeout=30).out)
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
