"""The disks a machine offers for writing: its `lsblk -J` (over the boot Channel's host side) and its card helper's word on each."""

import json
from collections import namedtuple

from wk import act
from wk.boot.driver import CARD_PRIV
from wk.kv import kv

LSBLK = "lsblk -J -p -o NAME,SIZE,TRAN,RM,TYPE,MODEL,LABEL"
BOOTED = "is a disk this machine is running from"
TRANSPORTS = (("/dev/sd", "usb"), ("/dev/mmcblk", "mmc"), ("/dev/nvme", "nvme"))

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
        self.name, self.device = conf.get("name", ""), conf.get("device", "")

    def card(self, *args):
        return self.ch.card(*args, input="")

    def candidates(self):
        r = self.ch.run(LSBLK.split(), "host", input="")
        return parse_lsblk(r.out) if r.ok else []

    def whose(self, dev):
        """(machine=, booted): the helper refuses to mount the disk this machine runs from, and that refusal says so."""
        r = self.card("whose", dev)
        text = r.out + r.err
        return kv(text).get("machine", ""), BOOTED in text

    def for_machine(self, want, owners=None):
        owners = owners if owners is not None else {d.name: self.whose(d.name) for d in self.candidates()}
        return next((name for name, (owner, _) in owners.items() if want and owner == want), "")

    def listing(self):
        out, disks = [], self.candidates()
        owners = {d.name: self.whose(d.name) for d in disks}
        for d in disks:
            desc = "labels: " + ",".join(d.labels) if d.labels else "empty -- no partition table"
            here = "   <- %s is configured to boot from this one (wk boot %s)" % (self.name, self.name) if d.name == self.device else ""
            owner, booted = owners[d.name]
            what = ("holds this machine's own system" if owner == self.name else "holds a system for " + owner) if owner \
                else "this machine's own system (booted)" if booted else "no wk system on it"
            out += ["    %s%s" % (line(d), here), "        %s  --  %s" % (desc, what)]
        mine = self.for_machine(self.name, owners)
        if self.device and mine and mine != self.device:
            act.warn("%s's conf says it boots %s, but the disk holding\n  %s's own system is %s. Kernel names are assigned "
                     "in enumeration\n  order and these have moved. Trust the marker, not the name." % (self.name, self.device, self.name, mine))
        return "\n".join(out or ["    (none -- no removable disk is attached to %s)" % self.name])

    def refuse_unless_safe(self, dev):
        """The rule is the helper's `check`. Fit is not asked: the image is a stream whose size is known only once written."""
        if not self.card("status").ok:
            act.die("%s cannot write a disk: its card helper is missing, or its\n    sudoers rule is not in force. Everything "
                    "privileged here goes through it and\n    there is deliberately no second way in.\n    What fails:  "
                    "sudo -n %s status\n    The remedy, from a terminal on %s:  ./setup --stage quiesce" % (self.name, CARD_PRIV, self.name))
        r = self.card("check", dev)
        said = (r.out + r.err).rstrip("\n")
        if not r.ok:
            act.die("%s will not write %s:\n%s\n    Disks there:\n%s" % (
                self.name, dev, "\n".join("    " + s for s in said.splitlines()), self.listing()))
        act.debug(said)
