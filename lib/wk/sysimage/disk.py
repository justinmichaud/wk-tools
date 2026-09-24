"""The disks a machine offers for writing: `lsblk -J` run on it and parsed here, and what its card helper says about
each one. The machine is reached over a boot Channel: its `m_ssh` for reads, its `card_priv` for the helper."""

import json
import os
import sys
from collections import namedtuple

from wk import act
from wk.boot.driver import BashChannel, disk_of, kv, part, partno

CARD_PRIV = "/usr/local/libexec/wk-card-priv"
LSBLK = "lsblk -J -p -o NAME,SIZE,TRAN,RM,TYPE,MODEL,LABEL"
BOOTED = "is a disk this machine is running from"
TRANSPORTS = (("/dev/sd", "usb"), ("/dev/mmcblk", "mmc"), ("/dev/nvme", "nvme"))
UPDATE = ("Update it first: on a workstation,\n    ./setup --stage quiesce from a terminal there; on a rescue, rebuild the\n"
          "    rescue image and write it again.")

Disk = namedtuple("Disk", "name size tran rm model labels")


def parse_spec(spec):
    machine, _, dev = spec.partition(":")
    if not machine:
        act.die("--disk needs a machine: --disk <machine>:<device>")
    return machine, dev


def is_second(dev):
    return dev.endswith(("@second", "@third"))


def tran_of_name(dev):
    return next((t for prefix, t in TRANSPORTS if dev.startswith(prefix)), "")


def parse_lsblk(text):
    """Whole disks with RM set (readers, sticks) or on usb/mmc (USB SSDs, SD slots); RM is "0"/"1" before util-linux 2.33."""
    try:
        devices = json.loads(text).get("blockdevices") or []
    except (ValueError, AttributeError):
        act.die("could not read the disks: `%s` did not print JSON:\n    %s" % (LSBLK, text.strip()[:200]))
    found = []
    for d in devices:
        rm = d.get("rm") in (True, "1")
        if d.get("type") != "disk" or not (rm or d.get("tran") in ("usb", "mmc")):
            continue
        labels = [c.get("label") or "-" for c in d.get("children") or [] if c.get("type") == "part"]
        found.append(Disk(d.get("name", ""), d.get("size") or "", d.get("tran") or "-", "1" if rm else "0",
                          (d.get("model") or "").strip(), labels))
    return found


def line(d):
    return " ".join(w for w in (d.name, d.size, d.tran, d.rm, "disk", d.model) if w)


class Disks:
    def __init__(self, ch, conf):
        self.ch, self.conf = ch, conf
        self.name, self.device = conf.get("NODE_NAME", ""), conf.get("NODE_DEVICE", "")
        self._disks = self._can = None
        self._whose = {}

    def card(self, *args):
        return self.ch.call("card_priv", *args, input="")

    def candidates(self):
        if self._disks is None:
            r = self.ch.call("m_ssh", LSBLK, input="")
            self._disks = parse_lsblk(r.out) if r.ok else []
        return self._disks

    def can_identify(self):
        """A helper older than `whose` answers it with its usage line."""
        if self._can is None:
            r = self.card("whose")
            self._can = "usage: wk-card-priv" not in r.out + r.err
        return self._can

    def whose(self, dev):
        """(machine=, booted): the helper refuses to mount the disk this machine runs from, and that refusal says so."""
        if not self.can_identify():
            return "", False
        if dev not in self._whose:
            r = self.card("whose", dev)
            text = r.out + r.err
            self._whose[dev] = (kv(text, "machine"), BOOTED in text)
        return self._whose[dev]

    def resolve_own(self):
        """NODE_DEVICE's transport less disks marked for another machine, so a blank medium needs no marker; "" if not one."""
        want = tran_of_name(self.device)
        same = [d.name for d in self.candidates() if d.tran == want] if want else []
        if len(same) < 2:
            return "".join(same)
        left = []
        for dev in same:
            owner = self.whose(dev)[0]
            if owner == self.name:
                return dev
            if not owner:
                left.append(dev)
        return left[0] if len(left) == 1 else ""

    def own_or_declared(self):
        got = self.resolve_own()
        if not got:
            act.debug("could not resolve %s's own medium from the machine; using %s as declared" % (self.name, self.device or "none"))
            return self.device
        if got != self.device:
            act.warn("%s's conf says %s, but its own medium is %s right now.\n  Kernel names move; this is using %s, "
                     "which is what the machine says." % (self.name, self.device, got, got))
        return got

    def for_machine(self, want):
        return next((d.name for d in self.candidates() if want and self.whose(d.name)[0] == want), "")

    def listing(self):
        out = []
        for d in self.candidates():
            desc = "labels: " + ",".join(d.labels) if d.labels else "empty -- no partition table"
            here = "   <- %s is configured to boot from this one (wk boot %s)" % (self.name, self.name) if d.name == self.device else ""
            out.append("    %s%s" % (line(d), here))
            owner, booted = self.whose(d.name)
            if owner:
                out.append("        %s  --  holds %s" % (desc, "this machine's own system" if owner == self.name else "a system for " + owner))
            elif booted:
                out.append("        %s  --  this machine's own system (booted)" % desc)
            elif self.can_identify():
                out.append("        %s  --  no wk system on it" % desc)
            else:
                out.append("        " + desc)
        if not out:
            out.append("    (none -- no removable disk is attached to %s)" % self.name)
        if not self.can_identify():
            act.warn("cannot tell which system is on any of these: %s's card helper is\n  older than this checkout and has no "
                     "'whose' verb. The disks are listed by what\n  their filesystems are labelled, which is not the same "
                     "question.\n  Remedy, from a terminal on %s:  ./setup --stage quiesce" % (self.name, self.name))
        mine = self.for_machine(self.name)
        if self.device and mine and mine != self.device:
            act.warn("%s's conf says it boots %s, but the disk holding\n  %s's own system is %s. Kernel names are assigned "
                     "in enumeration\n  order and these have moved. Trust the marker, not the name." % (self.name, self.device, self.name, mine))
        return "\n".join(out)

    def refuse_unless_safe(self, dev):
        """The rule is the helper's `check`. Fit is not asked: the image is a stream whose size is known only once written."""
        status = self.card("status")
        if not status.ok:
            act.die("%s cannot write a disk: its card helper is missing, or its\n    sudoers rule is not in force. Everything "
                    "privileged here goes through it and\n    there is deliberately no second way in.\n    What fails:  "
                    "sudo -n %s status\n    The remedy, from a terminal on %s:  ./setup --stage quiesce" % (self.name, CARD_PRIV, self.name))
        # A helper without @second answers for the whole disk, and would write over the rescue it may be running from.
        slots = () if not is_second(dev) else ("second", "third") if dev.endswith("@third") else ("second",)
        for slot in slots:
            if slot + "=yes" not in status.out:
                act.die("%s's card helper predates %s systems (@%s), so it\n    would write the whole disk. %s" % (self.name, slot, slot, UPDATE))
        r = self.card("check", dev)
        said = (r.out + r.err).rstrip("\n")
        if not r.ok:
            act.die("%s will not write %s:\n%s\n    Disks there:\n%s" % (
                self.name, dev, "\n".join("    " + s for s in said.splitlines()), self.listing()))
        act.debug(said)


def _disks(env, root):
    conf = {k: v for k, v in env.items() if k.startswith("NODE_")}
    return Disks(BashChannel(root, conf, env.get("MODE_CHANNEL") or "none"), conf)


def _say(text):
    if text:
        print(text)
    return 0


VERBS = {
    "candidates": lambda d, a: _say("\n".join(line(x) for x in d().candidates())),
    "tran": lambda d, a: _say(tran_of_name(a[0])),
    "own-or-declared": lambda d, a: _say(d().own_or_declared()),
    "list": lambda d, a: _say(d().listing()),
    "part": lambda d, a: _say(part(a[0], a[1])),
    "disk-of": lambda d, a: _say(disk_of(a[0])),
    "partno": lambda d, a: _say(partno(a[0])),
}


def main(argv, env=None):
    env = os.environ if env is None else env
    if not argv or argv[0] not in VERBS:
        sys.stderr.write("usage: python3 -m wk.sysimage.disk {%s} [args]\n" % "|".join(VERBS))
        return 2
    root = env.get("WK_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    try:
        return VERBS[argv[0]](lambda: _disks(env, root), argv[1:]) or 0
    except act.Refused as e:
        return e.status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
