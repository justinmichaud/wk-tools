"""The Mac's benchmark install as a bench system (mac-volume). `wk bench stage` is its deploy, run in host mode;
`wk bench staged` is the one pipeline run on the install, which resolves itself as the system in bench mode; and
`staged --gates` asks every gate a run needs over the running install, reading only, before anything reboots."""

import base64
import json
import os
import re
import shlex
import statistics
import subprocess
import sys
import threading

from wk import act, buildconf, fleet, job, notify, pgo, record as wkrecord, sched, shell
from wk.act import Refused, die, info, log, warn
from wk.bench import ab, board_ab, pipeline, record, report, seed
from wk.bench.systems import System, first_line, root_device
from wk.boot import cli as bootcli, driver_class, open_driver
from wk.boot.mac import BENCH_ROOT, TOOLS, Channel, Script
from wk.kv import kv
from wk.lock import Lock
from wk.mac import SET_TOLERANCE
from wk.machine import Ssh
from wk.quiet import DESKTOP, PRIV, Quiesce, lib_argv
from wk.workspace import require_name

MARKER = "/etc/wk-image"
PRODUCT_SKIP = ("*.build", "XCBuildData", "DerivedSources", "PrecompiledHeaders", "compile_commands", "*.a", "*.noindex")
PUT_SKIP = (".git", "__pycache__")   # never carried onto a benchmark install: history, and bytecode whose source travels
PYTHONS = ("/usr/bin/python3", "/Library/Developer/CommandLineTools/usr/bin/python3", "/usr/local/bin/python3",
           "/opt/homebrew/bin/python3")
CHECK = "bench/mac-browser-check.py"
WKMAC = "lib/wkmac.py"
QUIET = "lib/quiet.sh"
VM_PROCESS = "com.apple.Virtualization.VirtualMachine"
WEB_PROCESS = "com.apple.WebKit.WebContent"
WEB_PROCESS_WAIT = 600
GATES = ("quiet desktop", "quiesce readback", "brightness", "display mode", "browser check", "staged dry run",
         "window in front", "no other machine running")
HOST_MODE = ("this is host mode, and a benchmark does not run here.\n"
             "    Nothing about a run on this machine-as-workstation is comparable with one\n"
             "    on the benchmark install -- same command, same plan, same shape of result,\n"
             "    a different machine underneath it -- and there is no way to tell the two\n"
             "    apart afterwards. So it does not run.\n"
             "        wk boot mbp        arm it; it prints the two clicks\n"
             "        ... boot the benchmark volume, then run this there ...\n"
             "    'wk bench staged --dry-run' still shows what it would do from here.")


def staged_python(m, env):
    """run-benchmark's driver does a bare `import objc`, which nothing autoinstalls."""
    for p in (env.get("WK_BENCH_PYTHON", ""),) + PYTHONS:
        if p and m.run([p, "-c", "import objc"]).ok:
            return p
    die("no python3 here can 'import objc', and run-benchmark drives the browser through PyObjC.\n"
        "    ./setup installs it (bench/mac-pyobjc.sh), or WK_BENCH_PYTHON names a python3 that has it")


def machine_driver(root, conf):
    return open_driver(root, conf)


class Install:
    """This machine as its running install says: the marker is bench mode, and its profile names the machine."""

    def __init__(self, root, here, env, driver=machine_driver):
        self.root, self.here, self.env, self.make_driver = str(root), here, env, driver
        self.marker = env.get("WK_IMAGE_MARKER") or MARKER

    def bench(self):
        return self.here.exists(self.marker)

    def fields(self):
        try:
            return kv(self.here.read(self.marker))
        except OSError:
            return {}

    def ident(self):
        return self.fields().get("id", "")

    def faked(self):
        return self.marker != MARKER

    def machine(self):
        f, want = fleet.Fleet(self.root, self.env), self.fields()
        for name in f.names(("mac", "guest")):
            conf = f.load(name)
            if conf.get("NODE_PROFILE") and conf["NODE_PROFILE"] == (want.get("profile") or want.get("id")):
                return name, dict(conf, NODE_NAME=name)
        return "", None

    def driver(self, name):
        conf = fleet.Fleet(self.root, self.env).load(name)
        if conf is None:
            die("unknown machine '%s' (wk boot --list)" % name)
        return self.make_driver(self.root, dict(conf, NODE_NAME=name))

    def staging_root(self):
        if self.env.get("WK_BENCH_ROOT"):
            return self.env["WK_BENCH_ROOT"]
        if self.bench():
            return BENCH_ROOT
        # Host mode holds two macOS machines in machines/ and nothing here can tell which one this is.
        name = self.env.get("WK_BENCH_MACHINE", "")
        d = self.driver(name) if name else None
        return d.bench_root() if d is not None and hasattr(d, "bench_root") else None


def screen_row(m, root):
    """The window server's own list: a window-title query needs assistive access a fresh install has not granted."""
    blocker = m.run(lib_argv(root, QUIET, "screen_blocker")).out.strip()
    if blocker == "?":
        return False, "the window server was not asked -- no compiler here to build the probe with (bench/mac-window-probe.sh)"
    if blocker:
        return False, "on the screen, and nothing wk put there: %s -- answer it at the screen" % blocker
    return True, "no onboarding or installer pane in front"


def display_row(m, root, py, expect, build=""):
    """Asked per leg: a panel attached between two legs resizes run-benchmark's window, and MotionMark scores the area."""
    argv = [py, os.path.join(root, CHECK)] + (["--build-directory", build] if build else ["--displays-only"])
    r = m.run(argv + (["--expect-display", expect] if expect else []))
    said = (r.out + r.err).strip().replace("\n", "; ")
    return r.ok, (said[len("displays="):] if said.startswith("displays=") else said) + ("  (declared: %s)" % expect if expect else "")


def auth_row(m):
    """SecurityAgent draws a modal sheet above the browser, never becomes frontmost, and ignores SIGTERM."""
    if m.run(["pgrep", "-x", "SecurityAgent"]).ok:
        return False, "SecurityAgent has a modal dialog up, above the browser -- sudo killall -9 SecurityAgent"
    return True, "no modal authentication dialog"


def named(what, row):
    return row[0], what, row[1]


def quiet_row(root, m, clock, env):
    bad = Quiesce(root, m, clock, env, macos=True).noise()
    return (True, "every setting read back") if not bad else (False, "%d setting(s) above are not a measured Mac's; each names its remedy" % bad)


class Gates:
    """Every gate a bench run needs, asked over `m` (the running install, its tree at `root`); nothing here writes."""

    def __init__(self, root, m, clock, env, plan, expect, build, py):
        self.root, self.m, self.clock, self.env = str(root), m, clock, env
        self.plan, self.expect, self.build, self.py = plan, expect, build, py

    def quiet_desktop(self):
        return quiet_row(self.root, self.m, self.clock, self.env)

    def quiesce_readback(self):
        r = self.m.run(["sudo", "-n", PRIV, "status"])
        return r.ok, (r.out + r.err).strip().replace("\n", "; ") or "wk quiesce on"

    def brightness(self):
        if self.expect.split()[:1] == ["external"]:
            return True, "no built-in panel to hold"
        level = first_line(self.m.run(["python3", os.path.join(self.root, WKMAC), "brightness"]))
        ambient = first_line(self.m.run(["python3", os.path.join(self.root, WKMAC), "auto-brightness"]))
        try:
            dim = float(level) <= SET_TOLERANCE
        except ValueError:
            dim = False
        return dim and ambient in ("off", "none"), "brightness %s, ambient-light compensation %s" % (level or "unread", ambient or "unread")

    def display_mode(self):
        return display_row(self.m, self.root, self.py, self.expect)

    def browser_check(self):
        if not self.build:
            return False, "nothing staged to check the browser with"
        return display_row(self.m, self.root, self.py, self.expect, self.build)

    def staged_dry_run(self):
        argv = [os.path.join(self.root, "wk"), "bench", "staged", "--dry-run", "--plan", self.plan]
        r = self.m.run(argv + (["--expect-display", self.expect] if self.expect else []))
        return r.ok, "every leg check passes" if r.ok else "a leg would be refused: " + " ".join(argv[1:])

    def window_in_front(self):
        return screen_row(self.m, self.root)

    def no_other_machine_running(self):
        r = self.m.run(["pgrep", "-x", VM_PROCESS])
        return (False, "a virtual machine is running beside the measurement (pid %s)" % first_line(r)) if r.ok else (True, "no virtual machine")

    def ask(self):
        return [(name,) + getattr(self, name.replace(" ", "_"))() for name in GATES]


class MacVolumeSystem(System):
    """The running install, measured where it stands: a staged tree in, a run directory on the volume out."""

    kind = "mac-volume"
    bench_host = "image"
    host_os = "macos"

    def __init__(self, root, reg, clock, install, home, stage_dir, o):
        self.root, self.reg, self.clock, self.install, self.o = str(root), reg, clock, install, o
        self.here, self.target, self.env = reg.machine, None, reg.env
        self.home, self.dir = home, stage_dir
        try:
            self.manifest = json.loads(self.here.read(os.path.join(stage_dir, "stage.json")))
        except (OSError, ValueError):
            die("%s/stage.json is not a stage's manifest -- stage it again" % stage_dir)
        self.ws = self.manifest.get("workspace", "")
        self.py = staged_python(self.here, self.env)
        self.machine, self.conf = install.machine()
        self.measures = bool(self.conf) and getattr(driver_class(self.conf.get("NODE_DRIVER", "")), "measures", False)

    def boot(self):
        if not self.install.bench() and not act.dry_run():
            die(HOST_MODE)

    def src(self):
        return self.dir

    def sha(self):
        return self.manifest.get("webkit_sha", "")

    def exec_ok(self, *argv):
        return self.here.run(list(argv)).ok

    def build_dir(self, leg):
        return os.path.join(self.dir, "WebKitBuild", os.path.basename(leg.cfg.build_dir()))

    def build_present(self, leg):
        build = self.build_dir(leg)
        if not self.exec_ok("test", "-x", os.path.join(build, "MiniBrowser.app/Contents/MacOS/MiniBrowser")):
            return False, "no MiniBrowser.app in %s -- the stage's %s is not on the disk" % (build, leg.cfg.name)
        if not any(n.endswith(".framework") for n in self.here.listdir(build)):
            return False, "no *.framework in %s -- the driver refuses it" % build
        return True, "%s @%s" % (os.path.basename(build), self.sha()[:10])

    def cores_refusal(self):
        return "no pin exists on macOS"

    def aslr_prefix(self):
        die("WK_BENCH_ASLR=off: ASLR cannot be turned off on Apple Silicon; the run records the slide it got")

    def run_dir(self, leg):
        return leg.out

    def payload_dir(self, leg):
        return leg.payload

    def run_env(self, leg):
        return ["DYLD_SHARED_REGION=avoid"] if self.env.get("WK_BENCH_SHARED_CACHE") == "avoid" else []

    def runner_argv(self, leg):
        return [self.py, os.path.join(self.dir, "Tools/Scripts/run-benchmark"), "--browser", leg.browser, "--platform", "osx"]

    def link(self, path, link):
        self.here.mkdir(os.path.dirname(link))
        self.here.act_run(["ln", "-sf", path, link])

    def deploy(self, leg):
        return None

    def run(self, leg, script, watched, log_path):
        want = self.o.get("profile") and not act.dry_run()
        capture = Capture(self.root, self.here, self.clock, self.o["profile"], leg.out) if want else None
        if capture is not None:
            capture.start()
        try:
            return watched(["bash", "-lc", script], None, log_path)
        finally:
            if capture is not None:
                capture.join(60)
                capture.report(leg)

    def collect(self, leg):
        log("  it is on the benchmark volume, so it survives the way back:")
        log("    wk boot %s --back        reboot back into host mode" % (self.machine or "mbp"))
        log("    wk bench staged --ls      list it from over there")

    def checks(self, leg):
        rows, notes = [], []
        if self.install.bench():
            rows.append((True, "bench mode", (self.install.ident() or "image") + (" (marker overridden)" if self.install.faked() else "")))
            if self.install.faked():
                warn("WK_IMAGE_MARKER points at %s, not %s -- recorded as a workstation number" % (self.install.marker, MARKER))
            rows.append((True, "the machine", self.machine + ("" if self.measures else " -- a rehearsal: its reading is no measurement"))
                        if self.conf else (False, "the machine", "the marker names no machine in machines/ (NODE_PROFILE)"))
        else:
            rows.append((False, "bench mode", "this is host mode -- a real run refuses"))
        runner = os.path.join(self.dir, "Tools/Scripts/run-benchmark")
        rows.append((True, "run-benchmark", runner) if self.exec_ok("test", "-x", runner) else
                    (False, "run-benchmark", "not in the staged tree: " + runner))
        rows.append((True, "python with PyObjC", self.py))
        console, me = first_line(self.here.run(["stat", "-f", "%Su", "/dev/console"])), first_line(self.here.run(["id", "-un"]))
        rows.append((console == me, "the console session", "%s is logged in at the screen" % me if console == me else
                     "the screen belongs to '%s', not %s -- MiniBrowser has nowhere to draw" % (console or "nobody", me)))
        rows.append(named("the screen is free", screen_row(self.here, self.root)))
        rows.append(named("the display", display_row(self.here, self.root, self.py, self.o.get("expect_display") or "")))
        rows.append(named("no auth panel", auth_row(self.here)))
        if self.install.bench():
            # macOS restarts a paused daemon on demand, so the leg about to measure stops them again before judging them.
            self.here.act_run(["sudo", "-n", *lib_argv(self.root, DESKTOP, "wk_quiet_daemons_pause")])
        rows.append(named("quiet machine", quiet_row(self.root, self.here, self.clock, self.env)))
        if not self.measures:
            notes.append("rehearsal")
        return rows, notes

    def sysctl(self, key):
        return first_line(self.here.run(["sysctl", "-n", key]))

    def facts(self, leg):
        m = self.here
        mem = self.sysctl("hw.memsize")
        therm = next((l.split("=", 1)[1].strip() for l in m.run(["pmset", "-g", "therm"]).out.splitlines() if "CPU_Speed_Limit" in l), "100")
        disp = first_line(m.run([self.py, "-c", "from AppKit import NSScreen; f=NSScreen.mainScreen().frame(); "
                                 "print('%dx%d' % (f.size.width, f.size.height))"]))
        return ["role=" + self.install.ident(), "system=" + self.install.ident(), "machine=" + self.machine,
                "profile=" + self.install.fields().get("profile", ""), "staged_from=" + self.dir, "stage.id=" + os.path.basename(self.dir),
                "stage.wk_tools=" + self.manifest.get("wk_tools", ""), "display=" + disp, "display_declared=" + (self.o.get("expect_display") or ""),
                "host.model=" + self.sysctl("hw.model"), "host.cores=" + self.sysctl("hw.ncpu"),
                "host.memory_mb=%d" % (int(mem) // 1048576 if mem.isdigit() else 0), "host.macos=" + first_line(m.run(["sw_vers", "-productVersion"])),
                "host.kernel=" + first_line(m.run(["uname", "-r"])), "host.kernel_arch=" + first_line(m.run(["uname", "-m"])),
                "host.root_device=" + root_device(m.run, m.read, "/", True), "host.power=" + first_line(m.run(["pmset", "-g", "batt"])),
                "host.cpu_speed_limit=" + therm]

    def bool_facts(self):
        return ["measures=" + ("1" if self.measures else ""), "role_marker_overridden=" + ("1" if self.install.faked() else "")] + \
            (["configuration.shared_cache="] if self.env.get("WK_BENCH_SHARED_CACHE") == "avoid" else [])

    def after(self, leg):
        """ASLR cannot be turned off on Apple Silicon, so what is recorded is the load address slide that happened."""
        pid = first_line(self.here.run(["pgrep", "-n", "MiniBrowser"]))
        slide = next((l.split(":", 1)[1].strip() for l in self.here.run(["vmmap", "-slide", pid]).out.splitlines()
                      if l.startswith("Load Address Slide:")), "") if pid else ""
        return ["configuration.aslr=" + (slide or "os-randomised")]


class Capture(threading.Thread):
    """samply attaches to a pid, so this waits for the web process; task_for_pid on another process is root's."""

    def __init__(self, root, here, clock, out_file, rundir):
        super().__init__(daemon=True)
        self.root, self.here, self.clock, self.out_file, self.rundir = root, here, clock, out_file, rundir
        self.taken = False

    def run(self):
        samply = first_line(self.here.run(shell.argv(self.root, '. "%s/lib/profiler.sh"; samply_fetch' % self.root,
                                                     first_line(self.here.run(["uname", "-m"])), "Darwin")))
        if not samply:
            warn("no samply for this machine, so the leg carries no profile")
            return
        def web_process():
            return first_line(self.here.run(["pgrep", "-n", "-f", WEB_PROCESS]))
        if not self.clock.wait_until(lambda: bool(web_process()), WEB_PROCESS_WAIT, 1):
            warn("no web process appeared, so nothing was profiled")
            return
        pid = web_process()
        log("  profiling the web process (pid %s) into %s" % (pid, self.out_file))
        r = self.here.act_run(["sudo", "-n", samply, "record", "--save-only", "--profile-name", "wk-warmup", "-o", self.out_file, "-p", pid])
        if not act.dry_run():
            with open(os.path.join(self.rundir, "profile.log"), "w") as f:
                f.write(r.out + r.err)
        self.taken = r.ok

    def report(self, leg):
        if self.taken:
            log("  profile: %s" % self.out_file)
            record.write_env(os.path.join(leg.out, "env.json"), ["profile=" + self.out_file], update=True)
        elif not act.dry_run():
            warn("  no samply capture was taken -- see %s/profile.log" % leg.out)


class StagedRun(pipeline.Run):
    """The pipeline on the running install: no workspace, no task (the host lane's task collects the run directory)."""

    def __init__(self, root, reg, system, clock, env, popen):
        self.root, self.reg, self.system, self.clock, self.popen = str(root), reg, system, clock, popen
        self.env = dict(env)
        self.env.setdefault("WK_STALL_SECONDS", pipeline.STALL_SECONDS)
        self.env.setdefault("WK_ABORT_SECONDS", pipeline.ABORT_SECONDS)
        self.here, self.ws, self.target = reg.machine, system.ws, None
        self.bench_dir = os.path.join(system.home, "results")
        self.lock, self.task, self.kill_cmd, self.dry_fails = Lock(reg.store, self.here, clock), None, "", 0

    def idle_rows(self):
        """A bench install builds nothing, and its one load is the leg before this one; the quiet gate judges the rest."""
        return []

    def leg(self, plan, o):
        s, leg = self.system, pipeline.Leg(plan, o)
        name = s.manifest.get("config", "")
        try:
            leg.cfg = buildconf.resolve(name, "macos", s.manifest.get("workspace_target") or "vm", self.env)
        except LookupError:
            die("%s names config '%s', which this wk-tools does not know -- stage it again" % (s.dir, name))
        leg.klass, leg.arch, leg.runner, leg.browser = pipeline.bench_class(plan), "native", "browser", "minibrowser"
        return leg

    def seed(self, leg):
        """A benchmark install has no network, so the payload is the one pinned into the stage, one directory per plan."""
        pinned = os.path.join(self.system.dir, "payload", leg.plan)
        leg.payload = leg.o.get("payload") or (pinned if self.here.isdir(pinned) else "")
        if not leg.payload and self.here.isdir(os.path.dirname(pinned)):
            warn("nothing pinned for '%s' in this staged tree (it holds: %s)" % (leg.plan, ", ".join(self.here.listdir(os.path.dirname(pinned)))))
            log("  run-benchmark will fetch %s itself, which needs the network. To pin it:" % leg.plan)
            log('    wk bench stage <ws> --to <machine> --plan %s --payload "$(wk bench seed <ws> %s)"' % (leg.plan, leg.plan))

    def begin(self, leg):
        s = self.system
        leg.id = "%s-%s-%s" % (self.clock.stamp(), leg.plan, os.path.basename(s.dir))
        leg.rel, leg.out = leg.id, os.path.join(self.bench_dir, leg.id)
        steps = ["run %s from %s (%s)" % (leg.plan, s.dir, leg.cfg.name), "record into %s" % leg.out]
        if leg.o.get("profile"):
            steps.append("profile the web process into %s" % leg.o["profile"])
        if act.dry_run():
            return steps
        os.makedirs(leg.out, exist_ok=True)
        record.write_env(os.path.join(leg.out, "env.json"), [
            "plan=" + leg.plan, "workspace=" + s.ws, "config=" + leg.cfg.name, "browser=" + leg.browser, "webkit_sha=" + s.sha(),
            "count=" + leg.count, "local_copy=" + leg.payload, "preflight_notes=" + leg.notes, "class=" + leg.klass,
            "runner=browser", "arch=native", "bench_host=" + s.bench_host] + s.facts(leg) + pipeline.configuration_fields(self.env),
            bool_fields=["forced=" + (self.env.get("WK_FORCE") or "")] + s.bool_facts())
        info("%s on %s, from '%s' (%s @%s)" % (leg.plan, s.sysctl("hw.model"), s.ws, leg.cfg.name, s.sha()[:10]))
        return steps


def pick(m, home, want):
    staged = os.path.join(home, "staged")
    names = [want] if want else sorted(m.listdir(staged), reverse=True) if m.isdir(staged) else []
    return next((os.path.join(staged, n) for n in names if m.exists(os.path.join(staged, n, "stage.json"))), None)


def listing(m, home):
    staged, results = os.path.join(home, "staged"), os.path.join(home, "results")
    if not m.isdir(staged):
        log("(nothing staged on %s)" % home)
        return 0
    for d in sorted(m.listdir(staged)):
        try:
            doc = json.loads(m.read(os.path.join(staged, d, "stage.json")))
        except (OSError, ValueError):
            log("  %-34s (incomplete)" % d)
            continue
        log("  %-34s %s %s" % (d, doc.get("config", ""), doc.get("webkit_sha", "")[:10]))
    if m.isdir(results):
        log("")
        log("  results:")
        for r in m.listdir(results):
            log("    " + r)
    return 0


def staged(root, reg, clock, o, popen=None, driver=machine_driver):
    """`wk bench staged`: this install's newest (or --id) stage, run through the pipeline."""
    if not reg.machine.run(["uname", "-s"]).out.startswith("Darwin"):
        die("'wk bench staged' is macOS bench mode. The Linux systems run their benchmark\n"
            "    from the machine that drives them -- wk bench run.")
    install = Install(root, reg.machine, reg.env, driver)
    home = install.staging_root()
    if not home:
        die("this machine has no benchmark volume to read.\n"
            "    In host mode, set WK_BENCH_MACHINE to which fleet machine this is (mbp or\n"
            "    benchvm -- 'wk boot --list') and make sure its volume is attached ('wk boot\n"
            "    mbp --status'); in bench mode /etc/wk-image says which system is running.")
    if o.get("ls"):
        return listing(reg.machine, home)
    d = pick(reg.machine, home, o.get("id") or "")
    if d is None:
        die("nothing staged on this machine's benchmark volume%s.\n    Stage a build from the workspace that built it:\n"
            "        wk bench stage <workspace> --to mbp --config mac-release" % (" under '%s'" % o["id"] if o.get("id") else ""))
    system = MacVolumeSystem(root, reg, clock, install, home, d, o)
    plan = o.get("plan") or (system.manifest.get("plans") or "").split(",")[0]
    if not plan:
        die("which benchmark? --plan <name>\n    (the staged payload does not name one; 'wk bench staged --ls' shows what\n"
            "    is here, and Tools/Scripts/run-benchmark --list-plans what it can run)")
    if o.get("gates"):
        return gates(root, reg, clock, system, plan)
    return StagedRun(root, reg, system, clock, reg.env, popen or subprocess.Popen).go(plan, o)


def gates(root, reg, clock, system, plan):
    expect = system.o.get("expect_display") or (install_display(system) or "")
    leg = StagedRun(root, reg, system, clock, reg.env, None).leg(plan, system.o)
    rows = Gates(root, reg.machine, clock, reg.env, plan, expect, system.build_dir(leg), system.py).ask()
    for name, ok, detail in rows:
        pipeline.Run.check(ok, name, detail)
    fails = [r for r in rows if not r[1]]
    if fails:
        warn("%d gate(s) refuse a run here: nothing should reboot into a leg that would be refused" % len(fails))
        return 1
    info("every gate passes")
    return 0


def install_display(system):
    return (system.install.make_driver(system.root, system.conf).display() or "") if system.conf else ""


class Stage:
    """`wk bench stage <ws> --to <machine>`: the products, Tools/ and each pinned payload, delivered with the manifest last."""

    def __init__(self, root, reg, clock, driver=machine_driver):
        self.root, self.reg, self.clock, self.here, self.env = str(root), reg, clock, reg.machine, reg.env
        self.install = Install(root, reg.machine, reg.env, driver)

    def tools_version(self):
        said = kv(self.here.run([os.path.join(self.root, "cmd", "version")]).out)
        return (said.get("sha") or "unknown") + ("+dirty" if said.get("dirty") == "yes" else "")

    def run(self, words, machine, config, plans):
        if len(words) != 1:
            die("usage: wk bench stage <workspace> --to <machine> [--config C] [--plan P [--payload <dir>]]...; see wk bench -h")
        ws = words[0]
        require_name(ws)
        if not machine:
            die("which machine is going to run it? --to <machine> (wk boot --list)")
        drv = self.install.driver(machine)
        if not hasattr(drv, "bench_root"):
            die("%s has no place to stage a build onto from here.\n    Its other role is reached over the network rather than mounted, "
                "so the\n    payload is pushed to it while it runs rather than copied to it while it\n    does not." % machine)
        home = drv.bench_root()
        if not home:
            die("%s's benchmark disk is not attached, so there is nowhere to\n    stage a build onto. 'wk boot %s --status' says what it can see."
                % (machine, machine))
        deliver = not drv.bench_local()
        try:
            target = self.reg.load(self.reg.ws_target(ws))
        except LookupError as e:
            die(str(e))
        target.wait_ready(ws, self.clock)
        config = config or self.env.get("WK_CONFIG") or self.reg.default_config(ws)
        try:
            cfg = buildconf.resolve(config, target.os(), target.kind, target.env)
        except LookupError:
            die("unknown config '%s' (wk build --list)" % config)
        src = target.src(ws)
        build = cfg.build_dir(src)
        if not target.exec(ws, ["test", "-d", build]).ok:
            die("'%s' has no %s build to stage (%s).\n    Build it first:  wk build %s %s" % (ws, config, build, ws, config))
        for plan, payload in plans:
            if payload and not self.here.isdir(payload):
                die("no such payload directory: %s\n    Nothing has been staged. 'wk bench seed <ws> %s' makes one, and it is read\n"
                    "    on the machine that stages, not the one that asked." % (payload, plan))
        sha = first_line(target.exec(ws, ["git", "-C", src, "rev-parse", "HEAD"]))
        stamp = self.clock.stamp()
        dest = os.path.join(home, "staged", "%s-%s" % (stamp, config))
        vol = drv.c("NODE_VOLUME")
        info("staging %s from '%s' onto %s%s" % (config, ws, machine, " (%s)" % vol if vol else ""))
        assemble = os.path.join(self.reg.store.state_dir(), "bench-stage", os.path.basename(dest)) if deliver else dest
        manifest = {"staged_at": self.clock.iso(), "staged_by": wkrecord.host_name(self.here), "workspace": ws,
                    "workspace_target": target.name, "config": config, "webkit_sha": sha, "plans": ",".join(p for p, _ in plans),
                    "payloads_pinned": ", ".join(p for p, d in plans if d), "machine": machine, "volume": vol,
                    "wk_tools": self.tools_version(), "bench_host": "image"}
        done = False
        with job.Signals():
            try:
                self.assemble(target, ws, build, src, assemble, plans)
                self.publish(drv, machine, assemble, dest, manifest, deliver)
                done = True
            finally:
                if deliver or not done:
                    self.here.remove(assemble)
        self.next_steps(drv, machine, dest, plans)
        return 0

    def assemble(self, target, ws, build, src, into, plans):
        """Products, not the build tree: what the driver launches, what DYLD_FRAMEWORK_PATH resolves, and the dSYMs."""
        self.here.remove(into)
        self.here.mkdir(os.path.join(into, "WebKitBuild"))
        log("  the build product (frameworks, MiniBrowser.app and dSYMs; no intermediates)")
        products = os.path.join(into, "WebKitBuild", os.path.basename(build))
        self.here.mkdir(products)
        target.pull_dir(ws, build, products, exclude=PRODUCT_SKIP)
        log("  Tools/ -- run-benchmark, webkitpy and the plans")
        target.pull_dir(ws, os.path.join(src, "Tools"), os.path.join(into, "Tools"))
        for plan, payload in plans:
            if not payload:
                continue
            log("  the benchmark itself, pinned: %s (%s)" % (plan, os.path.basename(payload)))
            pinned = os.path.join(into, "payload", plan)
            self.here.mkdir(pinned)
            # A working clone can carry git's fsmonitor socket, which no copy reproduces.
            if not self.here.act_run(["rsync", "-a", "--exclude", ".git", payload.rstrip("/") + "/", pinned + "/"]).ok:
                die("could not pin %s from %s" % (plan, payload))

    def publish(self, drv, machine, assemble, dest, manifest, deliver):
        """The manifest crosses last: a delivery gives no order guarantee, and a half-delivered tree must not look finished."""
        text = json.dumps(manifest, indent=2) + "\n"
        if not deliver:
            self.here.write(os.path.join(assemble, "stage.json"), text)
            return
        info("sending it to %s" % machine)
        if drv.bench_put(assemble, dest, *PUT_SKIP):
            die("could not deliver the payload to %s" % machine)
        pending = assemble + ".stage.json"
        self.here.write(pending, text)
        rc = drv.bench_put_file(pending, os.path.join(dest, "stage.json"))
        self.here.remove(pending)
        if rc:
            die("delivered the payload to %s but could not publish its manifest.\n"
                "    Nothing will use it until one is there -- re-run the stage." % machine)

    def next_steps(self, drv, machine, dest, plans):
        first = " --plan " + plans[0][0] if plans else ""
        arming = drv.facts().get("BOOT_ARMING", "")
        if arming == "guest":
            arm, go = ("wk boot %s        start the guest -- for a guest that *is* the transition" % machine,
                       "wk enter %s -- wk bench staged%s" % (drv.facts().get("NODE_GUEST", "<guest>"), first))
        else:
            arm, go = ("wk boot %s        arm the one-shot and reboot into it" % machine,
                       "wk bench staged%s       on the machine, once it is up" % first)
        info("staged: %s" % dest)
        log("")
        log("  next:")
        log("    " + arm)
        log("    " + go)
        log("    wk boot %s --back   leave the role again; the result stays" % machine)
        log("                              on the machine that took it")


def plans(order, names, payloads):
    """[plan, payload] pairs: --plan repeats, each optionally followed by the --payload pinning that benchmark."""
    out, names, payloads = [], iter(names), iter(payloads)
    for opt in order:
        if opt == "--plan":
            out.append([next(names), ""])
            if not re.match(r"^[A-Za-z0-9._][A-Za-z0-9._-]*$", out[-1][0]):
                die("--plan '%s' is not a plan name" % out[-1][0])
        elif opt == "--payload":
            if not out:
                die("--payload names a pinned copy of one benchmark, so it follows the --plan it is a copy of")
            out[-1][1] = next(payloads)
    return out


def stage(root, reg, clock, words, machine, config, pairs, driver=machine_driver):
    return Stage(root, reg, clock, driver).run(words, machine, config, pairs)


HOST_BENCH_TIMEOUT = "2700"   # the first run after a stage is legitimately slow
HOST_BOOT_WAIT = 3600         # somebody choosing the volume at the startup manager
HOST_BACK_WAIT = 600
HOST_POLL = 10
HOST_TOOLS = (TOOLS, "wk-tools")


class MacHostSystem(System):
    """`--system mbp`, the Mac's benchmark volume, driven from wherever this runs: `boot()` stages the build and
    reboots into it, `run()` is `wk bench staged` over the bench-mode ssh alias, `after()` reboots back."""

    kind = "mac-volume"
    bench_host = "image"
    host_os = "macos"

    def __init__(self, root, reg, target, ws, clock, name, conf, channel_factory=None, stage_driver=None):
        super().__init__(root, reg, target, ws, clock)
        self.name, self.conf, self.env = name, dict(conf, NODE_NAME=name), reg.env
        self.pending = None   # stashed by HostRun.leg(): System.boot() itself takes no leg
        self.channel_factory = channel_factory or (lambda conf, env, ch, via, root: Channel(conf, env=env, channel=ch, via=via, root=root))
        self.stage_driver = stage_driver or self.make_driver

    def make_driver(self, root, conf):
        return driver_class(conf.get("NODE_DRIVER", ""))(root, conf, self.channel_factory(conf, self.env, "none", self.here, root))

    def driver(self):
        return self.make_driver(self.root, self.conf)

    def boot_obj(self):
        return bootcli.Boot(self.root, self.conf, self.driver(), env=self.env)

    def bench_channel(self):
        return self.channel_factory(self.conf, self.env, "bench", self.here, self.root)

    def bench_machine(self):
        ch = self.bench_channel()
        dest = ch.dest("i_ssh")
        if not dest:
            die("%s (machines/%s.conf) sets no NODE_BENCH_SSH -- needed to reach its bench-mode install" % (self.name, self.name))
        return self.here if ch.here() else Ssh(dest, via=self.here)

    def cores_refusal(self):
        return "no pin exists on macOS"

    def aslr_prefix(self):
        die("WK_BENCH_ASLR=off: ASLR cannot be turned off on Apple Silicon; the run records the slide it got")

    def has_gpu(self, arch):
        return True

    def headless_reason(self, arch):
        return ""

    def default_browser(self, cfg):
        return "minibrowser"

    def build_present(self, leg):
        return True, "staged fresh during boot"

    def checks(self, leg):   # the real gates run inside the `wk bench staged` this run() invokes there
        return [], []

    def src(self):
        return self.target.src(self.ws)

    def facts(self, leg):
        return ["role=" + self.conf.get("NODE_PROFILE", ""), "system=" + self.conf.get("NODE_PROFILE", ""), "machine=" + self.name]

    def deploy(self, leg):   # already crossed: boot() stages it before the reboot into bench mode
        pass

    def boot(self):
        if self.target.info(self.ws) in ("absent", "unreachable"):
            die("no such workspace: %s ('wk ls' lists them)" % self.ws)
        leg = self.pending
        if leg is None:
            die("internal: %s.boot() reached with no leg pending (HostRun.leg() sets it)" % type(self).__name__)
        Stage(self.root, self.reg, self.clock, driver=self.stage_driver).run([self.ws], self.name, leg.cfg.name, [(leg.plan, leg.payload)])
        self.boot_obj().arm()
        self._wait_for("bench", HOST_BOOT_WAIT)

    def _wait_for(self, want, timeout):
        if act.dry_run():
            log("  would wait up to %ss for %s mode" % (timeout, want))
            return
        driver = self.driver()
        if not self.clock.wait_until(lambda: driver.probe().startswith(want), timeout, HOST_POLL):
            die("gave up after %ss waiting for %s to answer in %s mode" % (timeout, self.name, want))

    def remote_tools(self, m):
        for rel in (self.env.get("WK_MAC_BENCH_TOOLS") or "",) + HOST_TOOLS:
            if not rel:
                continue
            path = rel if rel.startswith("/") else "$HOME/" + rel
            r = m.run(["sh", "-c", "test -x %s/wk && cd %s && pwd" % (path, path)])
            if r.ok and r.out.strip():
                return r.out.strip()
        die("no wk-tools checkout found on %s's benchmark install.\n"
            "    'wk bench staged' runs from it, so the benchmark install needs this repository\n"
            "    (clone it there); WK_MAC_BENCH_TOOLS names it directly." % self.name)

    def bench_argv(self, leg):
        m = self.bench_machine()
        tools = self.remote_tools(m)
        cmd = "cd %s && ./wk bench staged --plan %s --timeout %s" % (
            shlex.quote(tools), shlex.quote(leg.plan), shlex.quote(str(leg.o.get("timeout") or HOST_BENCH_TIMEOUT)))
        if leg.count:
            cmd += " --count %s" % shlex.quote(leg.count)
        if m is self.here:
            return ["bash", "-c", cmd]
        return m.argv(cmd)

    def collect(self, leg):
        if act.dry_run():
            log("  would collect the newest run in %s/results on %s" % (BENCH_ROOT, self.name))
            return
        m = self.bench_machine()
        results = BENCH_ROOT + "/results"
        newest = sorted(m.listdir(results)) if m.isdir(results) else []
        if not newest:
            warn("no result appeared in %s on %s" % (results, self.name))
            return
        remote_dir = results + "/" + newest[-1]
        m.copy_out(remote_dir + "/result.json", os.path.join(leg.out, "result.json"))
        record.write_env(os.path.join(leg.out, "env.json"), ["remote.run=" + newest[-1], "remote.dir=" + remote_dir], update=True)
        log("  collected from %s:%s" % (self.name, remote_dir))

    def after(self, leg):
        try:
            self.boot_obj().back()
        except Refused:
            warn("%s cannot bless itself back to host mode from its own benchmark install -- only the\n"
                 "  host install carries the boot helper. The result is already collected; shut %s down,\n"
                 "  hold the power button until 'Loading startup options', and pick the host install."
                 % (self.name, self.name))
            return []
        self._wait_for("host", HOST_BACK_WAIT)
        return []


class HostRun(pipeline.Run):
    """`--system mbp`'s task record and steps; the measurement itself is `wk bench staged`, over ssh."""

    def leg(self, plan, o):
        leg = super().leg(plan, o)
        self.seed(leg)   # before boot(), which stages leg.payload
        self.system.pending = leg
        return leg

    def seed(self, leg):
        if leg.payload:
            return
        def read(path):
            r = self.target.exec(self.ws, ["cat", "%s/Tools/Scripts/%s" % (self.system.src(), path)])
            return r.out.replace("\r", "") if r.ok else None
        seeder = seed.Seeder(self.here, self.lock, os.path.join(self.reg.store.artifact_dir(), "bench"))
        leg.payload = seeder.seed(leg.plan, seed.plan_json(read, leg.plan))

    def idle_rows(self):
        """The install's own gates judge it quiet; this machine's own load is not the measurement."""
        return []

    def run_jsc(self, leg):
        return self.run_remote(leg)

    def run_browser(self, leg):
        return self.run_remote(leg)

    def run_remote(self, leg):
        argv = self.system.bench_argv(leg)
        path = os.path.join(leg.out, "run.log")
        rc = self.watched(argv, None, path)
        return rc, "wk bench staged (over ssh) exited %d" % rc, path



def rubble(install):
    from wk.rubble import du_kb, remover, row
    try:
        home = install.staging_root()
    except Refused:
        return []
    m, staged = install.here, os.path.join(home or "", "staged")
    if not home or not m.isdir(staged):
        return []
    names = m.listdir(staged)
    done = [n for n in names if m.exists(os.path.join(staged, n, "stage.json"))]
    rows = []
    for n in names:
        if n in done[-1:]:
            continue
        d = os.path.join(staged, n)
        what, flag = ("finished stage %s" % n, "--purge-builds") if n in done else ("stage %s, never finished" % n, "")
        rows.append(row("staged", what, du_kb(m, d), flag, remover(m, d)))
    return rows


AB_PLANS = ("jetstream3", "speedometer3", "motionmark")
AB_CONFIG = "mac-release-pgo"
AB_DEFAULTS = (("count", "2"), ("timeout", "1800"), ("settle", "90"))
DETECT = "0.3"   # nobody is in the room to extend a run, so a Mac's rounds go on until they resolve a third of a per cent
READS = ("preflight", "progress", "status", "collect")
MAC_USAGE = ("usage: wk bench ab --devices <mac> --systems <staged-a>,<staged-b> [--plan P]... [--rounds N] [--max-rounds N]\n"
             "           [--detect PCT] [--count N] [--timeout S] [--settle S] [--a-args ...] [--b-args ...] [--plant] [--rehearse]\n"
             "       wk bench ab --devices <mac> --patch <ref|diff> --workspace <ws> [--base <ref>] [--config C] ...")
BOARD_ONLY = ("release", "builder", "bits", "build_on", "slot", "detach", "task")
SITE = "Library/Python/3.9/lib/python/site-packages"
AGENT = "com.wk.bench-ab"
AUTORUN = BENCH_ROOT + "/wk-tools/lib/wk/bench/autorun.py"
PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>%(agent)s</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>%(autorun)s</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>%(root)s/autorun.agent.log</string>
  <key>StandardErrorPath</key><string>%(root)s/autorun.agent.log</string>
  <key>EnvironmentVariables</key>
  <dict><key>WK_AB_ROOT</key><string>%(root)s</string></dict>
</dict>
</plist>
"""   # RunAtLoad alone: KeepAlive would restart a finished benchmark, and the agent removes itself when its job is done
DOWN_WAIT, DOWN_POLL = 150, 5
BOOT_SETTLE, BOOT_POLL, BOOT_WAIT = 45, 20, 600   # for its first seconds a restarting machine still answers
PGO_INSTR = "-instr"
PGO_STATE = ".local/state/wk/pgo"
CHECKOUT = "git -C %s checkout -q %s"


def q(*words):
    return " ".join(shlex.quote(str(w)) for w in words)


def stripped(r):
    return r.__class__(r.rc, r.out.replace("\r", ""), r.err)


class OnMac(Script):
    """A bench/onboard/mac-* file: the Mac A/B's side of the measured install, read verbatim."""

    where = os.path.join("bench", "onboard")


class Remote:
    """The measured install, over whichever channel its driver's probe answered on; each command a named file."""

    def __init__(self, root, driver):
        self.root, self.d = str(root), driver

    def call(self, script, mutates=False):
        return stripped(self.d.ch.call("r_ssh", script, mutates=mutates))

    def run(self, name, mutates=False, **params):
        return self.call(OnMac(self.root, name, **params), mutates=mutates)

    def out(self, name, **params):
        r = self.run(name, **params)
        return r.out.strip() if r.ok else ""

    def test(self, flag, path):
        return self.call(Script(self.root, "mac-test.sh", WK_TEST=flag, WK_PATH=path)).ok

    def read(self, path):
        return self.call(Script(self.root, "mac-read.sh", WK_PATH=path)).out

    def py(self, rel, *args):
        """A tree file run by the install's python3, as a parameter: a guest's channel carries no stdin."""
        with open(os.path.join(self.root, rel)) as f:
            params = dict(("WK_%d" % (i + 1), a) for i, a in enumerate(args))
            return self.out("mac-py.sh", WK_PY=f.read(), **params)


def display_verdict(text, want):
    """(ok, detail): one ONLINE display, and it the declared kind; a second panel changes what MotionMark draws."""
    try:
        doc = json.loads(text)
    except ValueError:
        return False, ("wkmac.py displays did not print JSON" if text
                       else "'wkmac.py displays' answered nothing -- CoreGraphics could not be asked")

    def kind(d):
        return "builtin" if d.get("builtin") else "external"

    def one(d):
        p = (d.get("points") or []) + ["?", "?"]
        return "%s %sx%s" % (kind(d), p[0], p[1])

    on = [d for d in doc.get("displays") or [] if d.get("online")]
    shown = ", ".join(one(d) for d in on) or "none"
    if len(on) != 1:
        return False, "%d online display(s): %s" % (len(on), shown)
    if kind(on[0]) != want:
        return False, "the one online display is not the %s panel this machine declares (%s)" % (want, shown)
    return True, "%s alone, as the install that answers here reads it" % shown


class MacAB:
    """`wk bench ab --devices <mac>`: no session this side survives the reboot into the benchmark install, so the job
    is planted on it while it is merely mounted, and a LaunchAgent starts it at autologin."""

    def __init__(self, root, reg, clock, spec, o, driver=machine_driver):
        self.root, self.reg, self.clock, self.spec, self.o = str(root), reg, clock, spec or "", dict(o)
        self.here, self.env, self.make_driver = reg.machine, reg.env, driver
        self.name = self.o.get("devices") or ""
        self.lock = Lock(reg.store, self.here, clock)
        self.bench_dir = reg.store.bench_dir()
        self.a = self.b = self.fw_detail = self.task = self.taskdir = ""
        self._mgr = self._tools = None

    def check(self):
        o = self.o
        if self.spec:
            die("a Mac's arms are staged builds, not a change resolved in the mirror: drop '%s'.\n%s" % (self.spec, MAC_USAGE))
        given = [k for k in BOARD_ONLY if o.get(k)]
        if given:
            die("--%s is a board A/B's; a Mac A/B is planted on its benchmark install and runs by itself" % given[0].replace("_", "-"))
        self.rounds, self.plans = ab.check_plan(o, AB_PLANS)
        top, detect = board_ab.stopping(o, self.rounds, DETECT)
        o["max_rounds"], o["detect"] = str(top), "%g" % detect
        for key, default in AB_DEFAULTS:
            o[key] = o.get(key) or default
        for key in ("count", "settle"):
            if not o[key].isdigit():
                die("--%s takes a number (got '%s')" % (key, o[key]))
        self.config = o.get("config") or AB_CONFIG
        if o.get("systems"):
            if o.get("patch") or o.get("workspace") or o.get("base"):
                die("--systems names two builds already staged; --patch, --workspace and --base build them. One or the other.")
            self.a, self.b = board_ab.pair(o["systems"], "systems")
        elif o.get("patch"):
            if not o.get("workspace"):
                die("--patch builds both arms in a workspace: --workspace <ws> (a macOS one, 'wk ls')")
            require_name(o["workspace"])
        else:
            die(MAC_USAGE)
        self.ws = o.get("workspace") or ""

    def resolve(self):
        conf = fleet.Fleet(self.root, self.env).load(self.name)
        if not conf:
            die("unknown machine '%s' (wk boot --list)" % self.name)
        self.conf = dict(conf, NODE_NAME=self.name)
        self.d = self.make_driver(self.root, self.conf)
        self.d.probe()
        self.guest = self.d.arming == "guest"
        self.mac = Remote(self.root, self.d)

    # -- what preflight reads
    def firmware_is_bench(self):
        """The firmware's own default has to be the bench volume, or the restart below needs a human."""
        grp = self.mac.py(WKMAC, "boot-volume").rsplit(":", 1)[-1]
        if not grp:
            self.fw_detail = "the firmware publishes no boot-volume, so what a restart enters cannot be read"
            return False
        bench = self.mac.py(WKMAC, "volume-group", self.d.volume())
        host = self.mac.py(WKMAC, "volume-group", "/")
        if bench and grp == bench:
            self.fw_detail = "%s = '%s', so the restart below needs no human" % (grp, self.d.c("NODE_VOLUME"))
            return True
        self.fw_detail = ("%s = the host install, so a restart comes back here and the A/B never runs" % grp if host and grp == host
                          else "%s matches neither install on this disk" % grp)
        return False

    def display_check(self):
        want = (self.d.display() or "builtin").split()[0]
        return display_verdict(self.mac.py(WKMAC, "displays"), want)

    def firstboot_log(self, root):
        return os.path.dirname(root) + "/log/wk-bench-firstboot.log"

    def provisioned(self, root):
        """The volume's first boot logs its completion line last and then deletes itself, so the log is the record."""
        return "provisioning complete" in self.mac.read(self.firstboot_log(root))

    def staged_ids(self, root):
        return sorted(self.mac.out("mac-ls.sh", WK_PATH=root + "/staged").split())

    def preflight(self):
        """Every check is something that, if wrong, is discovered after the reboot on a machine nobody can reach."""
        info("preflight for an unattended A/B on %s" % self.name)
        fails = []

        def ck(ok, what, detail, *remedy):
            pipeline.Run.check(ok, what, detail)
            if not ok:
                fails.append(what)
                for line in remedy:
                    log("       " + line)

        mode, n = self.d.mode, self.name
        if mode == "unreachable":
            ck(False, "reachable", "%s does not answer ssh with a key, on its host node or its benchmark install's" % n)
            log("  everything below needs the machine, so nothing else was checked.")
            return len(fails)
        bench = mode.startswith("bench")
        if self.guest:
            ck(bench, "a benchmark install", "%s answers and is marked (%s)" % (n, mode[6:]) if bench else
               "%s answers but carries no /etc/wk-image, so every leg would be refused" % n)
        else:
            ck(not bench, "host mode", "%s answers and carries no bench marker" % n if not bench else
               "%s is in BENCH mode (%s) -- the arms and the tools are on the host install, so a plant needs it" % (n, mode[6:]))
        root = self.d.bench_root()
        ck(bool(root), "staging root", root or "%s's driver can see no staging root" % n)
        if not root:
            return len(fails)
        if not self.guest:
            done = self.provisioned(root)
            ck(done, "provisioned", "'%s' has finished a first boot" % self.d.c("NODE_VOLUME") if done
               else "no 'provisioning complete' in %s" % self.firstboot_log(root),
               "so the desktop was never quieted, and every leg is refused after the reboot. On the Mac:",
               "  wk sysimage build %s --repair    then boot it once" % (self.d.c("NODE_PROFILE") or "<profile>"))
        ck(self.mac.test("-w", root), "writable", "%s takes a plant without sudo" % root)
        bh = self.d.bench_home() or ""
        ck(bool(bh) and self.mac.test("-d", bh) and self.mac.test("-w", bh + "/Library"), "bench home",
           "%s (LaunchAgents installable without sudo)" % bh if bh else "the driver names no bench home")
        alu = self.mac.out("mac-defaults.sh", WK_PATH=bh + "/../../Library/Preferences/com.apple.loginwindow", WK_KEY="autoLoginUser")
        ck(alu == "bench", "autologin", "the bench account logs in at the console" if alu == "bench" else
           "autoLoginUser is '%s' -- the run would have no session" % (alu or "unset"))
        ck(self.mac.test("-d", "%s/%s/objc" % (bh, SITE)), "pyobjc over there", "run-benchmark's prepare_env does a bare 'import objc'")
        if not self.mac.test("-d", "%s/%s/scipy" % (bh, SITE)):
            log("  note scipy is not on the bench install; the plant installs it (needs this machine's network)")
        if self.mac.test("-f", bh + "/../../Library/LaunchDaemons/com.wk.bench-firstboot.plist"):
            log("  note the first-boot daemon is still installed; the autorun stands aside until it has provisioned")
        staged = self.staged_ids(root)
        ck(bool(staged or self.o.get("patch")), "staged builds", " ".join(staged) or
           ("none yet; --patch stages both arms" if self.o.get("patch") else "nothing on the volume, and no --patch to build from"))
        ok, said = self.display_check()
        ck(ok, "one display", said, "Disconnect it: a second panel changes the compositing, the refresh rate and which",
           "GPU the window lands on, and MotionMark scores the area it draws. No --force crosses it.")
        if self.guest:
            ck(True, "enters bench mode", "starting the guest is the transition")
        else:
            ck(self.firmware_is_bench(), "firmware default", self.fw_detail,
               "This restarts %s and expects benchmarking to begin with nobody at the keyboard:" % n,
               "  wk boot %s            arms it; or the startup manager: hold the power button," % n,
               "  pick '%s'. --plant leaves the job on the volume and reboots nothing." % self.d.c("NODE_VOLUME"))
        ready = self.d.restart_ready()
        ck(ready, "restartable", "this restarts %s itself" % n if ready else self.d.restart_detail(),
           "A graceful restart is refusable by any application, so an unattended one uses the helper:",
           "  wk machine setup %s    (one password prompt there). Crossing this leaves the job planted and" % n,
           "  correct: reboot %s by any means and the A/B runs by itself." % n)
        log("")
        if fails:
            warn("%d preflight check(s) failed" % len(fails))
        else:
            info("preflight clean")
        return len(fails)

    # -- the machine that builds and stages the arms
    def manager(self):
        if self._mgr is None:
            self._mgr = self.d.manager()
            self._tools = self.d.manager_tools(self._mgr)
            if not self._tools:
                die("no wk-tools on %s's host install, so nothing there can build or stage an arm. One command puts\n"
                    "    it there, with the privileged helpers:  wk machine setup %s" % (self.name, self.name))
        return self._mgr

    def rwk(self, *words, logged=""):
        m = self.manager()
        argv = ["sh", "-c", "cd %s && ./wk %s" % (shlex.quote(self._tools), q(*words))]
        if not logged:
            return m.run(argv)
        local = argv if m is self.here or not isinstance(m, Ssh) else m.argv(q(*argv))
        log("  %s   (log: %s)" % (" ".join(("wk",) + words), logged))
        return self.here.act_run(["sh", "-c", sched.LOGGED, "sh", logged] + local)

    def guest_sh(self, script, mutates=False):
        """Written to a file and then run: on stdin a script is truncated by the first thing in it that reads stdin."""
        inner = "printf %%s %s | base64 -d > /tmp/wk-guest.sh && bash /tmp/wk-guest.sh" % base64.b64encode(script.encode()).decode()
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "wk-" + self.ws, inner]
        m = self.manager()
        return stripped(m.act_run(argv) if mutates else m.run(argv))

    def guest_src(self):
        src = (self.guest_sh('for p in ~/WebKit ~/webkit; do [ -d "$p/.git" ] && { echo "$p"; exit 0; }; done').out.split() or [""])[0]
        if not src:
            die("no WebKit checkout found in the build guest '%s'" % self.ws)
        return src

    def stage_plans(self):
        words, m = [], self.manager()
        for p in self.plans:
            said = self.rwk("bench", "seed", self.ws, p).out.replace("\r", "").split()
            payload = said[-1] if said else ""
            if payload.startswith("/") and m.isdir(payload):
                log("  payload pinned: %s -> %s" % (p, payload))
                words += ["--plan", p, "--payload", payload]
            elif self.o.get("allow_network_fetch"):
                warn("  %s is not pinned, and --allow-network-fetch was given: that leg clones the benchmark itself, so the\n"
                     "  benchmark install needs a network and the two arms could get different revisions of it." % p)
                words += ["--plan", p]
            else:
                die("the %s payload could not be pinned, so nothing may be staged: that leg would clone the benchmark\n"
                    "    over a network the benchmark install may not have, and fail after the reboot where nothing can say so.\n"
                    "    Pin it here:  wk bench seed %s %s    then re-run; its error is the thing to fix.\n"
                    "    To go ahead anyway (a benchmark install with a route out):  --allow-network-fetch" % (p, self.ws, p))
        return words

    def build_and_stage(self, label, slug):
        root = self.d.bench_root()
        before = set(self.staged_ids(root))
        info("  building %s" % label)
        path = os.path.join(self.taskdir, "build-%s.log" % slug)
        if not self.rwk("build", self.ws, self.config, logged=path).ok:
            die("the %s build failed; its log is %s" % (label, path))
        plans = self.stage_plans()
        info("  staging %s" % label)
        if not self.rwk("bench", "stage", self.ws, "--to", self.name, "--config", self.config, *plans,
                        logged=os.path.join(self.taskdir, "stage-%s.log" % slug)).ok:
            die("staging %s failed" % label)
        new = sorted(set(self.staged_ids(root)) - before)
        if not new:
            die("staging %s produced no new directory on %s" % (label, self.d.c("NODE_VOLUME") or self.name))
        log("  %s staged as %s" % (label, new[-1]))
        self.reclaim(label)
        return new[-1]

    def reclaim(self, label):
        """A profile-guided arm leaves ~100 GB of products in the guest, and once it is staged both trees are spent."""
        measured = buildconf.resolve(self.config, "macos", "vm", self.env).build_dir(self.guest_src())
        dirs = q(measured, measured + PGO_INSTR)
        said = self.guest_sh("du -sk %s 2>/dev/null | awk '{s+=$1} END {print int(s/1048576)}'\nrm -rf %s\n"
                             "df -g / | awk 'NR==2 {print $4}'" % (dirs, dirs), mutates=True).out.split() + ["?", "?"]
        log("  reclaimed %s's products (%s GB); %s GB free in the guest now" % (label, said[0], said[1]))

    def build_ab(self):
        base, patch = self.o.get("base") or "", self.o["patch"]
        if act.dry_run():
            log("  would build the baseline (%s) in '%s' and stage it" % (base or "current HEAD", self.ws))
            log("  would then apply '%s' and stage that as the second arm" % patch)
            self.a, self.b = "<baseline %s>" % (base or "HEAD"), "<patched %s>" % patch
            return
        self.rwk("start", self.ws)
        src = self.guest_src()
        orig = (self.guest_sh("git -C %s symbolic-ref --quiet --short HEAD 2>/dev/null || git -C %s rev-parse HEAD" % (q(src), q(src)))
                .out.split() or [""])[0]
        if not orig:
            die("could not read the guest checkout's current ref")
        log("  checkout %s; '%s' is restored when done" % (src, orig))
        base = base or orig
        if not self.guest_sh("set -e; " + CHECKOUT % (q(src), q(base)), mutates=True).ok:
            die("could not check out the baseline '%s' in the guest" % base)
        self.a = self.build_and_stage("baseline (%s)" % base, "a")
        if os.path.isfile(patch):
            with open(patch, "rb") as f:
                diff = base64.b64encode(f.read()).decode()
            step = "set -e; printf %%s %s | base64 -d > /tmp/wk-ab.patch; git -C %s apply --index /tmp/wk-ab.patch" % (diff, q(src))
            why = "the patch did not apply cleanly to '%s'" % base
        else:
            step, why = "set -e; " + CHECKOUT % (q(src), q(patch)), "no such ref '%s' in the guest checkout" % patch
        restore = CHECKOUT % (q(src), "-f " + q(orig))
        if not self.guest_sh(step, mutates=True).ok:
            self.guest_sh(restore, mutates=True)
            die(why + "; the tree has been put back")
        self.b = self.build_and_stage("patched (%s)" % patch, "b")
        if not self.guest_sh(restore, mutates=True).ok:
            warn("  could not restore '%s' in the guest -- the tree is left on the patched ref" % orig)
        info("  stopping the build guest")   # a running macOS VM competes for CPU with what runs next
        self.rwk("stop", self.ws)
        info("arms: A=%s  B=%s" % (self.a, self.b))

    # -- the plant
    def job(self, declared, stamp):
        o = self.o
        return {"plans": list(self.plans), "rounds": self.rounds, "max_rounds": int(o["max_rounds"]), "detect_pct": float(o["detect"]),
                "timeout": int(o["timeout"]), "count": o["count"], "display": declared, "settle": int(o["settle"]), "n_arms": 2,
                "arms": [{"label": "A", "id": self.a, "browser_args": o.get("a_args") or ""},
                         {"label": "B", "id": self.b, "browser_args": o.get("b_args") or ""}],
                "wk_tools": BENCH_ROOT + "/wk-tools", "created_at": self.clock.iso(), "created_by": wkrecord.host_name(self.here),
                "stamp": stamp, "aslr": self.env.get("WK_BENCH_ASLR", ""), "env_pad": self.env.get("WK_BENCH_ENV_PAD", ""),
                "path_pad": self.env.get("WK_BENCH_PATH_PAD", ""), "shared_cache": self.env.get("WK_BENCH_SHARED_CACHE", ""),
                "rehearsal": "1" if o.get("rehearse") else ""}

    def command(self):
        words = ["wk", "bench", "ab", "--devices", self.name, "--systems", "%s,%s" % (self.a, self.b), "--rounds", str(self.rounds)]
        for key in ("max_rounds", "detect", "count", "timeout", "settle"):
            words += ["--" + key.replace("_", "-"), self.o[key]]
        return " ".join(words + [w for p in self.plans for w in ("--plan", p)])

    def create_task(self, stamp):
        """Recorded before the Mac is touched, where `wk status` lists it, so a killed run still names what was asked."""
        self.task = "%s-%s-mac-ab" % (stamp, self.name)
        self.taskdir = os.path.join(self.bench_dir, self.task)
        if act.dry_run():
            return
        if os.path.exists(self.taskdir):
            die("task %s already exists (%s); a task is one request, made once" % (self.task, self.taskdir))
        self.lock.hold("bench-task-" + self.task, timeout=5)
        slots = [self.a or "baseline %s" % (self.o.get("base") or "HEAD"), self.b or "patched %s" % self.o.get("patch")]
        record.task_write(self.taskdir, ["task=" + self.task, "requested=" + self.clock.iso(), "devices=%s=%s" % (self.name, self.config),
                                         "plans=" + ",".join(self.plans), "rounds=%d" % self.rounds, "slots=" + ",".join(slots)],
                          [self.command()])

    def put_file(self, src, dest):
        """The driver delivers and what landed is judged here: a transport that wrote nothing still exits 0."""
        if self.d.bench_put_file(src, dest):
            return False
        want = self.here.run(["sh", "-c", 'wc -c < "$1"', "sh", src]).out.strip()
        got = self.mac.out("mac-size.sh", WK_PATH=dest)
        if not want or want != got:
            warn("put_file: %s is %s bytes, expected %s" % (dest, got or "unreadable", want or "unreadable"))
        return bool(want) and want == got

    def put_tree(self, src, dest):
        """Every file verified, not a sentinel: a tree stale in one file behaves as an older lane, after the reboot."""
        if self.d.bench_put(src, dest, *PUT_SKIP):
            return False
        ex = [w for x in PUT_SKIP for w in ("--exclude", x)]
        want = first_line(self.here.run(["python3", os.path.join(self.root, "lib", "treehash.py"), src] + ex))
        got = (self.mac.py("lib/treehash.py", dest, *ex).splitlines() or [""])[-1]
        if not want or not got:
            warn("put_tree: could not digest %s (%s) or %s (%s), so what landed is unknown" % (src, want, dest, got))
            return False
        if want != got:
            warn("put_tree: %s hashes %s, this tree hashes %s -- what landed is not this tree" % (dest, got, want))
            return False
        log("  verified: %s is this tree file for file (%s)" % (dest, want[:16]))
        return True

    def check_arms(self, root):
        staged = self.staged_ids(root)
        for arm in (self.a, self.b):
            if arm not in staged:
                die("no staged build '%s' on %s. There is:\n%s" % (arm, self.name, "\n".join("    " + s for s in staged) or "    nothing"))

    def show(self):
        o = self.o
        if float(o["detect"]) == 0:
            info("plan: %s, exactly %d round(s), interleaved; no precision target" % (" ".join(self.plans), self.rounds))
        else:
            info("plan: %s, %d-%s round(s), interleaved, until it resolves %s%%" % (" ".join(self.plans), self.rounds, o["max_rounds"], o["detect"]))
        if o.get("rehearse"):
            warn("  --rehearse: every leg is forced past its own preflight and every number recorded as forced. This\n"
                 "  rehearses the path; it measures nothing comparable with a clean run.")
        log("  arm A: %s%s" % (self.a or "built from %s" % (o.get("base") or "HEAD"), "  args: " + o["a_args"] if o.get("a_args") else ""))
        log("  arm B: %s%s" % (self.b or "built with %s" % o.get("patch"), "  args: " + o["b_args"] if o.get("b_args") else ""))
        for i, p in enumerate(self.plans):
            legs = (2 if i == 0 else 0) + 2 * self.rounds   # the warmup round runs the first plan, one leg per arm
            seen = ab.leg_seconds(self.bench_dir, self.name, p, o["count"])
            each = statistics.median(seen) if seen else None
            log("  cost  %s: at least %d legs%s" % (p, legs, " x ~%s = ~%s" % (ab.duration(each), ab.duration(legs * each)) if seen
                                                  else "; no leg of it at --count %s measured on %s yet" % (o["count"], self.name)))

    def plant(self):
        declared = self.d.display()
        if not declared:
            die("%s declares no display, so nothing here knows what the measured install must read.\n"
                "    Add its own mode to machines/%s.conf, in points, kind first:\n        NODE_DISPLAY=\"builtin 1470x956\"\n"
                "    ('python3 lib/wkmac.py displays' on that install prints both.)" % (self.name, self.name))
        root, bh = self.d.bench_root(), self.d.bench_home()
        if not root or not bh:
            die("nothing on %s is readable right now: it answers on neither node, or its volume is not attached.\n"
                "    'wk boot %s --status' says which." % (self.name, self.name))
        if not self.o.get("patch"):
            self.check_arms(root)
        stamp = self.task.split("-", 1)[0]
        if act.dry_run():
            for line in ("sync wk-tools to %s/wk-tools" % root, "point the launch agent at %s" % AUTORUN,
                         "plant samply for the warmup round's profile", "record the task %s in %s and write its job.json" % (self.task, self.bench_dir),
                         "copy that job to %s/job.json and reset %s/autorun.state" % (root, root),
                         "turn Do Not Disturb on for the bench account and read it back",
                         "install %s/Library/LaunchAgents/%s.plist" % (bh, AGENT),
                         "on that boot hold %s, dim the panel, check the browser, then measure and power off" % declared):
                log("  would " + line)
            return
        info("  syncing wk-tools onto the volume")   # /var/wk: the first-boot daemon rsyncs over ~bench's own checkout
        if not self.put_tree(self.root, root + "/wk-tools"):
            die("could not sync wk-tools onto the bench volume")
        site = "%s/%s" % (bh, SITE)
        if not self.mac.test("-d", site + "/scipy"):
            info("  installing scipy into the bench account's site-packages")
            if not self.mac.run("mac-scipy.sh", mutates=True, WK_PATH=site).ok:
                warn("  scipy did not install; the A/B will be compared from host mode instead")
        self.quiet_account(bh, root)
        self.plant_samply(root)
        self.plant_tailnet(root)
        info("  installing the autorun")
        if not self.mac.test("-r", root + "/wk-tools/lib/wk/bench/autorun.py"):
            die("the planted tree carries no lib/wk/bench/autorun.py, so the launch agent has nothing to start.")
        info("  writing the job")
        job_json = os.path.join(self.taskdir, "job.json")
        self.here.write(job_json, json.dumps(self.job(declared, stamp), indent=2) + "\n")
        if not self.put_file(job_json, root + "/job.json"):
            die("could not write the job onto the volume")
        state = os.path.join(self.taskdir, "planted.state")   # reset here and nowhere else: the autorun only advances it
        self.here.write(state, "phase=planted\njob_stamp=%s\nattempts=0\nplanted_at=%s\n" % (stamp, self.clock.iso()))
        if not self.put_file(state, root + "/autorun.state"):
            die("could not reset the autorun's state on the volume")
        self.mac.run("mac-mkdir.sh", mutates=True, WK_PATH="%s/ab/%s" % (root, stamp))
        info("  installing the launch agent")
        agent = os.path.join(self.taskdir, AGENT + ".plist")
        self.here.write(agent, PLIST % {"agent": AGENT, "autorun": AUTORUN, "root": BENCH_ROOT})
        if not self.put_file(agent, "%s/Library/LaunchAgents/%s.plist" % (bh, AGENT)):
            die("could not install the launch agent")
        info("planted: %s" % stamp)
        log("  task   %s   ('wk status' lists it; 'wk bench report %s' reads it)" % (self.taskdir, self.task))
        log("  log    %s/autorun.log   ('wk bench ab --devices %s --status' tails it, in either mode)" % (root, self.name))

    def quiet_account(self, bh, root):
        """A lock mid-run and a banner over the browser are both invisible to every later gate, so both are refused here."""
        force = bool(self.env.get("WK_FORCE"))
        uuid = next((l.split('"')[3] for l in self.mac.out("mac-platform.sh").splitlines()
                     if "IOPlatformUUID" in l and l.count('"') >= 4), "")
        ss = "%s/Library/Preferences/ByHost/com.apple.screensaver.%s" % (bh, uuid)
        said = self.mac.run("mac-screensaver.sh", mutates=True, WK_BYHOST=bh + "/Library/Preferences/ByHost", WK_SAVER=ss,
                            WK_PREFS=bh + "/Library/Preferences/com.apple.screensaver")
        idle = (said.out.split() or [""])[-1] if said.ok else ""
        if idle == "0":
            log("  screen lock: screensaver disabled on the volume (idleTime=0, verified)")
        elif force:
            warn("  screen lock: idleTime reads '%s'; --force given, so planting anyway: the screen may lock during a run" % (idle or "unreadable"))
        else:
            die("could not disable the screensaver on %s -- idleTime reads '%s'. A benchmark makes no input, so the idle\n"
                "    timer runs as on an abandoned machine and the lock behind it ends a run in silence. Nothing has been\n"
                "    rebooted. To plant anyway:  --force" % (self.name, idle or "unreadable"))
        dnd = (self.mac.run("mac-dnd.sh", mutates=True, WK_TOOLS=root + "/wk-tools", WK_HOME=bh).out.split() or [""])[-1]
        if dnd == "on":
            log("  notifications: Do Not Disturb on for the bench account (verified)")
        elif force:
            warn("  notifications: Do Not Disturb reads '%s'; --force given, so a banner may be drawn over a measured browser" % (dnd or "unreadable"))
        else:
            die("could not turn Do Not Disturb on for the bench account -- it reads '%s'. A banner is drawn over the\n"
                "    browser and no gate downstream sees one. Nothing has been rebooted. To plant anyway:  --force" % (dnd or "unreadable"))

    def plant_samply(self, root):
        """No network over there, so the warmup round's profiler goes in now, where samply_fetch will look for it."""
        arch = self.mac.out("mac-arch.sh")
        fn = ('. "%s/lib/profiler.sh"; f() { t=$(samply_triple "$1" Darwin) && p=$(samply_fetch "$1" Darwin) && '
              'printf "%%s\\n%%s\\n%%s\\n" "$SAMPLY_VER" "$t" "$p"; }; f' % self.root)
        said = self.here.run(shell.argv(self.root, fn, arch)).out.split()
        if len(said) != 3:
            warn("  no samply for %s here -- the warmup round will carry no profile" % (arch or "that machine"))
            return
        ver, triple, path = said
        dest = "%s/cache/samply/%s-%s/samply" % (root, ver, triple)
        if self.put_file(path, dest) and self.mac.run("mac-executable.sh", mutates=True, WK_PATH=dest).ok:
            log("  samply %s planted for the warmup round" % ver)
        else:
            warn("  could not plant samply -- the warmup round will carry no profile")

    def plant_tailnet(self, root):
        """The half that needs a network, Go and this machine's auth key; the install half runs over there at boot."""
        from wk.sysimage.mactailnet import Tailnet
        info("  collecting the tailnet payload")
        try:
            got = Tailnet(self.here, self.env).collect(self.name, os.path.join(self.reg.store.state_dir(), "mac-tailnet", "collected"))
        except Refused:
            got = ""
        if not got or not self.here.isdir(got):
            warn("  tailnet: nothing collected (needs 'wk key set tailnet' and a network here), so that install cannot be\n"
                 "  watched while it measures. The A/B still runs.")
        elif self.put_tree(got, root + "/tailnet"):
            log("  tailnet: payload on the volume; that install joins the tailnet on its next boot")
        else:
            warn("  tailnet: the payload did not land, so that install stays unreachable while it measures")

    # -- the restart, and which install came up
    def restart(self):
        """The display is asked again seconds before the transition: a monitor plugged in since preflight costs a cycle."""
        ok, said = self.display_check()
        if not ok:
            die("the display on %s does not read as one panel of the declared kind: %s\n"
                "    Nothing has been rebooted, and the job stays planted. Disconnect the monitor and re-run, or reboot\n"
                "    %s by hand once it reads right -- the planted job runs by itself either way." % (self.name, said, self.name))
        self.boot_before = self.d.boot_id()
        info("go: reboot %s now (boot before: %s)" % (self.name, self.boot_before or "unknown"))
        if not self.guest:
            log("  '%s' is the firmware default (preflight asserted it), so this restart enters bench mode by itself and\n"
                "  nobody has to be at the keyboard." % self.d.c("NODE_VOLUME"))
        self.d.reboot()
        if not self.clock.wait_until(lambda: not self.mac.test("-d", "/"), DOWN_WAIT, DOWN_POLL):
            die("could not reboot %s -- it is still answering. The helper exits 0 without acting when the reboot is\n"
                "    refused, so this is checked rather than trusted. Nothing is lost: reboot it by hand, or from the\n"
                "    startup manager, and the planted job runs." % self.name)
        info("  %s is going down" % self.name)
        if self.guest:
            self.d.arm()   # a guest's reboot is its stop, and starting it again is the transition

    def wait(self):
        """Both nodes, every poll: the benchmark install answers as its own while it measures."""
        info("wait: up to %d minutes for %s to answer on either node" % (self.boot_wait // 60, self.name))
        self.clock.sleep(BOOT_SETTLE)
        start, said = self.clock.monotonic(), False
        while True:
            mode = self.d.probe()
            if mode.startswith("bench"):
                info("  %s answers in BENCH mode (%s)" % (self.name, mode[6:]))
                return "bench"
            if mode == "host":
                now = self.d.boot_id()   # asked only here: in bench mode it is another install's boot time
                if self.boot_before and now == self.boot_before:
                    warn("  %s is answering on the SAME boot (%s) -- it never rebooted" % (self.name, now))
                    return "noreboot"
                info("  %s is back in HOST mode" % self.name)
                return "host"
            if self.clock.monotonic() - start >= self.boot_wait:
                warn("  %s has answered on neither node in %ds" % (self.name, self.boot_wait))
                return "silent"
            if not said:
                log("  no answer on either node yet -- this is the reboot itself")
                said = True
            self.clock.sleep(BOOT_POLL)

    def notify(self, headline, detail):
        """A notification that did not go out must never cost a measurement."""
        if not notify.send(self.root, headline, detail, "mac-ab", env=self.env, machine=self.here):
            warn("  could not send the notification '%s'" % headline)

    def outcome(self, came):
        n, vol = self.name, self.d.c("NODE_VOLUME") or "the benchmark install"
        if came == "bench":
            info("%s answers in BENCH mode -- the A/B is running there. 'wk bench ab --devices %s --status' follows it,\n"
                 "  and the machine powers itself off when the job ends." % (n, n))
        elif came == "host":
            warn("%s rebooted and came back in HOST mode, so the A/B has not run: the firmware default is not '%s'\n"
                 "  after all. The job is planted and still valid:  wk boot %s   arms it." % (n, vol, n))
            self.notify("mac-ab: %s came back to host mode" % n, "the A/B has not run -- the reboot did not enter '%s'. The job is "
                        "planted and still valid; 'wk boot %s' arms the firmware." % (vol, n))
        elif came == "noreboot":
            warn("%s never rebooted, so the A/B has not run. The job is planted and still valid: reboot it by any means\n"
                 "  (the startup manager works too) and it runs by itself; 'wk bench ab --devices %s --collect' reads it after." % (n, n))
            self.notify("mac-ab: %s never rebooted" % n, "the A/B has not run. The job is planted and still valid: reboot %s by "
                        "any means, including the startup manager, and it runs by itself." % n)
        else:
            info("%s answers on neither node. A measuring install answers as its own, so this is not the run: it is still\n"
                 "  restarting, it halted, or its join did not come up. Nothing is lost -- each leg is written to the volume\n"
                 "  as it ends:  wk bench ab --devices %s --status   once one answers;  --collect   reads it." % (n, n))

    def go(self):
        self.check()
        self.resolve()
        self.boot_wait = int(self.env.get("WK_MAC_BOOT_WAIT") or BOOT_WAIT)
        if not self.guest and self.d.ch.here():
            die("this reboots %s, so it cannot be driven from %s -- the reboot would take the driver with it.\n"
                "    Run it from another machine." % (self.name, self.name))
        if self.preflight():
            if act.dry_run():
                warn("preflight failed; showing the plan anyway because this is --dry-run")
            else:
                act.barrier("the preflight on %s failed, and nothing there has been changed yet. Each failure is something\n"
                            "    a run discovers after the reboot, in bench mode, where nothing can report it." % self.name)
        self.show()
        plant_only = bool(self.o.get("plant"))
        if not act.confirm("plant this A/B on %s%s?" % (self.name, "" if plant_only else " and restart it into its benchmark install")):
            die("not run")
        self.create_task(self.clock.stamp())
        try:
            if self.o.get("patch"):
                self.build_ab()
            self.plant()
            if act.dry_run():
                if not plant_only:
                    log("  would re-check that the declared panel is the only display, then reboot %s through its boot helper" % self.name)
                info("dry run -- nothing on %s was changed and nothing was rebooted" % self.name)
                log("  not checked here: whether a leg would pass on the benchmark install. That gate reads the running\n"
                    "  system, and this one is not running. In bench mode, ask it:  wk bench staged --gates --plan %s" % self.plans[0])
                return 0
            if plant_only:
                info("planted and not started. The A/B runs the next time the benchmark install boots.")
                return 0
            self.restart()
            self.notify("mac-ab planted on %s" % self.name, "%s, %d-%s rounds, count %s. Arms %s / %s. %s has gone down to measure; "
                        "then 'wk bench ab --devices %s --collect'." % (" ".join(self.plans), self.rounds, self.o["max_rounds"], self.o["count"],
                                                                 self.a, self.b, self.name, self.name))
            self.outcome(self.wait())
            return 0
        finally:
            self.lock.release_all()


    # -- the back half: a planted job read back, over whichever install answers
    def back(self):
        """--preflight, --progress, --status or --collect, one at a time, and nothing but --devices beside it."""
        verbs = [k for k in READS if self.o.get(k)]
        if len(verbs) > 1:
            die("--%s: one reading at a time" % " and --".join(verbs))
        extra = [k for k, v in self.o.items() if v and k not in READS + ("devices",)]
        if self.spec or extra:
            die("--%s reads a planted job and takes only --devices <mac> (got %s)" % (verbs[0], self.spec or "--" + extra[0].replace("_", "-")))
        self.resolve()
        return getattr(self, "read_" + verbs[0])()

    def read_preflight(self):
        return 1 if self.preflight() else 0

    def staging_root(self):
        root = self.d.bench_root()
        if not root:
            die("nothing on %s is readable right now: it answers on neither node, or it is in host mode with its benchmark\n"
                "    volume not attached. 'wk boot %s --status' says which." % (self.name, self.name))
        return root

    def read_status(self):
        root, mode = self.staging_root(), self.d.mode
        info("%s is in bench mode (%s) -- this is the run itself, read over its own node" % (self.name, mode[6:]) if mode.startswith("bench")
             else "%s is in host mode, and this is read off the volume it mounts" % self.name)
        for text, none in ((self.mac.read(root + "/job.json"), "  no job planted"), (self.mac.read(root + "/autorun.state"), "  no autorun state")):
            log("\n".join("  " + l for l in text.splitlines()) or none)
            log("")
        for title, text, none in (("legs", self.mac.py("lib/wkdata.py", "ab-legs", root), "(unreadable)"),
                                  ("last 20 lines of the autorun log", self.mac.out("mac-tail.sh", WK_PATH=root + "/autorun.log"), "(none)")):
            log("  %s:" % title)
            log("\n".join("    " + l for l in text.splitlines()) or "    " + none)
        return 0

    def read_collect(self):
        """The volume's result directories carry the env.json `wk bench staged` wrote beside each, so they are copied rather
        than recomposed; the run map adds which round and arm each is. A contaminated leg is not compared, so not copied."""
        root = self.staging_root()
        info("collect: reading the A/B off %s" % (self.d.c("NODE_VOLUME") or self.name))
        st = self.mac.read(root + "/autorun.state")
        if st:
            log("  autorun state:\n" + "\n".join("    " + l for l in st.splitlines()))
        else:
            warn("  no autorun state on the volume -- the agent never ran")
        stamp = kv(st).get("job_stamp", "")
        runs = "%s/ab/%s/runs.tsv" % (root, stamp)
        tsv = self.mac.read(runs) if stamp else ""
        if not tsv.strip():
            warn("  no run map at %s -- no arm completed" % runs)
            log("  the autorun's own log is the place to look:\n    %s/autorun.log   ('wk bench ab --devices %s --status' tails it)" % (root, self.name))
            return 1
        log("\n  runs:\n" + "\n".join("    " + l for l in tsv.splitlines()) + "\n")
        taskdir = os.path.join(self.bench_dir, "%s-%s-mac-ab" % (stamp, self.name))
        if not os.path.isdir(taskdir):
            die("job %s has no task in %s, so nothing here can record it; its results stay on the volume" % (stamp, self.bench_dir))
        self.here.write(os.path.join(taskdir, "autorun.state"), st)
        if self.collect_runs(taskdir, root, tsv):
            try:
                report.task_report(taskdir, False, text=True)
            except (Refused, SystemExit, OSError, ValueError) as e:
                warn("the report did not complete (%s); the runs are recorded:  wk bench report %s" % (e, os.path.basename(taskdir)))
        return 0

    def collect_runs(self, taskdir, root, tsv):
        rows = [r for r in (report.map_row(l) for l in tsv.splitlines() if l.strip()) if r[0] != "0" and r[4] == "clean" and r[3]]
        if not rows:
            warn("  no clean leg after the warmup round -- nothing to record on the task")
            return 0
        into, n = os.path.join(taskdir, "runs"), 0
        self.here.mkdir(into)
        for rnd, label, sid, rid, _, plan in rows:
            got, packed = self.mac.run("mac-tar.sh", WK_PATH=root + "/results", WK_DIR=rid), os.path.join(into, rid + ".tar.b64")
            if got.ok:
                self.here.write(packed, got.out)
            landed = got.ok and self.here.act_run(["sh", "-c", 'base64 -d < "$1" | tar -xf - -C "$2"', "sh", packed, into]).ok
            if got.ok:
                self.here.remove(packed)
            if not landed:
                warn("  could not copy %s onto the task" % rid)
                continue
            if act.dry_run():
                continue
            env = os.path.join(into, rid, "env.json")
            if not os.path.isfile(env):
                warn("  %s carries no env.json, so it cannot be paired with round %s arm %s" % (rid, rnd, label))
                continue
            record.write_env(env, ["machine=" + self.name, "plan=" + plan, "ab.round=" + rnd, "ab.staged=" + sid,
                                   "ab.arm=" + label.lower()], update=True)
            n += 1
        log("  recorded %d clean leg(s) onto %s" % (n, taskdir))
        return n

    def read_progress(self):
        """Every step of a Mac A/B: what it is, the command that does it, and the one that proves it."""
        info("the A/B on %s, step by step" % self.name)
        n, mode, vol, status = self.name, self.d.mode, self.d.c("NODE_VOLUME"), "wk bench ab --devices %s --status" % self.name
        steps = []

        def step(state, title, detail="", do="", verify=""):
            steps.append(state)
            log("  %s %d. %s" % ({"yes": "[x]", "part": "[~]"}.get(state, "[ ]"), len(steps), title))
            for label, text in (("", detail), ("do:     ", do if state != "yes" else ""), ("verify: ", verify)):
                if text:
                    log("         " + label + text)

        if mode == "unreachable":
            step("part", "the run is under way", "%s answers on neither node. It is restarting, or it is off." % n, "", status + "   (once one answers)")
            return 0
        if mode.startswith("bench"):
            step("yes", "the machine is in bench mode", "%s -- the A/B is on it now" % mode[6:], "", status)
            return 0
        root = self.d.bench_root() or ""
        if not self.guest:
            profile = self.d.c("NODE_PROFILE") or "<profile>"
            version = self.mac.out("mac-version.sh", WK_PATH=self.d.volume() + "/System/Library/CoreServices/SystemVersion.plist")
            step("yes" if version else "no", "the benchmark volume exists", "'%s', macOS %s" % (vol, version) if version else
                 "'%s' is not mounted here (a shutdown unmounts it; --all makes one that is not there at all)" % vol,
                 "wk sysimage build %s --all   (on the Mac)" % profile, "wk boot %s --status" % n)
            if not version:
                return 0
            marker = kv(self.mac.read(self.d.volume() + MARKER)).get("id", "")
            pyobjc = "yes" if self.mac.test("-d", "%s/%s/objc" % (self.d.bench_home() or "", SITE)) else "no"
            done = bool(root) and self.provisioned(root)
            step("yes" if done else "no", "it is provisioned", ("first boot completed; marker %s, pyobjc %s" if done else
                 "its first-boot log has no completion line (marker %s, pyobjc %s)") % (marker or "none", pyobjc),
                 "wk sysimage build %s --repair   (on the Mac), then boot it once" % profile, "wk bench ab --devices %s --preflight" % n)
        arms = self.staged_arms(root) if root else []
        shown = "; ".join("%s (%s) gated=%s" % (i, sha[:12], "yes" if g else "no") for i, sha, g in arms) or "nothing staged"
        step("yes" if len(arms) > 1 else "part" if arms else "no", "two arms are built and staged", shown,
             "wk bench ab --devices %s --patch <ref> --workspace <ws> --plant" % n, "wk bench staged --ls   (on the Mac)")
        gated = sum(1 for a in arms if a[2])
        step("yes" if arms and gated == len(arms) else "part", "each arm can prove how it was collected",
             "%d of %d carry their readings" % (gated, len(arms)), "rebuild it: wk build <ws> mac-release-pgo",
             "cat .../staged/<id>/WebKitBuild/*/wk-profile-check.json   (on the Mac)")
        armed = not self.guest and self.firmware_is_bench()
        can = armed and self.d.restart_ready()
        if self.guest:
            step("no", "the machine is in bench mode", "the guest carries no marker", "wk boot %s" % n, "wk boot %s --status" % n)
        elif can:
            step("no", "the machine is in bench mode", "it is in host mode, and '%s' is the firmware default with a helper that answers -- "
                 "so this needs no arming, only the restart" % vol, "wk bench ab --devices %s --systems <a>,<b>   (plants and restarts)" % n,
                 "wk boot %s --status" % n)
        elif armed:
            step("no", "the machine is in bench mode", "it is in host mode. '%s' is the firmware default, so any reboot enters it; what is "
                 "missing is a restart this can make -- %s" % (vol, self.d.restart_detail()), "wk machine setup %s   (one password prompt there)" % n,
                 "wk boot %s --status" % n)
        else:
            step("no", "the machine is in bench mode", "it is in host mode, and " + self.fw_detail,
                 "wk boot %s   (arms the firmware and reboots)" % n, "wk boot %s --status" % n)
        job = self.read_json(root + "/job.json") if root else None
        job_arms = [a.get("id", "") for a in (job or {}).get("arms") or []]
        fresh = bool(job_arms) and set(job_arms) <= {a[0] for a in arms}
        plant = "wk bench ab --devices %s --systems <a>,<b>" % n
        if job is None:
            step("no", "a job is planted for the staged arms", "none", plant, status)
        elif fresh:
            step("yes", "a job is planted for the staged arms", " ".join(job_arms), "", status)
        else:
            step("no", "a job is planted for the staged arms", "the planted job names arms that are not staged now (%s) -- it is an older "
                 "experiment's, and its rounds below are not this one's" % " ".join(job_arms), plant, status)
        st = self.mac.read(root + "/autorun.state") if root else ""
        last = dict(l.split("=", 1) for l in st.splitlines() if "=" in l)
        results = len(self.mac.out("mac-ls.sh", WK_PATH=root + "/results").split()) if root else 0
        if not fresh:
            step("no", "the rounds are done", "nothing has run for these arms", "boot the volume; the planted job runs them", status)
        elif last.get("outcome"):
            step("yes", "the rounds are done", "%s round(s), outcome %s, %d result(s)" % (last.get("rounds_done") or "0", last["outcome"], results),
                 "", "wk bench ab --devices %s --collect" % n)
        elif last.get("rounds_done"):
            step("part", "the rounds are done", "%s so far, %d result(s)" % (last["rounds_done"], results), "", status)
        else:
            step("no", "the rounds are done", "not started", "boot the volume; the planted job runs them", status)
        step("no", "the result is read back", "", "wk bench ab --devices %s --collect" % n, "wk bench report <task>")
        return 0

    def read_json(self, path):
        try:
            return json.loads(self.mac.read(path) or "null")
        except ValueError:
            return None

    def staged_arms(self, root):
        """(id, webkit sha, gated) per staged build: gated is whether it carries the readings its collection was judged by."""
        out = []
        for ident in self.staged_ids(root):
            d = "%s/staged/%s" % (root, ident)
            stage = self.read_json(d + "/stage.json")
            if isinstance(stage, dict):
                out.append((ident, stage.get("webkit_sha") or "", self.mac.run("mac-gated.sh", WK_PATH=d).ok))
        return out


class PgoCollect:
    """build/mac-pgo.sh's middle phase, in the guest that builds: the instrumented browser profiled, unthrottled."""

    def __init__(self, root, m, env, src, clock=None):
        self.root, self.m, self.env, self.src = str(root), m, env, src
        self.clock = clock
        self.state = os.path.join(env.get("HOME", ""), PGO_STATE)
        self.scripts = os.path.join(src, "Tools", "Scripts")

    def faults(self):
        """Every reason, not the first: a throttled collection looks exactly like a good one."""
        out = []
        console, me = first_line(self.m.run(["stat", "-f", "%Su", "/dev/console"])), first_line(self.m.run(["id", "-un"]))
        if console != me:
            out.append("the screen belongs to '%s', not %s -- a browser driven over ssh has nowhere to draw" % (console or "nobody", me))
        if not self.m.run(lib_argv(self.root, "bench/mac-pyobjc.sh", "wk_pyobjc_have")).ok:
            return out + ["no pyobjc: run-benchmark cannot size the screen and no raiser can hold the browser in front"]
        if not self.m.run(["/usr/bin/python3", "-c", "import AppKit, sys; sys.exit(0 if AppKit.NSScreen.mainScreen() else 1)"]).ok:
            out.append("there is no main screen, so nothing can be drawn at all")
        ok, said = screen_row(self.m, self.root)
        return out + ([] if ok else [said])

    def read(self, rel):
        try:
            return self.m.read(os.path.join(self.scripts, rel))
        except OSError:
            return None

    def pins(self):
        """speedometer3 and jetstream3 name a moving branch, so each benchmark is pinned by its upstream commit first."""
        from wk.clock import Clock
        from wk.store import Store
        store = Store(self.env)
        seeder = seed.Seeder(self.m, Lock(store, self.m, self.clock or Clock()), os.path.join(store.artifact_dir(), "bench"))
        out = []
        for plan in pgo.BENCHMARKS:
            d = seeder.seed(plan, seed.plan_json(self.read, plan))
            if not d or not self.m.isdir(d):
                die("could not pin the %s payload, so the collection could profile an unpinned revision of it" % plan)
            out.append((plan, d))
        return out

    def argv(self, instr, profile, arch, pins):
        custom = [w for plan, d in pins for w in ("--benchmark-custom-options", plan, "local-copy:" + d, "timeout:" + pgo.collect_timeout(self.env))]
        return (["env", "WK_WEBKIT_SCRIPTS=" + self.scripts, "/usr/bin/python3", os.path.join(self.scripts, "collect-pgo-profiles"),
                 "--run-benchmark-harness", os.path.join(self.root, "build", "pgo-run-benchmark.py"), "--benchmarks"] + list(pgo.BENCHMARKS)
                + ["--output-directory", profile, "--compressed-profile-sub-path", arch, "--build-directory", instr, "--browser", "minibrowser"]
                + custom)

    def run(self, instr, profile, arch):
        if act.dry_run():
            print("rm -rf %s && %s" % (q(profile), q(*self.argv(instr, profile, arch, [("<each benchmark>", "<pinned payload>")]))))
            return 0
        faults = self.faults()
        if faults:
            die("this machine cannot present an unthrottled browser, so every profile it collected would be of a throttled one:\n"
                "%s\n    Collect on a machine that can -- the benchmark install." % "\n".join("  " + f for f in faults))
        self.m.mkdir(self.state)
        self.m.act_run(lib_argv(self.root, QUIET, "mac_raiser_on", self.state))
        try:
            check = os.path.join(self.state, "browser-check.json")
            if not self.m.act_run(["/usr/bin/python3", os.path.join(self.root, CHECK), "--build-directory", instr, "--json", check]).ok:
                die("the instrumented build cannot present an accelerated, unthrottled browser here, so every profile it\n"
                    "    collected would be of the wrong code (above)")
            pins = self.pins()
            text = "".join("%s\t%s\n" % p for p in pins)
            self.m.write(os.path.join(self.state, "payload-pins"), text)
            log("wk: profiling against pinned payloads:\n" + "\n".join("  " + l for l in text.splitlines()))
            self.m.remove(profile)   # collect-pgo-profiles refuses a directory that is not empty
            watch = os.path.join(self.state, "screen-watch")
            self.m.act_run(lib_argv(self.root, QUIET, "screen_watch_start", watch))
            rc = self.m.run_tty(self.argv(instr, profile, arch, pins)).rc
            seen = self.m.act_run(lib_argv(self.root, QUIET, "screen_watch_stop", watch))
            if not seen.ok:
                die("something drew over this collection, so every leg after it profiled a covered browser:\n%s"
                    % "\n".join("  " + l for l in seen.out.splitlines()))
        finally:
            self.m.act_run(lib_argv(self.root, QUIET, "mac_raiser_off", self.state))
        if rc:
            return rc
        self.m.write(os.path.join(profile, "payload-pins"), text)
        if not self.m.act_run(["env", "PYTHONPATH=" + os.path.join(self.root, "lib"), "/usr/bin/python3", "-m", "wk.pgo", "check",
                               "--dir", profile, "--scripts", self.scripts, "--compressed", arch,
                               "--json", os.path.join(self.state, "profile-check.json")]).ok:
            die("the collection finished and its profile is not one to build against (above)")
        return 0

    def evidence(self, final, profile):
        """The readings that justify this build go beside its products, so a staged arm carries them."""
        if act.dry_run():
            return 0
        self.m.mkdir(final)
        for src, name in ((os.path.join(self.state, "browser-check.json"), "wk-browser-check.json"),
                          (os.path.join(self.state, "profile-check.json"), "wk-profile-check.json"),
                          (os.path.join(profile, "payload-pins"), "wk-payload-pins")):
            if self.m.exists(src):
                self.m.write(os.path.join(final, name), self.m.read(src))
        return 0


def main(argv, env=None, here=None):
    """python3 -m wk.bench.mac <verb>: what build/mac-pgo.sh asks, one line each there."""
    env = os.environ if env is None else env
    root = env.get("WK_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from wk.machine import Local
    here = here or Local()
    verb, args = (argv or [""])[0], argv[1:]
    try:
        if verb == "pgo-instr" and len(args) == 1:
            print(args[0] + PGO_INSTR)
            return 0
        if verb == "pgo-collect" and len(args) == 4:
            return PgoCollect(root, here, env, args[0]).run(*args[1:])
        if verb == "pgo-evidence" and len(args) == 3:
            return PgoCollect(root, here, env, args[0]).evidence(*args[1:])
    except Refused as e:
        return e.status
    sys.stderr.write("usage: python3 -m wk.bench.mac pgo-instr <final> | pgo-collect <src> <instr> <profile> <arch>\n"
                     "       | pgo-evidence <src> <final> <profile>\n")
    return 2


if __name__ == "__main__":
    from wk.bench.mac import main as _main
    sys.exit(_main(sys.argv[1:]))
