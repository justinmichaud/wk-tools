"""A board in memory behind the real Channel: its host and bench systems are two Fake machines answering the argv the
Channel sends -- an on-board file under `sh -c`, a helper verb -- from its media, a firmware one-shot, its bootloader
EEPROM and a clock. `firmware` is how each arrangement picks the next boot; `stuck` makes every arming write land
nowhere, as an older helper does."""

import hashlib
import os
import re

from wk.boot.driver import BOOT_PRIV, CARD_PRIV, Channel, Onboard, disk_of, part, root_priv
from wk.clock import FakeClock
from wk.kv import kv
from wk.machine import Fake, Result

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
PARAMS = re.compile(r"^((?:[A-Z_][A-Z0-9_]*=[^;]*; )*)")


class Side(Fake):
    """One of the board's two systems as ssh reaches it: it answers only while it is the one running."""

    def __init__(self, board, name):
        super().__init__(name)
        self.board = board

    def run(self, argv, input=None, timeout=None):
        self.record_run(argv)
        return self.board.answer(self.name, list(argv), input)

    def copy_in(self, src, dest):
        super().copy_in(src, dest)
        if dest in self.files:
            self.board.bootfs[dest] = self.files[dest]

    def write(self, path, text):
        super().write(path, text)
        if path in self.files:
            self.board.bootfs[path] = text.encode()


class FakeBoard(Channel):
    def __init__(self, conf, clock=None):
        super().__init__(ROOT, dict(conf), "none", env={})
        self.clock = clock or FakeClock()
        self.sides = {"m_ssh": Side(self, "host"), "i_ssh": Side(self, "bench")}
        self.onboard = {Onboard(ROOT, n).text(): n for n in os.listdir(os.path.join(ROOT, "boot", "onboard"))}
        self.fat = {}
        self.roots = {self.conf["NODE_ROOT"]: {"id": "", "role": "rescue"}}
        self.running = self.conf["NODE_ROOT"]
        self.boots, self.booted = 1, int(self.clock.now())
        self.one_shot = None
        self.tryboot = False
        self.mbr = {}
        self.record = None
        self.stuck = False
        self.up = True
        self.effects = []
        self.kept = False
        self.eeprom = "BOOT_ORDER=0xf41\n"
        self.eeprom_tool, self.vc, self.soc = True, True, "raspberrypi,4-model-b\nbrcm,bcm2711\n"
        self.bootfs = {}

    # -- laying out media
    def rescue(self, ident, role="rescue"):
        self.roots[self.conf["NODE_ROOT"]] = {"id": ident, "role": role}
        self.fat[part(disk_of(self.conf["NODE_ROOT"]), 1)] = {"config.txt": "dtparam=audio=on\n", "cmdline.txt": "root=%s\n" % self.conf["NODE_ROOT"]}

    def write_system(self, boot, ident, failsafe=None, watchdog=True, systemd=True):
        root = part(disk_of(boot), int(re.search(r"(\d+)$", boot).group(1)) + 1)
        self.fat[boot] = {"wk-image.id": ident + "\n", "config.txt": "kernel=kernel8.img\n",
                          "cmdline.txt": "root=%s rootwait\n" % root, "kernel8.img": "k"}
        self.roots[root] = {"id": ident, "role": "bench", "failsafe": failsafe, "watchdog": watchdog, "systemd": systemd}
        disk = disk_of(boot)
        if self.conf.get("NODE_DRIVER") == "pi-mbr":
            self.mbr[disk] = "83"
        return root

    def sd(self):
        return part(disk_of(self.conf["NODE_ROOT"]), 1)

    # -- the firmware
    def firmware(self):
        drv, dev, sd = self.conf.get("NODE_DRIVER"), self.conf.get("NODE_DEVICE", ""), self.fat.get(self.sd(), {})
        if drv == "pi-sd":
            m = re.search(r"(?m)^os_prefix=(\w+)/", sd.get("config.txt", ""))
            return kv(sd.get(m.group(1) + "/cmdline.txt", "").replace(" ", "\n")).get("root", "") if m else None
        if drv == "pi-tryboot":
            return kv(sd.get("second/cmdline.txt", "").replace(" ", "\n")).get("root", "") if "tryboot.txt" in sd else None
        if drv == "rpi5-usb" and self.one_shot and self.one_shot.endswith("4"):
            pair = kv(self.fat.get(part(dev, 1), {}).get("autoboot.txt", "")).get("boot_partition", "") or "1"
            return part(dev, int(pair) + 1)
        if drv == "pi-mbr" and self.mbr.get(dev) == "0c":
            return part(dev, 2)
        return None

    def reboot(self, tryboot=False):
        self.effects.append(("reboot", tryboot))
        self.tryboot = tryboot
        root = self.firmware()
        self.one_shot = None
        self.running = root if root in self.roots else self.conf["NODE_ROOT"]
        self.boots += 1
        self.clock.sleep(40)
        self.booted = int(self.clock.now())
        failsafe = self.roots[self.running].get("failsafe")
        if failsafe:
            self.run_onboard(failsafe, {"WK_SD": self.sd()})
        return Result(0)

    # -- the Channel: every call is the real one, over the two sides
    def on_rescue(self):
        return self.running == self.conf["NODE_ROOT"]

    def machine(self, fn):
        return self.sides[fn]

    def call(self, fn, *args, input=None, mutates=False):
        if mutates:
            self.effects.append((fn,) + tuple(getattr(a, "name", a) for a in args))
        return super().call(fn, *args, input=input, mutates=mutates)

    def answer(self, side, argv, input=None):
        if not self.up or self.on_rescue() != (side == "host"):
            return Result(255, "", "ssh: connect: no route")
        argv = argv[2:] if argv[:2] == ["sudo", "-n"] else argv
        if argv[:1] == [CARD_PRIV]:
            return self.card(*argv[1:])
        if argv[:1] == [BOOT_PRIV]:
            return self.priv(*argv[1:])
        if argv[:1] == ["vcmailbox"]:
            return self.priv("order", argv[-1])
        for verb in ("reboot", "reboot-tryboot"):
            if argv == root_priv(verb):
                return self.priv(verb)
        if argv[:2] == ["sh", "-c"]:
            head = PARAMS.match(argv[2]).group(1)
            name = self.onboard.get(argv[2][len(head):])
            params = dict(w.split("=", 1) for w in head.split("; ") if w)
            if name:
                return self.run_onboard(name, params, input)
        return Result(127, "", "%s: no answer on this board" % " ".join(argv))

    def run_onboard(self, name, p, input=None):
        sys_ = self.roots[self.running]
        sd = self.fat.setdefault(self.sd(), {})
        if name == "probe.sh":
            ident = "id=%s\nrole=%s\n" % (sys_["id"], sys_["role"]) if sys_["id"] else ""
            return Result(0, "%srootdev=%s\n" % (ident, self.running))
        if name == "boot-id.sh":
            return Result(0, "boot-%d\n" % self.boots)
        if name == "booted-at.sh":
            return Result(0, "%d\n" % self.booted)
        if name == "watchdog-present.sh":
            return Result(0 if sys_.get("watchdog") else 1)
        if name == "has-systemd.sh":
            return Result(0 if sys_.get("systemd", True) else 1)
        if name == "part-absent.sh":
            return Result(0, "yes\n" if p["WK_DEV"] in self.fat else "no\n")
        if name == "medium-read.sh":
            return Result(0, self.fat.get(p["WK_PART"], {}).get(p["WK_NAME"], ""))
        if name == "keep.sh":
            self.kept = True
            return Result(0)
        if name == "eeprom.sh":
            return self.eeprom_do(p, input)
        if name == "eeprom-order.sh":
            return Result(0, "eeprom_boot_order=0xf41\n")
        if name == "pimbr-type.sh":
            return Result(0, " %s\n" % self.mbr.get(p["WK_DEV"], ""))
        if name == "pimbr-set-type.sh":
            if not self.stuck:
                self.mbr[p["WK_DEV"]] = "%02x" % int(p["WK_OCT"], 8)
            return Result(0)
        if name == "pimbr-self-disarm.sh":
            self.mbr[disk_of(self.running)] = "83"
            return Result(0)
        if name == "pisd-self-disarm.sh":
            if "config.txt.rescue" in sd:
                sd["config.txt"] = sd.pop("config.txt.rescue")
            return Result(0)
        if name == "tryboot-self-disarm.sh":
            self.tryboot_drop(sd)
            return Result(0)
        if name == "tryboot.sh":
            return self.tryboot_do(p, sd)
        if name.startswith("record-"):
            return self.record_do(name, input)
        return Result(127, "", "%s: not an on-board script this board knows" % name)

    def eeprom_do(self, p, input):
        do, tool = p["WK_DO"], self.eeprom_tool
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

    def tryboot_do(self, p, sd):
        do = p["WK_DO"]
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
        if do == "staged":
            return Result(0, "yes\n" if "tryboot.txt" in sd and "second/cmdline.txt" in sd else "no\n")
        if do == "staged-root":
            return Result(0, "root=%s\n" % kv(sd.get("second/cmdline.txt", "").replace(" ", "\n")).get("root", ""))
        running = "root=%s" % self.running
        staged = "root=%s" % kv(sd.get("second/cmdline.txt", "").replace(" ", "\n")).get("root", "")
        return Result(0, "staging\n" if running == staged else "sd-config\n" if self.on_rescue() else "unknown\n")

    def record_do(self, name, input):
        if name == "record-write.sh":
            self.record = input + "armed_at=%s\n" % self.clock.iso()
        elif name == "record-clear.sh":
            self.record = None
        return Result(0, self.record or "")

    def card(self, verb, *args):
        if verb == "boot-read":
            p = part(args[0], args[1])
            return Result(0, self.fat[p].get(args[2], "")) if p in self.fat else Result(1, "", "no such partition")
        if verb == "unmount":
            return Result(0)
        if verb == "autoboot":
            if not self.stuck:
                self.fat.setdefault(part(args[0], 1), {})["autoboot.txt"] = "[all]\nboot_partition=%s\n" % args[1]
            return Result(0)
        dev, _, slot = args[0].partition("@")
        sd = self.fat.setdefault(part(dev, 1), {})
        if verb == "second-state":
            boots = (5, 3) if slot == "second" else (7,)
            present = any(part(dev, n) in self.fat for n in boots)
            armed = "config.txt.rescue" in sd
            prefix = re.search(r"(?m)^os_prefix=(\w+)/", sd.get("config.txt", ""))
            out = "wk-card-priv: armed=%s\n" % ("yes" if armed else "no")
            if armed and prefix:
                out += "wk-card-priv: armed_prefix=%s\n" % prefix.group(1)
            return Result(0, out + "wk-card-priv: present=%s\n" % ("yes" if present else "no"))
        if verb == "second-arm":
            src = next((self.fat[part(dev, n)] for n in ((5, 3) if slot == "second" else (7,)) if part(dev, n) in self.fat), None)
            if src is None:
                return Result(1, "", "holds no system")
            if not self.stuck:
                for k in [k for k in sd if k.startswith(("second/", "third/"))]:
                    del sd[k]
                for k, v in src.items():
                    sd["%s/%s" % (slot, k)] = v
                sd.setdefault("config.txt.rescue", sd.get("config.txt", ""))
                sd["config.txt"] = "os_prefix=%s/\n%s" % (slot, src["config.txt"])
            return Result(0)
        if verb == "second-disarm":
            if "config.txt.rescue" in sd:
                sd["config.txt"] = sd.pop("config.txt.rescue")
            return Result(0)
        return Result(3, "", "REFUSED: %s" % verb)

    def priv(self, verb, *args):
        if verb == "order":
            self.one_shot = args[0]
            return Result(0, "0x00000004 0x80000000 %s\n" % args[0])
        if verb == "reboot":
            return self.reboot()
        if verb == "reboot-tryboot":
            return self.reboot(tryboot=True)
        return Result(0)
