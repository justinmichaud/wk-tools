"""Machines in memory behind the real Channel, each side a Fake answering the argv the Channel sends -- an on-board
file under `sh -c`, a helper verb, a plain command. FakeBoard is a Pi: its media, a firmware one-shot, its EEPROM and a
clock. FakeMac is a Mac's two installs, its firmware's boot-volume and its boot helper. FakeGuest is the vm place
holding one Tart guest. `stuck` makes every arming write land nowhere, as an older helper does; `up` False silences
every side; `stop_after` kills the driving process at that effect, as tests/killpoints.py asks."""

import hashlib
import os
import re
import shlex
import tarfile

from tests.support import REPO
from wk import act, fleet
from wk.boot import drivers
from wk.boot.driver import BOOT_PRIV, CARD_PRIV, Channel, disk_of, part
from wk.boot.mac import HELPER, MacGuest
from tests.fakes import FakeClock
from wk.kv import kv
from wk.machine import Result
from tests import fakes
from tests.fakes import Fake, Killed

BTIME = ["sed", "-n", "s/^btime //p", "/proc/stat"]
MACHINES = {"pi-sd": "rpi3", "pi-tryboot": "rpi4", "rpi5-usb": "rpi5", "mac-volume": "mbp", "mac-guest": "benchvm"}
SLOTS_OF = {"pi-sd": (5, 7), "pi-tryboot": (1, 3), "rpi5-usb": (1, 3)}
HOST_GROUP = "1981BBBF-8B67-4DED-A3E5-41A2550DE0FB"
BENCH_GROUP = "73C12614-1130-40DF-B9B9-9CA73D10F3AA"


def bodies():
    out = {}
    for d in ("boot", "bench"):
        for n in os.listdir(REPO / d / "onboard"):
            with open(REPO / d / "onboard" / n) as f:
                out[f.read().replace("\\\n", "").rstrip("\n")] = n
    return sorted(out.items(), key=lambda kv_: -len(kv_[0]))


ONBOARD = bodies()


def onboard(text):
    """(name, params) of an on-board file's text, or ("", {})."""
    for body, name in ONBOARD:
        if not text.endswith(body):
            continue
        try:
            words = shlex.split(text[:len(text) - len(body)])
        except ValueError:
            continue
        if all(re.match(r"^[A-Z_][A-Z0-9_]*=.*;$", w, re.S) for w in words):
            return name, dict(w[:-1].split("=", 1) for w in words)
    return "", {}


def said(argv):
    """An effect as a test names it: ("card"|"priv", verb, ...), ("sh", file, WK_DO), or the argv."""
    argv = list(argv)
    argv = argv[2:] if argv[:2] == ["sudo", "-n"] else argv
    if argv[:1] in ([CARD_PRIV], [BOOT_PRIV]):
        return ("card" if argv[0] == CARD_PRIV else "priv",) + tuple(argv[1:])
    if argv[:1] == ["vcmailbox"]:
        return ("priv", "order", argv[-1])
    if argv[:2] == ["sh", "-c"]:
        name, p = onboard(argv[2])
        return ("priv", p["WK_DO"]) if name == "reboot.sh" else ("sh", name, p.get("WK_DO", ""))
    return tuple(argv)


def conf_for(kind):
    conf = fleet.Fleet(REPO, {"HOME": "/nonexistent"}).load(MACHINES[kind])
    conf = {k: v for k, v in conf.items() if k != "kind"}
    return dict(conf, name=MACHINES[kind], driver=kind)


def board(kind, ids=("sys-a",), env=None):
    """A machine of `kind` up in host mode with one system per id, and the driver over it."""
    fake = {"mac-volume": FakeMac, "mac-guest": FakeGuest}.get(kind, FakeBoard)(conf_for(kind), env=env)
    fake.lay_out(ids)
    return fake, fake.driver()


class Side(fakes.World):
    """One system as the Channel reaches it: its world answers, and every effect is the world's too."""

    def __init__(self, world, name):
        super().__init__(name=name)
        self.world = world

    def run(self, argv, input=None, timeout=None, stream=False):
        self.record_run(argv)
        return self.world.reply(self.name, list(argv), input)

    def effect(self, record):
        self.world.effect(record)
        self.effects.append(record)

    def copy_in(self, src, dest):
        """A source this host's fakes hold, not the disk, lands as nothing."""
        self.effect(("copy_in", src, dest))
        if not act.dry_run() and os.path.isfile(src):
            with open(src, "rb") as f:
                self._set_file(dest, f.read())
            self.world.landed(src, dest, self.files[dest])

    def write(self, path, text):
        super().write(path, text)
        if path in self.files:
            self.world.landed(None, path, text.encode())


class Board:
    """What a machine under `wk boot` holds and does beyond its two Sides: its record, its boots, every effect in order."""
    stop_after, applied = None, 0

    def setup(self, conf, env, clock):
        self.conf, self.clock = dict(conf), clock or FakeClock()
        self.env = env if env is not None else {"HOME": "/nonexistent", "WK_STORE": "/nonexistent/store"}
        self.machines = {"host": Side(self, "host"), "bench": Side(self, "bench")}
        self.up, self.stuck, self.effects, self.asked, self.files, self.record = True, False, [], [], {}, None
        self.boots, self.booted = 1, int(self.clock.now())

    def effect(self, record):
        if self.stop_after is not None and self.applied >= self.stop_after:
            raise Killed(record)
        self.applied += 1
        kind = record[0]
        self.effects.append(said(record[1]) if kind == "run" else ("push", os.path.basename(record[2])) if kind == "copy_in" else record)

    def landed(self, src, dest, data):
        pass

    def boot(self):
        self.boots += 1
        self.clock.sleep(40)
        self.booted = int(self.clock.now())

    def record_do(self, do, input):
        if do == "write":
            self.record = input + "armed_at=%s\n" % self.clock.iso()
        elif do == "clear":
            self.record = None
        return Result(0, self.record or "")

    def sh(self, cmd):
        return Result(127, "", "%s: no answer here" % cmd)


class FakeBoard(Board, Channel):
    def __init__(self, conf, clock=None, env=None):
        self.setup(conf, env, clock)
        Channel.__init__(self, REPO, self.conf, env=self.env, via=Fake("here"),
                         sides={s: (lambda s=s: self.machines[s]) for s in ("host", "bench")})
        self.onboard = {}
        self.fat, self.bootfs = {}, {}
        self.roots = {self.conf["root"]: {"id": "", "role": "rescue"}}
        self.running = self.conf["root"]
        self.one_shot, self.tryboot, self.kept = None, False, False
        self.eeprom = "BOOT_ORDER=0xf41\n"
        self.eeprom_tool, self.vc, self.soc = True, True, "raspberrypi,4-model-b\nbrcm,bcm2711\n"

    def driver(self):
        return drivers()[self.conf["driver"]](REPO, self.conf, self)

    def lay_out(self, ids):
        self.rescue("rescue-1" if self.conf["role"] == "bench-device" else "")
        for n, ident in zip(SLOTS_OF[self.conf["driver"]], ids):
            self.write_system(part(self.conf["device"], n), ident, failsafe=drivers()[self.conf["driver"]].failsafe)

    def rescue(self, ident, role="rescue"):
        self.roots[self.conf["root"]] = {"id": ident, "role": role}
        self.fat[self.sd()] = {"config.txt": "dtparam=audio=on\n", "cmdline.txt": "root=%s\n" % self.conf["root"]}

    def write_system(self, boot, ident, failsafe=None, watchdog=True, systemd=True):
        root = part(disk_of(boot), int(re.search(r"(\d+)$", boot).group(1)) + 1)
        self.fat[boot] = {"wk-image.id": ident + "\n", "config.txt": "kernel=kernel8.img\n",
                          "cmdline.txt": "root=%s rootwait\n" % root, "kernel8.img": "k"}
        self.roots[root] = {"id": ident, "role": "bench", "failsafe": failsafe, "watchdog": watchdog, "systemd": systemd}
        return root

    def sd(self):
        return part(disk_of(self.conf["root"]), 1)

    def on_rescue(self):
        return self.running == self.conf["root"]

    def landed(self, src, dest, data):
        self.bootfs[dest] = data

    # -- the firmware
    def firmware(self):
        drv, dev, sd = self.conf.get("driver"), self.conf.get("device", ""), self.fat.get(self.sd(), {})
        if drv == "pi-sd":
            m = re.search(r"(?m)^os_prefix=(\w+)/", sd.get("config.txt", ""))
            return kv(sd.get(m.group(1) + "/cmdline.txt", "").replace(" ", "\n")).get("root", "") if m else None
        if drv == "pi-tryboot":
            return kv(sd.get("second/cmdline.txt", "").replace(" ", "\n")).get("root", "") if "tryboot.txt" in sd else None
        if drv == "rpi5-usb" and self.one_shot and self.one_shot.endswith("4"):
            pair = kv(self.fat.get(part(dev, 1), {}).get("autoboot.txt", "")).get("boot_partition", "") or "1"
            return part(dev, int(pair) + 1)
        return None

    def restart(self, tryboot=False):
        self.effects.append(("reboot", tryboot))
        self.tryboot = tryboot
        root = self.firmware()
        self.one_shot = None
        self.running = root if root in self.roots else self.conf["root"]
        self.boot()
        failsafe = self.roots[self.running].get("failsafe")
        if failsafe:
            self.run_onboard(failsafe, {"WK_SD": self.sd()})
        return Result(0)

    def reply(self, side, argv, input=None):
        if not self.up or self.on_rescue() != (side == "host"):
            return Result(255, "", "ssh: connect: no route")
        argv = argv[2:] if argv[:2] == ["sudo", "-n"] else argv
        sys_ = self.roots[self.running]
        if argv[:1] == [CARD_PRIV]:
            return self.card_does(*argv[1:])
        if argv[:1] == [BOOT_PRIV]:
            return self.helper(*argv[1:])
        if argv[:1] == ["vcmailbox"]:
            return self.helper("order", argv[-1])
        if argv[:2] == ["sh", "-c"]:
            name, p = onboard(argv[2])
            if name == "reboot.sh":
                return self.helper(p["WK_DO"])
            if name:
                return self.run_onboard(name, p, input)
        if argv == BTIME:
            return Result(0, "%d\n" % self.booted)
        if argv[:2] == ["test", "-e"]:
            return Result(0 if argv[2] in self.fat else 1)
        if argv == ["test", "-d", "/run/systemd/system"]:
            return Result(0 if sys_.get("systemd", True) else 1)
        if argv in (["vcgencmd", "bootloader_config"], ["rpi-eeprom-config"]):
            return Result(0, self.eeprom)
        return Result(127, "", "%s: no answer on this board" % " ".join(argv))

    def run_onboard(self, name, p, input=None):
        sys_ = self.roots[self.running]
        sd = self.fat.setdefault(self.sd(), {})
        do = p.get("WK_DO", "")
        if name == "probe.sh":
            ident = "id=%s\nrole=%s\n" % (sys_["id"], sys_["role"]) if sys_["id"] else ""
            return Result(0, "%srootdev=%s\n" % (ident, self.running))
        if name == "boot-id.sh":
            return Result(0, "boot-%d\n" % self.boots)
        if name == "watchdog-present.sh":
            return Result(0 if sys_.get("watchdog") else 1)
        if name == "medium-read.sh":
            return Result(0, self.fat.get(p["WK_PART"], {}).get(p["WK_FILE"], ""))
        if name == "keep.sh":
            self.kept = True
            return Result(0)
        if name == "eeprom.sh":
            return self.eeprom_do(do, p, input)
        if name == "pisd-self-disarm.sh":
            if "config.txt.rescue" in sd:
                sd["config.txt"] = sd.pop("config.txt.rescue")
            return Result(0)
        if name == "tryboot-self-disarm.sh":
            self.tryboot_drop(sd)
            return Result(0)
        if name == "tryboot.sh":
            return self.tryboot_do(do, p, sd)
        if name == "record.sh":
            return self.record_do(do, input)
        return Result(127, "", "%s: not an on-board script this board knows" % name)

    def eeprom_do(self, do, p, input):
        tool = self.eeprom_tool
        answers = {"has-config": Result(0 if tool else 1), "has-vc": Result(0 if self.vc else 1),
                   "read": Result(0, self.eeprom) if tool else Result(127, "", "rpi-eeprom-config: not found"),
                   "vc-read": Result(0, self.eeprom if self.vc else ""), "soc": Result(0, self.soc),
                   "bootfs": Result(0, "/boot\n"), "sync": Result(0)}
        if do == "apply" and tool and not self.stuck:
            self.eeprom = input
        if do == "clear":
            self.bootfs.clear()
        if do == "sum":
            data = self.bootfs.get(p["WK_PATH"])
            return Result(0, hashlib.sha256(data).hexdigest() + "\n") if data is not None else Result(1)
        return answers.get(do, Result(0 if tool else 127))

    def tryboot_drop(self, sd):
        for k in [k for k in sd if k == "tryboot.txt" or k.startswith("second/")]:
            del sd[k]

    def tryboot_do(self, do, p, sd):
        if do == "stage":
            src = self.fat.get(p["WK_SRC"])
            if src is None:
                return Result(9, "", "mount: %s: no such partition" % p["WK_SRC"])
            if not self.stuck:
                self.tryboot_drop(sd)
                sd["second/cmdline.txt"] = src["cmdline.txt"].rstrip("\n") + " panic=10\n"
                sd["second/kernel8.img"] = src["kernel8.img"]
                sd["tryboot.txt"] = "os_prefix=second/\n" + src["config.txt"]
            return Result(0)
        if do == "disarm":
            self.tryboot_drop(sd)
            return Result(0)
        staged = kv(sd.get("second/cmdline.txt", "").replace(" ", "\n")).get("root", "")
        if do == "staged":
            return Result(0, "yes\n" if "tryboot.txt" in sd and staged else "no\n")
        if do == "staged-root":
            return Result(0, "root=%s\n" % staged)
        return Result(0, "staging\n" if self.running == staged else "sd-config\n" if self.on_rescue() else "unknown\n")

    def card_does(self, verb, *args):
        if verb == "boot-read":
            p = part(args[0], args[1])
            return Result(0, self.fat[p].get(args[2], "")) if p in self.fat else Result(1, "", "no such partition")
        if verb in ("unmount", "status"):
            return Result(0)
        if verb == "autoboot":
            if not self.stuck:
                self.fat.setdefault(part(args[0], 1), {})["autoboot.txt"] = "[all]\nboot_partition=%s\n" % args[1]
            return Result(0)
        dev, _, slot = args[0].partition("@")
        sd = self.fat.setdefault(part(dev, 1), {})
        boots = [part(dev, n) for n in ((5, 3) if slot == "second" else (7,)) if part(dev, n) in self.fat]
        if verb == "second-state":
            armed = "config.txt.rescue" in sd
            prefix = re.search(r"(?m)^os_prefix=(\w+)/", sd.get("config.txt", ""))
            out = "wk-card-priv: armed=%s\n" % ("yes" if armed else "no")
            if armed and prefix:
                out += "wk-card-priv: armed_prefix=%s\n" % prefix.group(1)
            return Result(0, out + "wk-card-priv: present=%s\n" % ("yes" if boots else "no"))
        if verb == "second-arm":
            if not boots:
                return Result(1, "", "holds no system")
            if not self.stuck:
                for k in [k for k in sd if k.startswith(("second/", "third/"))]:
                    del sd[k]
                for k, v in self.fat[boots[0]].items():
                    sd["%s/%s" % (slot, k)] = v
                sd.setdefault("config.txt.rescue", sd.get("config.txt", ""))
                sd["config.txt"] = "os_prefix=%s/\n%s" % (slot, self.fat[boots[0]]["config.txt"])
            return Result(0)
        if verb == "second-disarm":
            if "config.txt.rescue" in sd:
                sd["config.txt"] = sd.pop("config.txt.rescue")
            return Result(0)
        return Result(3, "", "REFUSED: %s" % verb)

    def helper(self, verb, *args):
        if verb == "order":
            self.one_shot = args[0]
            return Result(0, "0x00000004 0x80000000 %s\n" % args[0])
        if verb in ("reboot", "reboot-tryboot"):
            return self.restart(tryboot=verb == "reboot-tryboot")
        return Result(0)


class FakeMac(Board, Channel):
    """A Mac in host mode: `said` answers a helper verb as given; `helper_is` "current", "old" or None."""

    def __init__(self, conf, env=None, clock=None):
        self.setup(conf, env, clock)
        Channel.__init__(self, REPO, self.conf, env=self.env, via=Fake("here"), board=False, sides={
            "host": lambda: self.machines["host"],
            "bench": lambda: self.machines["bench"] if self.bench_name() else Result(255, "", "no bench node")})
        self.vol = "/Volumes/" + self.conf["volume"]
        self.roots = {"host": {"id": "", "group": HOST_GROUP, "name": "Macintosh HD"},
                      "bench": {"id": "", "group": BENCH_GROUP, "name": self.conf["volume"]}}
        self.running = self.firmware = "host"
        self.attached, self.data, self.helper_is, self.said, self.fact_input = True, True, "current", {}, None

    def driver(self):
        return drivers()["mac-volume"](REPO, self.conf, self)

    def lay_out(self, ids):
        self.roots["bench"]["id"] = ids[0]

    def on_rescue(self):
        return self.running == "host"

    def enter_bench(self):
        self.firmware = "bench"
        self.restart()

    def leave_bench(self):
        self.firmware = "host"
        self.restart()

    def restart(self):
        self.running = self.firmware
        self.boot()
        return Result(0)

    def priv_asked(self):
        return [a[4] for a in self.asked if a[1:4] == ("sudo", "-n", HELPER)]

    def landed(self, src, dest, data):
        if src and src.endswith(".tar"):
            with tarfile.open(src) as t:
                self.effects.append(("tar", tuple(sorted(t.getnames()))))

    def reply(self, side, argv, input=None):
        self.asked.append((side,) + tuple(argv))
        if not self.up or self.running != side:
            return Result(255, "", "ssh: connect: no route")
        return self.command(argv, input)

    def command(self, argv, input):
        me = self.roots[self.running]
        if argv[:2] == ["sh", "-c"]:
            name, p = onboard(argv[2])
            if name:
                return self.script(name, p, input)
        if argv[:3] == ["sudo", "-n", HELPER]:
            return self.helper(argv[3])
        if argv == ["sysctl", "-n", "kern.boottime"]:
            return Result(0, "{ sec = %d, usec = 451078 } Sat Aug 15 04:12:16 2026\n" % self.booted)
        if argv[:2] == ["python3", "-"]:
            self.fact_input = input
            out = self.fact(argv[2], (argv[3:] or [""])[0])
            return Result(0 if out else 1, out + "\n" if out else "")
        if argv[:1] == ["test"] and argv[2] in self.known():
            hit = argv[2] in self.dirs_() if argv[1] == "-d" else bool(self.helper_is)
            return Result(0 if hit else 1)
        if argv[:1] == ["cat"] and argv[1] in ("/etc/wk-image", self.vol + "/etc/wk-image"):
            owner = me if argv[1] == "/etc/wk-image" else self.roots["bench"]
            return Result(0, "id=%s\n" % owner["id"] if owner["id"] else "")
        if argv[:1] == ["cat"]:
            return Result(0, self.files[argv[1]]) if argv[1] in self.files else Result(1, "", "no such file")
        if argv[:1] in (["true"], ["rm"], ["mkdir"], ["tar"], ["mv"]):
            return Result(0)
        return self.sh(shlex.join(argv))

    def known(self):
        return {self.vol + "/System/Library/CoreServices", self.vol + " - Data", HELPER}

    def dirs_(self):
        return ({self.vol + "/System/Library/CoreServices"} | ({self.vol + " - Data"} if self.data else set())) if self.attached else set()

    def script(self, name, p, input):
        me = self.roots[self.running]
        if name == "probe.sh":
            return Result(0, ("id=%s\n" % me["id"] if me["id"] else "") + "rootdev=\n")
        if name == "record.sh":
            return self.record_do(p["WK_DO"], input)
        return Result(127, "", "%s: not an on-board script this Mac knows" % name)

    def fact(self, what, arg):
        if what == "boot-volume":
            return "EF57347C-0000-AA11-AA11-00306543ECAC:D7E4E11B:" + self.roots[self.firmware]["group"]
        if what == "volume-name" and arg == "/":
            return self.roots[self.running]["name"]
        if what == "volume-group":
            if arg == "/":
                return self.roots[self.running]["group"]
            if arg == self.vol and self.running == "host" and self.attached:
                return BENCH_GROUP
        return ""

    def helper(self, verb):
        if not self.helper_is:
            return Result(1, "", "sudo: a password is required\n")
        if verb in self.said:
            return Result(*self.said[verb])
        if verb == "status":
            return Result(0, "wk-boot-priv: ok\n" + ("wk-boot-priv: detach=nohup\n" if self.helper_is == "current" else ""))
        if verb in ("boot-host", "boot-volume"):
            if not self.stuck:
                self.firmware = self.running if verb == "boot-host" else "bench"
            return Result(0, "wk-boot-priv: blessed %s\n" % ("/" if verb == "boot-host" else self.vol))
        if verb == "reboot":
            return self.restart()
        return Result(2, "wk-boot-priv: usage\n")


class FakeGuest(Board):
    """The vm place holding one Tart guest: absent, stopped or running, carrying the marker or not; `stuck` starts it
    with no marker."""

    def __init__(self, conf, env=None, clock=None):
        self.setup(conf, env or {"HOME": "/nonexistent"}, clock)
        self.ws = self.env.get("WK_BENCH_GUEST") or "wk-bench"
        self.ident, self.st, self.marked, self.has_tart = "", "stopped", True, True

    def driver(self):
        return MacGuest(REPO, self.conf, MacGuest.channel(REPO, self.conf, env=self.env, via=Fake("here")), vm=self)

    def lay_out(self, ids):
        self.ident = ids[0]

    def on_rescue(self):
        return not (self.st == "running" and self.marked)

    def enter_bench(self):
        self.start(self.ws)

    def leave_bench(self):
        self.stop(self.ws)

    # -- the vm place
    def vm_state(self, ws):
        return self.st

    def start(self, ws):
        self.effects.append(("start",))
        self.st, self.marked = "running", not self.stuck
        self.boot()
        return True

    def stop(self, ws):
        self.effects.append(("stop",))
        self.st = "stopped"

    def tart(self):
        return "/opt/homebrew/bin/tart" if self.has_tart else ""

    def guest(self, ws):
        return self.machines["host"] if self.st == "running" and self.up else None

    def reply(self, side, argv, input=None):
        self.asked.append(tuple(argv))
        ident = self.ident if self.marked else ""
        if argv[:2] == ["sh", "-c"]:
            name, p = onboard(argv[2])
            if name:
                return self.script(name, p, input)
        if argv == ["cat", "/etc/wk-image"]:
            return Result(0, "id=%s\n" % ident if ident else "")
        if argv[:1] == ["cat"]:
            return Result(0, self.files[argv[1]]) if argv[1] in self.files else Result(1, "", "no such file")
        if argv == ["sysctl", "-n", "kern.boottime"]:
            return Result(0, "{ sec = %d, usec = 0 }\n" % self.booted)
        if argv == ["printenv", "HOME"]:
            return Result(0, "/Users/admin\n")
        if argv == ["id", "-un"]:
            return Result(0, "admin\n")
        if argv[:2] == ["sudo", "-n"] or argv[:1] in (["rm"], ["mkdir"], ["tar"], ["mv"]):
            return Result(0)
        return self.sh(shlex.join(argv))

    def script(self, name, p, input):
        ident = self.ident if self.marked else ""
        if name == "probe.sh":
            return Result(0, ("id=%s\n" % ident if ident else "") + "rootdev=\n")
        if name == "record.sh":
            return Result(0, "")
        return Result(127, "", "%s: not an on-board script this guest knows" % name)
