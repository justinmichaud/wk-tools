"""The boot-driver interface: how a machine is put into its bench system for one boot, and read back, over a Channel."""

import os
import re
import shlex
import time

from wk import act, record as wkrecord, shell
from wk.machine import Local

RECORD = "/var/lib/wk/boot/armed"
SLOTS = ("first", "second", "third")
SAFE = re.compile(r"^[A-Za-z0-9_./:@+-]*$")


def part(disk, n):
    return "%sp%s" % (disk, n) if disk[-1:].isdigit() else "%s%s" % (disk, n)


def disk_of(p):
    m = re.match(r"^(.*\d)p\d+$", p)
    return m.group(1) if m else re.sub(r"\d+$", "", p)


def partno(p):
    return re.search(r"(\d+)$", p).group(1) if re.search(r"\d$", p) else ""


def kv(text, key):
    for line in (text or "").splitlines():
        k, sep, v = line.partition("=")
        if sep and k == key:
            return v.rstrip("\r")
    return ""


class Onboard:
    """A boot/onboard/ file verbatim, led by its `KEY=value; ` parameters; continuations joined: an ExecStart takes one line."""

    def __init__(self, root, name, **params):
        for k, v in params.items():
            if not SAFE.match(str(v)):
                raise ValueError("%s=%r is not one word both a board's shell and a systemd ExecStart read literally" % (k, v))
        self.root, self.name, self.params = str(root), name, params

    def path(self):
        return os.path.join(self.root, "boot", "onboard", self.name)

    def text(self):
        with open(self.path()) as f:
            body = f.read().replace("\\\n", "").rstrip("\n")
        return "".join("%s=%s; " % kv for kv in sorted(self.params.items())) + body


class BashChannel:
    """The answering system through boot/machines.sh's transport (m_ssh, i_ssh, r_ssh, r_sudo, boot_priv, card_priv)."""

    def __init__(self, root, conf, channel="none", bash_driver=False, machine=None):
        self.root, self.conf, self.channel, self.bash_driver = str(root), conf, channel, bash_driver
        self.machine = machine or Local()

    def call(self, fn, *args, input=None, mutates=False):
        words = [("sh -c " + shlex.quote(a.text())) if isinstance(a, Onboard) else a for a in args]
        env = ["%s=%s" % (k, v) for k, v in sorted(self.conf.items()) if k.startswith("NODE_")]
        argv = ["env", *env, "MODE_CHANNEL=" + self.channel] + shell.argv(
            self.root, '. "%s/boot/machines.sh"; boot_bridge' % self.root,
            *(["--driver"] if self.bash_driver else []), fn, *words)
        r = (self.machine.act_run if mutates else self.machine.run)(argv, input=input)
        if mutates and r.err:
            act.log(r.err.rstrip("\n"))
        return r


class Driver:
    name = ""
    arming = "one-shot"
    arm_from_bench = False
    order_image = order_normal = ""
    system_parts = (1,)
    failsafe = None
    selects_by_partition = False
    disarms = False

    def __init__(self, root, conf, ch, mode=""):
        self.root, self.conf, self.ch, self.mode = str(root), conf, ch, mode

    def c(self, key):
        return self.conf.get(key, "")

    def ob(self, name, **params):
        return Onboard(self.root, name, **params)

    def run(self, name, mutates=False, **params):
        return self.ch.call("r_ssh", self.ob(name, **params), mutates=mutates)

    def sudo(self, name, mutates=False, **params):
        return self.ch.call("r_sudo", self.ob(name, **params), mutates=mutates)

    def card(self, *args, mutates=False):
        return self.ch.call("card_priv", *args, mutates=mutates)

    def facts(self):
        return {"BOOT_ARMING": self.arming, "B_ARM_FROM_BENCH": "yes" if self.arm_from_bench else "no",
                "BOOT_ORDER_IMAGE": self.order_image, "BOOT_ORDER_NORMAL": self.order_normal, "NODE_RECORD": RECORD}

    # -- what is running
    def probeable(self):
        return True

    def probe(self):
        pr = self.ob("probe.sh")
        r = self.ch.call("m_ssh", pr)
        if r.ok:
            self.ch.channel = "host"
        else:
            r = self.ch.call("i_ssh", pr)
            if not (r.ok and re.search(r"(?m)^id=", r.out)):
                self.ch.channel, self.mode = "none", "unreachable"
                return self.mode
            self.ch.channel = "bench"
        ident = kv(r.out, "id")
        if not ident:
            self.mode = "host"
            return self.mode
        kind = self.system_kind(kv(r.out, "rootdev"))
        if kind == "unknown":
            kind = "base" if kv(r.out, "role") == "rescue" else "bench"
        self.mode = "%s %s" % (kind, ident)
        return self.mode

    def system_kind(self, rootdev):
        """NODE_ROOT first: on a one-medium board (rpi3) both prefixes match."""
        for key, kind in (("NODE_ROOT", "base"), ("NODE_DEVICE", "bench")):
            if rootdev and self.c(key) and rootdev.startswith(self.c(key)):
                return kind
        return "unknown"

    def boot_id(self):
        return self.run("boot-id.sh").out.strip()

    def booted_at(self):
        btime = re.sub(r"\D", "", self.run("booted-at.sh").out)
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(btime))) if btime else ""

    def display(self):
        return self.c("NODE_DISPLAY") or None

    def watchdog_present(self):
        return self.sudo("watchdog-present.sh").ok

    # -- the medium
    def boot_part(self):
        return part(self.c("NODE_DEVICE"), 1)

    def system_part(self, n):
        return part(self.c("NODE_DEVICE"), n)

    def slot(self, p):
        n = int(partno(p) or 0)
        return SLOTS[self.system_parts.index(n)] if n in self.system_parts else "?"

    def part_absent(self, dev):
        fn = "r_ssh" if self.c("NODE_ROLE") == "bench-device" else "m_ssh"
        r = self.ch.call(fn, self.ob("part-absent.sh", WK_DEV=dev))
        return r.ok and r.out.replace("\r", "").strip() == "no"

    def medium_read(self, p, name):
        """"" for a file the partition lacks, None when unreadable; a bench-device mounts its own medium."""
        if self.c("NODE_ROLE") == "bench-device":
            r = self.sudo("medium-read.sh", WK_PART=p, WK_NAME=name)
            return r.out if r.ok else None
        r = self.card("boot-read", disk_of(p), partno(p), name)
        if r.ok:
            return r.out
        if self.part_absent(p):
            return ""
        act.warn("%s could not read %s off %s.\n    Its card helper is older than this verb, or its sudoers rule is not in\n"
                 "    force; a workstation has no second way to reach the medium.\n"
                 "    What fails:  sudo -n /usr/local/libexec/wk-card-priv boot-read ...\n"
                 "    The remedy, from a terminal on %s:  ./setup --stage quiesce" % (self.c("NODE_NAME"), name, p, self.c("NODE_NAME")))
        return None

    def device_image(self, p=None):
        out = self.medium_read(p or self.boot_part(), "wk-image.id")
        return None if out is None else re.sub(r"[\r\n ]", "", out)

    def systems(self):
        found = []
        for n in self.system_parts:
            p = self.system_part(n)
            ident = self.device_image(p)
            if ident is None:
                return None
            if ident:
                found.append((p, ident))
        return found

    def label(self, systems, p, ident):
        return ident if [i for _, i in systems].count(ident) == 1 else "%s@%s" % (ident, self.slot(p))

    def select_system(self, want):
        """The (boot partition, id) `wk boot --system <want>` arms; <want> is <id>, <id>@<slot> or @<slot>."""
        systems = self.systems()
        name, dev = self.c("NODE_NAME"), self.c("NODE_DEVICE")
        if systems is None:
            act.die("could not read %s on %s to see what it holds" % (dev, name))
        if not systems:
            act.die("%s on %s holds no wk system yet.\n    Write one first:  wk sysimage write --from <path> --disk %s:%s\n"
                    "    ('wk sysimage ls' lists what a workspace here has built)" % (dev, name, name, dev))
        listing = "\n".join("        %s  (on %s)" % (self.label(systems, p, i), p) for p, i in systems)
        if not want:
            if len(systems) == 1:
                return systems[0]
            act.die("%s on %s holds %d systems:\n%s\n    Name the one to boot:  wk boot %s --system <id>[@<slot>]"
                    % (dev, name, len(systems), listing, name))
        ident, _, slot = want.partition("@")
        hits = [(p, i) for p, i in systems if (not ident or i == ident) and (not slot or self.slot(p) == slot)]
        if len(hits) == 1:
            return hits[0]
        if hits:
            act.die("%s on %s holds '%s' in %d slots:\n%s\n    Name the slot:  wk boot %s --system %s@<slot>"
                    % (dev, name, ident, len(hits), listing, name, ident))
        act.die("%s on %s holds:\n%s\n    not '%s'. Write it first:  wk sysimage write --from <path> --disk %s:%s\n"
                "    (a medium already holding a system takes a second one at ...:%s@second)" % (dev, name, listing, want, name, dev, dev))

    def diag(self):
        """Every system's own dump: after a failed boot of the second, the first one's is the stale one."""
        systems = self.systems()
        dev = self.c("NODE_DEVICE")
        if systems is None:
            act.die("cannot read %s on %s" % (dev, self.c("NODE_NAME")))
        if not systems:
            return "(%s holds no wk system, so there is no dump to read)" % dev
        out = []
        for p, ident in systems:
            dump = self.medium_read(p, "wk-diag.txt")
            out.append("== %s (%s) ==" % (ident, p))
            out.append("(cannot read %s)" % p if dump is None
                       else dump.rstrip("\n") or "(no wk-diag.txt -- the image did not get that far)")
        return "\n".join(out)

    # -- arming
    def arm(self, p, order=""):
        act.die("the %s driver cannot arm %s" % (self.name or self.c("NODE_DRIVER") or "unnamed", self.c("NODE_NAME")))

    def reboot(self, armed=False):
        return self.ch.call("boot_priv", "reboot", mutates=True).rc

    def reboot_tryboot(self):
        if not self.run("has-systemd.sh").ok:
            act.die("%s answered on a system with no systemd, which cannot pass the\n    tryboot flag to the reboot it rides on "
                    "(only 'systemctl reboot' with\n    /run/systemd/reboot-param does). Arm from a system that can:\n"
                    "        wk boot %s --back" % (self.c("NODE_NAME"), self.c("NODE_NAME")))
        return self.ch.call("boot_priv", "reboot-tryboot", mutates=True).rc

    def disarm(self):
        return 0

    def disarm_note(self):
        return ""

    def self_disarm_sh(self):
        return self.ob(self.failsafe, **self.failsafe_params()).text() if self.failsafe else None

    def failsafe_params(self):
        return {}

    def media(self):
        dev = self.c("NODE_DEVICE")
        return "media %s (this driver says nothing more about it)" % dev if dev else "no wk-managed media declared"

    def evidence(self):
        return ""

    def reprovision(self):
        return ""

    # -- the record of intent, on the host install: once armed, nothing a probe reads has changed
    def record(self, name, input=None, mutates=False):
        return self.ch.call("m_ssh", self.ob(name, WK_RECORD=RECORD), input=input, mutates=mutates)

    def record_write(self, image, profile, device, order):
        if self.ch.channel != "host":
            act.debug("%s answered as its bench system; the arming is on its medium and no record is written" % self.c("NODE_NAME"))
            return 0
        body = "image=%s\nprofile=%s\ndevice=%s\norder=%s\narmed_by=%s\narmed_boot_id=%s\n" % (
            image, profile, device, order, wkrecord.host_name(), self.boot_id())
        return self.record("record-write.sh", input=body, mutates=True).rc

    def record_read(self):
        return self.record("record-read.sh").out if self.ch.channel == "host" else ""

    def record_clear(self):
        if self.ch.channel != "host":
            act.debug("%s answered as its bench system; its arming record is on the host install and stays" % self.c("NODE_NAME"))
            return 0
        return self.record("record-clear.sh", mutates=True).rc

    def armed_barrier(self, what):
        """Between `wk boot` and its reboot, the filesystem answering ssh is not the one about to run."""
        name = self.c("NODE_NAME")
        mode = self.mode or self.probe()
        if mode in ("", "unreachable"):
            act.barrier("could not tell what %s is running, so whether it is armed for a\n    one-shot boot is unknown -- and if it is, "
                        "the filesystem answering ssh is\n    not the one about to run:\n    %s\n    Ask from a machine that can reach it:  "
                        "wk boot %s --status\n    A board in bench mode answers a workspace; one in host mode answers only a\n    workstation."
                        % (name, what, name))
            return
        if mode != "host":
            return
        rec = self.record_read()
        image, armed_boot = kv(rec, "image"), kv(rec, "armed_boot_id")
        if not image:
            return
        now = self.boot_id()
        if armed_boot and now and armed_boot != now:
            return
        act.barrier("%s is armed for system '%s' and has not rebooted yet.\n    %s\n    Disarm it first:   wk boot %s --disarm\n"
                    "    Or see the state:  wk boot %s --status" % (name, image, what, name, name))


def interface():
    return ("probeable", "probe", "system_kind", "boot_id", "booted_at", "display", "watchdog_present", "systems",
            "select_system", "diag", "arm", "reboot", "disarm", "disarm_note", "self_disarm_sh", "media", "evidence",
            "reprovision", "record_write", "record_read", "record_clear", "armed_barrier")

