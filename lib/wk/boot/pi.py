"""The Pi arrangements: how each board is armed for one boot of the system on its bench medium."""

from wk import act
from wk.boot.driver import Driver, disk_of, part, partno
from wk.kv import kv


class PiSd(Driver):
    """One SD card holds every system, the rescue on partitions 1-2. The firmware boots the first FAT partition only,
    so arming is an `os_prefix=` line the card helper writes into the rescue's config.txt."""

    name = "pi-sd"
    arming = "medium"
    arm_from_bench = True
    system_parts = (3, 5, 7)
    failsafe = "pisd-self-disarm.sh"
    disarms = True

    def boot_part(self):
        return part(self.c("NODE_DEVICE"), 3)

    def slot(self, p):
        n = partno(p)
        if n in ("3", "5"):
            return "second"
        if n == "7":
            return "third"
        act.die("'%s' is not a bench system's boot partition on %s" % (p, self.c("NODE_DEVICE")))

    def state(self, addr):
        r = self.card("second-state", addr)
        return r.out.replace("\r", "") if r.ok else None

    def arm(self, p, order=""):
        if not p:
            act.die("arming needs the selected boot partition (select_system, wk boot)")
        slot = self.slot(p)
        addr = "%s@%s" % (self.c("NODE_DEVICE"), slot)
        state = self.state(addr)
        if state is None:
            act.die("could not read %s's arming on %s.\n    Arming this board is an edit of its rescue's boot partition, made by the\n"
                    "    card helper on the rescue, so the rescue has to be up and carry the helper." % (self.c("NODE_DEVICE"), self.c("NODE_NAME")))
        if "present=yes" not in state:
            act.die("%s on %s holds no bench system at %s.\n    Write one first:  wk sysimage write --from <path> --disk <reader>:%s"
                    % (self.c("NODE_DEVICE"), self.c("NODE_NAME"), slot, addr))
        if "armed_prefix=%s" % slot in state:
            act.debug("%s is already armed for %s" % (self.c("NODE_NAME"), slot))
            return 0
        if not self.card("second-arm", addr, mutates=True).ok:
            act.die("could not arm the %s system on %s" % (slot, self.c("NODE_NAME")))
        if "armed_prefix=%s" % slot not in (self.state(addr) or ""):
            act.die("%s on %s is not armed for the %s system after being told to, so the board would\n"
                    "    boot another system and measure it under this one's name." % (self.c("NODE_DEVICE"), self.c("NODE_NAME"), slot))
        return 0

    def disarm(self):
        addr = self.c("NODE_DEVICE") + "@second"
        state = self.state(addr)
        if not state or "armed=yes" not in state:
            return 0
        if not self.card("second-disarm", addr, mutates=True).ok:
            act.die("could not disarm the bench system on %s" % self.c("NODE_NAME"))
        return 0

    def disarm_note(self):
        return ("  the rescue's config.txt is back on %s, so the firmware boots the\n"
                "  rescue's kernel again. 'wk boot %s' arms the bench system once more." % (self.c("NODE_DEVICE"), self.c("NODE_NAME")))

    def evidence(self):
        state = self.state(self.c("NODE_DEVICE") + "@second")
        if state is None:
            return "arming=unreadable (the rescue did not answer)"
        lines = ["lane=one SD card, rescue on partitions 1-2, bench system(s) beside it (os_prefix arming)"]
        lines += [l[len("wk-card-priv: "):] for l in state.splitlines() if l.startswith("wk-card-priv: armed=")]
        lines += ["system=%s (on %s)" % (i, p) for p, i in (self.systems() or [])]
        return "\n".join(lines)

    def media(self):
        return ("SD card %s holds every system: rescue on p1-p2, bench system(s) beside it -- p3-p4, or pairs 5-6 and 7-8 "
                "in an extended p3 (wk boot %s --system <id> arms one for one boot)" % (self.c("NODE_DEVICE"), self.c("NODE_NAME")))

    def reprovision(self):
        dev, name, prof = self.c("NODE_DEVICE"), self.c("NODE_NAME"), self.c("NODE_PROFILE")
        return ("wk sysimage build %s\n    in a workspace; hours\n"
                "wk sysimage write --from <path> --disk <reader>:%s --rescue --profile %s\n"
                "    no --grow: the rest of the card is where the bench system goes\n"
                "wk sysimage write --from <path> --disk <reader>:%s@second --profile <bench profile>\n"
                "    the first bench system beside the rescue\n"
                "wk sysimage write --from <path> --disk <reader>:%s@third --profile <bench profile>\n"
                "    optional: a second bench system (the shared layout holds two), for an\n"
                "    A/B across images; 'wk boot %s --system <id>' picks one\n"
                "    then carry the card to %s and power it on\nwk boot %s" % (prof, dev, prof, dev, dev, name, name, name))


class PiTryboot(Driver):
    """A Pi 4 whose bench medium the bootloader will not boot: the selected system's kernel is staged into `second/`
    on the rescue's SD beside a `tryboot.txt` a `reboot "0 tryboot"` makes the firmware read, and the kernel mounts
    the bench root by PARTUUID. The flag rides systemd's reboot parameter, so the arming system runs systemd."""

    name = "pi-tryboot"
    arming = "medium"
    system_parts = (1, 3)
    failsafe = "tryboot-self-disarm.sh"
    disarms = True

    def sd(self):
        return part(disk_of(self.c("NODE_ROOT")), 1)

    def failsafe_params(self):
        """This board does not consume the tryboot flag, so the boot that spends the staging removes it."""
        return {"WK_SD": self.sd()}

    def tryboot(self, do, mutates=False, **params):
        return self.run("tryboot.sh", mutates=mutates, WK_DO=do, WK_SD=self.sd(), **params)

    def arm(self, p, order=""):
        if not p:
            act.die("arming needs the selected boot partition (select_system, wk boot)")
        if not self.tryboot("stage", mutates=True, WK_SRC=p, WK_DTB=self.c("NODE_DTB")).ok:
            act.die("could not stage the tryboot files on %s.\n    Arming copies the selected system's kernel out of %s's boot\n"
                    "    partition onto the SD, so the board has to answer -- as its rescue or as a\n"
                    "    bench system, either will do -- and both media have to be readable there." % (self.c("NODE_NAME"), self.c("NODE_DEVICE")))
        want = kv((self.medium_read(p, "cmdline.txt") or "").replace(" ", "\n")).get("root", "")
        staged = kv(self.tryboot("staged-root").out).get("root", "")
        if not want or staged != want:
            act.die("the staging on %s's SD boots root=%s, not the selected system's root=%s (on %s),\n"
                    "    so the board would measure another system under this one's name." % (self.c("NODE_NAME"), staged or "?", want or "?", p))
        return 0

    def reboot(self, armed=False):
        return self.reboot_tryboot() if armed else Driver.reboot(self)

    def disarm(self):
        self.tryboot("disarm", mutates=True)
        return 0

    def disarm_note(self):
        return ("  the staged second/ and tryboot.txt are gone from the SD's boot partition;\n"
                "  the tryboot flag itself is the firmware's and any boot clears it.")

    SOURCES = {
        "staging": "the tryboot staging now on the SD (second/), so this boot spent this arming",
        "sd-config": "the SD config.txt, the plain path",
        "unknown": "neither the SD config.txt nor the staging now on the SD -- this\n  boot came from an earlier staging, so the last arming "
                   "either did not reboot\n  the board or the firmware did not consume its flag. What is staged now is\n"
                   "  what the next boot would use, not what is running.",
    }

    def evidence(self):
        staged = self.tryboot("staged").out.replace("\r", "").strip().split("\n")[0]
        source = self.tryboot("source").out.replace("\r", "").strip().split("\n")[0]
        lines = ["lane=kernel from the SD via tryboot (one boot, firmware-reverting); bench root on %s" % self.c("NODE_DEVICE"),
                 "boot_source=" + self.SOURCES.get(source, "unreadable (the board did not answer the probe)"),
                 "tryboot_staged=" + (staged or "unreadable")]
        lines += ["system=%s (on %s)" % (i, p) for p, i in (self.systems() or [])]
        return "\n".join(lines)

    def media(self):
        return ("%s holds the bench system(s): root on 1-2 and, when written, a second on 3-4; the armed kernel is "
                "tryboot-staged onto the SD, which also carries the rescue" % self.c("NODE_DEVICE"))

    def reprovision(self):
        dev, name, prof = self.c("NODE_DEVICE"), self.c("NODE_NAME"), self.c("NODE_PROFILE")
        return ("wk sysimage build %s\n    in a workspace; hours\n"
                "wk sysimage write --from <path> --disk <reader>:%s --rescue --profile %s\n"
                "    the SD card -- the system this board falls back to, and the firmware's boot medium\n"
                "wk boot %s --boot-order sd-first\n"
                "    the SD first: the bench medium is mounted by the kernel, never firmware-booted\n"
                "wk sysimage write --from <path> --disk %s:%s --profile <bench profile>\n"
                "    the bench system's root medium, written from the rescue\n"
                "wk sysimage write --from <path> --disk %s:%s@second --profile <bench profile>\n"
                "    optional: a second system beside the first (partitions 3-4), for an\n"
                "    A/B across images; 'wk boot %s --system <id>' picks one\n"
                "wk boot %s\n    one shot; the firmware reverts by itself"
                % (prof, disk_of(self.c("NODE_ROOT")), prof, name, name, dev, name, dev, name, name))


class Rpi5Usb(Driver):
    """Raspberry Pi 5: one-shot USB boot through the firmware mailbox's set_reboot_order, a register the firmware
    clears after one use (nibbles lowest first: 4=USB, 6=NVMe, f=restart). The pair is selected by `boot_partition=`
    in the stick's autoboot.txt: this board's tryboot flag belongs to flash-kernel's staging on its NVMe."""

    name = "rpi5-usb"
    order_image = "0xf64"
    order_normal = "0xf461"
    system_parts = (1, 3)
    selects_by_partition = True
    AUTOBOOT = "autoboot.txt"

    def arm(self, p, order=""):
        if not self.ch.call("boot_priv_require").ok:
            raise act.Refused(1)
        n = partno(p) if p else "1"
        if n not in ("1", "3"):
            act.die("'%s' is not a boot partition this stick selects between\n"
                    "    (partition 1 or 3, the two pairs of a dedicated bench medium)" % p)
        self.select_pair(n)
        r = self.ch.call("boot_priv", "order", order, mutates=True)
        if not r.ok:
            act.die("the firmware mailbox call failed on %s" % self.c("NODE_NAME"))
        words = r.out.split()
        if words[1:2] != ["0x80000000"]:
            act.die("the firmware refused the boot order (%s, wanted 0x80000000)\n    reply: %s"
                    % (words[1] if len(words) > 1 else "", r.out.strip()))
        act.debug("mailbox reply: %s" % r.out.strip())
        return 0

    def select_pair(self, pair):
        dev, name = self.c("NODE_DEVICE"), self.c("NODE_NAME")
        if not self.ch.call("disk_unmount", dev, mutates=True).ok:
            raise act.Refused(1)
        if not self.card("autoboot", dev, pair, mutates=True).ok:
            act.die("could not write %s's pair selector on %s" % (dev, name))
        out = self.medium_read(part(dev, 1), self.AUTOBOOT) or ""
        if "boot_partition=%s" % pair not in out:
            act.die("%s on %s does not select pair %s after being told to,\n    so the board would boot the other system and it would be "
                    "measured under this\n    one's name. Its card helper is older than the pair argument and ignores it.\n"
                    "    The remedy, from a terminal on %s:  ./setup --stage quiesce\n    what its %s says now:\n%s"
                    % (dev, name, pair, name, self.AUTOBOOT, "\n".join("      " + l for l in out.splitlines())))

    def evidence(self):
        """The one-shot order is write-only from userspace, so the EEPROM's persistent order is the only evidence."""
        return self.run("eeprom-order.sh").out.rstrip("\n")

    def media(self):
        dev, mode = self.c("NODE_DEVICE"), self.mode
        if mode.startswith("bench"):
            return "booted from its USB stick (system %s); NVMe untouched" % mode[6:]
        if mode.startswith("base"):
            return "booted %s -- a wk system on the medium that is never armed (%s)" % (self.c("NODE_ROOT") or "its base medium", mode[5:])
        if mode != "host":
            return "USB stick %s: state unknown (board unreachable)" % dev
        order = kv(self.evidence()).get("eeprom_boot_order", "")
        return "USB stick %s holds %s; NVMe workstation untouched%s" % (
            dev, self.device_image() or "no wk system (wk sysimage write puts one there)", " (eeprom %s)" % order if order else "")

    def reprovision(self):
        dev, name, prof = self.c("NODE_DEVICE"), self.c("NODE_NAME"), self.c("NODE_PROFILE")
        return ("wk sysimage build %s\n    in a workspace; hours\n"
                "wk sysimage write --from <path> --disk %s:%s\n"
                "wk sysimage write --from <path> --disk %s:%s@second\n"
                "    optional: a second system beside the first, for an A/B across two images.\n"
                "    Making it also writes the firmware's selector (autoboot.txt) onto the\n"
                "    medium, which is what lets 'wk boot %s --system <id>' choose\n"
                "wk boot %s\n    one shot; it reverts by itself" % (prof, name, dev, name, dev, name, name))


class PiMbr(Driver):
    """Two media, armed by one byte: the bench medium's partition 1 MBR type, 0x0c armed and 0x83 disarmed. Firmware
    finding no FAT partition steps over the medium to the rescue; one finding it incomplete halts, so the byte moves."""

    name = "pi-mbr"
    arming = "medium"
    failsafe = "pimbr-self-disarm.sh"
    disarms = True
    ARMED, DISARMED = "0c", "83"

    def dev(self):
        from wk.sysimage.disk import Disks   # the disk model is sysimage's; this is its one reader in boot
        got = Disks(self.ch, self.conf).own_or_declared()
        if not got:
            act.die("cannot tell which disk on %s is its bench medium.\n    Its conf says %s, and the board does not agree or could not be\n"
                    "    asked. Refusing to write a partition type byte to a disk chosen by name:\n"
                    "    on this board that byte decides whether it comes back at all." % (self.c("NODE_NAME"), self.c("NODE_DEVICE") or "nothing"))
        return got

    @staticmethod
    def word(dev):
        return {"/dev/mm": "SD card", "/dev/sd": "USB stick"}.get(dev[:7], dev)

    def rescue_disk(self):
        return disk_of(self.c("NODE_ROOT"))

    def boot_part(self):
        return part(self.dev(), 1)

    def type(self):
        return self.sudo("pimbr-type.sh", WK_DEV=self.dev()).out.replace(" ", "").strip()

    def state(self):
        t = self.type()
        return {self.ARMED: "armed", self.DISARMED: "disarmed", "": None}.get(t, "foreign")

    def set_type(self, hexbyte):
        if not self.sudo("pimbr-set-type.sh", mutates=True, WK_DEV=self.dev(), WK_OCT="%03o" % int(hexbyte, 16)).ok:
            act.die("could not write %s's partition type on %s" % (self.c("NODE_DEVICE"), self.c("NODE_NAME")))
        got = self.type()
        if got != hexbyte:
            act.die("%s's partition type on %s still reads\n    0x%s after writing 0x%s. This byte is what decides whether the board's\n"
                    "    firmware boots the %s or steps over it to the rescue, so a write\n    that did not take is not something to continue past.\n\n"
                    "    The board has not been rebooted; it is still in whatever role it was in."
                    % (self.c("NODE_DEVICE"), self.c("NODE_NAME"), got or "unreadable", hexbyte, self.word(self.c("NODE_DEVICE"))))

    def arm(self, p, order=""):
        dev, name = self.c("NODE_DEVICE"), self.c("NODE_NAME")
        state = self.state()
        if state is None:
            act.die("could not read %s's partition table on %s.\n    Arming this machine means writing one byte of it, so the bench medium has to be\n"
                    "    attached and readable from the rescue." % (dev, name))
        if state == "armed":
            act.debug("%s's %s is already armed" % (name, self.word(dev)))
        elif state == "disarmed":
            self.set_type(self.ARMED)
        else:
            act.die("%s on %s has partition type 0x%s on partition\n    1, which is neither 0x%s nor 0x%s. That is not a disk\n"
                    "    this driver put an image on, and arming it would be a guess.\n\n    Write one first:  wk sysimage write <id> --disk %s:%s"
                    % (dev, name, self.type(), self.ARMED, self.DISARMED, name, dev))
        return 0

    def disarm(self):
        if self.state() == "armed":
            self.set_type(self.DISARMED)
        return 0

    def disarm_note(self):
        return ("  %s's partition 1 is typed 0x%s, so the firmware finds no\n  boot filesystem there and %s boots its rescue on %s. "
                "'wk boot %s' puts it back." % (self.c("NODE_DEVICE"), self.DISARMED, self.c("NODE_NAME"), self.rescue_disk(), self.c("NODE_NAME")))

    def evidence(self):
        return "%s\nbench_medium=%s" % (self.run("eeprom-order.sh").out.rstrip("\n"), self.state() or "unreadable")

    def media(self):
        dev, mode = self.c("NODE_DEVICE"), self.mode
        bench, rescue = self.word(dev), self.word(self.rescue_disk())
        if mode.startswith("bench"):
            return "booted from its %s (system %s); the %s is the rescue" % (bench, mode[6:], rescue)
        if not (mode == "host" or mode.startswith("base")):
            return "%s %s: state unknown (board unreachable); the %s is the rescue" % (bench, dev, rescue)
        held = "%s %s holds %s, %s" % (bench, dev, self.device_image() or "no wk system (wk sysimage write puts one there)",
                                      self.state() or "unreadable")
        if mode.startswith("base"):
            return "booted its rescue on the %s (%s); %s" % (rescue, mode[5:], held)
        return "%s; the %s is the rescue" % (held, rescue)

    def reprovision(self):
        dev, name, rescue = self.c("NODE_DEVICE"), self.c("NODE_NAME"), self.rescue_disk()
        return ("wk sysimage build %s\n    in a workspace; hours\n"
                "wk sysimage write <id> --disk <reader>:%s --rescue\n    the %s -- the system this board falls back to\n"
                "wk boot %s --boot-order %s\n    the %s first, the rescue behind it\n"
                "wk sysimage write <id> --disk %s:%s\n    the %s -- the system it is measured on\n"
                "wk boot %s\n    one shot; it reverts by itself"
                % (self.c("NODE_PROFILE"), rescue, self.word(rescue), name, "sd-first" if dev.startswith("/dev/mm") else "usb-first",
                   self.word(dev), name, dev, self.word(dev), name))


DRIVERS = {d.name: d for d in (PiSd, PiTryboot, Rpi5Usb, PiMbr)}
