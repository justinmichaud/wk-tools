"""The Mac drivers: a second macOS install on another volume of the same Mac (mac-volume), and a Tart guest standing in
for one (mac-guest). A Mac is reached over ssh and nothing else, so its on-board files take any string, quoted."""

import glob
import os
import re
import shlex
import tempfile
import time

from wk import act, guest, reach, record as wkrecord
from wk.boot.driver import Driver, Onboard
from wk.machine import Local, Result, Ssh, is_macos
from wk.store import Store

HELPER = "/usr/local/libexec/wk-boot-priv"
RECORD_SHOWN = "${XDG_STATE_HOME:-$HOME/.local/state}/wk/boot-armed"
BENCH_ROOT = "/var/wk"
TOOLS = "Development/wk-tools"
BOOTTIME = re.compile(r"\{ *sec *= *(\d+)")   # `{ sec = 1786800736, usec = 451078 }`: anchored on the brace, or usec matches


class Script(Onboard):
    """A boot/onboard/mac-* file led by its parameters, each shell-quoted, and by `lead`'s files verbatim."""

    where = os.path.join("boot", "onboard")

    def __init__(self, root, name, lead=(), **params):
        self.root, self.name, self.params, self.lead = str(root), name, params, tuple(lead)

    def text(self):
        body = []
        for name in self.lead + (self.name,):
            with open(os.path.join(self.root, self.where, name)) as f:
                body.append(f.read().rstrip("\n"))
        return "".join("%s=%s; " % (k, shlex.quote(str(v))) for k, v in sorted(self.params.items())) + "\n".join(body)


def run_script(m, ob, input=None, mutates=False):
    return (m.act_run if mutates else m.run)(["sh", "-c", ob.text()], input=input)


class Channel:
    """The Mac's two installs as two ssh destinations: host mode on NODE_SSH, bench mode on its own tailnet node."""

    def __init__(self, conf, env=None, channel="none", via=None, root=None):
        self.conf, self.env, self.channel = conf, os.environ if env is None else env, channel
        self.via = via or Local()
        self.root = str(root or os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
        self._here = self._peers = None

    def offline(self, dest):
        """Why the tailnet already reports `dest` down, or "": ssh to such a node spends its whole ConnectTimeout learning it."""
        if self._peers is None:
            self._peers = reach.Reach(self.via, self.env)
        return self._peers.offline(dest)

    def dest(self, fn):
        if fn == "m_ssh":
            return self.conf.get("NODE_SSH", "")
        return self.env.get("WK_MAC_BENCH_SSH") or self.conf.get("NODE_BENCH_SSH", "")

    def machine(self, fn="m_ssh"):
        dest = self.dest(fn)
        if not dest:
            return None
        if fn == "m_ssh":
            if self._here is None:
                self._here = wkrecord.host_name(self.via) == dest.lower()
            if self._here:
                return self.via
        return Ssh(dest, timeout=int(self.env.get("WK_SSH_TIMEOUT") or 10), via=self.via)

    def call(self, fn, *args, input=None, mutates=False):
        if fn == "r_ssh":
            fn = {"host": "m_ssh", "bench": "i_ssh"}.get(self.channel, "")
        m = self.machine(fn) if fn in ("m_ssh", "i_ssh") else None
        if m is None:
            return Result(255, "", "%s: no ssh destination on this channel" % (fn or self.channel))
        why = "" if m is self.via else self.offline(m.dest)
        if why:
            act.warn(why)
            return Result(255, "", why)
        return run_script(m, args[0], input=input, mutates=mutates)

    def here(self):
        return self.machine() is self.via

    def push(self, src, dest):
        self.machine().copy_in(src, dest)

    def exec_argv(self, cmd):
        m = self.machine()
        return ["bash", "-c", cmd] if m is self.via else ["ssh", *m.opts, m.dest, cmd]


class GuestChannel:
    """A guest through the vm target: its address changes every boot and is in no ssh config."""

    def __init__(self, root, conf, env=None, channel="none", vm=None, via=None):
        self.root, self.conf, self.env, self.channel = str(root), conf, os.environ if env is None else env, channel
        self.ws = self.env.get("WK_BENCH_GUEST") or "wk-bench"
        self._vm, self.via = vm, via

    def vm(self):
        if self._vm is None:
            from wk.targets import Registry
            self._vm = Registry(self.root, self.env, machine=self.via).load("vm")
        return self._vm

    def call(self, fn, *args, input=None, mutates=False):
        if fn not in ("m_ssh", "r_ssh"):
            return Result(255, "", "a guest is reached one way, through the vm target")
        if mutates and act.dry_run():
            act.log("would run in guest %s: %s" % (self.ws, args[0].name))
            return Result(0)
        return self.vm().exec(self.ws, ["sh", "-c", args[0].text()])

    def state(self):
        return self.vm().vm_state(self.ws)

    def start(self):
        if act.dry_run():
            act.log("would start guest %s" % self.ws)
        elif not self.vm().start(self.ws):
            act.die("could not start guest '%s'" % self.ws)

    def stop(self):
        self.vm().stop(self.ws)

    def push(self, src, dest):
        self.vm().push(self.ws, src, dest)

    def exec_argv(self, cmd):
        return self.vm().exec_argv(self.ws, ["sh", "-c", cmd])[0]

    def display(self):
        return guest.display(self.vm().env)

    def probeable(self):
        return is_macos() and bool(self.vm().tart())


class MacDriver(Driver):
    measures = True
    bench_channel = "bench"
    shims = ()

    def ob(self, name, lead=(), **params):
        return Script(self.root, name, lead=lead, **params)

    def who(self):
        return self.c("NODE_NAME")

    def facts(self):
        return dict(super().facts(), B_MEASURES="yes" if self.measures else "no")

    def boottime(self):
        m = BOOTTIME.search(self.run("mac-boottime.sh").out)
        return m.group(1) if m else ""

    def boot_id(self):
        return self.boottime()

    def booted_at(self):
        sec = self.boottime()
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(sec))) if sec else ""

    def marker(self, fn, path="/etc/wk-image"):
        """The id= of a wk-image marker, "" for none, None when nothing answered."""
        r = self.ch.call(fn, self.ob("mac-read.sh", WK_PATH=path))
        if not r.ok:
            return None
        ids = re.findall(r"(?m)^id=(\S+)", r.out)
        return ids[0] if ids else ""

    def check_measurement(self):
        if not self.measures:
            act.die("%s is a rehearsal: %s proves every phase of the path, and a reading taken on it\n"
                    "    is not a measurement of any machine." % (self.who(), self.c("NODE_NOTE") or "it"))

    def refuse_on_bench(self):
        """The arms and the tools are on the host install; the staging root read in bench mode is the running install's."""
        if self.ch.channel == "bench":
            act.die("%s answers as its benchmark install, and nothing is staged onto a\n"
                    "    running measurement. The arms and the tools are on the host install, so stage\n"
                    "    once it is back:  wk boot %s --status   says which is up." % (self.who(), self.who()))

    def bench_put(self, src, dest, *skip):
        """Replaces dest with src's tree less the `skip` names: a tar, since the Mac's openrsync fails on the escaped
        `/Volumes/<v> - Data/...`."""
        self.refuse_on_bench()
        self.own(dest)
        fd, tar = tempfile.mkstemp(prefix="wk-put-", suffix=".tar")
        os.close(fd)
        try:
            excludes = [w for x in skip for w in ("--exclude", x)]
            if not Local().act_run(["tar", "-cf", tar, *excludes, "-C", src, "."]).ok:
                act.die("could not pack %s" % src)
            return self.land(tar, dest, "tree")
        finally:
            os.unlink(tar)

    def bench_put_file(self, src, dest):
        self.refuse_on_bench()
        self.own(os.path.dirname(dest))
        return self.land(src, dest, "file")

    def land(self, src, dest, do):
        tmp = "/tmp/wk-put-%d-%s" % (os.getpid(), os.path.basename(src))
        try:
            self.ch.push(src, tmp)
        except OSError as e:
            act.warn("could not copy %s to %s: %s" % (src, self.who(), e))
            return 1
        return self.ch.call("m_ssh", self.ob("mac-put.sh", WK_DO=do, WK_TMP=tmp, WK_DEST=dest), mutates=True).rc

    def own(self, dest):
        return 0


class MacVolume(MacDriver):
    """Only the boot helper blesses the install the firmware boots next; --setBoot is sticky, so the bench job hands back."""

    name = "mac-volume"
    arming = "command"
    disarms = True
    shims = ("b_disarm", "b_disarm_note")

    @staticmethod
    def transport(root, conf, channel="none", env=None, via=None):
        return Channel(conf, env=env, channel=channel, via=via, root=root)

    def facts(self):
        return dict(super().facts(), NODE_RECORD=RECORD_SHOWN, BOOT_HELPER=HELPER)

    def volume(self):
        return "/Volumes/" + self.c("NODE_VOLUME")

    def bench_name(self):
        return self.ch.dest("i_ssh") or "its benchmark install"

    def probe(self):
        r = self.ch.call("m_ssh", self.ob("mac-probe.sh"))
        if r.ok:
            self.ch.channel = "host"
            first = (r.out.replace("\r", "").splitlines() or ["READY"])[0]
            self.mode = "host" if first == "READY" else "bench " + first
            return self.mode
        if self.ch.dest("i_ssh"):
            r = self.ch.call("i_ssh", self.ob("mac-probe.sh"))
            first = (r.out.replace("\r", "").splitlines() or ["READY"])[0] if r.ok else "READY"
            if first != "READY":
                self.ch.channel, self.mode = self.bench_channel, "bench " + first
                return self.mode
        self.ch.channel, self.mode = "none", "unreachable"
        return self.mode

    def test(self, flag, path):
        return self.ch.call("m_ssh", self.ob("mac-test.sh", WK_TEST=flag, WK_PATH=path)).ok

    def volume_present(self):
        return self.test("-d", self.volume() + "/System/Library/CoreServices")

    def data_path(self):
        d = self.volume() + " - Data"
        return d if self.test("-d", d) else self.volume()

    def in_bench(self, p):
        """A path inside the bench install as the answering channel reaches it: its own `/` in bench mode, and in host
        mode under the volume's Data mount -- the system volume is sealed, with /var firmlinked to private/var."""
        if self.ch.channel == "bench":
            return p
        if not self.volume_present():
            return None
        d = self.data_path()
        if d.endswith(" - Data") and p.startswith("/var/"):
            p = "/private" + p
        return d + p

    def fact(self, *args):
        with open(os.path.join(self.root, "lib", "wk", "mac.py")) as f:
            r = self.ch.call("r_ssh", self.ob("mac-fact.sh", WK_FACT=args[0], WK_ARG=(args[1:] or [""])[0]), input=f.read())
        return r.out.replace("\r", "").strip() if r.ok else ""

    def group(self, path):
        return self.fact("volume-group", path)

    def firmware_default(self):
        bv = self.fact("boot-volume")
        if not bv:
            return "unknown (the firmware publishes no boot-volume)"
        grp = bv.rsplit(":", 1)[-1]
        host = self.group("/") if self.ch.channel == "host" else ""
        bench = self.group("/") if self.ch.channel == "bench" else self.group(self.volume()) if self.volume_present() else ""
        if bench and grp == bench:
            return "%s ('%s' -- a plain reboot is expected to enter bench mode)" % (grp, self.c("NODE_VOLUME"))
        if host and grp == host:
            return "%s (the host install -- a plain reboot stays in host mode)" % grp
        if self.ch.channel == "bench":
            return "%s (not '%s', so a plain reboot leaves bench mode)" % (grp, self.c("NODE_VOLUME"))
        return "%s (matches neither install on this disk)" % grp

    def planted(self):
        """`wk bench ab --devices <mac>` names each plant's task after the UTC stamp it was planted at, so the newest sorts last."""
        bench = Store(self.ch.env).bench_dir()
        tasks = [d for d in sorted(glob.glob(os.path.join(bench, "*-%s-mac-ab" % self.who()))) if os.path.isfile(os.path.join(d, "job.json"))]
        if not tasks:
            return "none in %s" % bench
        return "%s (planted %s)" % (tasks[-1], os.path.basename(tasks[-1]).split("-")[0])

    def evidence(self):
        vol, disp = self.c("NODE_VOLUME"), self.display() or "unpinned"
        if self.ch.channel != "bench" and not self.ch.call("m_ssh", self.ob("mac-probe.sh")).ok:
            return "\n".join(["booted_volume=unknown (neither %s nor %s answers)" % (self.c("NODE_SSH"), self.bench_name()),
                              "benchmark_volume=%s (on that Mac; nothing on it is readable while both nodes are silent)" % vol,
                              "firmware_default=unknown (nvram answers only from a running install)",
                              "bench_display=%s (the install that is measured)" % disp, "planted_job=" + self.planted()])
        if self.ch.channel == "bench":
            where = "%s (this install's own /, so it is under no /Volumes path)" % vol
            disp += " (the install that answers here is the measured one)"
        else:
            where = "%s (attached at %s)" % (vol, self.volume()) if self.volume_present() else "%s (not attached)" % vol
            disp += " (the install that is measured)"
        return "\n".join(["booted_volume=" + (self.fact("volume-name", "/") or "unknown (diskutil would not name it)"),
                          "benchmark_volume=" + where, "firmware_default=" + self.firmware_default(),
                          "bench_display=" + disp, "planted_job=" + self.planted()])

    def media(self):
        what = "bench volume '%s'" % self.c("NODE_VOLUME")
        if self.ch.channel == "bench":
            return "%s: %s is running from it, so it is / there and under no /Volumes path" % (what, self.bench_name())
        if self.volume_present():
            return "%s attached at %s" % (what, self.volume())
        if self.ch.call("m_ssh", self.ob("mac-probe.sh")).ok:
            return "%s MISSING on %s -- see 'wk help hardware'" % (what, self.c("NODE_SSH"))
        return "%s: neither %s nor %s answers, so this Mac is between its two installs or off" % (what, self.c("NODE_SSH"), self.bench_name())

    def reprovision(self):
        return ("wk sysimage build perf-macos-tolken --create\n    a second APFS volume in its own container, on the Mac\n"
                "wk sysimage build perf-macos-tolken --install\nwk sysimage build perf-macos-tolken --provision\nhold the power button and pick the volume\n"
                "    by command: wk boot %s, which proves the way back before it arms" % self.who())

    def systems(self):
        if self.ch.channel == "bench":
            ident = self.marker("i_ssh")
            return None if ident is None else [("/", ident)] if ident else []
        if not self.volume_present():
            return None if not self.ch.call("m_ssh", self.ob("mac-probe.sh")).ok else []
        ident = self.marker("m_ssh", self.volume() + "/etc/wk-image")
        return None if ident is None else [(self.volume(), ident)] if ident else []

    def diag(self):
        if not self.volume_present():
            act.die("'%s' is not attached to %s, so there is nothing to read." % (self.c("NODE_VOLUME"), self.who()))
        out = self.ch.call("m_ssh", self.ob("mac-read.sh", WK_PATH=self.volume() + "/var/log/wk-diag.txt")).out
        return out.rstrip("\n") or "(no var/log/wk-diag.txt on '%s' -- it has not been provisioned, or has never booted)" % self.c("NODE_VOLUME")

    def priv(self, verb):
        """Merged stderr: every refusal the helper makes is quoted back. Its reboot, unlike an Apple event, no app can decline."""
        r = self.ch.call("m_ssh", self.ob("mac-priv.sh", WK_HELPER=HELPER, WK_VERB=verb), mutates=verb != "status")
        return r.ok, r.out.replace("\r", "").strip()

    def arm(self, p="", order=""):
        name, vol = self.who(), self.c("NODE_VOLUME")
        if not self.volume_present():
            act.die("'%s' is not attached to %s, or is not a macOS system volume.\n"
                    "    What has to exist is a full macOS *install* on another volume, personalised\n"
                    "    for this Mac -- an image copied onto a disk will not boot (the boot policy\n"
                    "    lives in this machine's secure storage). Install it from Recovery or with\n"
                    "    the macOS installer app, name the volume '%s', and see\n"
                    "    'wk help hardware' for what to turn off on it.\n"
                    "    A different name:  WK_BENCH_VOLUME='...' wk boot %s" % (vol, name, vol, name))
        if not self.test("-x", HELPER):
            act.die("the privileged boot helper is not installed on %s, so nothing here\n"
                    "    can tell the firmware which install to boot:  ./setup --stage quiesce\n"
                    "    Without it this is a person at the keyboard: shut down, hold the power\n"
                    "    button until 'Loading startup options', pick '%s', press Return." % (name, vol))
        # The return first: Apple Silicon has no one-shot form, so an unproven way back boots bench mode forever.
        ok, said = self.priv("boot-host")
        if not ok:
            act.die("%s cannot be told to boot itself again, so it must not be told to\n"
                    "    boot '%s': the trip out is one way and the machine would come up\n"
                    "    in bench mode every time. What it answered:\n%s\n"
                    "    Meanwhile the startup manager is the way: shut down, hold the power button\n"
                    "    until 'Loading startup options', pick '%s', press Return." % (name, vol, said or "(nothing -- %s did not answer)" % self.c("NODE_SSH"), vol))
        act.log(said)
        back = self.firmware_default()
        if "the host install" not in back:
            act.die("bless blessed %s's running install and the firmware names: %s\n"
                    "    Nothing was armed: a return this cannot see is not a proven one, and the\n"
                    "    trip out to '%s' is one way." % (name, back, vol))
        ok, said = self.priv("boot-volume")
        if not ok:
            act.die("%s's firmware would not take '%s', and nothing was changed.\n    What it answered:\n%s"
                    % (name, vol, said or "(nothing -- %s did not answer)" % self.c("NODE_SSH")))
        act.log(said)
        now = self.firmware_default()
        if "'%s'" % vol not in now:
            act.die("bless reported success and %s's firmware still names: %s\n"
                    "    Nothing was rebooted; read that rather than working around it." % (name, now))
        act.info("the firmware will boot '%s' next" % vol)
        return 0

    def disarm(self):
        ok, said = self.priv("boot-host")
        if not ok:
            act.die("%s's firmware still names the benchmark volume, and this could not set it back:\n%s\n"
                    "    Nothing was cleared: the record stays until the firmware names this install again.\n"
                    "    The remedy:  wk machine setup %s   installs the helper if it is missing or old, then\n"
                    "                 wk boot %s --disarm    once more; by hand, System Settings -> General ->\n"
                    "                 Startup Disk on the Mac picks the host install."
                    % (self.who(), said or "  (nothing -- %s did not answer)" % self.c("NODE_SSH"), self.who(), self.who()))
        return 0

    def disarm_note(self):
        return "  the firmware is set back to this install; a plain reboot stays here."

    def reboot(self, armed=False):
        if self.ch.channel == "bench":
            act.die("%s answers as its benchmark install, which carries no boot helper --\n"
                    "    only the host install does, and it is down. What ends a run there is the job\n"
                    "    itself, which blesses this install back and reboots into it.\n"
                    "    Read it meanwhile:  wk bench ab --devices %s --status" % (self.who(), self.who()))
        if self.priv("reboot")[0]:
            return 0
        act.die("could not restart %s. The helper takes no password and is not\n"
                "    installed there; plain sudo wants one, and an unattended transition has no\n"
                "    terminal to answer it on. One command installs it:  wk machine setup %s" % (self.who(), self.who()))

    def record(self, name, input=None, mutates=False):
        return self.ch.call("m_ssh", self.ob(name, lead=("mac-record.sh",)), input=input, mutates=mutates)

    def restart_ready(self):
        """The helper names its detach mechanism, and one too old to name it exits 0 having rebooted nothing."""
        return any(l.startswith("wk-boot-priv: detach=") for l in self.priv("status")[1].splitlines())

    def restart_detail(self):
        if self.priv("status")[1].startswith("wk-boot-priv: ok"):
            return ("the boot helper on %s answers, but names no detach mechanism, so it is older than this tree -- "
                    "its reboot exits 0 having rebooted nothing" % self.c("NODE_SSH"))
        return "no boot helper on %s, and plain sudo there wants a password" % self.c("NODE_SSH")

    def bench_root(self):
        return self.in_bench(BENCH_ROOT)

    def bench_home(self):
        return self.in_bench("/Users/bench")

    def bench_local(self):
        return self.ch.here()

    def manager(self):
        m = self.ch.machine()
        if m is None:
            act.die("%s (machines/%s.conf) sets no NODE_SSH, so nothing can reach its host install" % (self.who(), self.who()))
        return m

    def manager_tools(self, m):
        return TOOLS if m.run(["test", "-x", TOOLS + "/wk"]).ok else None


class MacGuest(MacDriver):
    """A guest rehearses the path and not the number: arming it is starting it, and leaving the role is stopping it."""

    name = "mac-guest"
    arming = "guest"
    measures = False
    bench_channel = "host"
    shims = ()

    @staticmethod
    def transport(root, conf, channel="none", env=None, via=None):
        return GuestChannel(root, conf, env=env, channel=channel, via=via)

    def guest(self):
        return self.ch.ws

    def facts(self):
        return dict(super().facts(), NODE_GUEST=self.guest())

    def probeable(self):
        return self.ch.probeable()

    def probe(self):
        ident = self.marker("m_ssh")
        self.ch.channel = "none" if ident is None else self.bench_channel
        self.mode = "unreachable" if ident is None else "bench " + ident if ident else "host"
        return self.mode

    def display(self):
        return "external " + self.ch.display()

    def systems(self):
        return [] if self.ch.state() == "absent" else [(self.guest(), self.guest())]

    def arm(self, p="", order=""):
        g, name = self.guest(), self.who()
        st = self.ch.state()
        if st == "absent":
            act.die("%s has no guest '%s'.\n    Make one from the golden base and mark it as a benchmark install:\n"
                    "        wk new %s --target vm && wk start %s\n        then, in it:  sudo tee /etc/wk-image <<<'id=%s'"
                    % (name, g, g, g, self.c("NODE_PROFILE") or "perf-macos-benchvm"))
        if st != "running":
            act.info("starting guest '%s'" % g)
            self.ch.start()
        if not self.marker("m_ssh"):
            act.die("'%s' is running but carries no /etc/wk-image, so it is a workstation guest\n"
                    "    and not %s's benchmark install. A run in it would be refused by\n"
                    "    'wk bench staged', which is the correct answer -- mark it first." % (g, name))
        return 0

    def reboot(self, armed=False):
        """The arming was the start; only leaving the role stops it."""
        if armed:
            return 0
        self.ch.stop()
        act.info("stopped '%s' -- for a guest, leaving the role is leaving the machine" % self.guest())
        return 0

    def diag(self):
        return self.ch.call("m_ssh", self.ob("mac-read.sh", WK_PATH="/var/log/wk-diag.txt")).out.rstrip("\n") or "(no diag on the guest)"

    def evidence(self):
        lines = ["guest=%s (%s)" % (self.guest(), self.ch.state() or "unknown"),
                 "measurement=refused (a rehearsal proves the path, not the number)"]
        ident = self.marker("m_ssh")
        return "\n".join(lines + ([] if ident is None else ["marker=" + ident]))

    def media(self):
        if not self.probeable():
            return "a Tart guest, %s (managed on the macOS host)" % self.guest()
        return "a Tart guest, %s (%s); no physical media" % (self.guest(), self.ch.state() or "unknown")

    def reprovision(self):
        return ("wk sysimage build macos-guest-base\n    the golden guest every vm workspace is cloned from\nwk new %s --target vm\nwk bench stage <ws> --to %s"
                % (self.who(), self.who()))

    def own(self, dest):
        """/var/wk is root's on a fresh guest; the run and every put after it are the login user's."""
        r = self.ch.call("m_ssh", self.ob("mac-own.sh", WK_DEST=dest, WK_OWN=BENCH_ROOT), mutates=True)
        if not r.ok:
            act.die("could not make %s in '%s'" % (dest, self.guest()))

    def restart_ready(self):
        return True

    def restart_detail(self):
        return "unreachable: stopping a guest needs no helper"

    def bench_root(self):
        return BENCH_ROOT

    def bench_home(self):
        r = self.ch.call("m_ssh", self.ob("mac-home.sh"))
        return r.out.replace("\r", "").strip() if r.ok else None

    def bench_local(self):
        return False

    def manager(self):
        """tart runs on the macOS host and nowhere else, so the machine managing the guest is this one."""
        return self.ch.via or Local()

    def manager_tools(self, m):
        return self.root


DRIVERS = {d.name: d for d in (MacVolume, MacGuest)}

