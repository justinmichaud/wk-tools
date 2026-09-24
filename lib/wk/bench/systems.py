"""A bench system: boot, deploy, run, collect. `run` leaves its run directory on the system's machine and
`collect` brings its result into the store through that machine's one copy; where the benchmark runs is
the system's choice. A workspace is two of them: its container and its macOS guest."""

import json
import os
import plistlib
import shlex

from wk import shell
from wk.act import die
from wk.resources import Resources
from wk.session import Session

GOVERNOR = "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"
DRM = "/sys/class/drm"
THERMAL = "/sys/class/thermal"
# __EGL_VENDOR_LIBRARY_FILENAMES is what decides it: the other two steer Mesa, and glvnd would still load the NVIDIA vendor.
SOFTWARE_ENV = ("WAYLAND_DISPLAY=", "WEBKIT_DISABLE_DMABUF_RENDERER=1", "LIBGL_ALWAYS_SOFTWARE=1",
                "GALLIUM_DRIVER=llvmpipe", "__EGL_VENDOR_LIBRARY_FILENAMES=/usr/share/glvnd/egl_vendor.d/50_mesa.json")


def first_line(r):
    return r.out.replace("\r", "").strip().split("\n")[0] if r.ok and r.out.strip() else ""


def root_device(run, read, path, macos):
    """The disk a path is on, its bus and whether it is solid state, asked through that machine's `run` and `read`."""
    if macos:
        r = run(["diskutil", "info", "-plist", path])
        try:
            d = plistlib.loads(r.out.encode()) if r.ok else {}
        except Exception:
            d = {}
        if not d.get("DeviceNode"):
            return "unknown"
        return "%s (%s, %s)" % (d["DeviceNode"], d.get("BusProtocol") or "unknown", "ssd" if d.get("SolidState") else "rotational")
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
    d = disks[0]
    try:
        trim = int(read("/sys/block/%s/queue/discard_max_bytes" % d["name"]).strip() or 0) > 0
    except (OSError, ValueError):
        trim = False
    rotational = d.get("rota") in (True, "1", 1)
    return "%s %s(%s, %s, %s)" % (d["name"], (d.get("model") or "").strip() + " " if (d.get("model") or "").strip() else "",
                                  d.get("tran") or "unknown", "rotational" if rotational else "ssd", "trim" if trim else "no-trim")


class System:
    """The interface. `leg` is one run (lib/wk/bench/pipeline.py's Leg); `watched(argv, cwd, log)` is the pipeline's one watched run."""

    kind = ""
    bench_host = ""
    host_os = ""

    def __init__(self, root, reg, target, ws, clock):
        self.root, self.reg, self.target, self.ws, self.clock = str(root), reg, target, ws, clock
        self.here = reg.machine

    def boot(self):
        if self.target.info(self.ws) in ("absent", "unreachable"):
            die("no such workspace: %s ('wk ls' lists them)" % self.ws)

    def deploy(self, leg):
        raise NotImplementedError

    def run(self, leg, script, watched, log):
        argv, cwd = self.target.exec_argv(self.ws, ["bash", "-lc", script])
        return watched(argv, cwd, log)

    def collect(self, leg):
        raise NotImplementedError

    def src(self):
        return self.target.src(self.ws)

    def build_dir(self, leg):
        return leg.cfg.build_dir(self.src())

    def after(self, leg):
        return []

    def exec_ok(self, *argv):
        return self.target.exec(self.ws, list(argv)).ok

    def sha(self):
        return first_line(self.target.exec(self.ws, ["git", "-C", self.src(), "rev-parse", "HEAD"]))

    def build_present(self, leg):
        build = leg.cfg.build_dir(self.src())
        if leg.runner == "jsc":
            jsc = leg.cfg.jsc_path(self.src())
            return (True, jsc) if self.exec_ok("test", "-x", jsc) else (False, "no jsc in %s -- wk build %s %s" % (build, self.ws, leg.cfg.name))
        if any(self.exec_ok("test", "-x", p) for p in self.browser_products(leg)):
            return True, build
        return False, "no MiniBrowser in %s -- wk build %s %s" % (build, self.ws, leg.cfg.name)

    def link(self, path, link):
        self.target.act_exec(self.ws, ["mkdir", "-p", os.path.dirname(link)])
        self.target.act_exec(self.ws, ["ln", "-sf", path, link])


class ContainerSystem(System):
    """run-benchmark in the container; its /bench is this store's bench directory, so what `run` leaves is already collected."""

    kind = "container"
    bench_host = "container"
    host_os = "linux"

    def __init__(self, root, reg, target, ws, clock):
        super().__init__(root, reg, target, ws, clock)
        self.session = Session(self.root, self.here, clock, reg.env)
        self.renderer, self.mode = "", None

    def cores_refusal(self):
        return ""

    def aslr_prefix(self):
        return "setarch $(uname -m) -R -- "

    def has_gpu(self, arch):
        return shell.arch_has_gpu(self.root, self.here, arch)

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

    def default_browser(self, cfg):
        b = {"--wpe": "minibrowser-wpe", "--gtk": "minibrowser-gtk"}.get(cfg.port)
        if not b:
            die("no benchmark browser for port %s" % (cfg.port or cfg.name))
        return b

    def browser_products(self, leg):
        build = leg.cfg.build_dir(self.src())
        return [build + "/bin/MiniBrowser", build + "/bin/WPEWebProcess"]

    def run_dir(self, leg):
        return "/bench/" + leg.rel

    def payload_dir(self, leg):
        return "/cache/bench/" + os.path.basename(leg.payload) if leg.payload else ""

    def run_env(self, leg):
        return list(SOFTWARE_ENV) if leg.software else []

    def runner_argv(self, leg):
        return ["Tools/Scripts/run-benchmark", "--browser", leg.browser]

    def deploy(self, leg):
        """The build is the workspace's own, and the payload is on the store's /cache/bench mount."""

    def collect(self, leg):
        """/bench is the store's bench directory, bind-mounted: the result is where the record is."""

    def doctor(self, gpu):
        return self.here.run(["env", "WK_NAME=" + self.ws, os.path.join(self.root, "cmd", "doctor")] + (["--gpu"] if gpu else []))

    def checks(self, leg):
        rows, notes = [], []
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
                rows.append((False, "graphical session", "no compositor -- wk session on"))
            elif self.session_mode() in ("gpu", "none"):
                rows.append((True, "graphical session", "%s (%s)" % (self.session.socket, self.session_mode())))
            else:
                rows.append((False, "graphical session", "session mode '%s' -- watchable over the BMC, not measurable; wk session on"
                             % self.session_mode()))
        gov = self.governor()
        rows.append((True, "cpu governor", gov) if gov == "performance" else (False, "cpu governor", gov + " -- wk quiesce on"))
        temp = self.temperature()
        if temp:
            notes.append("temp " + temp)
        return rows, notes

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
                mode = self.here.read(os.path.join(DRM, n, "modes")).split("\n")[0]
            except OSError:
                continue
            out += "%s %s; " % (n, mode)
        return out

    def facts(self, leg):
        m, res = self.here, Resources(self.here, self.reg.env, "linux")
        cpu = next((l.split(":", 1)[1].strip() for l in m.run(["lscpu"]).out.splitlines() if l.startswith("Model name")), "")
        return ["gpu_renderer=" + self.renderer, "session_mode=" + self.session_mode(), "display=" + self.display(),
                "host.kernel=" + first_line(m.run(["uname", "-r"])), "host.cpu=" + cpu, "host.cores=%d" % res.host_cores(),
                "host.kernel_arch=" + first_line(m.run(["uname", "-m"])), "host.governor=" + self.governor(),
                "host.nvidia_driver=" + first_line(m.run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"])),
                "host.root_device=" + root_device(m.run, m.read, self.reg.store.root(), False),
                "host.container_cpus=%d" % res.envelope_cores(), "host.container_mem_mb=%d" % res.envelope_mem_mb()]


class GuestSystem(System):
    """run-benchmark in the macOS guest, into a run directory there; `collect` copies its result out through the guest's copy."""

    kind = "guest"
    bench_host = "guest"
    host_os = "macos"
    ROOT = "wk-bench"

    def cores_refusal(self):
        return "no pin exists on macOS; the guest's vCPU count is not a pin"

    def aslr_prefix(self):
        die("WK_BENCH_ASLR=off: ASLR cannot be turned off on macOS; the guest records the slide it got")

    def has_gpu(self, arch):
        return True

    def headless_reason(self, arch):
        return ""

    def default_browser(self, cfg):
        return "minibrowser"

    def browser_products(self, leg):
        return [leg.cfg.browser_path(self.src())]

    def home(self):
        return os.path.join(self.target.home(), self.ROOT)

    def run_dir(self, leg):
        return os.path.join(self.home(), leg.rel)

    def payload_dir(self, leg):
        return os.path.join(self.home(), "payload", os.path.basename(leg.payload)) if leg.payload else ""

    def run_env(self, leg):
        return []

    def runner_argv(self, leg):
        return ["Tools/Scripts/run-benchmark", "--browser", leg.browser, "--platform", "osx"]

    def deploy(self, leg):
        """The pinned payload, copied in: the guest cannot see this store."""
        if leg.payload:
            self.target.push_dir(self.ws, leg.payload, self.payload_dir(leg))

    def run(self, leg, script, watched, log):
        """The run directory is made by the run itself, so a dry run shows it in the line it prints."""
        return super().run(leg, "mkdir -p %s\n%s" % (shlex.quote(self.run_dir(leg)), script), watched, log)

    def collect(self, leg):
        if leg.runner == "browser":
            self.target.pull(self.ws, os.path.join(self.run_dir(leg), "result.json"), os.path.join(leg.out, "result.json"))

    def checks(self, leg):
        if leg.runner != "browser":
            return [], []
        ok = self.exec_ok("python3", "-c", "import objc")
        return [(True, "python with PyObjC", "the guest's python3") if ok else
                (False, "python with PyObjC", "the guest's python3 cannot 'import objc'; run-benchmark's driver needs it")], []

    def sysctl(self, key):
        return first_line(self.target.exec(self.ws, ["sysctl", "-n", key]))

    def facts(self, leg):
        """host.cores is the guest's vCPU count, which is what its cores are held to."""
        return ["host.model=" + self.sysctl("hw.model"), "host.cores=" + self.sysctl("hw.ncpu"),
                "host.macos=" + first_line(self.target.exec(self.ws, ["sw_vers", "-productVersion"])),
                "host.kernel_arch=" + first_line(self.target.exec(self.ws, ["uname", "-m"])),
                "host.root_device=" + root_device(lambda a: self.target.exec(self.ws, a), None, self.target.home(), True)]


SYSTEMS = {"container": ContainerSystem, "vm": GuestSystem}


def for_workspace(root, reg, ws, clock):
    try:
        target = reg.load(reg.ws_target(ws))
    except LookupError as e:
        die(str(e))
    cls = SYSTEMS.get(target.kind)
    if cls is None:
        die("wk bench run measures a container workspace or a macOS guest; '%s' is on target '%s' (%s).\n"
            "    A board or the Mac's bench volume is measured from its own lane (wk pi bench, wk bench mac)."
            % (ws, target.name, target.kind))
    return cls(root, reg, target, ws, clock)

