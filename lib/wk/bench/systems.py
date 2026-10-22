"""A bench system: the contract `Run` (lib/wk/bench/pipeline.py) keeps to, and the two a workspace is -- its container and
its macOS guest. A board (board.py) and the Mac's bench volume (mac.py) are the other two; `for_system` picks one."""

import json
import os
import plistlib
import shlex

from wk import act, fleet, job, project, record as progress
from wk.act import die, info
from wk.bench import record
from wk.resources import Resources, arch_has_gpu
from wk.session import Session
from wk.store import no_such_workspace

GOVERNOR = "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"
DRM = "/sys/class/drm"
THERMAL = "/sys/class/thermal"
# __EGL_VENDOR_LIBRARY_FILENAMES is what decides it: the other two steer Mesa, and glvnd would still load the NVIDIA vendor.
SOFTWARE_ENV = ("WAYLAND_DISPLAY=", "LIBGL_ALWAYS_SOFTWARE=1",
                "GALLIUM_DRIVER=llvmpipe", "__EGL_VENDOR_LIBRARY_FILENAMES=/usr/share/glvnd/egl_vendor.d/50_mesa.json")
MAX_LOAD = 4


def first_line(r):
    return r.out.replace("\r", "").strip().split("\n")[0] if r.ok and r.out.strip() else ""


def linux_disk(run, read, path):
    src = first_line(run(["findmnt", "-no", "SOURCE", "--target", path]))
    if not src:
        return "unknown"
    r = run(["lsblk", "-J", "-l", "-s", "-o", "NAME,TYPE,ROTA,TRAN,MODEL", src])
    try:
        disks = [b for b in json.loads(r.out).get("blockdevices", []) if b.get("type") == "disk"] if r.ok else []
    except ValueError:
        disks = []
    if not disks:
        return src
    d, model = disks[0], (disks[0].get("model") or "").strip()
    try:
        trim = int(read("/sys/block/%s/queue/discard_max_bytes" % d["name"]).strip() or 0) > 0
    except (OSError, ValueError):
        trim = False
    return "%s %s(%s, %s, %s)" % (d["name"], model + " " if model else "", d.get("tran") or "unknown",
                                  "rotational" if d.get("rota") in (True, "1", 1) else "ssd", "trim" if trim else "no-trim")


def macos_facts(run, path):
    def sysctl(key):
        return first_line(run(["sysctl", "-n", key]))
    try:
        d = plistlib.loads(run(["diskutil", "info", "-plist", path]).out.encode())
    except Exception:
        d = {}
    mem = sysctl("hw.memsize")
    therm = next((l.split("=", 1)[1].strip() for l in run(["pmset", "-g", "therm"]).out.splitlines() if "CPU_Speed_Limit" in l), "100")
    return {"host.model": sysctl("hw.model"), "host.cores": sysctl("hw.ncpu"), "host.memory_mb": "%d" % (int(mem) // 1048576 if mem.isdigit() else 0),
            "host.macos": first_line(run(["sw_vers", "-productVersion"])), "host.kernel": first_line(run(["uname", "-r"])),
            "host.kernel_arch": first_line(run(["uname", "-m"])), "host.power": first_line(run(["pmset", "-g", "batt"])),
            "host.cpu_speed_limit": therm,
            "host.root_device": "%s (%s, %s)" % (d["DeviceNode"], d.get("BusProtocol") or "unknown", "ssd" if d.get("SolidState") else "rotational")
            if d.get("DeviceNode") else "unknown"}


def one_minute_load(m, os_name):
    if os_name == "macos":
        fields, index = m.run(["sysctl", "-n", "vm.loadavg"]).out.split(), 1
    else:
        try:
            fields, index = m.read("/proc/loadavg").split(), 0
        except OSError:
            fields, index = [], 0
    try:
        return float(fields[index])
    except (IndexError, ValueError):
        return None


class Leg:
    """One run of one plan: what was asked, what it resolved to, and where it lands."""

    def __init__(self, plan, o):
        self.plan, self.o = plan, o
        self.count, self.subtests, self.cores = o.get("count") or "", o.get("subtests") or "", o.get("cores") or ""
        self.software, self.software_reason, self.browser = bool(o.get("software")), "", o.get("browser") or ""
        self.slot = o.get("slot") or ""
        self.preset = self.runner = self.klass = self.arch = None
        self.payload = self.id = self.task = self.out = self.notes = ""
        self.machine = None
        self.port = 0
        self.args = shlex.split(o.get("arm_args") or "")   # an argument-set A/B's arm: the shell's options, or the browser's


class System:
    """What every system answers. `local`: the script Run writes execs the benchmark, so the core pin and the variance knobs
    apply; `warmup`: an A/B on it runs a discarded warmup round, which profiles each arm, and with `evidence` reads what the
    running browser is off it; `refused`: the options it cannot honour, and why."""

    kind = bench_host = host_os = ""
    local, pid_watch, warmup, evidence = True, False, False, False
    refused = ((), "")
    ws_driver = None

    def __init__(self, root, reg, ws, clock):
        self.root, self.reg, self.ws, self.clock, self.here = str(root), reg, ws, clock, reg.machine

    def refuse(self, o):
        asked = [k for k in self.refused[0] if o.get(k)]
        if asked:
            die("--%s: %s" % (asked[0].replace("_", "-"), self.refused[1]))

    def leg(self, plan, o, kit):
        """The request resolved against what this system holds, or refused."""
        raise NotImplementedError

    def boot(self, leg):
        """Up, and the system this leg is for; whatever it prepares once per boot."""

    def checks(self, leg):
        """(rows, notes) of the preflight: (ok, what, detail), ok True, False or record.UNKNOWN."""
        raise NotImplementedError

    def payload(self, leg, kit, lock):
        raise NotImplementedError

    def tasks(self):
        return record.leg_home(self.reg, self.ws)

    def name(self, leg):
        return self.ws

    def label(self, leg):
        return "'%s'" % self.ws

    def kill_cmd(self):
        return "wk bench run%s --kill" % ("" if self.reg.in_workspace() else " " + self.ws)

    def subject(self, leg):
        """(kind, spec, device, flags) a one-run task records, its spec its one slot."""
        raise NotImplementedError

    def progress(self, env, kill, log, steps):
        return None

    def env_fields(self, leg):
        raise NotImplementedError

    def facts(self, leg):
        return {}

    def runner(self, leg, kit, pad):
        """(exports, cwd, argv) of the benchmark runner, each word already quoted."""
        raise NotImplementedError

    def run(self, leg, script, watched, log):
        raise NotImplementedError

    def collect(self, leg, rc):
        """The result into the run directory, and whatever the system keeps as evidence of the leg."""

    def after(self, leg):
        return {}

    def reading(self, leg):
        """Where on this machine the measured build's PGO reading is, or ""."""
        return ""

    def deploy(self, o, kit):
        die("a %s runs its workspace's own build: nothing is deployed to it" % self.kind)

    def stop(self, env):
        raise NotImplementedError

    # what an A/B (lib/wk/bench/ab.py) asks of the system its arms are on
    machine_name = ""

    def plan_doc(self, plan, kit, lock):
        return {}

    def width(self, idents):
        return 0

    def claim(self, env, name, kill, steps):
        return None

    def device(self, arm, kind):
        return ""

    def idle_rows(self, recs):
        busy = sum(1 for t in recs.list() if t.field("kind") == "build" and t.verdict("capped") in ("running", "silent", "starting"))
        load = one_minute_load(self.here, self.host_os)
        if busy:
            return [(False, "no builds running", "%d build(s) in progress" % busy)]
        if load is None:
            return [(record.UNKNOWN, "machine idle", "load average unreadable, no wk builds")]
        if round(load) > int(self.reg.env.get("WK_BENCH_MAX_LOAD") or MAX_LOAD):
            return [(False, "machine idle", "1-minute load average is %.2f" % load)]
        return [(True, "machine idle", "load %.2f, no wk builds" % load)]


def bench_class(plan):
    """gpu by default: guessing gpu fails as an easy refusal, guessing cpu as a rendering score off llvmpipe."""
    return "cpu" if plan.startswith(project.get("CPU_PLANS")) else "gpu"


class Workspace(System):

    pid_watch = True
    refused = (("collect",), "takes a PGO profile from a board's instrumented slot: --system <board> --slot <name>-instr")

    def __init__(self, root, reg, ws, clock, driver):
        super().__init__(root, reg, ws, clock)
        self.ws_driver = driver

    def src(self):
        return self.ws_driver.src(self.ws)

    def exec_ok(self, *argv):
        return self.ws_driver.exec(self.ws, list(argv)).ok

    def leg(self, plan, o, kit):
        leg, d, shell = Leg(plan, o), self.ws_driver, project.get("SHELL")
        if leg.cores and self.cores_refusal():
            die("--cores: " + self.cores_refusal())
        name, browser_preset = o.get("preset") or project.get("BENCH_PRESET"), project.get("BENCH_PRESET")
        try:
            leg.preset = kit.resolve_preset(name, d.os(), d.kind, d.env)
        except LookupError:
            die("unknown preset '%s' (wk build --list)" % name)
        leg.klass, leg.arch, cpu = bench_class(plan), d.arch(self.ws), ", ".join(project.get("CPU_PLANS"))
        leg.runner = shell if leg.preset.jsc_only else "browser"
        if leg.runner == shell and leg.klass == "gpu":
            die("%s is a gpu-class benchmark and %s builds no browser.\n    Either build a browser port (wk build %s %s) and pass\n"
                "    --preset %s, or run a cpu-class plan -- %s -- which the %s shell can drive directly."
                % (plan, name, self.ws, browser_preset, browser_preset, cpu, shell))
        if leg.klass == "gpu" and not self.has_gpu(leg.arch):
            die("%s is gpu-class and '%s' is an %s workspace, which has no GPU.\n    cpu-class plans (%s) do run in here,\n"
                "    with either a browser or a %s preset. For a 32-bit rendering number\n    measure a board:  wk bench run %s %s --system <board>"
                % (plan, self.ws, leg.arch, cpu, project.get("SHELL_PORT"), self.ws, plan))
        if leg.runner == "browser":
            leg.browser = leg.browser or self.default_browser(leg.preset)
        if leg.software:
            leg.software_reason = "--software"
        elif leg.runner == "browser" and leg.klass == "cpu":
            leg.software_reason = self.headless_reason(leg.arch)
            leg.software = bool(leg.software_reason)
            if leg.software:
                info("cpu-class: running headless (%s)" % leg.software_reason)
        return leg

    def boot(self, leg):
        if self.ws_driver.info(self.ws) in ("absent", "unreachable"):
            die(no_such_workspace(self.ws))

    def build_present(self, leg):
        build = leg.preset.build_dir(self.src())
        if leg.runner == project.get("SHELL"):
            ok, what = self.exec_ok("test", "-x", leg.preset.jsc_path(self.src())), project.get("SHELL")
        else:
            ok, what = any(self.exec_ok("test", "-x", p) for p in self.browser_products(leg)), project.get("BROWSER")
        return (True, "build present", build) if ok else (False, "build present", "no %s in %s -- wk build %s %s" % (what, build, self.ws, leg.preset.name))

    def records(self, env=None):
        return progress.of_driver(self.ws_driver, self.clock, self.here, env=dict(self.ws_driver.env, **(env or {})))

    def payload(self, leg, kit, lock):
        p, shell = kit.pin_payload(self.here, lock, self.reg.store, self.ws_driver, self.ws, leg.plan), project.get("SHELL")
        if leg.runner == shell and not p:
            die("%s has no pinned payload, and the %s runner has nothing to run without one; a plan whose source cannot be\n"
                "    pinned runs with a browser preset." % (leg.plan, shell))
        if leg.runner == shell and not act.dry_run() and not self.here.exists(os.path.join(p, project.get("SHELL_DRIVER"))):
            die("%s has no %s, so %s cannot be driven from a JavaScript shell. Run it with a browser preset\n"
                "    instead (--preset %s), which is the official number for every plan anyway."
                % (p, project.get("SHELL_DRIVER"), leg.plan, project.get("BENCH_PRESET")))
        return p

    def subject(self, leg):
        return "workspace", self.ws, "%s=%s" % (self.kind, leg.preset.name), " --preset " + leg.preset.name

    def progress(self, env, kill, log, steps):
        return self.records({"WK_ABORT_SECONDS": str(progress.watchdog_abort(env))}).begin("bench", "here", self.ws, kill, log, steps)

    def env_fields(self, leg):
        return {"workspace": self.ws, "preset": leg.preset.name, "browser": leg.browser,
                project.get("SHA_FIELD"): first_line(self.ws_driver.exec(self.ws, ["git", "-C", self.src(), "rev-parse", "HEAD"]))}

    def runner(self, leg, kit, pad):
        result = os.path.join(self.run_dir(leg), "result.json")
        args = self.runner_argv(leg) + kit.browser_args(leg, result, self.payload_dir(leg), pad(leg.preset.build_dir(self.src())))
        return self.run_env(leg), self.src(), [shlex.quote(a) for a in args]

    def run(self, leg, script, watched, log):
        argv, cwd = self.ws_driver.exec_argv(self.ws, ["bash", "-lc", script])
        return watched(argv, cwd, log)

    def link(self, path, link):
        self.ws_driver.act_exec(self.ws, ["mkdir", "-p", os.path.dirname(link)])
        self.ws_driver.act_exec(self.ws, ["ln", "-sf", path, link])

    def stop(self, env):
        if job.stop(self.ws_driver, self.records(), self.ws, "bench", self.here, self.clock, env) == 1:
            die("the benchmark in '%s' outlived a TERM and a KILL.\n    Look at it:  wk enter %s" % (self.ws, self.ws))
        return 0


class Container(Workspace):

    kind, bench_host, host_os = "container", "container", "linux"

    def __init__(self, root, reg, ws, clock, driver):
        super().__init__(root, reg, ws, clock, driver)
        self.session = Session(self.root, self.here, clock, reg.env)
        self.renderer, self.mode = "", None

    def cores_refusal(self):
        return ""

    def aslr_prefix(self):
        return "setarch $(uname -m) -R -- "

    def has_gpu(self, arch):
        return arch_has_gpu(arch)

    def session_mode(self):
        if self.mode is None:
            self.mode = self.session.mode()
        return self.mode

    def headless_reason(self, arch):
        if not self.has_gpu(arch) or not self.here.exists(self.session.socket):
            return "cpu-class, no usable display"
        if self.session_mode() not in ("gpu", "none"):
            return "cpu-class, session not on the GPU"
        return ""

    def default_browser(self, preset):
        b = project.get("BENCH_BROWSERS").get(preset.port)
        if not b:
            die("no benchmark browser for port %s" % (preset.port or preset.name))
        return b

    def browser_products(self, leg):
        return [leg.preset.build_dir(self.src()) + "/" + p for p in project.get("BROWSER_PRODUCTS")]

    def run_dir(self, leg):
        return os.path.join("/var/lib/wk/ws", self.ws, os.path.relpath(leg.out, self.ws_driver.store.ws_dir(self.ws)))

    def payload_dir(self, leg):
        return "/cache/bench/" + os.path.basename(leg.payload) if leg.payload else ""

    def run_env(self, leg):
        return list(SOFTWARE_ENV) + list(project.get("SOFTWARE_ENV")) if leg.software else []

    def runner_argv(self, leg):
        return [project.get("BENCH_RUNNER"), "--browser", leg.browser]

    def doctor(self, gpu):
        return self.here.run(["env", "WK_NAME=" + self.ws, os.path.join(self.root, "cmd", "doctor")] + (["--gpu"] if gpu else []))

    def checks(self, leg):
        rows, notes = [self.build_present(leg)], []
        if leg.klass == "cpu" or leg.software:
            rows.append((True, "class", "cpu -- no GPU or compositor required") if leg.klass == "cpu"
                        else (True, "sandbox (software rendering)", "not comparable with a GPU run"))
            if not self.doctor(False).ok:
                rows.append((False, "sandbox intact", "wk doctor %s" % self.ws))
            if leg.klass != "cpu":
                notes.append("software rendering")
        else:
            r = self.doctor(True)
            text = r.out + r.err
            self.renderer = next((l.split("renderer=", 1)[1].split("|")[0].strip() for l in text.splitlines() if "renderer=" in l), "")
            rows.append((True, "sandbox and GPU", self.renderer[:44]) if r.ok else
                        (False, "sandbox and GPU", " ".join([l for l in text.splitlines() if " -> " in l][:2])))
            if not self.here.exists(self.session.socket):
                rows.append((False, "graphical session", "no compositor -- wk quiesce session on"))
            elif self.session_mode() in ("gpu", "none"):
                rows.append((True, "graphical session", "%s (%s)" % (self.session.socket, self.session_mode())))
            else:
                rows.append((False, "graphical session", "session mode '%s' -- watchable over the BMC, not measurable; wk quiesce session on"
                             % self.session_mode()))
        gov = self.governor()
        rows.append((True, "cpu governor", gov) if gov == "performance" else (False, "cpu governor", gov + " -- wk quiesce on"))
        temp = self.temperature()
        return rows + self.idle_rows(self.records()), notes + (["temp " + temp] if temp else [])

    def governor(self):
        try:
            return self.here.read(GOVERNOR).strip() or "unknown"
        except OSError:
            return "unknown"

    def temperature(self):
        try:
            zones = [z for z in self.here.listdir(THERMAL) if z.startswith("thermal_zone")]
        except OSError:
            return ""
        for z in zones:
            try:
                return "%dC" % (int(self.here.read(os.path.join(THERMAL, z, "temp")).strip()) // 1000)
            except (OSError, ValueError):
                continue
        return ""

    def display(self):
        out = ""
        try:
            names = self.here.listdir(DRM)
        except OSError:
            return out
        for n in names:
            try:
                if self.here.read(os.path.join(DRM, n, "status")).strip() != "connected":
                    continue
                out += "%s %s; " % (n, self.here.read(os.path.join(DRM, n, "modes")).split("\n")[0])
            except OSError:
                continue
        return out

    def facts(self, leg):
        m, res = self.here, Resources(self.here, self.reg.env, "linux")
        cpu = next((l.split(":", 1)[1].strip() for l in m.run(["lscpu"]).out.splitlines() if l.startswith("Model name")), "")
        return {"gpu_renderer": self.renderer, "session_mode": self.session_mode(), "display": self.display(),
                "host.kernel": first_line(m.run(["uname", "-r"])), "host.cpu": cpu, "host.cores": "%d" % res.host_cores(),
                "host.kernel_arch": first_line(m.run(["uname", "-m"])), "host.governor": self.governor(),
                "host.nvidia_driver": first_line(m.run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"])),
                "host.root_device": linux_disk(m.run, m.read, self.reg.store.store_dir()),
                "host.container_cpus": "%d" % res.envelope_cores(), "host.container_mem_mb": "%d" % res.envelope_mem_mb()}


class Guest(Workspace):

    kind, bench_host, host_os = "guest", "guest", "macos"
    ROOT = "wk-bench"

    def cores_refusal(self):
        return "no pin exists on macOS; the guest's vCPU count is not a pin"

    def aslr_prefix(self):
        die("WK_BENCH_ASLR=off: ASLR cannot be turned off on macOS; the guest records the slide it got")

    def has_gpu(self, arch):
        return True

    def headless_reason(self, arch):
        return ""

    def default_browser(self, preset):
        return project.get("BENCH_BROWSERS")["macos"]

    def browser_products(self, leg):
        return [leg.preset.browser_path(self.src())]

    def run_dir(self, leg):
        return os.path.join(self.ws_driver.home(), self.ROOT, os.path.relpath(leg.out, os.path.dirname(os.path.dirname(os.path.dirname(leg.out)))))

    def payload_dir(self, leg):
        return os.path.join(self.ws_driver.home(), self.ROOT, "payload", os.path.basename(leg.payload)) if leg.payload else ""

    def run_env(self, leg):
        return []

    def runner_argv(self, leg):
        return [project.get("BENCH_RUNNER"), "--browser", leg.browser, "--platform", "osx"]

    def run(self, leg, script, watched, log):
        """The pinned payload copied in, since the guest cannot see this store; the run directory made by the run itself."""
        if leg.payload:
            self.ws_driver.push_dir(self.ws, leg.payload, self.payload_dir(leg))
        return super().run(leg, "mkdir -p %s\n%s" % (shlex.quote(self.run_dir(leg)), script), watched, log)

    def collect(self, leg, rc):
        if rc == 0 and leg.runner == "browser":
            self.ws_driver.pull(self.ws, os.path.join(self.run_dir(leg), "result.json"), os.path.join(leg.out, "result.json"))

    def checks(self, leg):
        rows = [self.build_present(leg)]
        if leg.runner == "browser":
            ok = self.exec_ok("python3", "-c", "import objc")
            rows.append((True, "python with PyObjC", "the guest's python3") if ok else
                        (False, "python with PyObjC", "the guest's python3 cannot 'import objc'; %s's driver needs it"
                         % os.path.basename(project.get("BENCH_RUNNER"))))
        return rows + self.idle_rows(self.records()), []

    def facts(self, leg):
        """host.cores is the guest's vCPU count, which is what its cores are held to."""
        return macos_facts(lambda a: self.ws_driver.exec(self.ws, a), self.ws_driver.home())


def for_system(root, reg, ws, clock, name=""):
    """The system `--system <name>` names (a board, a Mac's bench volume), else the workspace's own place."""
    if name:
        kind = (fleet.Fleet(root, reg.env).load(name) or {}).get("kind")
        if kind == "board":
            from wk.bench import board
            return board.for_board(root, reg, ws, clock, name)
        if kind in ("mac", "guest"):
            from wk.bench import mac
            return mac.Volume(root, reg, ws, clock, name)
        die("--system '%s' names no board or Mac in machines/ (wk boot --list)" % name)
    try:
        driver = reg.load(reg.ws_place(ws))
    except LookupError as e:
        die(str(e))
    cls = {"container": Container, "vm": Guest}.get(driver.kind)
    if cls is None:
        die("wk bench run measures a container workspace or a macOS guest; '%s' is on place '%s' (%s).\n"
            "    A board is measured with --system <machine>." % (ws, driver.name, driver.kind))
    return cls(root, reg, ws, clock, driver)
