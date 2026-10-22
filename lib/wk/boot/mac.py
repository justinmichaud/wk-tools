"""The Mac drivers: a second macOS install on another volume of the same Mac (mac-volume), and a Tart guest standing in
for one (mac-guest). A Mac is reached over ssh and nothing else, so its on-board files take any string, quoted."""

import os
import re
import time

from wk import act, guest, images
from wk.boot.driver import Channel, Driver, Onboard
from wk.machine import Result, is_macos
from wk.sudo import BOOT_PRIV

HELPER = BOOT_PRIV
BOOTTIME = re.compile(r"\{ *sec *= *(\d+)")   # `{ sec = 1786800736, usec = 451078 }`: anchored on the brace, or usec matches


class Script(Onboard):
    quote = True


class MacDriver(Driver):
    measures = True

    @staticmethod
    def channel(root, conf, env=None, via=None):
        return Channel(root, conf, env=env, via=via, board=False)

    def ob(self, name, **params):
        return Script(self.root, name, **params)

    def who(self):
        return self.c("name")

    def watchdog(self):
        return ""

    def boottime(self):
        m = BOOTTIME.search(self.run(["sysctl", "-n", "kern.boottime"]).out)
        return m.group(1) if m else ""

    def boot_id(self):
        return self.boottime()

    def booted_at(self):
        sec = self.boottime()
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(sec))) if sec else ""

    def marker(self, side=None, path=images.MARKER):
        """"" for no marker, None when nothing answered."""
        r = self.ch.run(["cat", path], side)
        if r.rc == 255:
            return None
        ids = re.findall(r"(?m)^id=(\S+)", r.out)
        return ids[0] if ids else ""

class MacVolume(MacDriver):
    """Only the boot helper blesses the install the firmware boots next; --setBoot is sticky, so the bench task hands back."""

    name = "mac-volume"
    sticky = True

    record_at = "${XDG_STATE_HOME:-$HOME/.local/state}/wk/boot-armed"

    def volume(self):
        return "/Volumes/" + self.c("volume")

    def bench_name(self):
        return self.ch.bench_name() or "its benchmark install"

    def test(self, flag, path):
        return self.ch.run(["test", flag, path], "host").ok

    def host_answers(self):
        return self.ch.run(["true"], "host").ok

    def volume_present(self):
        return self.test("-d", self.volume() + "/System/Library/CoreServices")

    def data_path(self):
        d = self.volume() + " - Data"
        return d if self.test("-d", d) else self.volume()

    def fact(self, what, arg=""):
        with open(os.path.join(self.root, "lib", "wk", "mac.py")) as f:
            r = self.run(["python3", "-", what] + ([arg] if arg else []), input=f.read())
        return r.out.replace("\r", "").strip() if r.ok else ""

    def firmware_default(self):
        bv = self.fact("boot-volume")
        if not bv:
            return "unknown (the firmware publishes no boot-volume)"
        grp = bv.rsplit(":", 1)[-1]
        on_bench = self.ch.side == "bench"
        host = self.fact("volume-group", "/") if self.ch.side == "host" else ""
        bench = self.fact("volume-group", "/") if on_bench else self.fact("volume-group", self.volume()) if self.volume_present() else ""
        if bench and grp == bench:
            return "%s ('%s' -- a plain reboot is expected to enter bench mode)" % (grp, self.c("volume"))
        if host and grp == host:
            return "%s (the host install -- a plain reboot stays in host mode)" % grp
        if on_bench:
            return "%s (not '%s', so a plain reboot leaves bench mode)" % (grp, self.c("volume"))
        return "%s (matches neither install on this disk)" % grp

    def evidence(self):
        vol = self.c("volume")
        if self.ch.side == "bench":
            booted, where, fw = self.fact("volume-name", "/"), "%s (this install's own /)" % vol, self.firmware_default()
        elif self.host_answers():
            booted, fw = self.fact("volume-name", "/"), self.firmware_default()
            where = "%s (attached at %s)" % (vol, self.volume()) if self.volume_present() else "%s (not attached)" % vol
        else:
            booted = "unknown (neither %s nor %s answers)" % (self.c("ssh"), self.bench_name())
            where, fw = vol, "unknown (nvram answers only from a running install)"
        return "\n".join(["booted_volume=" + (booted or "unknown (diskutil would not name it)"), "benchmark_volume=" + where,
                          "firmware_default=" + fw, "bench_display=%s (the install that is measured)" % (self.display() or "unpinned")])

    def media(self):
        what = "bench volume '%s'" % self.c("volume")
        if self.ch.side == "bench":
            return "%s: %s is running from it" % (what, self.bench_name())
        if self.volume_present():
            return "%s attached at %s" % (what, self.volume())
        if self.host_answers():
            return "%s MISSING on %s -- see 'wk help hardware'" % (what, self.c("ssh"))
        return "%s: neither %s nor %s answers" % (what, self.c("ssh"), self.bench_name())

    def reprovision(self):
        build = "wk sysimage build %s" % self.c("image_preset")
        return "%s --create\n%s --install\n%s --provision\nwk boot %s" % (build, build, build, self.who())

    def systems(self):
        if self.ch.side == "bench":
            ident = self.marker("bench")
            return None if ident is None else [("/", ident)] if ident else []
        if not self.volume_present():
            return None if not self.host_answers() else []
        ident = self.marker("host", self.volume() + images.MARKER)
        return None if ident is None else [(self.volume(), ident)] if ident else []

    def choose(self, want):
        image = want or self.c("volume")
        if not image:
            act.die("%s has no benchmark volume configured (volume)" % self.who())
        return self.volume(), image

    def diag(self):
        if not self.volume_present():
            act.die("'%s' is not attached to %s, so there is nothing to read." % (self.c("volume"), self.who()))
        out = self.ch.run(["cat", self.volume() + "/var/log/wk-diag.txt"], "host").out
        return out.rstrip("\n") or "(no var/log/wk-diag.txt on '%s')" % self.c("volume")

    def priv(self, verb):
        r = self.ch.priv(verb, side="host", mutates=verb != "status")
        return r.ok, (r.out + r.err).replace("\r", "").strip()

    def arm_sequence(self, p, image):
        vol = self.c("volume")
        attached = "attached" if self.volume_present() else "NOT attached -- arming would refuse"
        return [("check        '%s' is a macOS install (%s) and the boot helper is installed" % (vol, attached), self.armable_here),
                ("prove        the firmware takes this install back (now: %s)" % self.firmware_default(), self.prove_return),
                ("bless        '%s', and read the firmware's default back" % vol, self.bless),
                self.record_step(image), self.reboot_step(image)]

    def armable_here(self):
        name, vol = self.who(), self.c("volume")
        if not self.volume_present():
            act.die("'%s' is not attached to %s, or is not a macOS system volume.\n"
                    "    It has to be a full macOS install on another volume of this Mac ('wk help hardware').\n"
                    "    A different name:  WK_BENCH_VOLUME='...' wk boot %s" % (vol, name, name))
        if not self.test("-x", HELPER):
            act.die("the privileged boot helper is not installed on %s:  ./setup --stage quiesce\n"
                    "    Without it: shut down, hold the power button, pick '%s'." % (name, vol))

    def prove_return(self):
        # The return first: Apple Silicon has no one-shot form, so an unproven way back boots bench mode forever.
        name, vol = self.who(), self.c("volume")
        ok, said = self.priv("boot-host")
        if not ok:
            act.die("%s cannot be told to boot itself again, so it is not told to boot '%s'. What it answered:\n%s"
                    % (name, vol, said or "(nothing -- %s did not answer)" % self.c("ssh")))
        act.log(said)
        back = self.firmware_default()
        if "the host install" not in back:
            act.die("bless blessed %s's running install and the firmware names: %s\n    Nothing was armed." % (name, back))

    def bless(self):
        name, vol = self.who(), self.c("volume")
        ok, said = self.priv("boot-volume")
        if not ok:
            act.die("%s's firmware would not take '%s', and nothing was changed. What it answered:\n%s"
                    % (name, vol, said or "(nothing -- %s did not answer)" % self.c("ssh")))
        act.log(said)
        now = self.firmware_default()
        if "'%s'" % vol not in now:
            act.die("bless reported success and %s's firmware still names: %s\n    Nothing was rebooted." % (name, now))
        act.info("the firmware will boot '%s' next" % vol)

    def disarm(self, spent=False):
        ok, said = self.priv("boot-host")
        if not ok:
            act.die("%s's firmware still names the benchmark volume, and this could not set it back:\n%s\n"
                    "    The record stays. The remedy:  wk machine setup %s, then wk boot %s --disarm"
                    % (self.who(), said or "  (nothing -- %s did not answer)" % self.c("ssh"), self.who(), self.who()))
        return 0

    def disarm_note(self):
        return "  the firmware is set back to this install; a plain reboot stays here."

    def reboot(self, armed=False):
        if self.ch.side == "bench":
            act.die("%s answers as its benchmark install, which carries no boot helper; its job hands it back.\n"
                    "    Read it meanwhile:  wk bench ab --devices %s --status" % (self.who(), self.who()))
        if self.priv("reboot")[0]:
            return 0
        act.die("could not restart %s: its boot helper is not installed:  wk machine setup %s" % (self.who(), self.who()))

class MacGuest(MacDriver):
    """A guest rehearses the path and not the number: arming it is starting it, and leaving the role is stopping it.
    Its one side is the vm place's own channel into it: its address changes every boot and is in no ssh config."""

    name = "mac-guest"
    measures = False

    @staticmethod
    def channel(root, conf, env=None, via=None):
        return Channel(root, conf, env=env, via=via, board=False, sides={})

    def __init__(self, root, conf, ch, vm=None):
        super().__init__(root, conf, ch)
        self._vm = vm
        if ch is not None and "host" not in ch.sides:
            ch.sides["host"] = self.machine

    def guest(self):
        return (self.ch.env if self.ch else os.environ).get("WK_BENCH_GUEST") or "wk-bench"

    def vm(self):
        if self._vm is None:
            from wk.places import Registry
            self._vm = Registry(self.root, self.ch.env, machine=self.ch.via).load("vm")
        return self._vm

    def machine(self):
        return self.vm().guest(self.guest()) or Result(255, "", "'%s' is not running" % self.guest())

    def state(self):
        return self.vm().vm_state(self.guest())

    def probeable(self):
        return is_macos() and bool(self.vm().tart())

    def display(self):
        return "external " + guest.display(self.vm().env)

    def systems(self):
        return [] if self.state() == "absent" else [(self.guest(), self.guest())]

    def armable(self, mode):
        pass

    def choose(self, want):
        return self.guest(), want or self.guest()

    def arm_sequence(self, p, image):
        return [("start        guest %s (%s) and wait for its marker" % (self.guest(), self.state() or "unknown"), self.arm)]

    def arm(self):
        g, name = self.guest(), self.who()
        st = self.state()
        if st == "absent":
            act.die("%s has no guest '%s'.\n    Make one from the golden base and mark it as a benchmark install:\n"
                    "        wk new %s --on vm && wk start %s\n        then, in it:  sudo tee /etc/wk-image <<<'id=%s'"
                    % (name, g, g, g, self.c("image_preset") or "perf-macos-benchvm"))
        if st != "running":
            act.info("starting guest '%s'" % g)
            if not act.dry_run() and not self.vm().start(g):
                act.die("could not start guest '%s'" % g)
        if not self.marker("host"):
            act.die("'%s' is running but carries no /etc/wk-image, so it is not %s's benchmark install; mark it first." % (g, name))
        act.info("%s is in bench mode: guest '%s' is running and carries its marker" % (name, g))
        act.log("  'wk boot %s --back' stops it." % name)

    def reboot(self, armed=False):
        if armed:
            return 0
        self.vm().stop(self.guest())
        act.info("stopped '%s' -- for a guest, leaving the role is leaving the machine" % self.guest())
        return 0

    def diag(self):
        return self.ch.run(["cat", "/var/log/wk-diag.txt"], "host").out.rstrip("\n") or "(no diag on the guest)"

    def evidence(self):
        lines = ["guest=%s (%s)" % (self.guest(), self.state() or "unknown"),
                 "measurement=refused (a rehearsal proves the path, not the number)"]
        ident = self.marker("host")
        return "\n".join(lines + ([] if ident is None else ["marker=" + ident]))

    def media(self):
        if not self.probeable():
            return "a Tart guest, %s (managed on the macOS host)" % self.guest()
        return "a Tart guest, %s (%s); no physical media" % (self.guest(), self.state() or "unknown")

    def reprovision(self):
        return "wk sysimage build macos-guest-base\nwk new %s --on vm\nwk bench deploy <ws> %s" % (self.who(), self.who())

DRIVERS = {d.name: d for d in (MacVolume, MacGuest)}
