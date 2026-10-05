"""The boot-driver interface: how a machine is put into its bench system for one boot, and read back, over a Channel."""

import copy
import os
import re
import shlex
import time

from wk import act, reach as wkreach, record as wkrecord
from wk.kv import kv
from wk.machine import Local, Result, Ssh
from wk.sudo import BOOT_PRIV, CARD_PRIV

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


class Onboard:
    """A boot/onboard/ file verbatim, led by its `KEY=value; ` parameters and `lead`'s files; continuations joined: an
    ExecStart takes one line. A parameter is one literal word, or shell-quoted where `quote` (a Mac takes any string)."""

    quote = False
    where = os.path.join("boot", "onboard")

    def __init__(self, root, name, lead=(), **params):
        for k, v in params.items():
            if not (self.quote or SAFE.match(str(v))):
                raise ValueError("%s=%r is not one word both a board's shell and a systemd ExecStart read literally" % (k, v))
        self.root, self.name, self.params, self.lead = str(root), name, params, tuple(lead)

    def path(self, name=None):
        return os.path.join(self.root, self.where, name or self.name)

    def text(self):
        body = []
        for path in [self.path(n) for n in self.lead] + [self.path()]:
            with open(path) as f:
                body.append(f.read().replace("\\\n", "").rstrip("\n"))
        q = shlex.quote if self.quote else str
        return "".join("%s=%s; " % (k, q(str(v))) for k, v in sorted(self.params.items())) + "\n".join(body)


class Channel:
    """A board's host mode on ssh and its bench system at its found address, each a Machine; the helpers too."""

    def __init__(self, root, conf, channel="none", env=None, via=None):
        self.root, self.conf, self.channel = str(root), conf, channel
        self.env = os.environ if env is None else env
        self.via = via or Local()
        self._here = self._reach = None

    def c(self, key):
        return self.conf.get(key, "")

    def reach(self):
        if self._reach is None:
            self._reach = wkreach.Reach(self.via, self.env)
        return self._reach

    def here(self):
        if self._here is None:
            self._here = bool(self.c("ssh")) and wkrecord.host_name(self.via) == self.c("ssh").lower()
        return self._here

    def bench_name(self):
        return self.c("bench_ssh") or self.c("ssh") or self.c("name")

    def image_addr(self):
        if self.env.get("WK_IMAGE_HOST"):
            return self.env["WK_IMAGE_HOST"]
        peer = self.reach().peer(self.bench_name())
        if peer and peer[1]:
            return peer[1]
        found = self.reach().find_mac(self.c("mac")) if self.c("mac") else ""
        return found.split()[0] if found else self.c("ssh") or self.c("name")

    def opts(self, fn):
        # Root on a bench system whatever the role, a person on a workstation's host mode.
        return ["-l", "root"] + wkreach.UNPINNED if fn == "i_ssh" or self.c("role") == "bench-device" else []

    def machine(self, fn):
        if fn == "m_ssh" and self.here():
            return self.via
        name = self.c("ssh") if fn == "m_ssh" else self.bench_name()
        if not name:
            return Result(255, "", "%s: no ssh destination" % fn)
        why = self.reach().offline(name)
        if why:
            act.warn(why)
            return Result(255, "", why)
        dest = self.c("ssh") if fn == "m_ssh" else self.image_addr()
        return Ssh(dest, opts=self.opts(fn), timeout=wkreach.ssh_timeout(self.env), via=self.via)

    def through(self, via):
        self.here()
        self.reach()
        ch = copy.copy(self)
        ch.via = via
        return ch

    def fn_of(self, channel):
        return {"host": "m_ssh", "bench": "i_ssh"}.get(channel, "")

    def is_root(self):
        return self.channel == "bench" or self.c("role") == "bench-device"

    def run(self, fn, argv, input=None, mutates=False):
        m = self.machine(fn) if fn else Result(1, "", "no channel answered")
        if isinstance(m, Result):
            return m
        r = (m.act_run if mutates else m.run)(argv, input=input)
        if mutates and r.err:
            act.log(r.err.rstrip("\n"))
        return r

    def sudo(self, argv, input=None, mutates=False):
        return self.run(self.fn_of(self.channel), argv if self.is_root() else ["sudo", "-n"] + argv, input=input, mutates=mutates)

    def boot_priv(self, verb, *args, mutates=False):
        if not self.is_root():
            return self.sudo([BOOT_PRIV, verb, *args], mutates=mutates)
        if verb == "status":
            return Result(0)
        return self.run(self.fn_of(self.channel), root_priv(verb, *args), mutates=mutates)

    def boot_priv_require(self):
        if self.is_root() or self.boot_priv("status").ok:
            return Result(0)
        act.die("%s cannot be armed: its boot helper is missing or its sudoers rule is not in force.\n"
                "    What fails:  sudo -n %s status\n    The remedy, from a terminal on %s:  ./setup --stage quiesce"
                % (self.c("name"), BOOT_PRIV, self.c("name")))

    def disk_unmount(self, dev):
        if self.sudo([CARD_PRIV, "unmount", dev], mutates=True).ok:
            return Result(0)
        held = self.run("m_ssh", ["lsblk", "-lno", "NAME,MOUNTPOINT", dev]).out
        act.die("could not unmount what is on %s on %s.\n    Something is using it:\n%s" % (dev, self.c("name"), "\n".join(
            "    /dev/%s at %s" % tuple(l.split(None, 1)) for l in held.replace("\r", "").splitlines() if len(l.split()) > 1)))

    def call(self, fn, *args, input=None, mutates=False):
        if fn in ("m_ssh", "i_ssh", "r_ssh", "r_sudo"):
            argv = ["sh", "-c", args[0].text() if isinstance(args[0], Onboard) else args[0]]
            if fn == "r_sudo":
                return self.sudo(argv, input=input, mutates=mutates)
            return self.run(self.fn_of(self.channel) if fn == "r_ssh" else fn, argv, input=input, mutates=mutates)
        if fn == "card_priv":
            return self.sudo([CARD_PRIV, *args], input=input, mutates=mutates)
        if fn == "boot_priv":
            return self.boot_priv(*args, mutates=mutates)
        if fn == "boot_priv_require":
            return self.boot_priv_require()
        if fn == "disk_unmount":
            return self.disk_unmount(*args)
        raise ValueError("no transport call '%s'" % fn)


def root_priv(verb, *args):
    """What the boot helper does, run as root where there is no helper; `nohup`, since macOS ships no `setsid`."""
    if verb == "order":
        return ["vcmailbox", "0x0003808b", "4", "4", args[0]]
    tail = {"reboot": "reboot", "reboot-tryboot": "printf \"0 tryboot\" > /run/systemd/reboot-param && systemctl reboot"}[verb]
    return ["sh", "-c", "nohup sh -c '%s' </dev/null >/dev/null 2>&1 &" % ("sleep 3; " + tail)]


class Driver:
    name = ""
    arming = "one-shot"
    arm_from_bench = False
    order_image = order_normal = ""
    system_parts = (1,)
    failsafe = None
    selects_by_partition = False

    def __init__(self, root, conf, ch, mode=""):
        self.root, self.conf, self.ch, self.mode = str(root), conf, ch, mode

    @staticmethod
    def transport(root, conf, channel="none", env=None, via=None):
        return Channel(root, conf, channel, env=env, via=via)

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
        return {"arming": self.arming, "record": RECORD}

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
        d = kv(r.out)
        ident = d.get("id", "")
        if not ident:
            self.mode = "host"
            return self.mode
        kind = self.system_kind(d.get("rootdev", ""))
        if kind == "unknown":
            kind = "base" if d.get("role", "") == "rescue" else "bench"
        self.mode = "%s %s" % (kind, ident)
        return self.mode

    def system_kind(self, rootdev):
        # root first: on a one-medium board (rpi3) both prefixes match.
        for key, kind in (("root", "base"), ("device", "bench")):
            if rootdev and self.c(key) and rootdev.startswith(self.c(key)):
                return kind
        return "unknown"

    def boot_id(self):
        return self.run("boot-id.sh").out.strip()

    def booted_at(self):
        btime = re.sub(r"\D", "", self.run("booted-at.sh").out)
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(btime))) if btime else ""

    def display(self):
        return self.c("display") or None

    def watchdog_present(self):
        return self.sudo("watchdog-present.sh").ok

    # -- the medium
    def boot_part(self):
        return part(self.c("device"), 1)

    def system_part(self, n):
        return part(self.c("device"), n)

    def slot(self, p):
        n = int(partno(p) or 0)
        return SLOTS[self.system_parts.index(n)] if n in self.system_parts else "?"

    def part_absent(self, dev):
        fn = "r_ssh" if self.c("role") == "bench-device" else "m_ssh"
        r = self.ch.call(fn, self.ob("part-absent.sh", WK_DEV=dev))
        return r.ok and r.out.replace("\r", "").strip() == "no"

    def medium_read(self, p, name):
        """"" for a file the partition lacks, None when unreadable."""
        if self.c("role") == "bench-device":
            r = self.sudo("medium-read.sh", WK_PART=p, WK_FILE=name)
            return r.out if r.ok else None
        r = self.card("boot-read", disk_of(p), partno(p), name)
        if r.ok:
            return r.out
        if self.part_absent(p):
            return ""
        act.warn("%s could not read %s off %s: its card helper is older than this verb, or its sudoers rule is not in force.\n"
                 "    What fails:  sudo -n %s boot-read ...\n"
                 "    The remedy, from a terminal on %s:  ./setup --stage quiesce" % (self.c("name"), name, p, CARD_PRIV, self.c("name")))
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
        """The (boot partition, id) `--system <id>[@<slot>]` names."""
        systems = self.systems()
        name, dev = self.c("name"), self.c("device")
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
        systems = self.systems()
        dev = self.c("device")
        if systems is None:
            act.die("cannot read %s on %s" % (dev, self.c("name")))
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
        act.die("the %s driver cannot arm %s" % (self.name or self.c("driver") or "unnamed", self.c("name")))

    def reboot(self, armed=False):
        return self.ch.call("boot_priv", "reboot", mutates=True).rc

    def reboot_tryboot(self):
        if not self.run("has-systemd.sh").ok:
            act.die("%s answered on a system with no systemd, which cannot pass the tryboot flag to its reboot.\n"
                    "    Arm from a system that can:  wk boot %s --back" % (self.c("name"), self.c("name")))
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
        dev = self.c("device")
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
            act.debug("%s answered as its bench system; the arming is on its medium and no record is written" % self.c("name"))
            return 0
        body = "image=%s\nprofile=%s\ndevice=%s\norder=%s\narmed_by=%s\narmed_boot_id=%s\n" % (
            image, profile, device, order, wkrecord.host_name(), self.boot_id())
        return self.record("record-write.sh", input=body, mutates=True).rc

    def record_read(self):
        return self.record("record-read.sh").out if self.ch.channel == "host" else ""

    def record_clear(self):
        if self.ch.channel != "host":
            act.debug("%s answered as its bench system; its arming record is on the host install and stays" % self.c("name"))
            return 0
        return self.record("record-clear.sh", mutates=True).rc

    def armed_barrier(self, what):
        # Between `wk boot` and its reboot, the filesystem answering ssh is not the one about to run.
        name = self.c("name")
        mode = self.mode or self.probe()
        if mode in ("", "unreachable"):
            act.barrier("could not tell what %s is running, so whether it is armed for a one-shot boot is unknown:\n    %s\n"
                        "    Ask from a machine that can reach it:  wk boot %s --status" % (name, what, name))
            return
        if mode != "host":
            return
        d = kv(self.record_read())
        image, armed_boot = d.get("image", ""), d.get("armed_boot_id", "")
        if not image:
            return
        now = self.boot_id()
        if armed_boot and now and armed_boot != now:
            return
        act.barrier("%s is armed for system '%s' and has not rebooted yet.\n    %s\n    Disarm it first:   wk boot %s --disarm\n"
                    "    Or see the state:  wk boot %s --status" % (name, image, what, name, name))

