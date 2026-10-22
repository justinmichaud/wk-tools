"""The boot-driver interface: how a machine is put into its bench system for one boot, and read back, over a Channel."""

import copy
import os
import re
import shlex
import time

from wk import act, images, reach as wkreach, record as wkrecord
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
    """A boot/onboard/ file verbatim, led by its `KEY=value; ` parameters; continuations joined: an ExecStart takes one
    line. A parameter is one literal word, or shell-quoted where `quote` (a Mac takes any string)."""

    quote = False
    where = os.path.join("boot", "onboard")

    def __init__(self, root, name, **params):
        for k, v in params.items():
            if not (self.quote or SAFE.match(str(v))):
                raise ValueError("%s=%r is not one word both a board's shell and a systemd ExecStart read literally" % (k, v))
        self.root, self.name, self.params = str(root), name, params

    def path(self):
        return os.path.join(self.root, self.where, self.name)

    def text(self):
        with open(self.path()) as f:
            body = f.read().replace("\\\n", "").rstrip("\n")
        q = shlex.quote if self.quote else str
        return "".join("%s=%s; " % (k, q(str(v))) for k, v in sorted(self.params.items())) + body

    def argv(self):
        return ["sh", "-c", self.text()]


class Channel:
    """A machine's host install and its bench system, each a Machine: host mode at `ssh` (this machine when standing on
    it), bench mode at `bench_ssh`. `side` is the one the last probe found answering. A board's bench system is root,
    unpinned, at the address the tailnet or the LAN finds; a bench-role board is root on both."""

    def __init__(self, root, conf, env=None, via=None, board=True, sides=None):
        self.root, self.conf, self.board = str(root), conf, board
        self.env = os.environ if env is None else env
        self.via = via or Local()
        self.side = "none"
        self.sides = {"host": self.host, "bench": self.bench} if sides is None else sides
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

    def ssh(self, name, addr, root):
        if not name:
            return Result(255, "", "%s names no ssh destination for this" % (self.c("name") or "the machine"))
        why = self.reach().offline(name)
        if why:
            act.warn(why)
            return Result(255, "", why)
        return Ssh(addr(), opts=["-l", "root"] + wkreach.UNPINNED if root else [], timeout=wkreach.ssh_timeout(self.env), via=self.via)

    def host(self):
        return self.via if self.here() else self.ssh(self.c("ssh"), lambda: self.c("ssh"), self.is_root("host"))

    def bench_name(self):
        return (self.env.get("WK_MAC_BENCH_SSH", "") if not self.board else "") or self.c("bench_ssh")

    def bench(self):
        name = self.bench_name()
        return self.ssh(name, lambda: self.image_addr(name) if self.board else name, self.board)

    def image_addr(self, name):
        if self.env.get("WK_IMAGE_HOST"):
            return self.env["WK_IMAGE_HOST"]
        peer = self.reach().peer(name)
        if peer and peer[1]:
            return peer[1]
        found = self.reach().find_mac(self.c("mac")) if self.c("mac") else ""
        return found.split()[0] if found else name

    def machine(self, side=None):
        find = self.sides.get(side or self.side)
        return find() if find else Result(255, "", "no %s side answered" % (side or "machine's"))

    def through(self, via):
        self.here()
        self.reach()
        ch = copy.copy(self)
        ch.via, ch.sides = via, {"host": ch.host, "bench": ch.bench}
        return ch

    def is_root(self, side=None):
        return self.board and ((side or self.side) == "bench" or self.c("role") == "bench-device")

    def run(self, argv, side=None, input=None, mutates=False):
        m = self.machine(side)
        if isinstance(m, Result):
            return m
        r = (m.act_run if mutates else m.run)(argv, input=input)
        if mutates and r.err:
            act.log(r.err.rstrip("\n"))
        return r

    def sudo(self, argv, side=None, input=None, mutates=False):
        return self.run(argv if self.is_root(side) else ["sudo", "-n"] + argv, side, input, mutates)

    def card(self, *args, input=None, mutates=False):
        return self.sudo([CARD_PRIV, *args], input=input, mutates=mutates)

    def priv(self, verb, *args, side=None, mutates=False):
        """The boot helper's `verb`; a root system, which has no helper, does what it would."""
        if not self.is_root(side):
            return self.sudo([BOOT_PRIV, verb, *args], side, mutates=mutates)
        if verb == "status":
            return Result(0)
        argv = ["vcmailbox", "0x0003808b", "4", "4", args[0]] if verb == "order" else Onboard(self.root, "reboot.sh", WK_DO=verb).argv()
        return self.run(argv, side, mutates=mutates)

    def reboot(self, tryboot=False):
        return self.priv("reboot-tryboot" if tryboot else "reboot", mutates=True)

    def unmount(self, dev):
        if self.card("unmount", dev, mutates=True).ok:
            return
        held = self.run(["lsblk", "-lno", "NAME,MOUNTPOINT", dev], "host").out
        act.die("could not unmount what is on %s on %s.\n    Something is using it:\n%s" % (dev, self.c("name"), "\n".join(
            "    /dev/%s at %s" % tuple(l.split(None, 1)) for l in held.replace("\r", "").splitlines() if len(l.split()) > 1)))

    def push(self, src, dest):
        m = self.machine("host")
        if isinstance(m, Result):
            raise OSError(m.err)
        m.copy_in(src, dest)


class Driver:
    """`arm_from_bench`: armed where it stands; `on_medium`: armed on its medium, record or none; `sticky`: the bench system
    is the firmware default, so a reboot does not spend it and only a person's pick leaves it (`way_back`)."""

    name, record_at = "", RECORD
    arm_from_bench = on_medium = sticky = armed_by_tryboot = False
    system_parts = (1,)
    failsafe = None
    selects_by_partition = False

    def __init__(self, root, conf, ch):
        self.root, self.conf, self.ch, self.mode = str(root), conf, ch, ""

    @staticmethod
    def channel(root, conf, env=None, via=None):
        return Channel(root, conf, env=env, via=via)

    def c(self, key):
        return self.conf.get(key, "")

    def ob(self, name, **params):
        return Onboard(self.root, name, **params)

    def run(self, argv, mutates=False, input=None):
        return self.ch.run(argv, input=input, mutates=mutates)

    def probeable(self):
        return True

    def probe(self):
        """Host mode first; a bench system answering without a marker is some other computer."""
        for side in ("host", "bench"):
            r = self.ch.run(self.ob("probe.sh").argv(), side)
            d = kv(r.out)
            if r.ok and (side == "host" or d.get("id")):
                break
        else:
            self.ch.side, self.mode = "none", "unreachable"
            return self.mode
        self.ch.side, ident = side, d.get("id", "")
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
        return self.run(self.ob("boot-id.sh").argv()).out.strip()

    def booted_at(self):
        btime = re.sub(r"\D", "", self.run(["sed", "-n", "s/^btime //p", "/proc/stat"]).out)
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(btime))) if btime else ""

    def display(self):
        return self.c("display") or None

    def watchdog_present(self):
        return self.run(self.ob("watchdog-present.sh").argv()).ok

    def keep(self):
        return self.ch.sudo(self.ob("keep.sh").argv(), mutates=True).ok

    def watchdog(self):
        return (images.quiet_load(self.c("image_preset"), self.ch.env if self.ch else None) or {}).get("IMG_WATCHDOG", "")

    def boot_part(self):
        return part(self.c("device"), 1)

    def slot(self, p):
        n = int(partno(p) or 0)
        return SLOTS[self.system_parts.index(n)] if n in self.system_parts else "?"

    def medium_read(self, p, name):
        """"" for a file the partition lacks, None when unreadable."""
        if self.c("role") == "bench-device":
            r = self.ch.sudo(self.ob("medium-read.sh", WK_PART=p, WK_FILE=name).argv())
            return r.out if r.ok else None
        r = self.ch.card("boot-read", disk_of(p), partno(p), name)
        if r.ok:
            return r.out
        if self.ch.run(["test", "-e", p], "host").rc == 1:
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
            p = part(self.c("device"), n)
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
            out += ["== %s (%s) ==" % (ident, p), "(cannot read %s)" % p if dump is None
                    else dump.rstrip("\n") or "(no wk-diag.txt -- the image did not get that far)"]
        return "\n".join(out)

    def armable(self, mode):
        name = self.c("name")
        if mode == "unreachable":
            act.die("%s is not reachable over ssh in host mode, so there is nothing to arm." % name)
        if mode.startswith("bench") and not self.arm_from_bench:
            act.die("%s is already in bench mode (system %s), and only its rescue arms it:  wk boot %s --back" % (name, mode[6:], name))

    def choose(self, want):
        return self.select_system(want)

    def arm_sequence(self, p, image):
        """[(what, step)]: arming system `image` on `p`, in order, through to the boot that spends it."""
        raise NotImplementedError

    def record_step(self, image):
        return ("record       %s on %s, naming %s and this boot" % (self.record_at, self.c("name"), image),
                lambda: self.record_write(image))

    def reboot_step(self, image, tryboot=False):
        return ("then         reboot into %s; %s" % (image, "the watchdog returns it in %ss unless claimed" % self.watchdog()
                                                     if self.watchdog() else self.way_back()),
                lambda: self.reboot_into(image, tryboot))

    def way_back(self):
        return "the job it was armed for returns it"

    def reboot_into(self, image, tryboot=False):
        name = self.c("name")
        if tryboot and not self.run(["test", "-d", "/run/systemd/system"]).ok:
            act.die("%s answered on a system with no systemd, which cannot pass the tryboot flag to its reboot.\n"
                    "    Arm from a system that can:  wk boot %s --back" % (name, name))
        self.reboot(armed=True)
        act.info("%s is rebooting into system %s" % (name, image))
        if self.watchdog():
            act.log("  it returns by itself in %ss unless claimed:\n    wk boot %s --keep     claim it\n"
                    "    wk boot %s --back     hand it back now" % (self.watchdog(), name, name))
        else:
            act.log("  %s:  wk boot %s --status" % (self.way_back(), name))
        act.log("  if it never appears, its own account of that boot says why: wk boot %s --diag" % name)

    def reboot(self, armed=False):
        r = self.ch.reboot(tryboot=armed and self.armed_by_tryboot)
        if not r.ok:
            raise act.Refused(r.rc)
        return 0

    def disarm(self, spent=False):
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

    def sip(self):
        return None

    # -- the record of intent, on the host install: once armed, nothing a probe reads has changed
    def record(self, do, input=None, mutates=False):
        params = {"WK_RECORD": RECORD} if self.ch.board else {}
        return self.ch.run(self.ob("record.sh", WK_DO=do, **params).argv(), "host", input=input, mutates=mutates)

    def record_write(self, image):
        if self.ch.side != "host":
            act.debug("%s answered as its bench system; the arming is on its medium and no record is written" % self.c("name"))
            return
        body = "image=%s\nimage_preset=%s\narmed_by=%s\narmed_boot_id=%s\n" % (
            image, self.c("image_preset"), wkrecord.host_name(), self.boot_id())
        if not self.record("write", input=body, mutates=True).ok:
            act.die("could not write the arming record on %s" % self.c("name"))

    def record_read(self):
        return self.record("read").out if self.ch.side == "host" else ""

    def record_clear(self):
        if self.ch.side != "host":
            act.debug("%s answered as its bench system; its arming record is on the host install and stays" % self.c("name"))
            return
        self.record("clear", mutates=True)

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
