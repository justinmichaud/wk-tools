"""The Pi arrangements: how each board is armed for one boot of the system on its bench medium."""

from wk import act
from wk.boot.driver import Driver, disk_of, part, partno
from wk.kv import kv


class PiSd(Driver):
    """One SD card holds every system (rescue on partitions 1-2); the firmware boots the first FAT partition only, so arming is an `os_prefix=` line in the rescue's config.txt."""

    name = "pi-sd"
    arming = "medium"
    arm_from_bench = True
    system_parts = (3, 5, 7)
    failsafe = "pisd-self-disarm.sh"

    def boot_part(self):
        return part(self.c("device"), 3)

    def slot(self, p):
        n = partno(p)
        if n in ("3", "5"):
            return "second"
        if n == "7":
            return "third"
        act.die("'%s' is not a bench system's boot partition on %s" % (p, self.c("device")))

    def state(self, addr):
        r = self.card("second-state", addr)
        return r.out.replace("\r", "") if r.ok else None

    def arm(self, p, order=""):
        slot = self.slot(p)
        addr = "%s@%s" % (self.c("device"), slot)
        state = self.state(addr)
        if state is None:
            act.die("could not read %s's arming on %s: its rescue has to be up and carry the card helper."
                    % (self.c("device"), self.c("name")))
        if "present=yes" not in state:
            act.die("%s on %s holds no bench system at %s.\n    Write one first:  wk sysimage write --from <path> --disk <reader>:%s"
                    % (self.c("device"), self.c("name"), slot, addr))
        if "armed_prefix=%s" % slot in state:
            act.debug("%s is already armed for %s" % (self.c("name"), slot))
            return 0
        if not self.card("second-arm", addr, mutates=True).ok:
            act.die("could not arm the %s system on %s" % (slot, self.c("name")))
        if "armed_prefix=%s" % slot not in (self.state(addr) or ""):
            act.die("%s on %s is not armed for the %s system after being told to." % (self.c("device"), self.c("name"), slot))
        return 0

    def disarm(self):
        addr = self.c("device") + "@second"
        state = self.state(addr)
        if not state or "armed=yes" not in state:
            return 0
        if not self.card("second-disarm", addr, mutates=True).ok:
            act.die("could not disarm the bench system on %s" % self.c("name"))
        return 0

    def disarm_note(self):
        return "  the rescue's config.txt is back on %s; 'wk boot %s' arms again." % (self.c("device"), self.c("name"))

    def evidence(self):
        state = self.state(self.c("device") + "@second")
        if state is None:
            return "arming=unreadable (the rescue did not answer)"
        lines = ["arrangement=one SD card, rescue on partitions 1-2, bench system(s) beside it (os_prefix arming)"]
        lines += [l[len("wk-card-priv: "):] for l in state.splitlines() if l.startswith("wk-card-priv: armed=")]
        lines += ["system=%s (on %s)" % (i, p) for p, i in (self.systems() or [])]
        return "\n".join(lines)

    def media(self):
        return "SD card %s holds every system: rescue on p1-p2, bench system(s) on p3-p4 or 5-6 and 7-8" % self.c("device")

    def reprovision(self):
        dev, name, prof = self.c("device"), self.c("name"), self.c("image_preset")
        return ("wk sysimage build %s\n"
                "wk sysimage write --from <path> --disk <reader>:%s --rescue --image-preset %s\n"
                "wk sysimage write --from <path> --disk <reader>:%s@second --image-preset <bench image preset>\n"
                "wk sysimage write --from <path> --disk <reader>:%s@third --image-preset <bench image preset>\n"
                "    optional: a second bench system\nwk boot %s" % (prof, dev, prof, dev, dev, name))


class PiTryboot(Driver):
    """A Pi 4 whose bench medium the bootloader will not boot: the selected system's kernel is staged into `second/`
    on the rescue's SD beside a `tryboot.txt` that `reboot "0 tryboot"` makes the firmware read."""

    name = "pi-tryboot"
    arming = "medium"
    system_parts = (1, 3)
    failsafe = "tryboot-self-disarm.sh"

    def sd(self):
        return part(disk_of(self.c("root")), 1)

    def failsafe_params(self):
        return {"WK_SD": self.sd()}

    def tryboot(self, do, mutates=False, **params):
        return self.run("tryboot.sh", mutates=mutates, WK_DO=do, WK_SD=self.sd(), **params)

    def arm(self, p, order=""):
        if not self.tryboot("stage", mutates=True, WK_SRC=p, WK_DTB=self.c("dtb")).ok:
            act.die("could not stage the tryboot files on %s: the board has to answer and read both the SD and %s."
                    % (self.c("name"), self.c("device")))
        want = kv((self.medium_read(p, "cmdline.txt") or "").replace(" ", "\n")).get("root", "")
        staged = kv(self.tryboot("staged-root").out).get("root", "")
        if not want or staged != want:
            act.die("the staging on %s's SD boots root=%s, not the selected system's root=%s (on %s)"
                    % (self.c("name"), staged or "?", want or "?", p))
        return 0

    def reboot(self, armed=False):
        return self.reboot_tryboot() if armed else Driver.reboot(self)

    def disarm(self):
        self.tryboot("disarm", mutates=True)
        return 0

    def disarm_note(self):
        return "  the staged second/ and tryboot.txt are gone from the SD's boot partition."

    SOURCES = {
        "staging": "the tryboot staging now on the SD (second/), so this boot spent this arming",
        "sd-config": "the SD config.txt, the plain path",
        "unknown": "an earlier staging: the last arming did not reboot the board, or the firmware did not consume its flag",
    }

    def evidence(self):
        staged = self.tryboot("staged").out.replace("\r", "").strip().split("\n")[0]
        source = self.tryboot("source").out.replace("\r", "").strip().split("\n")[0]
        lines = ["arrangement=kernel from the SD via tryboot (one boot, firmware-reverting); bench root on %s" % self.c("device"),
                 "boot_source=" + self.SOURCES.get(source, "unreadable (the board did not answer the probe)"),
                 "tryboot_staged=" + (staged or "unreadable")]
        lines += ["system=%s (on %s)" % (i, p) for p, i in (self.systems() or [])]
        return "\n".join(lines)

    def media(self):
        return "%s holds the bench system(s) on 1-2 and 3-4; the armed kernel is tryboot-staged onto the SD" % self.c("device")

    def reprovision(self):
        dev, name, prof = self.c("device"), self.c("name"), self.c("image_preset")
        return ("wk sysimage build %s\n"
                "wk sysimage write --from <path> --disk <reader>:%s --rescue --image-preset %s\n"
                "wk boot %s --boot-order sd-first\n"
                "wk sysimage write --from <path> --disk %s:%s --image-preset <bench image preset>\n"
                "wk sysimage write --from <path> --disk %s:%s@second --image-preset <bench image preset>\n"
                "    optional: a second system\nwk boot %s"
                % (prof, disk_of(self.c("root")), prof, name, name, dev, name, dev, name))


class Rpi5Usb(Driver):
    """Raspberry Pi 5: one-shot USB boot through the firmware mailbox's set_reboot_order, which the firmware clears after
    one use (nibbles lowest first: 4=USB, 6=NVMe, f=restart). The pair is `boot_partition=` in the stick's autoboot.txt:
    this board's tryboot flag belongs to flash-kernel's staging on its NVMe."""

    name = "rpi5-usb"
    order_image = "0xf64"
    order_normal = "0xf461"
    system_parts = (1, 3)
    selects_by_partition = True

    def arm(self, p, order=""):
        if not self.ch.call("boot_priv_require").ok:
            raise act.Refused(1)
        n = partno(p) if p else "1"
        if n not in ("1", "3"):
            act.die("'%s' is not a boot partition this stick selects between (1 or 3)" % p)
        self.select_pair(n)
        r = self.ch.call("boot_priv", "order", order, mutates=True)
        if not r.ok:
            act.die("the firmware mailbox call failed on %s" % self.c("name"))
        words = r.out.split()
        if words[1:2] != ["0x80000000"]:
            act.die("the firmware refused the boot order (%s, wanted 0x80000000)\n    reply: %s"
                    % (words[1] if len(words) > 1 else "", r.out.strip()))
        act.debug("mailbox reply: %s" % r.out.strip())
        return 0

    def select_pair(self, pair):
        dev, name = self.c("device"), self.c("name")
        if not self.ch.call("disk_unmount", dev, mutates=True).ok:
            raise act.Refused(1)
        if not self.card("autoboot", dev, pair, mutates=True).ok:
            act.die("could not write %s's pair selector on %s" % (dev, name))
        out = self.medium_read(part(dev, 1), "autoboot.txt") or ""
        if "boot_partition=%s" % pair not in out:
            act.die("%s on %s does not select pair %s after being told to: its card helper is older than the pair argument.\n"
                    "    The remedy, from a terminal on %s:  ./setup --stage quiesce" % (dev, name, pair, name))

    def evidence(self):
        # The one-shot order is write-only from userspace, so the EEPROM's persistent order is the only evidence.
        return self.run("eeprom-order.sh").out.rstrip("\n")

    def media(self):
        dev, mode = self.c("device"), self.mode
        if mode.startswith("bench"):
            return "booted from its USB stick (system %s); NVMe untouched" % mode[6:]
        if mode.startswith("base"):
            return "booted %s -- a wk system on the medium that is never armed (%s)" % (self.c("root") or "its base medium", mode[5:])
        if mode != "host":
            return "USB stick %s: state unknown (board unreachable)" % dev
        order = kv(self.evidence()).get("eeprom_boot_order", "")
        return "USB stick %s holds %s; NVMe workstation untouched%s" % (
            dev, self.device_image() or "no wk system (wk sysimage write puts one there)", " (eeprom %s)" % order if order else "")

    def reprovision(self):
        dev, name, prof = self.c("device"), self.c("name"), self.c("image_preset")
        return ("wk sysimage build %s\nwk sysimage write --from <path> --disk %s:%s\n"
                "wk sysimage write --from <path> --disk %s:%s@second\n    optional: a second system\nwk boot %s"
                % (prof, name, dev, name, dev, name))


DRIVERS = {d.name: d for d in (PiSd, PiTryboot, Rpi5Usb)}
