"""The Mac's bench volume as a bench system: its deploy stages a workspace's build and each plan's pinned payload onto
the volume as one slot, from host mode; a run measures a slot on the running install. `Side` is the Mac as staging reaches it."""

import json
import os
import re
import shlex
import tempfile
import threading

from wk import act, fleet, images, job, project, samply, screen, tools
from wk.act import die, info, log, warn
from wk.bench import record
from wk.bench.pipeline import variance
from wk.bench.systems import Leg, System, bench_class, first_line, macos_facts
from wk.boot import driver_class, open_driver
from wk.lock import Lock
from wk.machine import Local, Result, lib_argv
from wk.mac import SET_TOLERANCE
from wk.quiet import DESKTOP, PRIV, Quiesce
from wk.workspace import require_name

BENCH_ROOT = "/var/wk"
TOOLS = "Development/wk-tools"
PRODUCT_SKIP = ("*.build", "XCBuildData", "DerivedSources", "PrecompiledHeaders", "compile_commands", "*.a", "*.noindex")
PUT_SKIP = (".git", "__pycache__")   # never carried onto a bench install: history, and bytecode whose source travels
PYTHONS = ("/usr/bin/python3", "/Library/Developer/CommandLineTools/usr/bin/python3", "/usr/local/bin/python3", "/opt/homebrew/bin/python3")
CHECK = "bench/mac-browser-check.py"
WKMAC = "lib/wk/mac.py"
VM_PROCESS = "com.apple.Virtualization.VirtualMachine"
WEB_PROCESS_WAIT = 600
SU_PREFS = "/Library/Preferences/com.apple.SoftwareUpdate.plist"
HOST_MODE = ("this is host mode, and a benchmark does not run here: its number would be a workstation's.\n"
             "    wk boot <mac> arms the bench volume; run this there. --dry-run shows the plan and asks every gate from here.")


def bench_root(env):
    return env.get("WK_BENCH_ROOT") or BENCH_ROOT


def staged_python(m, env):
    """The benchmark runner's driver does a bare `import objc`, which nothing autoinstalls."""
    for p in (env.get("WK_BENCH_PYTHON", ""),) + PYTHONS:
        if p and m.run([p, "-c", "import objc"]).ok:
            return p
    die("no python3 here can 'import objc', and %s drives the browser through PyObjC.\n"
        "    ./setup installs it (bench/mac-pyobjc.sh), or WK_BENCH_PYTHON names a python3 that has it" % os.path.basename(project.get("BENCH_RUNNER")))


def screen_row(m, root):
    """The window server's own list: a window-title query needs assistive access a fresh install has not granted."""
    blocker = screen.blocker(m, root)
    if blocker == "?":
        return record.UNKNOWN, "the screen is free", screen.UNASKED
    if blocker:
        return False, "the screen is free", "on the screen, and nothing wk put there: %s -- answer it at the screen" % blocker
    return True, "the screen is free", "no onboarding or installer pane in front"


def display_row(m, root, py, expect, build=""):
    """Asked per leg: a panel attached between two legs resizes the runner's window, and a rendering benchmark scores the area."""
    argv = [py, os.path.join(root, CHECK)] + (["--build-directory", build] if build else ["--displays-only"])
    r = m.run(argv + (["--expect-display", expect] if expect else []))
    said = (r.out + r.err).strip().replace("\n", "; ")
    return (r.ok, "browser check" if build else "the display",
            (said[len("displays="):] if said.startswith("displays=") else said) + ("  (declared: %s)" % expect if expect else ""))


def console_row(m):
    console, me = first_line(m.run(["stat", "-f", "%Su", "/dev/console"])), first_line(m.run(["id", "-un"]))
    if not (console and me):
        return record.UNKNOWN, "the console session", "could not read %s" % ("the console owner" if me else "the user this runs as")
    return console == me, "the console session", ("%s is logged in at the screen" % me if console == me else
                                                  "the screen belongs to '%s', not %s -- the browser has nowhere to draw" % (console, me))


def quiet_row(root, m, clock, env):
    bad = Quiesce(root, m, clock, env, macos=True).noise()
    return (True, "quiet machine", "every setting read back") if not bad else \
        (False, "quiet machine", "%d setting(s) above are not a measured Mac's; each names its remedy" % bad)


class Side:

    def __init__(self, root, d):
        self.root, self.d, self.ch = str(root), d, d.ch
        self.measures = d.measures

    def run(self, argv, mutates=False):
        r = self.ch.run(argv, mutates=mutates)
        return Result(r.rc, r.out.replace("\r", ""), r.err)

    def out(self, argv):
        r = self.run(argv)
        return r.out.strip() if r.ok else ""

    def read(self, path):
        return self.out(["cat", path])

    def test(self, flag, path):
        return self.ch.run(["test", flag, path]).ok

    def ls(self, path):
        return sorted(self.out(["ls", "-1", path]).split())

    def py(self, rel, *args):
        with open(os.path.join(self.root, rel)) as f:
            return self.out(["python3", "-c", f.read()] + list(args))

    def put(self, src, dest, *skip):
        """Replaces dest with src's tree less the `skip` names: a tar, since the Mac's openrsync fails on the escaped
        `/Volumes/<v> - Data/...`."""
        self.refuse_on_bench()
        self.own(dest)
        fd, tar = tempfile.mkstemp(prefix="wk-put-", suffix=".tar")
        os.close(fd)
        try:
            if not Local().act_run(["tar", "-cf", tar] + [w for x in skip for w in ("--exclude", x)] + ["-C", src, "."]).ok:
                die("could not pack %s" % src)
            return self.land(tar, dest, [["rm", "-rf", dest], ["mkdir", "-p", dest], ["tar", "-xf", "{}", "-C", dest]])
        finally:
            os.unlink(tar)

    def put_file(self, src, dest):
        self.refuse_on_bench()
        self.own(os.path.dirname(dest))
        return self.land(src, dest, [["mv", "-f", "{}", dest]])

    def refuse_on_bench(self):
        if self.ch.side == "bench":
            die("%s answers as its bench install, and nothing is staged onto a running measurement.\n"
                "    Stage once it is back:  wk boot %s --status" % (self.d.who(), self.d.who()))

    def land(self, src, dest, steps):
        tmp = "/tmp/wk-put-%d-%s" % (os.getpid(), os.path.basename(src))
        try:
            self.ch.push(src, tmp)
        except OSError as e:
            warn("could not copy %s to %s: %s" % (src, self.d.who(), e))
            return 1
        rc = 0
        for argv in steps:
            rc = self.ch.run([tmp if w == "{}" else w for w in argv], "host", mutates=True).rc
            if rc:
                break
        self.ch.run(["rm", "-f", tmp], "host", mutates=True)
        return rc

    def own(self, dest):
        pass


class VolumeSide(Side):
    """A second install on another volume of the same Mac: in host mode reached under the volume's Data mount (the system
    volume is sealed, with /var firmlinked to private/var)."""

    def in_bench(self, p):
        if self.ch.side == "bench":
            return p
        if not self.d.volume_present():
            return None
        d = self.d.data_path()
        return d + ("/private" + p if d.endswith(" - Data") and p.startswith("/var/") else p)

    def root_dir(self):
        return self.in_bench(BENCH_ROOT)

    def home_dir(self):
        return self.in_bench("/Users/bench")

    def local(self):
        return self.ch.here()

    def manager(self):
        m = self.ch.machine("host")
        if not self.d.c("ssh") or isinstance(m, Result):
            die("%s (machines/%s.conf) sets no ssh, so nothing can reach its host install" % (self.d.who(), self.d.who()))
        return m

    def tools(self, m):
        return TOOLS if m.run(["test", "-x", TOOLS + "/wk"]).ok else None

    def restart_ready(self):
        # A helper too old to name its detach mechanism exits 0 having rebooted nothing.
        return any(l.startswith("wk-boot-priv: detach=") for l in self.d.priv("status")[1].splitlines())

    def restart_detail(self):
        if self.d.priv("status")[1].startswith("wk-boot-priv: ok"):
            return ("the boot helper on %s answers, but names no detach mechanism, so it is older than this tree -- "
                    "its reboot exits 0 having rebooted nothing" % self.d.c("ssh"))
        return "no boot helper on %s, and plain sudo there wants a password" % self.d.c("ssh")


class GuestSide(Side):

    def own(self, dest):
        me = self.ch.run(["id", "-un"], "host").out.strip()
        for argv in (["sudo", "-n", "mkdir", "-p", dest], ["sudo", "-n", "chown", "-R", me, BENCH_ROOT]):
            if not me or not self.ch.run(argv, "host", mutates=True).ok:
                die("could not make %s in '%s'" % (dest, self.d.guest()))

    def root_dir(self):
        return BENCH_ROOT

    def home_dir(self):
        r = self.ch.run(["printenv", "HOME"], "host")
        return r.out.replace("\r", "").strip() if r.ok else None

    def local(self):
        return False

    def manager(self):
        return self.ch.via

    def tools(self, m):
        return self.root

    def restart_ready(self):
        return True

    def restart_detail(self):
        return "unreachable: stopping a guest needs no helper"


SIDES = {"mac-volume": VolumeSide, "mac-guest": GuestSide}


def side(root, d):
    return SIDES[d.name](root, d)


class Install:

    def __init__(self, root, here, env):
        self.root, self.here, self.env = str(root), here, env
        self.marker = images.marker(env)

    def bench(self):
        return self.here.exists(self.marker)

    def fields(self):
        try:
            return dict(l.split("=", 1) for l in self.here.read(self.marker).splitlines() if "=" in l)
        except OSError:
            return {}

    def ident(self):
        return self.fields().get("id", "")

    def faked(self):
        return self.marker != images.MARKER

    def staging_root(self):
        if self.bench():
            return bench_root(self.env)
        f = fleet.Fleet(self.root, self.env)
        for name in f.names(("mac",)):
            d = open_driver(self.root, dict(f.load(name), name=name), env=self.env, via=self.here)
            if d.ch.here():
                return side(self.root, d).root_dir()
        return None

    def machine(self):
        f, want = fleet.Fleet(self.root, self.env), self.fields()
        for name in f.names(("mac", "guest")):
            conf = f.load(name)
            if conf.get("image_preset") and conf["image_preset"] == (want.get("profile") or want.get("id")):
                return name, dict(conf, name=name)
        return "", None


class Capture(threading.Thread):
    """samply attaches to a pid, so this waits for the web process; task_for_pid on another process is root's."""

    def __init__(self, cache, here, clock, out_file):
        super().__init__(daemon=True)
        self.cache, self.here, self.clock, self.out_file = cache, here, clock, out_file
        self.said = ""

    def run(self):
        binary = samply.fetch(self.here, self.cache, first_line(self.here.run(["uname", "-m"])), "Darwin")
        if not binary:
            self.said = "no samply for this machine, so the leg carries no profile"
            return

        def web_process():
            return first_line(self.here.run(["pgrep", "-n", "-f", project.get("MAC_WEB_PROCESS")]))
        if not self.clock.wait_until(lambda: bool(web_process()), WEB_PROCESS_WAIT, 1):
            self.said = "no web process appeared, so nothing was profiled"
            return
        r = self.here.act_run(["sudo", "-n", binary, "record", "--save-only", "--profile-name", "wk-warmup", "-o", self.out_file, "-p", web_process()])
        self.said = "" if r.ok else "samply exited %d: %s" % (r.rc, (r.out + r.err).strip()[-400:])


class Volume(System):
    kind, bench_host, host_os = "mac-volume", "image", "macos"
    warmup = True
    refused = (("collect", "cores", "software", "browser", "preset"),
               "a Mac's bench volume runs the staged build --slot names, in its own browser; no pin exists on macOS")

    def __init__(self, root, reg, ws, clock, name):
        super().__init__(root, reg, ws, clock)
        self.machine_name, self.install = name, Install(root, reg.machine, reg.env)
        self.conf = dict((fleet.Fleet(root, reg.env).load(name) or {}), name=name)
        self.measures = getattr(driver_class(self.conf.get("driver", "")), "measures", False)
        self.dir = self.manifest_ = None
        self.py = self.profiler = self.stamp = ""

    def driver(self):
        return open_driver(self.root, self.conf, env=self.reg.env)

    def label(self, leg):
        return self.machine_name

    def kill_cmd(self):
        return ""

    def name(self, leg):
        return leg.slot

    def deploy(self, o, kit):
        name = o.get("slot") or "a"
        images.check_slot_name(name)
        require_name(self.ws)
        mac = side(self.root, self.driver())
        mac.d.probe()
        home = mac.root_dir()
        if not home:
            die("%s's bench disk is not attached, so there is nowhere to stage a build onto.\n"
                "    'wk boot %s --status' says what it can see." % (self.machine_name, self.machine_name))
        try:
            d = self.reg.load(self.reg.ws_place(self.ws))
        except LookupError as e:
            die(str(e))
        d.wait_ready(self.ws, self.clock)
        preset_name = o.get("preset") or kit.default_preset(self.reg, self.ws)
        try:
            preset = kit.resolve_preset(preset_name, d.os(), d.kind, d.env)
        except LookupError:
            die("unknown preset '%s' (wk build --list)" % preset_name)
        src = d.src(self.ws)
        build = preset.build_dir(src)
        if not d.exec(self.ws, ["test", "-d", build]).ok:
            die("'%s' has no %s build to stage (%s).\n    Build it first:  wk build %s %s" % (self.ws, preset_name, build, self.ws, preset_name))
        lock = Lock(self.reg.store, self.here, self.clock)
        pinned = [(p, kit.pin_payload(self.here, lock, self.reg.store, d, self.ws, p)) for p in o.get("plans") or []]
        pinned = [(p, path) for p, path in pinned if path]   # a local_copy plan's payload is in the staged tree already
        dest = os.path.join(home, "staged", name)
        info("staging %s from '%s' onto %s as '%s'" % (preset_name, self.ws, self.machine_name, name))
        deliver = not mac.local()
        assemble = os.path.join(self.reg.store.state_dir(), "bench-stage", name) if deliver else dest
        said = tools.identity(self.root, self.here)
        manifest = {"staged_at": self.clock.iso(), "workspace": self.ws, "workspace_place": d.name, "preset": preset_name,
                    project.get("SHA_FIELD"): first_line(d.exec(self.ws, ["git", "-C", src, "rev-parse", "HEAD"])),
                    "plans": ",".join(p for p, _ in pinned), "machine": self.machine_name, "bench_host": "image",
                    "wk_tools": (said.get("sha") or "unknown") + ("+dirty" if said.get("dirty") == "yes" else "")}
        done = False
        with job.Signals():
            try:
                self.assemble(d, build, src, assemble, pinned)
                self.publish(mac, assemble, dest, json.dumps(manifest, indent=2) + "\n", deliver)
                done = True
            finally:
                if deliver or not done:
                    self.here.remove(assemble)
        info("staged: %s" % dest)
        log("  next:  wk bench ab --devices %s --systems %s,<other> --workspace %s   (or wk boot %s, then on it:\n"
            "         wk bench run %s <plan> --system %s --slot %s)" % (self.machine_name, name, self.ws, self.machine_name,
                                                                        self.ws, self.machine_name, name))
        return 0

    def assemble(self, d, build, src, into, pinned):
        """Products, not the build tree: what the driver launches, what DYLD_FRAMEWORK_PATH resolves, and the dSYMs."""
        self.here.remove(into)
        products = os.path.join(into, project.get("BUILD_DIR"), os.path.basename(build))
        self.here.mkdir(products)
        log("  the build product (frameworks, %s.app and dSYMs; no intermediates)" % project.get("BROWSER"))
        d.pull_dir(self.ws, build, products, exclude=PRODUCT_SKIP)
        top = project.get("BENCH_RUNNER").split("/")[0]
        log("  %s/ -- %s, its harness and the plans" % (top, os.path.basename(project.get("BENCH_RUNNER"))))
        d.pull_dir(self.ws, os.path.join(src, top), os.path.join(into, top))
        for plan, payload in pinned:
            log("  the benchmark itself, pinned: %s (%s)" % (plan, os.path.basename(payload)))
            self.here.mkdir(os.path.join(into, "payload", plan))
            # A working clone can carry git's fsmonitor socket, which no copy reproduces.
            if not self.here.act_run(["rsync", "-a", "--exclude", ".git", payload.rstrip("/") + "/", os.path.join(into, "payload", plan) + "/"]).ok:
                die("could not pin %s from %s" % (plan, payload))

    def publish(self, mac, assemble, dest, text, deliver):
        """The manifest crosses last: a delivery gives no order guarantee, and a half-delivered tree must not look finished."""
        if not deliver:
            self.here.write(os.path.join(assemble, "stage.json"), text)
            return
        info("sending it to %s" % self.machine_name)
        if mac.put(assemble, dest, *PUT_SKIP):
            die("could not deliver the build to %s" % self.machine_name)
        pending = assemble + ".stage.json"
        self.here.write(pending, text)
        rc = mac.put_file(pending, os.path.join(dest, "stage.json"))
        self.here.remove(pending)
        if rc:
            die("delivered the build to %s but could not publish its manifest.\n"
                "    Nothing will use it until one is there -- deploy it again." % self.machine_name)

    def tasks(self):
        return Local(), os.path.join(bench_root(self.reg.env), "results")

    def manifest(self, slot):
        d = os.path.join(bench_root(self.reg.env), "staged", slot)
        try:
            return json.loads(self.here.read(os.path.join(d, "stage.json")))
        except (OSError, ValueError):
            die("nothing staged as '%s' on this install (%s/stage.json).\n    Stage it from the workspace that built it:\n"
                "        wk bench deploy <workspace> %s --slot %s" % (slot, d, self.machine_name, slot))

    def leg(self, plan, o, kit):
        leg = Leg(plan, o)
        leg.slot = o.get("slot") or "a"
        self.manifest_, self.dir = self.manifest(leg.slot), os.path.join(bench_root(self.reg.env), "staged", leg.slot)
        if self.ws and self.manifest_.get("workspace") and self.ws != self.manifest_["workspace"]:
            die("'%s' is staged from workspace '%s', not '%s'" % (leg.slot, self.manifest_["workspace"], self.ws))
        name = self.manifest_.get("preset", "")
        try:
            leg.preset = kit.resolve_preset(name, "macos", self.manifest_.get("workspace_place") or "vm", self.reg.env)
        except LookupError:
            die("%s names preset '%s', which this wk-tools does not know -- stage it again" % (self.dir, name))
        leg.klass, leg.arch, leg.runner, leg.browser = bench_class(plan), "native", "browser", project.get("BENCH_BROWSERS")["macos"]
        self.py = staged_python(self.here, self.reg.env)
        return leg

    def boot(self, leg):
        if not self.install.bench() and not act.dry_run():
            die(HOST_MODE)
        self.stamp = self.scanned()

    def scanned(self):
        """Out of the plist file, since cfprefsd answers values it does not carry; any Last*Date moving means a scan ran."""
        return "".join(sorted(l.replace(" ", "") for l in self.here.run(["/usr/bin/plutil", "-p", SU_PREFS]).out.splitlines()
                              if re.search(r'"Last[A-Za-z]*Date"', l)))

    def build_dir(self, leg):
        return os.path.join(self.dir, project.get("BUILD_DIR"), os.path.basename(leg.preset.build_dir()))

    def display(self):
        return (open_driver(self.root, self.conf).display() or "") if self.conf.get("driver") else ""

    def checks(self, leg):
        m, root, build, expect = self.here, self.root, self.build_dir(leg), self.display()
        app = os.path.join(build, project.get("BROWSER") + ".app", "Contents", "MacOS", project.get("BROWSER"))
        rows = [(True, "build present", "%s @%s" % (os.path.basename(build), self.manifest_.get(project.get("SHA_FIELD"), "")[:10]))
                if m.run(["test", "-x", app]).ok and any(n.endswith(".framework") for n in m.listdir(build)) else
                (False, "build present", "no %s.app and frameworks in %s -- the staged build is not on the disk" % (project.get("BROWSER"), build))]
        if self.install.bench():
            rows.append((True, "bench mode", (self.install.ident() or "image") + (" (marker overridden)" if self.install.faked() else "")))
            rows.append((True, "the machine", self.machine_name + ("" if self.measures else " -- a rehearsal: its reading is no measurement")))
            # macOS restarts a paused daemon on demand, so the leg about to measure stops them again before judging them.
            m.act_run(["sudo", "-n"] + lib_argv(root, DESKTOP, "wk_quiet_daemons_pause"))
        else:
            rows.append((False, "bench mode", "this is host mode -- a real run refuses"))
        runner = os.path.join(self.dir, project.get("BENCH_RUNNER"))
        rows.append((True, os.path.basename(runner), runner) if m.run(["test", "-x", runner]).ok else
                    (False, os.path.basename(runner), "not in the staged tree: " + runner))
        readback = m.run(["sudo", "-n", PRIV, "status"])
        level, ambient = (first_line(m.run(["python3", os.path.join(root, WKMAC), k])) for k in ("brightness", "auto-brightness"))
        try:
            dim = float(level) <= SET_TOLERANCE
        except ValueError:
            dim = False
        vm = m.run(["pgrep", "-x", VM_PROCESS])
        rows += [(True, "python with PyObjC", self.py), console_row(m), screen_row(m, root), display_row(m, root, self.py, expect),
                 display_row(m, root, self.py, expect, build),
                 (False, "no auth panel", "SecurityAgent has a modal dialog up, above the browser -- sudo killall -9 SecurityAgent")
                 if m.run(["pgrep", "-x", "SecurityAgent"]).ok else (True, "no auth panel", "no modal authentication dialog"),
                 (readback.ok, "quiesce readback", (readback.out + readback.err).strip().replace("\n", "; ") or "wk quiesce on"),
                 (True, "brightness", "no built-in panel to hold") if expect.split()[:1] == ["external"] else
                 (record.UNKNOWN if not (level and ambient) else dim and ambient in ("off", "none"), "brightness",
                  "brightness %s, ambient-light compensation %s" % (level or "unread", ambient or "unread")),
                 (False, "no other machine running", "a virtual machine is running beside the measurement (pid %s)" % first_line(vm))
                 if vm.ok else (True, "no other machine running", "no virtual machine"),
                 quiet_row(root, m, self.clock, self.reg.env)]
        return rows, [] if self.measures else ["rehearsal"]

    def payload(self, leg, kit, lock):
        pinned = os.path.join(self.dir, "payload", leg.plan)
        if not self.here.isdir(pinned):
            die("nothing pinned for '%s' in staged build '%s', and this install has no network to fetch it.\n"
                "    Stage it with the plan:  wk bench deploy <ws> %s --slot %s --plan %s" % (leg.plan, leg.slot, self.machine_name, leg.slot, leg.plan))
        return pinned

    def subject(self, leg):
        return "staged", leg.slot, "%s=%s" % (self.machine_name, leg.preset.name), " --system %s --slot %s" % (self.machine_name, leg.slot)

    def env_fields(self, leg):
        man = self.manifest_
        return {"workspace": man.get("workspace", ""), "preset": leg.preset.name, "browser": leg.browser,
                project.get("SHA_FIELD"): man.get(project.get("SHA_FIELD"), ""), "role": self.install.ident(), "system": self.install.ident(),
                "machine": self.machine_name, "profile": self.install.fields().get("profile", ""), "staged_from": self.dir,
                "stage.id": leg.slot, "stage.wk_tools": man.get("wk_tools", ""), "display_declared": self.display(),
                "display": first_line(self.here.run([self.py, "-c", "from AppKit import NSScreen; f=NSScreen.mainScreen().frame(); "
                                                     "print('%dx%d' % (f.size.width, f.size.height))"])),
                "measures": self.measures, "role_marker_overridden": self.install.faked()}

    def facts(self, leg):
        return macos_facts(self.here.run, "/")

    def link(self, path, link):
        self.here.mkdir(os.path.dirname(link))
        self.here.act_run(["ln", "-sf", path, link])

    def aslr_prefix(self):
        die("WK_BENCH_ASLR=off: ASLR cannot be turned off on Apple Silicon; the run records the slide it got")

    def runner(self, leg, kit, pad):
        exports = ["DYLD_SHARED_REGION=avoid"] if variance(self.reg.env)["shared_cache"] == "avoid" else []
        args = [self.py, os.path.join(self.dir, project.get("BENCH_RUNNER")), "--browser", leg.browser, "--platform", "osx"]
        args += kit.browser_args(leg, os.path.join(leg.out, "result.json"), leg.payload, pad(self.build_dir(leg)))
        return exports, self.dir, [shlex.quote(a) for a in args]

    def warm_file(self, leg):
        return os.path.join(os.path.dirname(os.path.dirname(leg.out)), "warmup", "%s-%s.profile.json" % (self.machine_name, leg.o.get("arm", "")))

    def run(self, leg, script, watched, log_path):
        """A warmup leg's web process is profiled for as long as it runs."""
        capture, self.profiler = None, ""
        if leg.o.get("warmup") and not leg.o.get("no_warmup_profile") and not act.dry_run():
            self.here.mkdir(os.path.dirname(self.warm_file(leg)))
            capture = Capture(self.reg.store.cache_dir(), self.here, self.clock, self.warm_file(leg))
            capture.start()
        try:
            return watched(["bash", "-lc", script], None, log_path)
        finally:
            if capture is not None:
                capture.join(60)
                self.profiler = record.UNMEASURED + capture.said if capture.said else "samply"
                (warn if capture.said else log)("  profile     %s" % (capture.said or "%s (samply)" % self.warm_file(leg)))

    def collect(self, leg, rc):
        if not act.dry_run() and self.scanned() != self.stamp:
            warn("a software-update scan ran during this leg; the report drops it")
            record.write_env(os.path.join(leg.out, "env.json"), {"unclean": "a software-update scan ran across it"}, update=True, machine=leg.machine)
        log("  it is on the bench volume, so it survives the way back:  wk bench ab --devices %s --collect" % self.machine_name)

    def after(self, leg):
        """ASLR cannot be turned off on Apple Silicon, so what is recorded is the load address slide that happened."""
        pid = first_line(self.here.run(["pgrep", "-n", project.get("BROWSER")]))
        slide = next((l.split(":", 1)[1].strip() for l in self.here.run(["vmmap", "-slide", pid]).out.splitlines()
                      if l.startswith("Load Address Slide:")), "") if pid else ""
        return dict({"configuration.aslr": slide or "os-randomised"}, **({"profiler": self.profiler} if leg.o.get("warmup") else {}))

    def reading(self, leg):
        build = os.path.join(self.dir, project.get("BUILD_DIR"))
        for sub in sorted(self.here.listdir(build)) if self.here.isdir(build) else []:
            check = os.path.join(build, sub, "wk-profile-check.json")
            if self.here.exists(check):
                return check
        return ""

    def stop(self, env):
        die("a run on %s's bench volume is stopped where it runs: ^C there, or its autorun's watchdog" % self.machine_name)


def rubble(install):
    from wk.rubble import du_kb, remover, row
    home, m = install.staging_root(), install.here
    staged = os.path.join(home or "", "staged")
    if not home or not m.isdir(staged):
        return []
    names = m.listdir(staged)
    done = [n for n in names if m.exists(os.path.join(staged, n, "stage.json"))]
    return [row("staged", ("finished stage %s" % n) if n in done else ("stage %s, never finished" % n), du_kb(m, os.path.join(staged, n)),
                "--purge-builds" if n in done else "", remover(m, os.path.join(staged, n)))
            for n in names if n not in done[-1:]]
