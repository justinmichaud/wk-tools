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
from wk.slot import sha256_file
from wk.store import Store

COMMIT = "86759b04b22173e10186139ac3ae4debcd0d7252"
IMAGE = "pieeprom-2026-05-17.bin"
PINS = (("rpi-eeprom-config", "rpi-eeprom-config", "39895792eb724afe5a4ed39e5798db844292efcca4317228aa790c580ddbb70f"),
        (IMAGE, "firmware-2711/default/" + IMAGE, "f1da1bda48c8f19d6eccd94f160a2c8da48fd0acdbfa47f0ac58353208d188c3"),
        ("recovery.bin", "firmware-2711/default/recovery.bin", "9ec8816886f3938d962a837347d65ccc1d03e811a84bf1ad4b608906b288d995"))
ORDERS = {"usb-first": "4", "sd-first": "1", "local": ""}
NET = re.compile(r"^(TFTP_IP|CLIENT_IP|SUBNET|GATEWAY)=")


def first(order, nibble):
    """BOOT_ORDER reads lowest nibble first and 2 is the network: `0xf412 4 -> 0xf14`; "" only drops the network."""
    body = order[2:] if order.startswith("0x") else order
    return "0x%s%s" % (body.replace("2", "").replace(nibble, "") if nibble else body.replace("2", ""), nibble)


def reorder(current, name):
    want = first(kv(current).get("BOOT_ORDER", "") or "0xf41", ORDERS[name])
    lines = [l for l in current.splitlines() if not NET.match(l)]
    if any(l.startswith("BOOT_ORDER=") for l in lines):
        return "\n".join("BOOT_ORDER=" + want if l.startswith("BOOT_ORDER=") else l for l in lines)
    return "\n".join(lines + ["BOOT_ORDER=" + want])


class BootOrder:
    def __init__(self, d, env=None, here=None, clock=None):
        self.d, self.env, self.here, self.clock = d, os.environ if env is None else env, here or Local(), clock or Clock()
        self.name, self.host = d.c("name"), d.c("ssh") or d.c("name")

    def eeprom(self, do, sudo=False, mutates=False, input=None, **params):
        return self.d.ch.call("r_sudo" if sudo else "r_ssh", self.d.ob("eeprom.sh", WK_DO=do, **params), input=input, mutates=mutates)

    def said(self, r):
        return r.out.replace("\r", "").rstrip("\n")

    def run(self, name):
        if name not in ORDERS:
            act.die("no such boot order '%s': usb-first, sd-first or local (the local disks, no network)" % name)
        if self.d.probe() == "unreachable":
            act.die("cannot ssh to %s, and writing its firmware configuration needs it running and reachable." % self.host)
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
        self.d.armed_barrier("A boot-order write now races the reboot the arming is waiting for.")
        act.warn("this writes %s's EEPROM, which both of its roles share; a bad value needs physical access to fix." % self.host)
        if not onboard:
            act.warn("%s has no rpi-eeprom-config, so recovery.bin replaces the whole bootloader with rpi-eeprom's pinned %s,\n"
                     "carrying the configuration above." % (self.host, IMAGE))
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
        act.die("%s is not a BCM2711 (Pi 4 / CM4 / Pi 400), and the pinned bootloader is firmware-2711's only; "
                "/proc/device-tree/compatible says:\n%s" % (self.host, compat or "(nothing)"))

    def read_vc(self):
        out = self.said(self.eeprom("vc-read", sudo=True))
        if out:
            return out
        if not self.eeprom("has-vc").ok:
            act.die("%s is running a system with no vcgencmd, so its EEPROM cannot be read; its rescue can:  wk boot %s --back"
                    % (self.name, self.name))
        act.die("could not read %s's bootloader configuration: 'vcgencmd bootloader_config' returned nothing." % self.name)

    def bootfs(self):
        r = self.eeprom("bootfs")
        found = self.said(r)
        if not (r.ok and found):
            act.die("found no mounted FAT boot partition on %s holding start4.elf; mount it and re-run." % self.host)
        return found

    def clear_staged(self):
        # recovery.bin renames itself and leaves pieeprom.upd behind, so an applied update looks like a pending one.
        r = self.eeprom("bootfs")
        if r.ok and self.said(r):
            self.eeprom("clear", sudo=True, mutates=True, WK_DIR=self.said(r))

    def fetch(self):
        cache = os.path.join(Store(self.env).store_dir(), "rpi-eeprom", COMMIT)
        self.here.mkdir(cache)
        for name, path, sha in PINS:
            f = os.path.join(cache, name)
            if os.path.isfile(f) and sha256_file(f) == sha:
                continue
            url = "https://raw.githubusercontent.com/raspberrypi/rpi-eeprom/%s/%s" % (COMMIT, path)
            act.info("fetching %s from rpi-eeprom@%s" % (name, COMMIT[:7]))
            if not self.here.act_run(["curl", "-fL", "--retry", "5", "-o", f, url]).ok:
                act.die("could not fetch %s" % url)
            if sha256_file(f) != sha:
                self.here.remove(f)
                act.die("checksum mismatch on %s (removed), expected %s; a second mismatch is a stale pin" % (f, sha))
        return cache

    def board(self):
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
        # recovery.bin last: it is the trigger, so a copy cut short leaves a board that boots as it did.
        cache, board = self.fetch(), self.board()
        work = os.path.join(Store(self.env).store_dir(), "rpi-eeprom", "stage-" + self.name)
        self.here.remove(work)
        self.here.mkdir(work)
        try:
            self.here.write(os.path.join(work, "boot.conf"), config + "\n")
            upd = os.path.join(work, "pieeprom.upd")
            if not self.here.act_run(["python3", os.path.join(cache, "rpi-eeprom-config"), "--config", os.path.join(work, "boot.conf"),
                                      "--out", upd, os.path.join(cache, IMAGE)]).ok:
                act.die("rpi-eeprom-config could not apply that configuration to %s" % IMAGE)
            bootfs, upd_sha = self.bootfs(), sha256_file(upd)
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
                    act.die("%s did not survive the copy to %s (%s here, %s there); recovery.bin was not copied."
                            % (f, self.host, sha, there))
            self.copy(board, [os.path.join(cache, "recovery.bin")], bootfs)
            self.eeprom("sync", mutates=True)
        finally:
            self.here.remove(work)
