"""How a machine is reached, computed and never written down: the tailnet, then `ssh -G`, then a sweep for its hardware
address. One `Reach` reads the tailnet once, under a ceiling: a wedged tailscaled has no timeout of its own."""

import ipaddress
import json
import os
import re
import sys
import threading

from wk import fleet
from wk.kv import kv
from wk.machine import Local, Ssh

APP_CLI = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"   # the App Store build
NEIGH_STATES = ("REACHABLE", "STALE", "DELAY", "PROBE", "PERMANENT", "NOARP")
# Unprivileged nmap calls a host with tcp/80 and tcp/443 closed `down`, so the neighbour table is the answer.
SWEEP = """PATH=$PATH:/usr/sbin:/sbin
command -v nmap >/dev/null 2>&1 || exit 66
nmap -sn -n --host-timeout 5s "$1" >/dev/null 2>&1 || true
ip neigh show 2>/dev/null"""


def parse_peers(text):
    """DNSName, not HostName: macOS capitalises HostName, and both bridge phones answer `localhost`."""
    try:
        doc = json.loads(text)
    except ValueError:
        return []
    rows = []

    def row(p, online):
        name = (p.get("DNSName") or "").split(".")[0].lower()
        if name:
            rows.append((name, (p.get("TailscaleIPs") or [""])[0], "up" if online else "down"))
    if doc.get("Self"):
        row(doc["Self"], True)
    for p in (doc.get("Peer") or {}).values():
        row(p, p.get("Online"))
    return rows


def peer_rows(text):
    return [tuple((line.split("\t") + ["", ""])[:3]) for line in text.splitlines() if line.strip()]


def parse_neigh(text, cidr):
    net = ipaddress.ip_network(cidr, strict=False)
    out = []
    for line in text.splitlines():
        f = line.split()
        if not f or "FAILED" in f or "INCOMPLETE" in f or not re.match(r"^\d+\.", f[0]):
            continue
        mac = f[f.index("lladdr") + 1].lower() if "lladdr" in f[:-1] else ""
        try:
            inside = ipaddress.ip_address(f[0]) in net
        except ValueError:
            continue
        if mac and inside:
            out.append((f[0], mac, next((s for s in f if s in NEIGH_STATES), "?")))
    return out


def parse_segments(text):
    out = []
    for line in text.splitlines():
        f = line.split()
        if len(f) < 4 or f[1] in ("lo", "tailscale0") or "/" not in f[3]:
            continue
        seg = str(ipaddress.ip_network(f[3], strict=False))
        if seg not in out:
            out.append(seg)
    return out


def ssh_timeout(env):
    return int(env.get("WK_SSH_TIMEOUT") or 10)


class Reach:
    def __init__(self, machine=None, env=None, fleet_=None, peers=None):
        self.machine = machine or Local()
        self.env = os.environ if env is None else env
        self.fleet = fleet_ or fleet.Fleet(self.env.get("WK_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), self.env)
        self._peers = peers
        self._lock = threading.Lock()

    def peers(self):
        with self._lock:
            if self._peers is None:
                self._peers = self._read_peers()
            return self._peers

    def _read_peers(self):
        cap = int(self.env.get("WK_TAILSCALE_TIMEOUT") or 4)
        r = self.machine.run(["tailscale", "status", "--json"], timeout=cap)
        if r.rc == 127 and self.machine.exists(APP_CLI):
            r = self.machine.run([APP_CLI, "status", "--json"], timeout=cap)
        return parse_peers(r.out) if r.ok else []

    def peer(self, name):
        return next((p for p in self.peers() if p[0] == (name or "").lower()), None)

    def tailnet(self, name):
        p = self.peer(name)
        return "%s (%s)" % (p[1], p[2]) if p else ""

    def offline(self, name):
        """Why the coordinator already reports `name` down, or "": ssh to such a node spends its whole ConnectTimeout learning it."""
        p = self.peer(name)
        if p and p[2] == "down":
            return "the tailnet says %s is offline -- power it on, or 'wk machine probe %s'" % (name, name)
        return ""

    def conf(self, m):
        try:
            return self.fleet.load(m) or {}
        except fleet.ConfError:
            return {}

    def names(self, m):
        c = self.conf(m)
        out = []
        for n in (c.get("NODE_SSH") or m, c.get("NODE_BENCH_SSH"), c.get("BR_SSH")):
            if n and n not in out:
                out.append(n)
        return out

    def fleet_line(self, m):
        if self.conf(m).get("KIND") not in fleet.BENCH_KINDS:
            return ""
        return "; ".join("%s %s" % (n, self.tailnet(n) or "not a node") for n in self.names(m))

    def ssh_path(self, name):
        r = self.machine.run(["ssh", "-G", name])
        if not r.ok:
            return ""
        g = {}
        for line in r.out.splitlines():
            k, _, v = line.partition(" ")
            g.setdefault(k, v.strip())
        host = g.get("hostname", "")
        if not host:
            return ""
        out = host + ("" if g.get("port", "22") == "22" else ":" + g["port"])
        if g.get("user"):
            out = g["user"] + "@" + out
        if g.get("proxyjump") and g["proxyjump"] != "none":
            out += "  (through %s)" % g["proxyjump"]
        return out

    def segments_local(self):
        return parse_segments(self.machine.run(["ip", "-4", "-o", "addr", "show"]).out)

    def sweep(self, cidr, vantage="local", timeout=60):
        on = self.machine if vantage == "local" else Ssh(vantage, timeout=ssh_timeout(self.env), via=self.machine)
        r = on.run(["sh", "-c", SWEEP, "sh", cidr], timeout=timeout + 10)
        if r.rc in (66, 127, 255):
            return None
        return parse_neigh(r.out, cidr)

    def swept(self):
        for seg in self.segments_local():
            yield seg, self.sweep(seg)

    def find_mac(self, mac):
        mac = (mac or "").lower()
        for seg, rows in self.swept() if mac else ():
            hit = next((ip for ip, m, _s in rows or () if m == mac), None)
            if hit:
                return "%s  (found by sweeping %s -- not stored)" % (hit, seg)
        return ""

    def without_tailnet(self, m):
        if any(self.tailnet(n) for n in [m] + self.names(m)):
            return ""
        path = self.ssh_path(m)
        dialled = path.split("@")[-1].split(":")[0].split("  ")[0]
        if path and dialled != m:   # `ssh -G` answers with the name itself when no HostName is written down
            return path
        mac = self.conf(m).get("NODE_MAC", "")
        if not mac:
            return ""
        return self.find_mac(mac) or "not on the tailnet -- wk machine probe %s sweeps for it" % m



IDENTIFY = """cat /etc/wk-image 2>/dev/null
echo "uname=$(uname -srm 2>/dev/null)"
echo "host=$(hostname 2>/dev/null)"
command -v tailscale >/dev/null 2>&1 && echo "tailscale=yes" || echo "tailscale=no"
[ -e /etc/wk/rescue ] && echo "marker=rescue" || echo "marker=none"
"""
UNPINNED = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR"]
# Not a database: the prefixes the Raspberry Pi Foundation owns, and the one the phones' USB adapters synthesise.
OUI = {"b8:27:eb": "Raspberry Pi (pre-4)", "d8:3a:dd": "Raspberry Pi", "dc:a6:32": "Raspberry Pi",
       "e4:5f:01": "Raspberry Pi", "2c:cf:67": "Raspberry Pi", "00:00:00": "synthesised (USB adapter, no EEPROM)"}
HIT_KEYS = ("ip", "mac", "state", "machine", "vendor", "lease", "bridge", "hostname", "tailnet_peer", "wk_image_id",
            "wk_role", "wk_profile", "wk_builder", "tailscale", "marker", "uname")


class Survey:
    """Every device on every segment this machine or a bridge phone sits on, joined to what this repo declares."""

    def __init__(self, reach, segment=None, vantage=None, ssh=True, timeout=60):
        self.r, self.segment, self.vantage, self.ssh, self.timeout = reach, segment, vantage, ssh, timeout

    def bridges(self):
        return self.r.fleet.names(("bridge",))

    def vantages(self):
        if self.segment:
            return [(self.vantage or "local", self.segment, "given")]
        out = [("local", seg, "local") for seg in self.r.segments_local()]
        for b in self.bridges():
            c = self.r.conf(b)
            if c.get("BR_SEGMENT"):
                out.append((c.get("BR_SSH") or b, c["BR_SEGMENT"], b))
        return [v for v in out if not self.vantage or v[0] == self.vantage]

    def fleet_macs(self):
        out = {}
        for m in self.r.fleet.names(fleet.BENCH_KINDS):
            mac = self.r.conf(m).get("NODE_MAC", "").lower()
            if mac:
                out[mac] = m
        return out

    def leases(self):
        out = {}
        for b in self.bridges():
            for lease in self.r.conf(b).get("BR_LEASES", "").split():
                f = lease.split(",")
                if len(f) >= 3:
                    out.setdefault(f[0].lower(), (f[1], f[2], b))
        return out

    def bridge_addrs(self):
        """A bridge phone declares no MAC (its adapter is whichever dock is plugged in): only its address ties it to the wire."""
        out = {}
        for b in self.bridges():
            addr = self.r.without_tailnet(b).split(" ")[0]
            if re.match(r"^\d+\.\d+\.\d+\.\d+$", addr):
                out[addr] = b
        return out

    def _ask(self, dest, opts=()):
        r = Ssh(dest, opts=["-o", "LogLevel=ERROR"] + list(opts), timeout=4, via=self.r.machine).run(["sh", "-c", IDENTIFY],
                                                                                                    timeout=8)
        return r.out if r.ok and r.out.strip() else ""

    def identify(self, ip, name):
        """What answers at `ip`: by its declared ssh name first, since a fleet machine in host mode takes Tailscale SSH
        and refuses its own LAN address; then by address, as this account and as the bench install's."""
        if not self.ssh:
            return {}
        if name:
            out = self._ask(name)
            if out:
                return kv(out)
        me = self.r.machine.run(["id", "-un"]).out.strip()
        for who in (me, self.r.env.get("WK_BENCH_USER") or "bench"):
            out = self._ask("%s@%s" % (who, ip), UNPINNED)
            if out:
                return dict(kv(out), account=who)
        return {}

    def hit(self, ip, mac, state, known):
        macs, leases, addrs = known
        machine = macs.get(mac, "")
        lease = leases.get(mac)
        bridge = addrs.get(ip, "")
        declared = bridge or (self.r.conf(machine).get("NODE_SSH", "") if machine else "")
        seen = self.identify(ip, declared)
        peer = ""
        for n in (machine, lease[1] if lease else "", bridge, seen.get("host", "")):
            p = self.r.peer(n) if n else None
            if p:
                peer = "%s at %s (%s)" % p
                break
        h = dict(ip=ip, mac=mac, state=state, machine=machine, vendor=OUI.get(mac[:8], ""),
                 lease=lease[1] if lease else "", bridge=bridge, hostname=seen.get("host", ""), tailnet_peer=peer,
                 wk_image_id=seen.get("id", ""), wk_role=seen.get("role", ""), wk_profile=seen.get("profile", ""),
                 wk_builder=seen.get("builder", ""), tailscale=seen.get("tailscale", ""), marker=seen.get("marker", ""),
                 uname=seen.get("uname", ""))
        h["_lease"] = "%s (%s pins %s)" % (lease[1], lease[2], lease[0]) if lease else ""
        h["_account"] = seen.get("account", "")
        return h

    def run(self, want_mac=""):
        known = (self.fleet_macs(), self.leases(), self.bridge_addrs())
        vantages, hits = [], []
        for name, cidr, via in self.vantages():
            rows = self.r.sweep(cidr, name, self.timeout)
            vantages.append({"vantage": name, "segment": cidr, "via": via, "swept": rows is not None, "found": []})
            for ip, mac, state in sorted(rows or (), key=lambda row: ipaddress.ip_address(row[0])):
                if not want_mac or mac == want_mac:
                    hits.append(self.hit(ip, mac, state, known))
                    vantages[-1]["found"].append(ip)
        swept = sum(1 for v in vantages if v["swept"])
        return {"seen": len(hits), "swept": swept, "blind": len(vantages) - swept, "vantages": vantages, "hits": hits}


def main(argv, env=None, stdin=None, out=None):
    """lib/reach.sh's entry: `tailnet`, `offline` and `without-tailnet` take the shell's one tailnet read on stdin."""
    env = os.environ if env is None else env
    out = out or sys.stdout
    verb, args = (argv[0], argv[1:]) if argv else ("", [])
    peers = peer_rows((stdin or sys.stdin).read()) if verb in ("tailnet", "offline", "without-tailnet") else None
    r = Reach(env=env, peers=peers)
    if verb == "peers" and not args:
        out.write("".join("\t".join(p) + "\n" for p in r.peers()))
    elif verb == "tailnet" and len(args) == 1:
        out.write(r.tailnet(args[0]))
    elif verb == "offline" and len(args) == 1:
        out.write(r.offline(args[0]))
    elif verb == "without-tailnet" and len(args) == 1:
        out.write(r.without_tailnet(args[0]))
    else:
        sys.stderr.write("usage: python3 -m wk.reach peers | tailnet|offline|without-tailnet <name> (peers on stdin)\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
