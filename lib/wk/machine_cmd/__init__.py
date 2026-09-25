"""`wk machine`: set up and remove the build machines, peers, boards, Macs and bridges in machines/, list the
fleet, and probe for a machine or sweep for every device. Each kind's setup and rm is its own module here."""

import json
import os
import sys

from wk import fleet, reach, targets
from wk.act import die, info, log, warn
from wk.machine import Local, Ssh
from wk.machine_cmd.board import BoardMachines
from wk.machine_cmd.bridge import BridgeMachines
from wk.machine_cmd.build import CONFS, BuildMachines
from wk.machine_cmd.mac import MacMachines
from wk.workspace import require_name


class Machines(BuildMachines, BoardMachines, MacMachines, BridgeMachines):
    def __init__(self, root, env=None, here=None, far=None):
        self.root = str(root)
        self.env = os.environ if env is None else env
        self.here = here or Local()
        self.far = far
        self.fleet = fleet.Fleet(self.root, self.env)
        self.reach = reach.Reach(self.here, self.env, self.fleet)

    def conf(self, name):
        try:
            return self.fleet.load(name)
        except fleet.ConfError as e:
            die(str(e))

    def target(self, name, conf):
        env = dict(self.env)
        env.update({k: v for k, v in conf.items() if k != "KIND"})
        t = targets.Remote(name, self.root, env, self.here)
        if self.far is not None:
            t.machine = self.far
        return t

    def rel(self, path):
        return os.path.relpath(path, self.root) if path.startswith(self.root + os.sep) else path

    # -- setup

    def setup(self, name, kind=None, at=None, no_tailnet=False, disk=None, image=None, rebuild=False):
        require_name(name)
        conf, path = self.conf(name), self.fleet.conf_path(name)
        new = conf is None
        if new:
            if not kind:
                die("'%s' has no conf yet (%s), so say what it is:\n"
                    "        wk machine setup %s --kind build   someone else's machine this one builds on\n"
                    "        wk machine setup %s --kind peer    a workstation with its own wk-tools" % (name, self.rel(path), name, name))
            if kind == "board":
                die("'%s' has no conf yet (%s). A board's driver, device and partitions cannot be\n"
                    "    derived, so copy an existing board's conf (machines/rpi3.conf and its kin) and fill\n"
                    "    in this one's, then re-run 'wk machine setup %s'." % (name, self.rel(path), name))
            if kind not in fleet.TARGET_KINDS:
                die("--kind %s: 'wk machine setup' makes a build machine or a peer (--kind build | peer)" % kind)
            conf = fleet.parse_text(CONFS[kind] % {"name": name})
        elif kind and kind != conf["KIND"]:
            die("%s says KIND=%s, and --kind %s disagrees. The conf is the answer:\n"
                "    edit it, or drop --kind." % (self.rel(self.fleet.path(name)), conf["KIND"], kind))
        if conf["KIND"] == "bridge":
            return self.bridge().setup(name, at=at, no_tailnet=no_tailnet, disk=disk, image=image, rebuild=rebuild)
        self.refuse_bridge_flags(conf, at or no_tailnet or disk or image or rebuild)
        if conf["KIND"] == "board":
            return self.setup_board(name, conf)
        if conf["KIND"] == "mac":
            return self.setup_mac(name, conf)
        if conf["KIND"] not in fleet.TARGET_KINDS:
            die("'%s' is a %s (%s): 'wk machine setup' sets up a build machine, a peer, a board or a Mac"
                % (name, conf["KIND"], self.rel(self.fleet.path(name))))
        t = self.target(name, conf)
        ok, why = t.answers()
        if not ok:
            die("cannot ssh to '%s' non-interactively: %s\n"
                "    A machine is named after an ssh destination that already works, keys and\n"
                "    ProxyJump included:  ssh -o BatchMode=yes %s true" % (t.label(), why, t.label()))
        if conf["KIND"] == "peer":
            return self.setup_peer(name, t, path, new)
        return self.setup_build(name, t, path, new)

    def board_machine(self, dest):
        """The Fake a test hands the constructor, or a real ssh to `dest`: boards and Macs are not
        Targets, so they take a Machine straight rather than through `target()`'s Remote wrapping."""
        return self.far if self.far is not None else Ssh(dest, timeout=reach.ssh_timeout(self.env), via=self.here)

    # -- rm

    def rm(self, name, at=None):
        require_name(name)
        conf = self.conf(name)
        if conf is None:
            die("no conf for '%s' (%s) -- nothing to remove" % (name, self.rel(self.fleet.conf_path(name))))
        path = self.fleet.path(name)
        if conf["KIND"] == "bridge":
            return self.bridge().rm(name, at=at)
        self.refuse_bridge_flags(conf, at)
        if conf["KIND"] == "board":
            return self.rm_board(name, conf, path)
        if conf["KIND"] not in fleet.TARGET_KINDS:
            die("'%s' is a %s (%s): 'wk machine rm' removes a build machine or a peer" % (name, conf["KIND"], self.rel(path)))
        return self.rm_target(name, conf, path)

    # -- ls and probe

    def ls(self, as_json=False, out=None):
        out = out or sys.stdout
        rows = []
        for name in self.fleet.names():
            c = self.conf(name)
            reached = "; ".join("%s %s" % (n, self.reach.tailnet(n) or "not a node") for n in self.reach.names(name))
            rows.append({"name": name, "kind": c["KIND"], "tailnet": reached, "note": c.get("NODE_NOTE") or c.get("BR_NOTE", ""),
                         "conf": self.rel(self.fleet.path(name))})
        if as_json:
            out.write(json.dumps({"machines": rows}) + "\n")
            return 0
        for r in rows:
            out.write("%-26s %-7s %s\n" % (r["name"], r["kind"], r["tailnet"]))
            if r["note"]:
                out.write("%-26s %-7s %s\n" % ("", "", r["note"]))
        return 0

    def answers(self, name, conf):
        """(True, "") or (False, why): a build machine or peer by its one probe, anything else by an ssh `true`, and
        a node the tailnet already reports down by that alone."""
        if conf["KIND"] in fleet.TARGET_KINDS:
            return self.target(name, conf).answers()
        dest = conf.get("NODE_SSH") or conf.get("BR_SSH") or name
        down = self.reach.offline(dest)
        if down:
            return False, down
        r = Ssh(dest, timeout=reach.ssh_timeout(self.env), via=self.here).run(["true"], timeout=reach.ssh_timeout(self.env) + 5)
        return (True, "") if r.ok else (False, targets.ssh_last_word(r))

    def probe_one(self, name, survey, as_json=False, out=None):
        out = out or sys.stdout
        conf = self.conf(name)
        if conf is None:
            die("'%s' is not a machine here. machines:\n%s" % (name, "".join("      %s\n" % n for n in self.fleet.names())))
        ok, why = self.answers(name, conf)
        doc = {"machine": name, "kind": conf["KIND"], "answers": ok, "why": why,
               "tailnet": {n: self.reach.tailnet(n) for n in self.reach.names(name)},
               "ssh": self.reach.ssh_path(conf.get("NODE_SSH") or conf.get("BR_SSH") or name)}
        mac = conf.get("NODE_MAC", "").lower()
        if not ok and mac:
            doc["sweep"] = survey.run(mac)
        if as_json:
            out.write(json.dumps(doc) + "\n")
        else:
            for n, where in doc["tailnet"].items():
                out.write("  %-16s %s\n" % ("tailnet " + n, where or "not a node"))
            out.write("  %-16s %s\n" % ("ssh", doc["ssh"] or "no route in the ssh config"))
            out.write("  %-16s %s\n" % ("answers", "yes" if ok else "no -- unreachable: %s" % why))
            if "sweep" in doc:
                render_survey(doc["sweep"], out)
                if doc["sweep"]["seen"]:
                    info("%s is up, at the address above (%s)" % (name, mac))
                else:
                    warn("%s (%s) is on none of the %d segment(s) that could be swept.\n"
                         "  Either it is powered off, or it is on a segment nothing here can see. A board is\n"
                         "  found by its hardware address, so this is a fact about the wire, not a name."
                         % (name, mac, doc["sweep"]["swept"]))
        return 0 if ok or doc.get("sweep", {}).get("seen") else 1

    def sweep(self, survey, as_json=False, out=None):
        out = out or sys.stdout
        doc = dict(survey.run(), want=None, want_mac=None)
        if as_json:
            out.write(json.dumps({k: v if k != "hits" else [{h: x[h] for h in reach.HIT_KEYS} for x in v]
                                  for k, v in doc.items()}) + "\n")
            return 0
        render_survey(doc, out)
        info("%d device(s) on %d segment(s)%s" % (doc["seen"], doc["swept"], ", %d unsweepable" % doc["blind"] if doc["blind"] else ""))
        return 0


def render_survey(doc, out):
    for v in doc["vantages"]:
        where = "as asked" if v["via"] == "given" else "from this machine" if v["vantage"] == "local" else "through " + v["via"]
        if not v["swept"]:
            warn("%s (%s): cannot be swept" % (v["segment"], where))
            if v["vantage"] == "local":
                log("  nmap is not installed here, and there is deliberately no second sweeper. './setup' installs it.")
            else:
                log("  %s is unreachable, or has no nmap. Every device on this segment is reached through it,\n"
                    "  so none of them is at fault for being absent here.  wk machine status %s" % (v["vantage"], v["via"]))
            continue
        info("%s (%s)%s" % (v["segment"], where, "" if v["found"] else ": nothing answered"))
        for h in (x for x in doc["hits"] if x["ip"] in v["found"]):
            render_hit(h, out)


def render_hit(h, out):
    def row(k, v):
        out.write("    %-16s %s\n" % (k, v))
    out.write("  %-15s %-17s %-9s %s\n" % (h["ip"], h["mac"], h["state"], h["machine"] or h["bridge"] or h["hostname"] or h["vendor"]))
    if h["machine"]:
        row("fleet machine", "%s -- machines/%s.conf%s" % (h["machine"], h["machine"], ", " + h["vendor"] if h["vendor"] else ""))
    elif h["vendor"]:
        row("hardware", h["vendor"])
    if h["_lease"]:
        row("reserved as", h["_lease"])
    if h["bridge"]:
        row("tailnet bridge", "%s -- machines/%s.conf" % (h["bridge"], h["bridge"]))
    if h["machine"] or h["lease"] or h["bridge"] or h["hostname"]:
        row("on the tailnet", "as " + h["tailnet_peer"] if h["tailnet_peer"] else
            "not as any name this repo knows -- this address is how it is reached")
    if h["hostname"]:
        row("answers ssh as", h["hostname"] + (" (as %s)" % h["_account"] if h["_account"] else "") +
            ("  (%s)" % h["uname"] if h["uname"] else ""))
        if h["wk_image_id"]:
            row("running", h["wk_image_id"])
            row("  profile", (h["wk_profile"] or "not in its marker") + (", built by " + h["wk_builder"] if h["wk_builder"] else ""))
            row("  role", (h["wk_role"] or "not in its marker -- a card written before roles existed; rewrite it") +
                (" (the card carries the rescue marker)" if h["marker"] == "rescue" else ""))
        else:
            row("running", "no /etc/wk-image -- not a system wk wrote")
        if h["tailscale"] != "yes":
            row("tailscale", "not installed on it, so its name cannot be how it is reached")
