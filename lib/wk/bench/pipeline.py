"""`wk bench run`: one plan on one System -- the refusals, the preflight, the pinned payload, the task and
its progress record, the watched run, the collect and the verdict."""

import json
import os
import re
import shlex
import subprocess
import sys

from wk import act, buildconf, job, record as progress
from wk.act import Refused, die, info, log, warn
from wk.bench import record, seed, systems
from wk.lock import Lock
from wk.quiet import lib_argv
from wk.resources import Resources

# gpu by default: guessing gpu fails as an easy refusal, guessing cpu as a MotionMark score off llvmpipe.
CPU_PLANS = ("jetstream", "octane", "kraken", "sunspider", "ares6", "jsbench")
CORES_TOKEN = re.compile(r"^[0-9]+(-[0-9]+)?$")
PID_MATCH = "*run-benchmark* *cli.js*"
# A benchmark reports once per subtest, far less often than a compiler does.
STALL_SECONDS, ABORT_SECONDS = "900", "5400"
MAX_LOAD = 4
SCORE = re.compile(r"^(Score|Total|.*Score:)", re.I)
QUIET = "lib/quiet.sh"
AB_ONLY = ("rounds", "task", "exclude_subtests", "no_warmup_profile", "jit_tiers")


def bench_class(plan):
    return "cpu" if plan.startswith(CPU_PLANS) else "gpu"


def cores_valid(spec):
    return bool(spec) and all(CORES_TOKEN.match(t) for t in spec.split(","))


def knob(env, name):
    v = env.get(name) or "0"
    return int(v) if v.isdigit() else 0


def env_pad_prelude(env):
    """Source for the benchmark's own shell: envp's size moves where the initial stack starts."""
    n = knob(env, "WK_BENCH_ENV_PAD")
    return 'export WK_BENCH_ENV_PAD_DUMMY="$(head -c %d /dev/zero | tr "\\0" x)"; ' % n if n else ""


def padded(path, n):
    """The link a run goes through so the executable's own path, which also lands on the stack, is `n` characters of padding longer."""
    return os.path.join("/tmp/wk-bench-pad-" + "p" * n, os.path.basename(path))


def configuration_fields(env):
    """The knobs, recorded into `configuration` for `wk bench report` to group by; path_len is the pad asked for."""
    out = ["configuration.aslr=off"] if env.get("WK_BENCH_ASLR") == "off" else []
    for name, key in (("WK_BENCH_ENV_PAD", "env_pad_bytes"), ("WK_BENCH_PATH_PAD", "path_len")):
        if knob(env, name):
            out.append("configuration.%s=%d" % (key, knob(env, name)))
    return out


def _last_json(path):
    """A jsc-shell log carries the driver's resultsJSON() on one line, and jsc's exit noise after it."""
    for line in reversed(progress.normalised(path).split("\n")):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            if isinstance(doc, dict):
                return doc
    return None


def _merge(into, other):
    for key, value in other.items():
        if key not in into:
            into[key] = value
        elif isinstance(value, dict) and isinstance(into[key], dict):
            _merge(into[key], value)
        elif key == "current" and isinstance(value, list) and isinstance(into[key], list):
            into[key].extend(value)
    return into


def merge_jsc_logs(out, logs):
    """Each iteration's scores appended onto one tree, the shape run-benchmark's --count writes."""
    merged, missing = None, []
    for path in logs:
        one = _last_json(path)
        if one is None:
            missing.append(path)
        else:
            merged = one if merged is None else _merge(merged, one)
    if merged is None:
        die("no results in any iteration log -- the suite printed no JSON. A payload whose cli.js does not\n"
            "    accept --dump-json-results runs the whole suite and reports only in its own text format.")
    if missing:
        warn("no results in %s" % ", ".join(missing))
    with open(out, "w") as f:
        json.dump(merged, f, indent=2)


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
        die("cannot read the load average on this machine, and an idle machine is what a measurement needs")


class Leg:
    """One run of one plan: what was asked, what it resolved to, and where it lands."""

    def __init__(self, plan, o):
        self.plan, self.o = plan, o
        self.count, self.subtests, self.cores = o.get("count") or "", o.get("subtests") or "", o.get("cores") or ""
        self.software, self.software_reason, self.browser = bool(o.get("software")), "", o.get("browser") or ""
        self.cfg = self.runner = self.klass = self.arch = None
        self.payload = self.id = self.task = self.rel = self.out = ""
        self.notes = ""


class Run:
    def __init__(self, root, reg, system, clock, env=None, popen=subprocess.Popen):
        self.root, self.reg, self.system, self.clock, self.popen = str(root), reg, system, clock, popen
        self.env = dict(os.environ if env is None else env)
        self.env.setdefault("WK_STALL_SECONDS", STALL_SECONDS)
        self.env.setdefault("WK_ABORT_SECONDS", ABORT_SECONDS)
        self.here, self.ws, self.target = reg.machine, system.ws, system.target
        self.bench_dir = reg.store.bench_dir()
        self.recs = self.records(clock)
        self.lock = Lock(reg.store, self.here, clock)
        self.task, self.dry_fails = None, 0
        self.kill_cmd = "wk bench run%s --kill" % ("" if reg.in_workspace() else " " + self.ws)

    def records(self, clock):
        return progress.of_target(self.target, clock, self.here, env=dict(self.target.env, WK_ABORT_SECONDS=self.env["WK_ABORT_SECONDS"]))

    def put(self):
        self.target.task_put(self.ws, self.task)

    def stop(self):
        rc = job.stop(self.target, self.recs, self.ws, "bench", self.here, self.clock, self.env)
        if rc == 1:
            die("the benchmark in '%s' outlived a TERM and a KILL.\n    Look at it:  wk enter %s" % (self.ws, self.ws))
        return 0

    def leg(self, plan, o):
        s, leg = self.system, Leg(plan, o)
        if leg.cores:
            if not cores_valid(leg.cores):
                die("--cores '%s' is not a valid Linux cpu list (e.g. 0-3, 2,3, 0-1,4, 7)" % leg.cores)
            if s.cores_refusal():
                die("--cores: " + s.cores_refusal())
        name = o.get("config") or self.env.get("WK_CONFIG") or "wpe-release"
        try:
            leg.cfg = buildconf.resolve(name, self.target.os(), self.target.kind, self.target.env)
        except LookupError:
            die("unknown config '%s' (wk build --list)" % name)
        leg.klass, leg.arch = bench_class(plan), self.target.arch(self.ws)
        leg.runner = "jsc" if leg.cfg.jsc_only else "browser"
        if leg.runner == "jsc" and leg.klass == "gpu":
            die("%s is a gpu-class benchmark and %s builds no browser.\n    Either build a browser port (wk build %s wpe-release) and pass\n"
                "    --config wpe-release, or run a cpu-class plan -- jetstream3, octane,\n    kraken, sunspider, ares6 -- which the jsc shell can drive directly."
                % (plan, name, self.ws))
        if leg.klass == "gpu" and not s.has_gpu(leg.arch):
            die("%s is gpu-class and '%s' is an %s workspace, which has no GPU.\n    cpu-class plans (jetstream3, octane, kraken, sunspider) do run in here,\n"
                "    with either a browser or a JSCOnly config. For a 32-bit rendering number\n    measure a board:  wk bench run %s %s --system <board>" % (plan, self.ws, leg.arch, self.ws, plan))
        if leg.runner == "browser":
            leg.browser = leg.browser or s.default_browser(leg.cfg)
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
        mark = ("\033[32mok\033[0m  " if ok else "\033[31mFAIL\033[0m") if sys.stderr.isatty() else ("ok  " if ok else "FAIL")
        sys.stderr.write("  %s  %-34s %s\n" % (mark, what, detail))

    def busy_builds(self):
        return sum(1 for t in self.recs.list() if t.field("kind") == "build" and t.verdict("capped") in ("running", "silent", "starting"))

    def idle_rows(self):
        busy, load = self.busy_builds(), one_minute_load(Resources(self.here, self.reg.env, self.system.host_os))
        if busy:
            return [(False, "no builds running", "%d build(s) in progress" % busy)]
        if round(load) > int(self.reg.env.get("WK_BENCH_MAX_LOAD") or MAX_LOAD):
            return [(False, "machine idle", "1-minute load average is %.2f" % load)]
        return [(True, "machine idle", "load %.2f, no wk builds" % load)]

    def preflight(self, leg):
        info("preflight")
        rows, notes = self.system.checks(leg)
        ok, detail = self.system.build_present(leg)
        rows = [(ok, "build present", detail)] + rows + self.idle_rows()
        fails = [r for r in rows if not r[0]]
        for ok, what, detail in rows:
            self.check(ok, what, detail)
        leg.notes = "".join("%s: %s; " % (w, d.replace('"', "").replace("\\", "")) for _, w, d in fails) + "".join(n + "; " for n in notes)
        sys.stderr.write("\n")
        if not fails:
            info("preflight clean")
        elif act.dry_run():
            self.dry_fails = len(fails)
            warn("%d preflight check(s) would fail -- a real run would stop here" % len(fails))
        elif self.env.get("WK_FORCE"):
            warn("%d preflight check(s) failed -- continuing because --force was given" % len(fails))
            warn("the run will be recorded as forced, and is not comparable with a clean run")
        else:
            die("%d preflight check(s) failed -- refusing to produce a number that cannot be defended.\n"
                "    Fix the above, or re-run with --force to record a result anyway." % len(fails))

    def seed(self, leg):
        def read(path):
            r = self.target.exec(self.ws, ["cat", "%s/Tools/Scripts/%s" % (self.system.src(), path)])
            return r.out.replace("\r", "") if r.ok else None
        seeder = seed.Seeder(self.here, self.lock, os.path.join(self.reg.store.artifact_dir(), "bench"))
        leg.payload = seeder.seed(leg.plan, seed.plan_json(read, leg.plan))
        if leg.runner != "jsc":
            return
        if not leg.payload:
            die("%s has no seeded payload, and the jsc runner has nothing to run without one.\n    'wk bench seed %s %s' fetches it; "
                "a plan whose source cannot be pre-seeded can only be run with a browser config." % (leg.plan, self.ws, leg.plan))
        if not act.dry_run() and not self.here.exists(os.path.join(leg.payload, "cli.js")):
            die("%s has no cli.js, so %s cannot be driven from a JavaScript shell. Run it with a browser config\n"
                "    instead (--config wpe-release), which is the official number for every plan anyway." % (leg.payload, leg.plan))

    def begin(self, leg):
        """The task (task.json, under its lock) and its run directory, the env.json the report reads, and the progress record."""
        stamp = self.clock.stamp()
        leg.id, leg.task = "%s-%s-%s" % (stamp, leg.plan, self.ws), "%s-%s" % (stamp, self.ws)
        leg.rel = "%s/runs/%s" % (leg.task, leg.id)
        leg.out = os.path.join(self.bench_dir, leg.rel)
        steps = ["deploy %s to the %s '%s'" % (leg.cfg.name, self.system.kind, self.ws),
                 "run %s (%s, %s iteration(s))" % (leg.plan, leg.runner, leg.count or "default"), "collect into %s" % leg.out]
        if act.dry_run():
            return steps
        taskdir = os.path.join(self.bench_dir, leg.task)
        if os.path.exists(taskdir):
            die("task %s already exists (%s); a task is one request, made once" % (leg.task, taskdir))
        self.lock.hold("bench-task-" + leg.task, timeout=5)
        count = ["count=" + leg.count] if leg.count else []
        record.task_write(taskdir, ["task=" + leg.task, "requested=" + self.clock.iso(), "subject.kind=workspace",
                                    "subject.spec=" + self.ws, "devices=%s=%s" % (self.system.kind, leg.cfg.name),
                                    "plans=" + leg.plan, "rounds=1", "slots=" + self.ws] + count,
                          ["wk bench run %s %s --config %s%s" % (self.ws, leg.plan, leg.cfg.name, " --count " + leg.count if leg.count else "")])
        os.makedirs(leg.out, exist_ok=True)
        record.write_env(os.path.join(leg.out, "env.json"), [
            "plan=" + leg.plan, "workspace=" + self.ws, "config=" + leg.cfg.name, "browser=" + leg.browser, "task=" + leg.task,
            "webkit_sha=" + self.system.sha(), "count=" + leg.count, "local_copy=" + leg.payload,
            "software_reason=" + leg.software_reason, "class=" + leg.klass, "runner=" + leg.runner, "arch=" + leg.arch,
            "bench_host=" + self.system.bench_host, "preflight_notes=" + leg.notes, "cores.set=" + leg.cores]
            + self.system.facts(leg) + configuration_fields(self.env),
            bool_fields=["forced=" + (self.env.get("WK_FORCE") or ""), "software=" + ("1" if leg.software else ""), "cores.pinned=" + leg.cores])
        self.task = self.recs.begin("bench", "here", self.ws, self.kill_cmd, os.path.join(leg.out, "run.log"), steps)
        return steps

    def step(self, n):
        if self.task is not None:
            self.task.step(n)
            self.put()

    def end(self, word):
        if self.task is not None:
            self.task.end(word)
            self.put()
        self.lock.release_all()

    def watched(self, argv, cwd, path):
        watcher = None
        if self.task is not None:
            self.task.set("log", path)
            watcher = job.PidWatch(self.target, self.ws, self.task, path, "bench", PID_MATCH, int(self.env.get("WK_JOB_PID_TRIES") or 900))
            watcher.start()
        try:
            return job.watch(argv, path, self.here, self.clock, self.env, cwd, self.popen)
        finally:
            if watcher is not None:
                watcher.stop()

    def prefix(self, leg):
        """What the benchmark is exec'd through: the core pin, then ASLR off."""
        out = "taskset -c %s " % leg.cores if leg.cores else ""
        return out + (self.system.aslr_prefix() if self.env.get("WK_BENCH_ASLR") == "off" else "")

    def through_pad(self, path):
        n = knob(self.env, "WK_BENCH_PATH_PAD")
        if not n:
            return path
        self.system.link(path, padded(path, n))
        return padded(path, n)

    def script(self, leg, exports, cwd, argv):
        head = 'echo "wk: bench pid $$" >&2\n' + "".join("export %s\n" % e for e in exports)
        return head + env_pad_prelude(self.env) + "cd %s && exec %s%s" % (shlex.quote(cwd), self.prefix(leg), " ".join(argv))

    def run_jsc(self, leg):
        s, cfg = self.system, leg.cfg
        jsc, var, lib = self.through_pad(cfg.jsc_path(s.src())), cfg.run_var(), cfg.run_dir(s.src())
        cli = ["--dump-json-results"] + (["--test=" + ",".join(leg.subtests.split())] if leg.subtests else [])
        n = int(leg.count or 1)
        info("running %s in '%s' (%s, jsc shell, %d iteration(s))" % (leg.plan, self.ws, cfg.name, n))
        log("  results: %s" % leg.out)
        logs = []
        for i in range(1, n + 1):
            if n > 1:
                info("iteration %d/%d" % (i, n))
            logs.append(os.path.join(leg.out, "run-%d.log" % i))
            exports = ['%s="%s${%s:+:${%s}}"' % (var, lib, var, var)]
            rc = s.run(leg, self.script(leg, exports, s.payload_dir(leg), [shlex.quote(jsc), "cli.js", "--"] + cli), self.watched, logs[-1])
            if rc != 0:
                return rc, "jsc exited %d on iteration %d" % (rc, i), logs[-1]
        if not act.dry_run():
            merge_jsc_logs(os.path.join(leg.out, "result.json"), logs)
        return 0, "", logs[0]

    def run_browser(self, leg):
        s, src = self.system, self.system.src()
        args = s.runner_argv(leg) + ["--plan", leg.plan, "--build-directory", self.through_pad(s.build_dir(leg)),
                                     "--output-file", os.path.join(s.run_dir(leg), "result.json"), "--no-adjust-unit", "--show-iteration-values"]
        args += (["--count", leg.count] if leg.count else []) + (["--local-copy", s.payload_dir(leg)] if leg.payload else [])
        args += ["--timeout", leg.o["timeout"]] if leg.o.get("timeout") else []
        args += ["--subtests"] + leg.subtests.split() if leg.subtests else []
        extra = (["--headless"] if leg.software else []) + (leg.o.get("browser_args") or "").split()
        args += ["--"] + extra if extra else []
        info("running %s in '%s' (%s, %s)" % (leg.plan, self.ws, leg.cfg.name, leg.browser))
        log("  results: %s" % leg.out)
        path, watch = os.path.join(leg.out, "run.log"), os.path.join(leg.out, "screen-watch")
        # For as long as the browser is up: a dialog that draws mid-run covers every leg after it.
        self.here.act_run(lib_argv(self.root, QUIET, "screen_watch_start", watch))
        rc = s.run(leg, self.script(leg, s.run_env(leg), src, [shlex.quote(a) for a in args]), self.watched, path)
        drew = self.here.run(lib_argv(self.root, QUIET, "screen_watch_stop", watch))
        if not drew.ok:
            warn("the machine did not stay quiet under this run -- something drew over the\n"
                 "  browser, or a process that must not run came back while it measured:")
            sys.stderr.write("".join("    %s\n" % l for l in drew.out.splitlines()))
            if not self.env.get("WK_FORCE"):
                rc = rc or 1
            else:
                warn("  --force: keeping the number anyway; it is one to distrust")
        return rc, "run-benchmark exited %d" % rc, path

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
                rc, why, path = (self.run_jsc if leg.runner == "jsc" else self.run_browser)(leg)
                if rc == 0:
                    self.step(3)
                    self.system.collect(leg)
            except job.Interrupted as e:
                warn("interrupted -- stopping the benchmark in '%s'" % self.ws)
                if self.task is not None and not job.kill(self.target, self.ws, self.task, "cancelled", self.here, self.clock, self.env):
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
                         update=True)
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
            die("BENCH STALLED  %s in '%s' (killed after %ss with no output)\n    log: %s" % (leg.plan, self.ws, self.env["WK_ABORT_SECONDS"], path))
        self.end(rc)
        warn(why)
        for e in progress.first_error(path):
            log("  " + e)
        log("  full log: %s" % path)
        raise Refused(rc)


def _run_class(system):
    """A `--system <mac>` run is driven over ssh through that install's own `wk bench staged` (lib/wk/bench/mac.py), a
    `--system <board>` run is run-benchmark here driving the board's browser (lib/wk/bench/board.py); every other
    system runs run-benchmark or the jsc shell directly, through the base `Run`."""
    from wk.bench import board, mac
    if isinstance(system, board.BoardSystem):
        return board.BoardRun
    return mac.HostRun if isinstance(system, mac.MacHostSystem) else Run


def run(root, reg, words, o, kill, clock, popen=subprocess.Popen):
    """`wk bench run <ws> <plan>`: the dispatcher resolved the workspace (WK_NAME) and dropped it from `words`."""
    ws, plan = reg.env.get("WK_NAME", ""), (words[0] if words else "")
    if not ws or not (plan or kill):
        die("usage: wk bench run <workspace> <plan> [options]; see wk bench -h")
    ab = not kill and (o.get("ab") or o.get("ab_systems"))
    alone = [k for k in AB_ONLY if o.get(k)] if not ab else []
    if alone:
        die("--%s belongs to an A/B on a board (--ab or --ab-systems)" % alone[0].replace("_", "-"))
    if o.get("system") and reg.in_workspace() and (ab or o.get("collect")):
        die("an A/B or a collection on a board is not a request a workspace can make; run it on the workstation:\n"
            "    wk bench run %s %s --system %s ..." % (ws, plan, o["system"]))
    if ab:
        from wk.bench import board_ab
        return board_ab.run(root, reg, ws, plan, o, clock, popen)
    if o.get("system") and reg.in_workspace():
        from wk.bench import board
        return board.request(root, reg, "run", ["machine=" + o["system"], "workspace=" + ws, "plan=" + plan, "slot=" + (o.get("slot") or ""),
                                                "count=" + (o.get("count") or "")], "wk bench run %s %s --system %s" % (ws, plan, o["system"]))
    system = systems.for_workspace(root, reg, ws, clock, o.get("system") or "")
    if o.get("collect") and system.kind != "board":
        die("--collect takes a PGO profile from a board's instrumented slot: --system <board> --slot <name>-instr")
    r = _run_class(system)(root, reg, system, clock, reg.env, popen)
    if kill:
        return r.stop()
    if not act.dry_run():
        os.makedirs(r.bench_dir, exist_ok=True)
    return r.go(plan, o)

