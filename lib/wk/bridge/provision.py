"""`wk machine setup <bridge> --disk <machine>:<device>`: the bridge's system written to a named disk by
`wk sysimage write` and nothing else, the hands-on steps, and the wait for the phone; then wk.bridge.role."""

import os
import shlex

from wk import act, fleet, images
from wk.act import die, info, log
from wk.bridge import Unreachable
from wk.lock import Lock
from wk.store import Store
from wk.sysimage import pmos

PHONE_WAIT = 900       # the person's hands-on steps and a first boot
TICK = 5
DISCOVER_EVERY = 60    # a subnet sweep loads the link the phone is joining on
STEPS = """
the system is on %(disk)s. Now the part no script can do:
   1. power the phone OFF -- not reboot. A card wins a boot, so a service card
      left in it boots that card again rather than the system just written.
   2. take out any card that is not this system, and unplug the phone from %(machine)s.
      If %(disk)s is a card, it goes into the phone now.
   3. power the phone from a WALL CHARGER, never from the machine it exists to
      rescue: fed from that machine the out-of-band path dies exactly when it is
      needed, and on mains the battery is a UPS instead.
   4. kill switches: WiFi ON. %(kill)s
   5. plug in the USB-C Ethernet dock, powered BEFORE it meets the phone: the
      phone defaults to USB device mode, and only a powered dock swaps the data
      role, without which %(iface)s never appears.
   6. power the phone on.
"""
NEVER_ANSWERED = """%(name)s never answered in %(minutes)d minutes, on its conf name or a sweep of this machine's segments.
    In order of likelihood:
      - the WiFi kill switch is off, so the phone is up and on no network
      - a service card is still in it, so it booted that rather than the system
        just written: power it off, not reboot, with the card out
      - the image carries the wrong WiFi credential: it is copied from the build
        host's own connection when it is built
    The phone's own screen is the way in when none of that is it; 'wk help' has the
    console user and password. Nothing is lost: once it answers,
        wk machine setup %(name)s"""


def bridge_profile(name, env):
    for n in images.names(env):
        p = images.quiet_load(n, env)
        if p and p["IMG_BUILDER"] == "pmos" and p["PMO_BRIDGE"] == name:
            return n, p
    return None, None


def image_dir(store):
    return os.path.join(store.artifact_dir(), "bridge")


def image_lock(name):
    return "bridge-image-" + name


def rubble(store, machine, lock):
    from wk.rubble import du_kb, remover, row
    d = image_dir(store)
    if not machine.isdir(d):
        return []
    rows = []
    for n in machine.listdir(d):
        path, name = os.path.join(d, n), n[:-len(".img")] if n.endswith(".img") else n
        pid = lock.holder_pid(image_lock(name))
        held = pid is not None and machine.alive(pid)
        rows.append(row("bridge-image", "%s's image, copied for a write that did not finish" % name, du_kb(machine, path),
                        take=remover(machine, path), why="kept -- 'wk machine setup %s' (pid %d) is writing it" % (name, pid) if held else ""))
    return rows


def service_profile(device, env):
    """The fetch profile that boots this phone from a card and exports its internal storage (Jumpdrive)."""
    for n in images.names(env):
        p = images.quiet_load(n, env)
        if p and p["IMG_BUILDER"] == "fetch" and device and p["FET_DEVICE"] == device:
            return n
    return None


class Write:
    def __init__(self, role):
        self.r = role
        self.env = dict(role.env, WK_ROOT=role.root)
        self.wk = os.path.join(role.root, "wk")

    def child(self, *args):
        """A wk command with this terminal, so its own progress and refusals reach the person as they happen."""
        argv = [self.wk] + list(args)
        if act.dry_run():
            log("would run: %s" % " ".join(shlex.quote(a) for a in argv))
            return True
        return self.r.here.run_tty(argv).ok

    def image(self, name, profile, p, rebuild):
        host = pmos.ssh_machine(fleet.Fleet(self.r.root, self.env), self.env, self.r.here, pmos.host_for(p, self.env))
        found = None if rebuild else pmos.newest_out(host, self.env, profile)
        if found:
            info("using %s from %s (--rebuild builds a fresh one)" % (found, host.name))
        else:
            info("building %s on %s" % (profile, host.name) + (" (--rebuild)" if rebuild else ", which has no finished one"))
            if not self.child("sysimage", "build", profile):
                die("the %s build failed -- nothing was written to any disk" % profile)
            if act.dry_run():
                return "<the new %s build>" % profile
            found = pmos.newest_out(host, self.env, profile)
            if not found:
                die("the %s build reported success and left no finished build on %s" % (profile, host.name))
        if act.dry_run():
            return "<%s, copied off %s>" % (found, host.name)
        path = os.path.join(image_dir(Store(self.env)), name + ".img")
        self.r.here.mkdir(os.path.dirname(path))
        try:
            return pmos.fetch_out(host, self.env, found, path)
        except BaseException:
            self.r.here.remove(path)
            raise

    def run(self, bc, kill, disk, image, rebuild):
        machine, _, dev = disk.partition(":")
        if not (machine and dev):
            die("--disk takes <machine>:<device>, e.g. rpi5:/dev/sda -- 'wk sysimage disks <machine>' lists them")
        profile, p = bridge_profile(bc.name, self.env)
        if not (image or profile):
            die("no image profile builds %s.\n"
                "    A pmos profile in image/configs claims a bridge by setting PMO_BRIDGE to its\n"
                "    name, and none names this one: add one, or pass --image <path>." % bc.name)
        service = service_profile(p["PMO_DEVICE"], self.env) if p else None
        log("  write:    %s to %s" % (image or "the newest %s build" % profile, disk))
        if service:
            log("            the phone's internal storage as %s exports it from a card\n"
                "            ('wk sysimage build %s', then 'wk sysimage write' it to a card), or a card" % (service, service))
        if not act.confirm("erase %s and write %s's system to it?" % (disk, bc.name)):
            die("aborted -- nothing was written")
        if image:
            self.write(image, disk)
        else:
            with Lock(Store(self.env), self.r.here, self.r.clock).held(image_lock(bc.name), timeout=0):
                fetched = self.image(bc.name, profile, p, rebuild)
                try:
                    self.write(fetched, disk)
                finally:
                    if not act.dry_run():
                        self.r.here.remove(fetched)
        if act.dry_run():
            log("would wait up to %d minutes for %s to answer, then apply the role" % (PHONE_WAIT // 60, bc.name))
            return False
        log(STEPS % {"disk": disk, "machine": machine, "kill": kill, "iface": bc.iface})
        return True

    def write(self, image, disk):
        if not self.child("sysimage", "write", "--from", image, "--disk", disk, "--yes"):
            die("%s was not written -- nothing on the phone has changed" % disk)

    def wait(self, bc, at):
        info("waiting up to %d minutes for %s to answer" % (PHONE_WAIT // 60, bc.name))
        clock, start, swept = self.r.clock, self.r.clock.monotonic(), None
        while clock.monotonic() - start < PHONE_WAIT:
            sweep = swept is None or clock.monotonic() - swept >= DISCOVER_EVERY
            if sweep:
                swept = clock.monotonic()
            try:
                return self.r.b.resolve(bc.name, at=at, names_only=not sweep)
            except Unreachable:
                clock.sleep(TICK)
        die(NEVER_ANSWERED % {"name": bc.name, "minutes": PHONE_WAIT // 60})
