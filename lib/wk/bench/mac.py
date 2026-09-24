"""The Mac's benchmark install as a bench system (mac-volume). `wk bench stage` is its deploy, run in host mode;
`wk bench staged` is the one pipeline run on the install, which resolves itself as the system in bench mode; and
`staged --gates` asks every gate a run needs over the running install, reading only, before anything reboots."""

import json
import os
import re
import subprocess
import threading

from wk import act, buildconf, fleet, job, record as wkrecord, shell
from wk.act import die, info, log, warn
from wk.bench import pipeline, record
from wk.bench.systems import System, first_line, root_device
from wk.boot import driver_class
from wk.boot.driver import BashChannel
from wk.boot.mac import BENCH_ROOT
from wk.lock import Lock
from wk.mac import SET_TOLERANCE
from wk.quiet import DESKTOP, PRIV, Quiesce, lib_argv
from wk.workspace import require_name

MARKER = "/etc/wk-image"
PRODUCT_SKIP = ("*.build", "XCBuildData", "DerivedSources", "PrecompiledHeaders", "compile_commands", "*.a", "*.noindex")
PUT_SKIP = (".git", "__pycache__")   # lib/bench.sh's BENCH_PUT_SKIP, for the bash puts still left
PYTHONS = ("/usr/bin/python3", "/Library/Developer/CommandLineTools/usr/bin/python3", "/usr/local/bin/python3",
           "/opt/homebrew/bin/python3")
CHECK = "bench/mac-browser-check.py"
WKMAC = "lib/wkmac.py"
QUIET = "lib/quiet.sh"
VM_PROCESS = "com.apple.Virtualization.VirtualMachine"
WEB_PROCESS = "com.apple.WebKit.WebContent"
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


def kv(text):
    return dict(l.split("=", 1) for l in text.splitlines() if "=" in l)


def staged_python(m, env):
    """run-benchmark's driver does a bare `import objc`, which nothing autoinstalls."""
    for p in (env.get("WK_BENCH_PYTHON", ""),) + PYTHONS:
        if p and m.run([p, "-c", "import objc"]).ok:
            return p
    return PYTHONS[0]


def machine_driver(root, conf):
    return driver_class(conf.get("NODE_DRIVER", ""))(root, conf, BashChannel(root, conf, "none"))


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
        ok = self.exec_ok(self.py, "-c", "import objc")
        rows.append((ok, "python with PyObjC", self.py if ok else "no python3 here can 'import objc' -- xcode-select --install, "
                     "or WK_BENCH_PYTHON at one that can"))
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
        pid = ""
        for _ in range(600):
            pid = first_line(self.here.run(["pgrep", "-n", "-f", WEB_PROCESS]))
            if pid:
                break
            self.clock.sleep(1)
        if not pid:
            warn("no web process appeared, so nothing was profiled")
            return
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
                # A tree with no manifest is one nothing runs and nothing reclaims.
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

