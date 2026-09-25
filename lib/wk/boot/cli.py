"""wk boot: a machine's mode transition, read back against the record of intent the driver keeps on the machine."""

import json
import os
import sys

from wk import act, fleet, images, reach, record as wkrecord
from wk.boot import drivers, open_driver
from wk.kv import kv
from wk.machine import Local, is_macos

CONF_DEFAULTS = {"NODE_ROLE": "workstation", "NODE_OS": "any"}
BROKER_SOCKET = "/run/wk/broker.sock"
BROKER_VERBS = {"arm": "arm", "status": "status", "keep": "keep", "back": "release", "disarm": "disarm"}


def load_conf(root, name, env):
    """machines/<name>.conf of a board, a Mac or a guest, with NODE_NAME; None when it is none of those or declares no driver."""
    try:
        conf = fleet.Fleet(root, env).load(name)
    except fleet.ConfError:
        return None
    if not conf or conf["KIND"] not in fleet.BENCH_KINDS or not (conf.get("NODE_DRIVER") and conf.get("NODE_NOTE")):
        return None
    out = {k: v for k, v in conf.items() if k.startswith("NODE_")}
    out.update({k: v for k, v in CONF_DEFAULTS.items() if not out.get(k)}, NODE_NAME=name)
    return out


def listing(root, env):
    rows = []
    for n in fleet.Fleet(root, env).names(fleet.BENCH_KINDS):
        conf = load_conf(root, n, env)
        if conf:
            rows.append("%-8s%s" % (n, conf["NODE_NOTE"]))
    return "\n".join(rows)


def driver_for(root, conf):
    cls = drivers().get(conf["NODE_DRIVER"])
    if cls is None:
        act.die("no boot driver '%s' (machines/%s.conf's NODE_DRIVER; lib/wk/boot has %s)"
                % (conf["NODE_DRIVER"], conf["NODE_NAME"], ", ".join(sorted(drivers()))))
    return open_driver(root, conf)


class Boot:
    def __init__(self, root, conf, d, env=None, peers=None):
        self.root, self.conf, self.d = str(root), conf, d
        self.env = os.environ if env is None else env
        self.peers = peers
        self.name = conf["NODE_NAME"]
        self.rec = {}
        self.booted = self.boot_id = ""
        self.spent = None

    def c(self, key):
        return self.conf.get(key, "")

    def read_state(self):
        """Spent is decided by boot id, not by clocks: host and machine can disagree about the time without either being wrong.
        With either boot id missing, spent is None: unknown."""
        self.d.probe()
        self.booted, self.boot_id = self.d.booted_at(), self.d.boot_id()
        text = self.d.record_read()
        rec = kv(text)
        self.rec = {k: rec.get(k, "") for k in ("armed_at", "image", "armed_by", "armed_boot_id")}
        armed_boot = self.rec["armed_boot_id"]
        self.spent = armed_boot != self.boot_id if armed_boot and self.boot_id else None
        return self.d.mode

    def evidence(self):
        try:
            text = self.d.evidence()
        except act.Refused:
            return
        for line in (text or "").splitlines():
            act.log("  " + line)

    def gate(self):
        """Probing a machine its driver cannot reach reads the driving host and answers about the wrong computer."""
        if self.d.probeable():
            return
        here = "macos" if is_macos() else "linux"
        os_ = self.c("NODE_OS")
        if os_ != "any" and os_ != here:
            act.die("%s is driven from a %s host only (os=%s in\n    machines/%s.conf) and this is a %s host -- run this over there."
                    % (self.name, os_, os_, self.name, here))
        act.die("the '%s' driver cannot reach %s from here, so nothing about it\n    can be read. This host is the one machines/%s.conf\n"
                "    names (os=%s), so what is missing is on this host rather than the machine:\n    'wk doctor' names it -- a machine "
                "driven as a local guest needs tart and the\n    guest itself." % (self.c("NODE_DRIVER"), self.name, self.name, os_))

    # -- status

    def quiet_siblings(self):
        """(quiet, total) of the other fleet machines on this one's net and bridge, by what the tailnet says of their names."""
        rows = (self.peers or reach.Reach(env=self.env)).peers()
        if not rows:
            return 0, 0
        up = {p[0] for p in rows if p[2] == "up"}
        quiet = total = 0
        for n in fleet.Fleet(self.root, self.env).names(fleet.BENCH_KINDS):
            conf = load_conf(self.root, n, self.env) if n != self.name else None
            if not conf or conf.get("NODE_NET", "") != self.c("NODE_NET") or conf.get("NODE_BRIDGE", "") != self.c("NODE_BRIDGE"):
                continue
            total += 1
            names = [x.lower() for x in (conf.get("NODE_SSH"), conf.get("NODE_BENCH_SSH")) if x]
            quiet += not any(x in up for x in names)
        return quiet, total

    def status(self):
        mode, name, rec = self.read_state(), self.name, self.rec
        image = rec["image"]
        if mode == "unreachable":
            act.log("%s: unreachable over ssh in host mode" % name)
            self.evidence()
            if image:
                act.log("  the record says it was armed for system %s" % image)
            elif self.c("NODE_BRIDGE"):
                act.log("  no arming record could be read either -- but this machine is behind\n  %s, so rule the segment out before "
                        "the board:\n      wk machine status %s" % (self.c("NODE_BRIDGE"), self.c("NODE_BRIDGE")))
            else:
                act.log("  no arming record could be read either, so this is a plain outage\n"
                        "  or the machine is in a bench system that answers somewhere else.")
                quiet, total = self.quiet_siblings()
                if total and quiet == total:
                    act.log("  but every other %s device is quiet as well (%d of %d),\n  so rule the network out before the board -- "
                            "one of them may be a\n  machine nobody has touched, which no board fault explains."
                            % (self.c("NODE_NET") or "fleet", total, total))
            return 3
        if mode.startswith("bench"):
            act.log("%s: bench mode -- system %s (booted %s)" % (name, mode[6:], self.booted))
            self.evidence()
            if self.d.arming == "command":
                act.log("  the firmware default above is what the next boot enters; the job it was\n"
                        "  armed for hands the machine back when it ends.")
            else:
                act.log("  a plain reboot returns it to host mode: wk boot %s --back" % name)
            return 0
        if mode.startswith("base"):
            act.log("%s: base image -- system %s on %s (booted %s)" % (name, mode[5:], self.c("NODE_ROOT"), self.booted))
            self.evidence()
            act.log("  this is the fallback helper, not a bench system. To measure something,\n"
                    "  arm a system on %s:  wk boot %s --system <id>" % (self.c("NODE_DEVICE"), name))
            return 0
        act.log("%s: %s, host mode (booted %s)" % (name, self.c("NODE_ROLE"), self.booted))
        self.evidence()
        if not image:
            return 0
        who = "(by %s at %s)" % (rec["armed_by"], rec["armed_at"])
        if self.spent:
            act.log("  a spent arming record remains (system %s, armed by %s at %s)\n  it was consumed by the boot at %s; "
                    "clear it with: wk boot %s --disarm" % (image, rec["armed_by"], rec["armed_at"], self.booted, name))
            return 0
        if self.spent is None:
            act.warn("%s has an arming record for system %s %s, and whether a boot has spent it is unknown:\n  %s"
                     % (name, image, who, "the record carries no boot id" if not rec["armed_boot_id"] else "the machine reports no boot id"))
            act.log("  treat it as armed; clear it with: wk boot %s --disarm" % name)
            return 2
        if self.d.arming == "command":
            act.warn("%s is ARMED for '%s' %s" % (name, image, who))
            act.log("  the firmware was told, so a plain reboot enters bench mode.\n  cancel with: wk boot %s --disarm" % name)
            return 0
        act.warn("%s is ARMED to reboot into system %s %s" % (name, image, who))
        act.log("  it is still in host mode, so the one-shot has not been spent.\n  do not start work on it: the next reboot "
                "leaves host mode.\n  cancel with: wk boot %s --disarm" % name)
        return 2

    # -- the transitions

    def diag(self):
        mode = self.d.probe()
        if mode != "host":
            act.die("%s is in '%s'. The diagnostics dump is read off the\n    bench system's boot device from host mode -- ask the bench "
                    "system\n    directly, or 'wk boot %s --back' first." % (self.name, mode, self.name))
        print(self.d.diag())
        return 0

    def keep(self):
        mode = self.read_state()
        if not mode.startswith("bench"):
            act.die("%s is not in bench mode right now (%s), so there is no\n    watchdog to cancel." % (self.name, mode))
        if not self.d.watchdog_present():
            act.die("%s's bench system carries no self-return watchdog, so there is nothing\n    to claim: what ends its run is the job "
                    "itself. 'wk boot %s --status'\n    says what it is doing." % (self.name, self.name))
        if act.dry_run():
            act.log("would create /run/wk-keep-running on %s" % self.name)
            return 0
        if not self.d.sudo("keep.sh", mutates=True).ok:
            act.die("could not claim %s: /run/wk-keep-running was not created" % self.name)
        act.info("%s claimed: the self-return watchdog will not reboot it" % self.name)
        act.warn("nothing will hand this machine back on its own now -- 'wk boot %s --back' does" % self.name)
        return 0

    def reboot(self, armed):
        rc = self.d.reboot(armed=armed)
        if rc:
            raise act.Refused(rc)

    def back(self):
        mode = self.read_state()
        if mode == "unreachable":
            act.die("%s is not answering, so nothing here can reboot it.\n    The one-shot is spent by any boot, so a power cycle "
                    "returns it." % self.name)
        if act.dry_run():
            act.log("would reboot %s back to host mode and clear the record" % self.name)
            return 0
        if mode == "host":
            self.d.record_clear()
        self.reboot(False)
        act.info("%s is rebooting; the one-shot is spent, so it lands in host mode" % self.name)
        return 0

    def disarm(self):
        self.read_state()
        if not self.rec["image"] and self.d.arming != "medium":
            act.log("%s has no arming record" % self.name)
            return 0
        if act.dry_run():
            act.log("would disarm %s (its normal boot order, or its medium parked) and clear the record" % self.name)
            return 0
        if self.d.arming == "one-shot":
            if not self.spent:
                self.d.arm("", self.d.order_normal)
        elif self.d.disarms:
            self.d.disarm()
        self.d.record_clear()
        act.info("%s disarmed; its next reboot is a normal one" % self.name)
        if self.d.disarms and self.d.disarm_note():
            act.log(self.d.disarm_note())
        return 0

    def record_write(self, image, device, order):
        rc = self.d.record_write(image, self.c("NODE_PROFILE"), device, order)
        if rc:
            act.die("could not write the arming record on %s" % self.name, rc)

    def arm(self, want=""):
        mode, arming, name = self.read_state(), self.d.arming, self.name
        if mode == "unreachable" and arming != "guest":
            act.die("%s is not reachable over ssh in host mode.\n    Arming is an ssh command, so there is nothing to arm from here." % name)
        if mode.startswith("bench") and not self.d.arm_from_bench:
            act.die("%s is already in bench mode (system %s).\n    Arming it is an edit only its rescue can make, so it goes back "
                    "first:\n        wk boot %s --back" % (name, mode[6:], name))
        part = ""
        if arming == "command":
            image = want or self.c("NODE_VOLUME")
            if not image:
                act.die("%s has no benchmark volume configured (NODE_VOLUME)" % name)
        elif arming == "guest":
            image = want or self.d.facts().get("NODE_GUEST", "")
            if not image:
                act.die("%s has no guest configured (NODE_GUEST)" % name)
        else:
            part, image = self.d.select_system(want)
        watchdog = ""
        if arming in ("one-shot", "medium"):
            watchdog = (images.quiet_load(self.c("NODE_PROFILE"), self.env) or {}).get("IMG_WATCHDOG", "")
        if act.dry_run():
            act.log(self.arm_plan(image, watchdog) + "\ndry run -- nothing was armed.")
            return 0
        act.info("arming %s for system %s" % (name, image))
        if arming == "guest":
            self.d.arm(part, "")
            act.info("%s is in bench mode: guest '%s' is running and carries its marker" % (name, image))
            act.log("  'wk boot %s --back' stops it -- for a guest, leaving the role\n  is leaving the machine, so there is nothing "
                    "to hand back afterwards." % name)
            return 0
        if arming == "one-shot":
            self.record_write(image, self.c("NODE_DEVICE"), self.d.order_image)
            self.d.arm(part, self.d.order_image)
        elif arming == "command":
            self.d.arm(part, "")
            self.record_write(image, self.c("NODE_VOLUME"), "")
        else:
            self.record_write(image, self.c("NODE_DEVICE"), "")
            self.d.arm(part, "")
        self.reboot(True)
        act.info("%s is rebooting into system %s" % (name, image))
        if watchdog:
            act.log("  it returns by itself in %ss unless claimed:\n    wk boot %s --keep     claim it\n"
                    "    wk boot %s --back     hand it back now" % (watchdog, name, name))
        else:
            act.log("  nothing on this side returns it: the job it was armed for does, when it ends.\n"
                    "    wk boot %s --status   what it is doing, over its own node" % name)
        act.log("  if it never appears, its own account of that boot says why: wk boot %s --diag" % name)
        return 0

    def arm_plan(self, image, watchdog):
        d, name, arming = self.d, self.name, self.d.arming
        head = "would arm %s\n" % name
        if arming == "guest":
            return head + ("  guest        %s (%s)\n  boot order   nothing in firmware; arming a guest is starting it\n"
                           "  record       none; the guest either answers with a marker or it does not\n"
                           "  then         start the guest and wait for its marker. Nothing reboots this\n"
                           "               machine -- the rehearsal is a VM, which is the whole point."
                           % (image, d.ch.state() or "unknown"))
        record_at = d.facts()["NODE_RECORD"]
        if arming == "command":
            attached = "(attached)" if d.volume_present() else "(NOT attached -- arming would refuse)"
            return head + ("  volume       %s %s\n  boot order   the firmware's own boot-volume, through the privileged helper\n"
                           "  record       %s\n  then         prove the way back, tell the firmware, read it back, and reboot.\n"
                           "               Now: %s" % (image, attached, record_at, d.firmware_default()))
        order = ("untouched; the arming is %s's own boot partition" % self.c("NODE_DEVICE") if arming == "medium"
                 else "%s (one-shot; the normal order %s is untouched)" % (d.order_image, d.order_normal))
        return head + ("  system       %s (verified present on %s)\n  boot order   %s\n  record       %s on %s\n"
                       "  then         reboot, and the image's watchdog returns it in\n               %ss unless claimed"
                       % (image, self.c("NODE_DEVICE"), order, record_at, name, watchdog or "?"))


def fleet_probe(root, name, env):
    """What `wk status` shows of one fleet machine, as one JSON object; {} for a name that is not one."""
    conf = load_conf(root, name, env)
    if not conf or conf["NODE_DRIVER"] not in drivers():
        return {}
    d = open_driver(root, conf, env=env)
    out = dict(role=conf["NODE_ROLE"], probeable="yes", mode="", bridge=conf.get("NODE_BRIDGE", ""), armed="", armed_by="",
               armed_at="", armed_boot="", boot_id="")
    try:
        if d.probeable():
            out["mode"] = d.probe()
            if d.mode == "host":
                text = d.record_read()
                rec = kv(text)
                out.update(armed=rec.get("image", ""), armed_by=rec.get("armed_by", ""), armed_at=rec.get("armed_at", ""),
                           armed_boot=rec.get("armed_boot_id", ""), boot_id=d.boot_id())
        else:
            out["probeable"] = "no"
        out["media"] = d.media()
    except act.Refused:
        out.setdefault("media", "unknown")
    if not conf.get("NODE_PROFILE"):
        out["reprovision"] = "missing NODE_PROFILE in machines/%s.conf -- nothing to compose a recipe from" % name
    else:
        try:
            out["reprovision"] = d.reprovision()
        except act.Refused:
            out["reprovision"] = ""
    r = reach.Reach(env=env)
    out.update(tailnet=r.fleet_line(name), direct=r.without_tailnet(name))
    return out


def broker(root, name, action, system, env, machine=None):
    """A sandboxed `wk boot` is a request over the one socket a workspace sees; only the action word and the machine cross it."""
    from wk.targets import read_conf, workspace_marker_path
    if action == "diag":
        act.die("'wk boot %s --diag' mounts that machine's boot partition on the\n    workstation to read the system's own account of its last "
                "boot. That is a\n    disk read, not a mode transition, and it is not in the request broker's\n    vocabulary -- a workspace "
                "has no business mounting a filesystem out there.\n    Run it on the workstation:  wk boot %s --diag" % (name, name))
    if action not in BROKER_VERBS:
        act.die("'wk boot' acts on a host and its hardware, and this is workspace '%s'.\n    The request broker does not serve "
                "--%s -- what it does serve, it will say:\n    wk-broker-client.py capabilities\n    Run it on the workstation:  wk boot %s"
                % (read_conf(workspace_marker_path(env)).get("name", ""), action, name))
    words = ["machine=" + name] + (["system=" + system] if system and action == "arm" else [])
    words += ["dry_run=1"] if act.dry_run() and action != "status" else []
    return broker_request(root, BROKER_VERBS[action], words, env, machine or Local(), "wk boot %s" % name)


def broker_request(root, verb, words, env, machine, typed):
    """One request over the socket a workspace sees, `words` its key=value arguments; `typed` is what to run on the workstation instead."""
    sock = env.get("WK_BROKER_SOCKET") or BROKER_SOCKET
    if not machine.run(["test", "-S", sock]).ok:
        act.die("No request broker is listening at %s, so there is no door\n    from this workspace for '%s'. Somebody with the "
                "workstation opens it with:\n    ./setup --stage broker   ('wk doctor' then says it is reachable from in here).\n"
                "    Or run it on the workstation:  %s" % (sock, typed, typed))
    client = os.path.join(root, "container", "broker", "wk-broker-client.py")
    if not machine.exists(client):
        act.die("this workspace's copy of wk-tools has no broker client\n    (%s). Refresh it:  wk sync --tools container   on the "
                "workstation." % client)
    return machine.run_tty(["env", "WK_BROKER_SOCKET=" + sock, "python3", client, verb] + words).rc


def hold(root, name, action, env):
    """The two transitions that move the board out from under whatever is measuring on it, claimed fleet-wide."""
    records = wkrecord.Records(env=env)
    t = wkrecord.hold(records, lambda r: wkrecord.fleet_holders(r, records, wkrecord.fleet_stores(root, env, records.machine)),
                      name, "boot", name, "kill %d" % os.getpid(), "", ["%s %s" % (action, name)], os.getpid(), env)
    if t is not None:
        os.environ["WK_DEVICE_HELD"] = "device:" + name
    return t


def main(argv, env=None):
    """python3 -m wk.boot.cli fleet-probe <name>: `wk status`'s view of one machine, run under its ceiling."""
    env = os.environ if env is None else env
    root = env.get("WK_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    if len(argv) != 2 or argv[0] != "fleet-probe":
        print("usage: python3 -m wk.boot.cli fleet-probe <machine>", file=sys.stderr)
        return 2
    print(json.dumps(fleet_probe(root, argv[1], env)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
