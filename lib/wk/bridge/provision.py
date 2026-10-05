"""`wk machine setup <bridge> --disk <machine>:<device>`: the bridge's system written by `wk sysimage write`,
the hands-on steps, and the wait for the phone; then wk.bridge.role."""

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
   1. power the phone OFF, not reboot: a service card left in it wins the boot.
   2. take out any card that is not this system, and unplug the phone from %(machine)s.
      If %(disk)s is a card, it goes into the phone now.
   3. power the phone from a WALL CHARGER, never from the machine it rescues.
   4. kill switches: WiFi ON. %(kill)s
   5. plug in the USB-C Ethernet dock, powered BEFORE it meets the phone: only a
      powered dock swaps the data role, without which %(iface)s never appears.
   6. power the phone on.
"""
NEVER_ANSWERED = """%(name)s never answered in %(minutes)d minutes, on its conf name or a sweep of this machine's segments.
    Most likely: the WiFi kill switch is off; a service card is still in it (power off, card out);
    or the image carries the wrong WiFi credential (copied from the build host's connection).
    The phone's own screen is the way in otherwise ('wk help' has the console login). Once it answers:
        wk machine setup %(name)s"""


def preset_where(env, builder, key, value):
    for n in images.names(env):
        p = images.quiet_load(n, env)
        if p and value and p["IMG_BUILDER"] == builder and p[key] == value:
            return n, p
    return None, None


def bridge_preset(name, env):
    return preset_where(env, "pmos", "PMO_BRIDGE", name)


def image_dir(store):
    return os.path.join(store.cache_dir(), "bridge")


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


class Write:
    def __init__(self, role):
        self.r = role
        self.env = dict(role.env, WK_ROOT=role.root)
        self.wk = os.path.join(role.root, "wk")

    def child(self, *args):
        """A wk command on this terminal, so its progress and refusals reach the person."""
        argv = [self.wk] + list(args)
        if act.dry_run():
            log("would run: %s" % shlex.join(argv))
            return True
        return self.r.here.run_tty(argv).ok

    def image(self, name, preset, p, rebuild):
        host = pmos.ssh_machine(fleet.Fleet(self.r.root, self.env), self.env, self.r.here, pmos.host_for(p, self.env))
        found = None if rebuild else pmos.newest_out(host, self.env, preset)
        if found:
            info("using %s from %s (--rebuild builds a fresh one)" % (found, host.name))
        else:
            info("building %s on %s" % (preset, host.name) + (" (--rebuild)" if rebuild else ", which has no finished one"))
            if not self.child("sysimage", "build", preset):
                die("the %s build failed -- nothing was written to any disk" % preset)
            if act.dry_run():
                return "<the new %s build>" % preset
            found = pmos.newest_out(host, self.env, preset)
            if not found:
                die("the %s build reported success and left no finished build on %s" % (preset, host.name))
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
        preset, p = bridge_preset(bc.name, self.env)
        if not (image or preset):
            die("no image preset builds %s.\n"
                "    A pmos image preset in image/presets claims a bridge by setting PMO_BRIDGE to its\n"
                "    name, and none names this one: add one, or pass --image <path>." % bc.name)
        # The fetch image preset that boots this phone from a card and exports its internal storage (Jumpdrive).
        service = preset_where(self.env, "fetch", "FET_DEVICE", p["PMO_DEVICE"])[0] if p else None
        log("  write:    %s to %s" % (image or "the newest %s build" % preset, disk))
        if service:
            log("            the phone's internal storage as %s exports it from a card\n"
                "            ('wk sysimage build %s', then 'wk sysimage write' it to a card), or a card" % (service, service))
        if not act.confirm("erase %s and write %s's system to it?" % (disk, bc.name)):
            die("aborted -- nothing was written")
        if image:
            self.write(image, disk)
        else:
            with Lock(Store(self.env), self.r.here, self.r.clock).held(image_lock(bc.name), timeout=0):
                fetched = self.image(bc.name, preset, p, rebuild)
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
