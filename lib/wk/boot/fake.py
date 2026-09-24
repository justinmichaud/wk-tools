"""A board in memory, answering a driver's Channel: its media, a firmware one-shot and a clock. `firmware` is how
each arrangement picks the next boot; `stuck` makes every arming write land nowhere, as an older helper does."""

import re

from wk.boot.driver import disk_of, kv, part
from wk.clock import FakeClock
from wk.machine import Result


class FakeBoard:
    def __init__(self, conf, clock=None):
        self.conf = dict(conf)
        self.clock = clock or FakeClock()
        self.channel = "none"
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
            return kv(sd.get(m.group(1) + "/cmdline.txt", "").replace(" ", "\n"), "root") if m else None
        if drv == "pi-tryboot":
            return kv(sd.get("second/cmdline.txt", "").replace(" ", "\n"), "root") if "tryboot.txt" in sd else None
        if drv == "rpi5-usb" and self.one_shot and self.one_shot.endswith("4"):
            pair = kv(self.fat.get(part(dev, 1), {}).get("autoboot.txt", ""), "boot_partition") or "1"
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

    # -- the Channel
    def on_rescue(self):
        return self.running == self.conf["NODE_ROOT"]

    def call(self, fn, *args, input=None, mutates=False):
        if mutates:
            self.effects.append((fn,) + tuple(getattr(a, "name", a) for a in args))
        if fn in ("m_ssh", "i_ssh"):
            if not self.up or self.on_rescue() != (fn == "m_ssh"):
                return Result(255, "", "ssh: connect: no route")
            return self.script(args[0], input)
        if fn in ("r_ssh", "r_sudo"):
            return self.call({"host": "m_ssh", "bench": "i_ssh"}.get(self.channel, "none"), *args, input=input)
        if fn == "card_priv":
            return self.card(*args)
        if fn == "boot_priv":
            return self.priv(*args)
        if fn in ("boot_priv_require", "disk_unmount"):
            return Result(0)
        if fn == "disk_own_or_declared":
            return Result(0, self.conf.get("NODE_DEVICE", ""))
        return Result(1, "", "%s: not reachable" % fn)

    def script(self, ob, input):
        if ob == "true":
            return Result(0)
        return self.run_onboard(ob.name, ob.params, input)

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
            return Result(0, "root=%s\n" % kv(sd.get("second/cmdline.txt", "").replace(" ", "\n"), "root"))
        running = "root=%s" % self.running
        staged = "root=%s" % kv(sd.get("second/cmdline.txt", "").replace(" ", "\n"), "root")
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
