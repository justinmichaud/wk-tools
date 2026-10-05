"""`wk bench run`: one plan on one System -- refusals, preflight, pinned payload, task and progress record, the watched run, collect, verdict."""

import os
import re
import shlex
import sys

from wk import act, job, project, record as progress, screen
from wk.act import Refused, die, info, log, warn
from wk.bench import record
from wk.lock import Lock
from wk.resources import Resources

CORES_TOKEN = re.compile(r"^[0-9]+(-[0-9]+)?$")
# A benchmark reports once per subtest, far less often than a compiler does.
STALL_SECONDS, ABORT_SECONDS = "900", "5400"
MAX_LOAD = 4
SCORE = re.compile(r"^(Score|Total|.*Score:)", re.I)


def bench_class(plan):
    """gpu by default: guessing gpu fails as an easy refusal, guessing cpu as a rendering score off llvmpipe."""
    return "cpu" if plan.startswith(project.get("CPU_PLANS")) else "gpu"


def cores_valid(spec):
    return bool(spec) and all(CORES_TOKEN.match(t) for t in spec.split(","))


def check_cores(spec):
    if spec and not cores_valid(spec):
        die("--cores '%s' is not a valid Linux cpu list (e.g. 0-3, 2,3, 0-1,4, 7)" % spec)


def ab_fields(o):
    return ["ab.round=" + o["round"]] + ["ab.%s=%s" % (k, o.get(k, "")) for k in ("arm", "slot_a", "slot_b")] if o.get("round") else []


VARIANCE = (("aslr", "WK_BENCH_ASLR"), ("env_pad", "WK_BENCH_ENV_PAD"), ("path_pad", "WK_BENCH_PATH_PAD"),
            ("shared_cache", "WK_BENCH_SHARED_CACHE"))


def variance(env):
    return {key: env.get(name, "") for key, name in VARIANCE}


def knob(env, key):
    v = variance(env)[key]
    return int(v) if v.isdigit() else 0


def task_held(env, task):
    """The A/B that started this run holds its task's lock already: a leg in-process, or a step it spawned."""
    return env.get("WK_TASK_HELD") == task


def aslr_off(env):
    return variance(env)["aslr"] == "off"


def shared_cache_avoided(env):
    return variance(env)["shared_cache"] == "avoid"


def env_pad_prelude(env):
    """Source for the benchmark's own shell: envp's size moves where the initial stack starts."""
    n = knob(env, "env_pad")
    return 'export WK_BENCH_ENV_PAD_DUMMY="$(head -c %d /dev/zero | tr "\\0" x)"; ' % n if n else ""


def padded(path, n):
    """The link a run goes through so the executable's own path, which also lands on the stack, is `n` characters of padding longer."""
    return os.path.join("/tmp/wk-bench-pad-" + "p" * n, os.path.basename(path))


def configuration_fields(env):
    """The knobs, recorded into `configuration` for `wk bench report` to group by; path_len is the pad asked for."""
    out = ["configuration.aslr=off"] if aslr_off(env) else []
    for knob_key, field in (("env_pad", "env_pad_bytes"), ("path_pad", "path_len")):
        if knob(env, knob_key):
            out.append("configuration.%s=%d" % (field, knob(env, knob_key)))
    return out


def one_minute_load(res):
    """The 1-minute load average as the machine reports it, unrounded: Resources.host_load keeps whole cores."""
    if res.os_name == "macos":
        fields, index = res.machine.run(["sysctl", "-n", "vm.loadavg"]).out.split(), 1
    else:
        try:
            fields, index = res.machine.read("/proc/loadavg").split(), 0
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
        self.preset = self.runner = self.klass = self.arch = None
        self.payload = self.id = self.task = self.rel = self.out = ""
        self.machine = None   # what holds `out`: each Run's begin names it
        self.notes = ""
        self.args = shlex.split(o.get("arm_args") or "")   # an options A/B's arm: the shell's options, or the browser's


class Run:
    def __init__(self, root, reg, system, clock, kit, env=None):
        self.root, self.reg, self.system, self.clock, self.kit = str(root), reg, system, clock, kit
        self.env = dict(os.environ if env is None else env)
        progress.default_watchdog(self.env, STALL_SECONDS, ABORT_SECONDS)
        self.here, self.ws, self.ws_driver = reg.machine, system.ws, system.ws_driver
        self.recs = self.records(clock)
        self.lock = Lock(reg.store, self.here, clock)
        self.task, self.dry_fails = None, 0
        self.kill_cmd = "wk bench run%s --kill" % ("" if reg.in_workspace() else " " + self.ws)

    def records(self, clock):
        return progress.of_driver(self.ws_driver, clock, self.here, env=dict(self.ws_driver.env, WK_ABORT_SECONDS=str(progress.watchdog_abort(self.env))))

    def stop(self):
        rc = job.stop(self.ws_driver, self.recs, self.ws, "bench", self.here, self.clock, self.env)
        if rc == 1:
            die("the benchmark in '%s' outlived a TERM and a KILL.\n    Look at it:  wk enter %s" % (self.ws, self.ws))
        return 0

    def leg(self, plan, o):
        s, leg = self.system, Leg(plan, o)
        check_cores(leg.cores)
        if leg.cores and s.cores_refusal():
            die("--cores: " + s.cores_refusal())
        name, shell, browser_preset = o.get("preset") or project.get("BENCH_PRESET"), project.get("SHELL"), project.get("BENCH_PRESET")
        try:
            leg.preset = self.kit.resolve_preset(name, self.ws_driver.os(), self.ws_driver.kind, self.ws_driver.env)
        except LookupError:
            die("unknown preset '%s' (wk build --list)" % name)
        leg.klass, leg.arch, cpu = bench_class(plan), self.ws_driver.arch(self.ws), ", ".join(project.get("CPU_PLANS"))
        leg.runner = shell if leg.preset.jsc_only else "browser"
        if leg.runner == shell and leg.klass == "gpu":
            die("%s is a gpu-class benchmark and %s builds no browser.\n    Either build a browser port (wk build %s %s) and pass\n"
                "    --preset %s, or run a cpu-class plan -- %s -- which the %s shell can drive directly."
                % (plan, name, self.ws, browser_preset, browser_preset, cpu, shell))
        if leg.klass == "gpu" and not s.has_gpu(leg.arch):
            die("%s is gpu-class and '%s' is an %s workspace, which has no GPU.\n    cpu-class plans (%s) do run in here,\n"
                "    with either a browser or a %s preset. For a 32-bit rendering number\n    measure a board:  wk bench run %s %s --system <board>"
                % (plan, self.ws, leg.arch, cpu, project.get("SHELL_PORT"), self.ws, plan))
        if leg.runner == "browser":
            leg.browser = leg.browser or s.default_browser(leg.preset)
        if leg.software:
            leg.software_reason = "--software"
        elif leg.runner == "browser" and leg.klass == "cpu":
            leg.software_reason = s.headless_reason(leg.arch)
            leg.software = bool(leg.software_reason)
            if leg.software:
                info("cpu-class: running headless (%s)" % leg.software_reason)
        return leg

    @staticmethod
    def check(ok, what, detail):
        word, colour = ("unk ", "33") if ok is record.UNKNOWN else ("ok  ", "32") if ok else ("FAIL", "31")
        mark = "\033[%sm%s\033[0m" % (colour, word) if sys.stderr.isatty() else word
        sys.stderr.write("  %s  %-34s %s\n" % (mark, what, detail))

    def busy_builds(self):
        return sum(1 for t in self.recs.list() if t.field("kind") == "build" and t.verdict("capped") in ("running", "silent", "starting"))

    def idle_rows(self):
        busy, load = self.busy_builds(), one_minute_load(Resources(self.here, self.reg.env, self.system.host_os))
        if busy:
            return [(False, "no builds running", "%d build(s) in progress" % busy)]
        if load is None:
            return [(record.UNKNOWN, "machine idle", "load average unreadable, no wk builds")]
        if round(load) > int(self.reg.env.get("WK_BENCH_MAX_LOAD") or MAX_LOAD):
            return [(False, "machine idle", "1-minute load average is %.2f" % load)]
        return [(True, "machine idle", "load %.2f, no wk builds" % load)]

    def preflight(self, leg):
        info("preflight")
        rows, notes = self.system.checks(leg)
        ok, detail = self.system.build_present(leg)
        rows = [(ok, "build present", detail)] + rows + self.idle_rows()
        fails = record.failed(rows)
        for ok, what, detail in rows:
            self.check(ok, what, detail)
        leg.notes = record.preflight_notes(rows, notes)
        sys.stderr.write("\n")
        if not fails:
            info("preflight clean" + record.not_measured(len(record.unmeasured(rows))))
        elif act.dry_run():
            self.dry_fails = len(fails)
            warn("%d preflight check(s) would fail -- a real run would stop here" % len(fails))
        else:
            act.barrier("%d preflight check(s) failed -- a number from this run cannot be defended. Fix the above;\n"
                        "    a forced run is recorded as forced, and is not comparable with a clean run." % len(fails), env=self.env)

    def seed(self, leg):
        leg.payload = self.kit.pin_payload(self.here, self.lock, self.reg.store, self.ws_driver, self.ws, leg.plan)
        if leg.runner != project.get("SHELL"):
            return
        if not leg.payload:
            die("%s has no seeded payload, and the %s runner has nothing to run without one.\n    'wk bench seed %s %s' fetches it; "
                "a plan whose source cannot be pre-seeded can only be run with a browser preset." % (leg.plan, project.get("SHELL"), self.ws, leg.plan))
        if not act.dry_run() and not self.here.exists(os.path.join(leg.payload, project.get("SHELL_DRIVER"))):
            die("%s has no %s, so %s cannot be driven from a JavaScript shell. Run it with a browser preset\n"
                "    instead (--preset %s), which is the official number for every plan anyway."
                % (leg.payload, project.get("SHELL_DRIVER"), leg.plan, project.get("BENCH_PRESET")))

    def begin(self, leg):
        """The task (task.json, under its lock) and its run directory, the env.json the report reads, and the progress record."""
        stamp, given, rnd = self.clock.stamp(), leg.o.get("task") or "", leg.o.get("round") or ""
        leg.id = "%s-%s-%s%s" % (stamp, leg.plan, self.ws, "-r%s%s" % (rnd, leg.o.get("arm", "")) if rnd else "")
        leg.task = given or "%s-%s" % (stamp, self.ws)
        leg.rel = "%s/runs/%s" % (leg.task, leg.id)
        leg.machine, bench = record.leg_home(self.reg, self.ws, given)
        taskdir = os.path.join(bench, leg.task)
        leg.out = os.path.join(taskdir, "runs", leg.id)
        steps = ["deploy %s to the %s '%s'" % (leg.preset.name, self.system.kind, self.ws),
                 "run %s (%s, %s iteration(s))" % (leg.plan, leg.runner, leg.count or "default"), "collect into %s" % leg.out]
        if act.dry_run():
            return steps
        if given:
            if not task_held(self.env, leg.task):
                self.lock.hold("bench-task-" + leg.task, timeout=5)
        else:
            self.new_task(leg, bench, "workspace", self.ws, "%s=%s" % (self.system.kind, leg.preset.name),
                          "wk bench run %s %s --preset %s" % (self.ws, leg.plan, leg.preset.name))
        leg.machine.mkdir(leg.out)
        record.write_env(os.path.join(leg.out, "env.json"), [
            "plan=" + leg.plan, "workspace=" + self.ws, "preset=" + leg.preset.name, "browser=" + leg.browser, "task=" + leg.task,
            project.get("SHA_FIELD") + "=" + self.system.sha(), "count=" + leg.count, "local_copy=" + leg.payload,
            "software_reason=" + leg.software_reason, "class=" + leg.klass, "runner=" + leg.runner, "arch=" + leg.arch,
            "bench_host=" + self.system.bench_host, "preflight_notes=" + leg.notes, "cores.set=" + leg.cores]
            + ab_fields(leg.o) + (["arm_args=" + shlex.join(leg.args)] if rnd else [])
            + self.system.facts(leg) + configuration_fields(self.env),
            bool_fields=["forced=" + act.forced(self.env), "software=" + ("1" if leg.software else ""), "cores.pinned=" + leg.cores],
            machine=leg.machine)
        self.task = self.recs.begin("bench", "here", self.ws, self.kill_cmd, os.path.join(leg.out, "run.log"), steps)
        return steps

    def new_task(self, leg, bench, kind, spec, device, command):
        """A one-run task whose subject `spec` is also its one slot."""
        count = ["count=" + leg.count] if leg.count else []
        record.new_task(leg.machine, bench, leg.task, self.lock, self.clock.iso(), ["subject.kind=" + kind, "subject.spec=" + spec, "devices=" + device,
                        "plans=" + leg.plan, "rounds=1", "slots=" + spec] + count, command + (" --count " + leg.count if leg.count else ""))

    def step(self, n):
        if self.task is not None:
            self.task.step(n)

    def end(self, word):
        if self.task is not None:
            self.task.end(word)
        self.lock.release_all()

    pid_watch = True

    def watched(self, argv, cwd, path):
        watcher = None
        if self.task is not None:
            self.task.set("log", path)
        if self.task is not None and self.pid_watch:
            watcher = job.PidWatch(self.ws_driver, self.ws, self.task, path, "bench", project.get("BENCH_PID_MATCH"), job.pid_tries(self.env))
            watcher.start()
        try:
            return job.watch(argv, path, self.here, self.clock, self.env, cwd)
        finally:
            if watcher is not None:
                watcher.stop()

    def prefix(self, leg):
        """What the benchmark is exec'd through: the core pin, then ASLR off."""
        out = "taskset -c %s " % leg.cores if leg.cores else ""
        return out + (self.system.aslr_prefix() if aslr_off(self.env) else "")

    def through_pad(self, path):
        n = knob(self.env, "path_pad")
        if not n:
            return path
        self.system.link(path, padded(path, n))
        return padded(path, n)

    def script(self, leg, exports, cwd, argv):
        head = 'echo "wk: bench pid $$" >&2\n' + "".join("export %s\n" % e for e in exports)
        return head + env_pad_prelude(self.env) + "cd %s && exec %s%s" % (shlex.quote(cwd), self.prefix(leg), " ".join(argv))

    def run_shell(self, leg):
        s, preset = self.system, leg.preset
        shell, var, lib = self.through_pad(preset.jsc_path(s.src())), preset.run_var(), preset.run_dir(s.src())
        n = int(leg.count or 1)
        info("running %s in '%s' (%s, %s shell, %d iteration(s))" % (leg.plan, self.ws, preset.name, project.get("SHELL"), n))
        log("  results: %s" % leg.out)
        logs = []
        for i in range(1, n + 1):
            if n > 1:
                info("iteration %d/%d" % (i, n))
            logs.append(os.path.join(leg.out, "run-%d.log" % i))
            exports = ['%s="%s${%s:+:${%s}}"' % (var, lib, var, var)]
            argv = self.kit.shell_argv(shlex.quote(shell), [shlex.quote(a) for a in leg.args], leg.subtests)
            rc = s.run(leg, self.script(leg, exports, s.payload_dir(leg), argv), self.watched, logs[-1])
            if rc != 0:
                return rc, "%s exited %d on iteration %d" % (project.get("SHELL"), rc, i), logs[-1]
        if not act.dry_run():
            self.kit.merge_jsc_logs(os.path.join(leg.out, "result.json"), logs)
        return 0, "", logs[0]

    def run_browser(self, leg):
        s, src = self.system, self.system.src()
        args = s.runner_argv(leg) + self.kit.browser_args(leg, os.path.join(s.run_dir(leg), "result.json"), s.payload_dir(leg) if leg.payload else "",
                                                         self.through_pad(s.build_dir(leg)))
        info("running %s in '%s' (%s, %s)" % (leg.plan, self.ws, leg.preset.name, leg.browser))
        log("  results: %s" % leg.out)
        path = os.path.join(leg.out, "run.log")
        # For as long as the browser is up: a dialog that draws mid-run covers every leg after it.
        watch = screen.Watch(self.here, self.root, self.clock, self.env)
        if not act.dry_run():
            watch.start()
        rc = s.run(leg, self.script(leg, s.run_env(leg), src, [shlex.quote(a) for a in args]), self.watched, path)
        drew = watch.stop()
        if drew:
            warn("the machine did not stay quiet under this run -- something drew over the\n"
                 "  browser, or a process that must not run came back while it measured:")
            sys.stderr.write("".join("    %s\n" % l for l in drew))
            try:
                act.barrier("something drew over this run, so its number is one to distrust", env=self.env)
            except act.Refused:
                rc = rc or 1
        return rc, "%s exited %d" % (os.path.basename(project.get("BENCH_RUNNER")), rc), path

    def go(self, plan, o):
        leg = self.leg(plan, o)
        self.system.boot()
        self.preflight(leg)
        self.seed(leg)
        steps = self.begin(leg)
        start = self.clock.now()
        with job.Signals():
            try:
                self.step(1)
                self.system.deploy(leg)
                self.step(2)
                rc, why, path = (self.run_shell if leg.runner == project.get("SHELL") else self.run_browser)(leg)
                if rc == 0:
                    self.step(3)
                    self.system.collect(leg)
            except job.Interrupted as e:
                warn("interrupted -- stopping the benchmark in '%s'" % self.ws)
                if self.task is not None and not job.kill(self.ws_driver, self.ws, self.task, "cancelled", self.here, self.clock, self.env):
                    warn("it is still running; stop it with:  %s" % self.kill_cmd)
                self.lock.release_all()
                raise Refused(job.EXIT_OF.get(e.signum, 130))
            except Refused as e:
                self.end(e.status)
                raise
        if act.dry_run():
            info("dry run -- nothing was benchmarked")
            for n, s in enumerate(steps, 1):
                log("  %d. %s" % (n, s))
            return 1 if self.dry_fails else 0
        record.write_env(os.path.join(leg.out, "env.json"), ["wall_time_s=%d" % int(self.clock.now() - start)] + self.system.after(leg),
                         update=True, machine=leg.machine)
        return self.verdict(leg, rc, why, path)

    def verdict(self, leg, rc, why, path):
        if rc == 0:
            self.end(0)
            info("BENCH OK  %s -> %s/result.json" % (leg.plan, leg.out))
            try:
                scores = [l for l in progress.normalised(path).split("\n") if SCORE.match(l)][-5:]
            except OSError:
                scores = []
            for l in scores:
                log("  " + l)
            log("")
            log("  compare with:  wk bench report <other-run> %s" % leg.out)
            return 0
        if self.task is not None and self.task.field("stopping"):
            self.end(rc)
            warn("BENCH STOPPED  %s in '%s' (by '%s')" % (leg.plan, self.ws, self.kill_cmd))
            raise Refused(rc)
        if rc == 124:
            self.end("stalled")
            die("BENCH STALLED  %s in '%s' (killed after %ss with no output)\n    log: %s" % (leg.plan, self.ws, progress.watchdog_abort(self.env), path))
        self.end(rc)
        warn(why)
        for e in progress.first_error(path):
            log("  " + e)
        log("  full log: %s" % path)
        raise Refused(rc)


def run_class(system):
    """A `--system <board>` run drives the board's browser from here (lib/wk/bench/board.py); every other system runs
    the benchmark directly, through the base `Run`."""
    from wk.bench import board
    return board.BoardRun if isinstance(system, board.BoardSystem) else Run
