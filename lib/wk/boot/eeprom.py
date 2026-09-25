"""`wk boot <board> --boot-order`: a Pi's bootloader EEPROM BOOT_ORDER, written in place with rpi-eeprom-config or, on a
system without it, by staging a whole pinned bootloader for recovery.bin, which the ROM runs before anything else and
which flashes pieeprom.upd (checked against pieeprom.sig) on the next boot."""

import difflib
import hashlib
import os
import re

from wk import act
from wk.clock import Clock
from wk.kv import kv
from wk.machine import Local, Result
from wk.store import Store

COMMIT = "86759b04b22173e10186139ac3ae4debcd0d7252"
IMAGE = "pieeprom-2026-05-17.bin"
PINS = (("rpi-eeprom-config", "rpi-eeprom-config", "39895792eb724afe5a4ed39e5798db844292efcca4317228aa790c580ddbb70f"),
        (IMAGE, "firmware-2711/default/" + IMAGE, "f1da1bda48c8f19d6eccd94f160a2c8da48fd0acdbfa47f0ac58353208d188c3"),
        ("recovery.bin", "firmware-2711/default/recovery.bin", "9ec8816886f3938d962a837347d65ccc1d03e811a84bf1ad4b608906b288d995"))
ORDERS = {"usb-first": "4", "sd-first": "1", "local": ""}
NET = re.compile(r"^(TFTP_IP|CLIENT_IP|SUBNET|GATEWAY)=")


def without_net(order):
    """BOOT_ORDER reads lowest nibble first; 2 is the network."""
    return "0x" + order[2:].replace("2", "") if order.startswith("0x") else "0x" + order.replace("2", "")


def first(order, nibble):
    """`0xf412 4 -> 0xf14`: an entry this does not name (6 is NVMe on a Pi 5) keeps its place."""
    body = order[2:] if order.startswith("0x") else order
    return "0x%s%s" % (body.replace("2", "").replace(nibble, ""), nibble)


def reorder(current, name):
    order = kv(current).get("BOOT_ORDER", "") or "0xf41"
    want = without_net(order) if name == "local" else first(order, ORDERS[name])
    lines = [l for l in current.splitlines() if not NET.match(l)]
    if any(l.startswith("BOOT_ORDER=") for l in lines):
        return "\n".join("BOOT_ORDER=" + want if l.startswith("BOOT_ORDER=") else l for l in lines)
    return "\n".join(lines + ["BOOT_ORDER=" + want])


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class BootOrder:
    def __init__(self, d, env=None, here=None, clock=None):
        self.d, self.env, self.here, self.clock = d, os.environ if env is None else env, here or Local(), clock or Clock()
        self.name, self.host = d.c("NODE_NAME"), d.c("NODE_SSH") or d.c("NODE_NAME")

    def eeprom(self, do, sudo=False, mutates=False, input=None, **params):
        return self.d.ch.call("r_sudo" if sudo else "r_ssh", self.d.ob("eeprom.sh", WK_DO=do, **params), input=input, mutates=mutates)

    def said(self, r):
        return r.out.replace("\r", "").rstrip("\n")

    def run(self, name):
        if name not in ORDERS:
            act.die("no such boot order '%s'.\n    orders:\n      usb-first      the USB stick first, then the SD card -- for a bench\n"
                    "                     system on the stick, with the card as the rescue role\n"
                    "      sd-first       the SD card first, then the USB stick -- for a bench\n"
                    "                     system on the card, with the stick as the rescue role\n"
                    "      local          the local disks only, network removed" % name)
        if self.d.probe() == "unreachable":
            act.die("cannot ssh to %s.\n    This writes the board's firmware configuration, so it needs the board\n    running and reachable -- "
                    "a Pi that cannot be reached at all has to be met\n    physically first, with a card written by 'wk sysimage write'."
                    % self.host)
        onboard = self.eeprom("has-config").ok
        if onboard:
            r = self.eeprom("read", sudo=True)
            if not r.ok:
                act.die("could not read %s's EEPROM configuration" % self.host)
            current = self.said(r)
        else:
            self.check_soc()
            current = self.read_vc()
        new = reorder(current, name)
        if current == new:
            act.info("%s's firmware already says this; nothing to write" % self.host)
            if not onboard:
                act.nothing_to_ask()
                self.clear_staged()
            return 0
        diff = difflib.unified_diff(current.splitlines(), new.splitlines(), lineterm="")
        act.log("%s's firmware configuration would change (boot order: %s):\n%s"
                % (self.host, name, "\n".join("  " + l for l in list(diff)[2:])))
        if act.dry_run():
            act.log("dry run -- the EEPROM was not written.")
            return 0
        self.d.armed_barrier("A boot-order write now races the reboot the arming is\n    waiting for: BOOT_ORDER is shared by both of "
                             "the board's roles, and a write\n    that lands mid-transition can leave the EEPROM aimed at the wrong one.")
        act.warn("this writes %s's EEPROM. BOOT_ORDER is shared by both of that\nmachine's roles, and a bad value needs physical access "
                 "to fix." % self.host)
        if not onboard:
            act.warn("%s has no rpi-eeprom-config, so this goes through recovery.bin --\nwhich replaces the whole bootloader image with "
                     "rpi-eeprom's pinned\n%s, carrying the configuration above onto it. The\nbootloader is upgraded as well as configured."
                     % (self.host, IMAGE))
        if not act.confirm("write this configuration to %s's EEPROM?" % self.host):
            act.die("not written")
        if onboard:
            if not self.eeprom("apply", sudo=True, mutates=True, input=new + "\n").ok:
                act.die("rpi-eeprom-config refused the configuration; nothing was written")
            act.info("%s's EEPROM updated; it takes effect on the next boot" % self.host)
            act.log("  verify with: ssh %s sudo rpi-eeprom-config" % self.host)
        else:
            self.stage_recovery(new)
            act.info("%s's EEPROM update is staged; the ROM applies it on the next boot" % self.host)
            act.log("  apply with:  ssh %s reboot\n  verify with: ssh %s vcgencmd bootloader_config\n"
                    "  the board goes through firmware twice, so allow a few minutes" % (self.host, self.host))
        act.log("  undo with:   wk boot %s --boot-order local" % self.name)
        return 0

    # -- the recovery path
    def check_soc(self):
        compat = self.said(self.eeprom("soc"))
        if "brcm,bcm2711" in compat:
            return
        if not compat:
            act.die("could not read %s's SoC from /proc/device-tree/compatible, so\n    there is no way to tell whether the pinned "
                    "bootloader image is the right\n    one for it. Refusing to flash an EEPROM on a guess." % self.host)
        act.die("%s is not a BCM2711 (Pi 4 / CM4 / Pi 400); it reports:\n%s\n    lib/wk/boot/eeprom.py pins the firmware-2711 "
                "bootloader only, and flashing it\n    to another SoC would brick the board."
                % (self.host, "\n".join("      " + l for l in compat.splitlines())))

    def read_vc(self):
        out = self.said(self.eeprom("vc-read", sudo=True))
        if out:
            return out
        if not self.eeprom("has-vc").ok:
            act.die("%s is running a system with no vcgencmd, so its EEPROM cannot be\n    read from here at all -- the buildroot bench "
                    "images carry no VideoCore\n    tools. Its *rescue* does: boot that and re-run.\n        wk boot %s --back\n"
                    "    If the firmware keeps landing on the bench medium instead (which is the\n    very thing a boot order change "
                    "fixes), take that medium out for one boot." % (self.name, self.name))
        act.die("could not read %s's bootloader configuration: vcgencmd is there but\n    'bootloader_config' returned nothing, so this "
                "user cannot reach /dev/vcio." % self.name)

    def bootfs(self):
        r = self.eeprom("bootfs")
        found = self.said(r)
        if not (r.ok and found):
            act.die("found no mounted FAT boot partition on %s holding start4.elf.\n    recovery.bin has to be staged where the firmware "
                    "reads it, and this cannot\n    tell where that is. Mount the board's boot partition and re-run." % self.host)
        return found

    def clear_staged(self):
        """recovery.bin renames itself and leaves pieeprom.upd behind, so an applied update looks identical to a pending one."""
        r = self.eeprom("bootfs")
        if r.ok and self.said(r):
            self.eeprom("clear", sudo=True, mutates=True, WK_DIR=self.said(r))

    def fetch(self):
        cache = os.path.join(Store(self.env).root(), "rpi-eeprom", COMMIT)
        self.here.mkdir(cache)
        for name, path, sha in PINS:
            f = os.path.join(cache, name)
            if os.path.isfile(f) and sha256(f) == sha:
                continue
            url = "https://raw.githubusercontent.com/raspberrypi/rpi-eeprom/%s/%s" % (COMMIT, path)
            act.info("fetching %s from rpi-eeprom@%s" % (name, COMMIT[:7]))
            if not self.here.act_run(["curl", "-fL", "--retry", "5", "-o", f, url]).ok:
                act.die("could not fetch %s" % url)
            if sha256(f) != sha:
                self.here.remove(f)
                act.die("checksum mismatch on %s (removed)\n    expected %s\n    Re-run; if it mismatches again the pin in "
                        "lib/wk/boot/eeprom.py\n    is stale." % (f, sha))
        return cache

    def board(self):
        """The answering system's Machine, the one the driver's Channel reaches it by."""
        m = self.d.ch.machine(self.d.ch.fn_of(self.d.ch.channel))
        if isinstance(m, Result):
            act.die("cannot reach %s to stage the EEPROM update: %s" % (self.host, m.err))
        return m

    def copy(self, board, files, bootfs):
        for f in files:
            try:
                board.copy_in(f, "%s/%s" % (bootfs, os.path.basename(f)))
            except OSError as e:
                act.die("could not copy %s to %s:%s: %s" % (os.path.basename(f), self.host, bootfs, e))

    def stage_recovery(self, config):
        """recovery.bin last: it is the trigger, so a copy cut short leaves a board that boots as it did."""
        cache, board = self.fetch(), self.board()
        work = os.path.join(Store(self.env).root(), "rpi-eeprom", "stage-" + self.name)
        self.here.remove(work)
        self.here.mkdir(work)
        try:
            self.here.write(os.path.join(work, "boot.conf"), config + "\n")
            upd = os.path.join(work, "pieeprom.upd")
            if not self.here.act_run(["python3", os.path.join(cache, "rpi-eeprom-config"), "--config", os.path.join(work, "boot.conf"),
                                      "--out", upd, os.path.join(cache, IMAGE)]).ok:
                act.die("rpi-eeprom-config could not apply that configuration to %s" % IMAGE)
            bootfs, upd_sha = self.bootfs(), sha256(upd)
            sig = "%s\nts: %d\ntarget-soc: 2711\n" % (upd_sha, int(self.clock.now()))
            act.info("staging the EEPROM update in %s on %s" % (bootfs, self.host))
            self.copy(board, [upd], bootfs)
            try:
                board.write(bootfs + "/pieeprom.sig", sig)
            except OSError as e:
                act.die("could not write pieeprom.sig to %s:%s: %s" % (self.host, bootfs, e))
            for f, sha in (("pieeprom.upd", upd_sha), ("pieeprom.sig", hashlib.sha256(sig.encode()).hexdigest())):
                there = self.said(self.eeprom("sum", WK_PATH="%s/%s" % (bootfs, f)))
                if there != sha:
                    act.die("%s did not survive the copy to %s\n    (%s here, %s there). Nothing has been armed: recovery.bin\n"
                            "    was not copied, so the board still boots as it did." % (f, self.host, sha, there))
            self.copy(board, [os.path.join(cache, "recovery.bin")], bootfs)
            self.eeprom("sync", mutates=True)
        finally:
            self.here.remove(work)
