"""`wk bench run`: one plan on one System -- the refusals, the preflight, the pinned payload, the task and
its progress record, the watched run, the collect and the verdict."""

import json
import os
import re
import shlex
import sys

from wk import act, buildconf, job, record as progress, screen
from wk.act import Refused, die, info, log, warn
from wk.bench import record, seed, systems
from wk.lock import Lock
from wk.machine import replace_file
from wk.resources import Resources
from wk.store import ws_name

# gpu by default: guessing gpu fails as an easy refusal, guessing cpu as a MotionMark score off llvmpipe.
CPU_PLANS = ("jetstream", "octane", "kraken", "sunspider", "ares6", "jsbench")
CORES_TOKEN = re.compile(r"^[0-9]+(-[0-9]+)?$")
PID_MATCH = "*run-benchmark* *cli.js*"
# A benchmark reports once per subtest, far less often than a compiler does.
STALL_SECONDS, ABORT_SECONDS = "900", "5400"
MAX_LOAD = 4
DEFAULT_CONFIG = "wpe-release"
SCORE = re.compile(r"^(Score|Total|.*Score:)", re.I)
BOARD_AB_ONLY = ("exclude_subtests", "no_warmup_profile", "jit_tiers")
AB_ONLY = ("rounds",) + BOARD_AB_ONLY


def bench_class(plan):
    return "cpu" if plan.startswith(CPU_PLANS) else "gpu"


def cores_valid(spec):
    return bool(spec) and all(CORES_TOKEN.match(t) for t in spec.split(","))


VARIANCE = (("aslr", "WK_BENCH_ASLR"), ("env_pad", "WK_BENCH_ENV_PAD"), ("path_pad", "WK_BENCH_PATH_PAD"),
            ("shared_cache", "WK_BENCH_SHARED_CACHE"))


def variance(env):
    """The variance knobs as set, "" when not."""
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
    replace_file(out, json.dumps(merged, indent=2))


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
        self.cfg = self.runner = self.klass = self.arch = None
        self.payload = self.id = self.task = self.rel = self.out = ""
        self.machine = None   # what holds `out`: each Run's begin names it
        self.notes = ""
        self.args = shlex.split(o.get("arm_args") or "")   # an options A/B's arm: jsc options, or MiniBrowser's


class Run:
    def __init__(self, root, reg, system, clock, env=None):
        self.root, self.reg, self.system, self.clock = str(root), reg, system, clock
        self.env = dict(os.environ if env is None else env)
        progress.default_watchdog(self.env, STALL_SECONDS, ABORT_SECONDS)
        self.here, self.ws, self.target = reg.machine, system.ws, system.target
        self.recs = self.records(clock)
        self.lock = Lock(reg.store, self.here, clock)
        self.task, self.dry_fails = None, 0
        self.kill_cmd = "wk bench run%s --kill" % ("" if reg.in_workspace() else " " + self.ws)

    def records(self, clock):
        return progress.of_target(self.target, clock, self.here, env=dict(self.target.env, WK_ABORT_SECONDS=str(progress.watchdog_abort(self.env))))

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
        name = o.get("config") or DEFAULT_CONFIG
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
        def read(path):
            r = self.target.exec(self.ws, ["cat", "%s/Tools/Scripts/%s" % (self.system.src(), path)])
            return r.out.replace("\r", "") if r.ok else None
        seeder = seed.Seeder(self.here, self.lock, os.path.join(self.reg.store.artifact_dir(), "bench"), self.reg.store.mirror())
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
        stamp, given, rnd = self.clock.stamp(), leg.o.get("task") or "", leg.o.get("round") or ""
        leg.id = "%s-%s-%s%s" % (stamp, leg.plan, self.ws, "-r%s%s" % (rnd, leg.o.get("arm", "")) if rnd else "")
        leg.task = given or "%s-%s" % (stamp, self.ws)
        leg.rel = "%s/runs/%s" % (leg.task, leg.id)
        leg.machine, bench = record.leg_home(self.reg, self.ws, given)
        taskdir = os.path.join(bench, leg.task)
        leg.out = os.path.join(taskdir, "runs", leg.id)
        steps = ["deploy %s to the %s '%s'" % (leg.cfg.name, self.system.kind, self.ws),
                 "run %s (%s, %s iteration(s))" % (leg.plan, leg.runner, leg.count or "default"), "collect into %s" % leg.out]
        if act.dry_run():
            return steps
        if not given and leg.machine.exists(taskdir):
            die("task %s already exists (%s); a task is one request, made once" % (leg.task, taskdir))
        if not task_held(self.env, leg.task):
            self.lock.hold("bench-task-" + leg.task, timeout=5)
        count = ["count=" + leg.count] if leg.count else []
        command = "wk bench run %s %s --config %s%s" % (self.ws, leg.plan, leg.cfg.name, " --count " + leg.count if leg.count else "")
        if not given:
            record.task_write(taskdir, ["task=" + leg.task, "requested=" + self.clock.iso(), "subject.kind=workspace",
                                        "subject.spec=" + self.ws, "devices=%s=%s" % (self.system.kind, leg.cfg.name),
                                        "plans=" + leg.plan, "rounds=1", "slots=" + self.ws, "restart=%s --task %s" % (command, leg.task)] + count,
                              [command], machine=leg.machine)
        leg.machine.mkdir(leg.out)
        record.write_env(os.path.join(leg.out, "env.json"), [
            "plan=" + leg.plan, "workspace=" + self.ws, "config=" + leg.cfg.name, "browser=" + leg.browser, "task=" + leg.task,
            "webkit_sha=" + self.system.sha(), "count=" + leg.count, "local_copy=" + leg.payload,
            "software_reason=" + leg.software_reason, "class=" + leg.klass, "runner=" + leg.runner, "arch=" + leg.arch,
            "bench_host=" + self.system.bench_host, "preflight_notes=" + leg.notes, "cores.set=" + leg.cores]
            + (["ab.round=" + rnd, "ab.arm=" + leg.o.get("arm", ""), "ab.slot_a=" + leg.o.get("slot_a", ""),
                "ab.slot_b=" + leg.o.get("slot_b", ""), "arm_args=" + shlex.join(leg.args)] if rnd else [])
            + self.system.facts(leg) + configuration_fields(self.env),
            bool_fields=["forced=" + act.forced(self.env), "software=" + ("1" if leg.software else ""), "cores.pinned=" + leg.cores],
            machine=leg.machine)
        self.task = self.recs.begin("bench", "here", self.ws, self.kill_cmd, os.path.join(leg.out, "run.log"), steps)
        return steps

    def step(self, n):
        if self.task is not None:
            self.task.step(n)

    def end(self, word):
        if self.task is not None:
            self.task.end(word)
        self.lock.release_all()

    def watched(self, argv, cwd, path):
        watcher = None
        if self.task is not None:
            self.task.set("log", path)
            watcher = job.PidWatch(self.target, self.ws, self.task, path, "bench", PID_MATCH, job.pid_tries(self.env))
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
            rc = s.run(leg, self.script(leg, exports, s.payload_dir(leg), [shlex.quote(jsc)] + [shlex.quote(a) for a in leg.args] + ["cli.js", "--"] + cli), self.watched, logs[-1])
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
        extra = (["--headless"] if leg.software else []) + (leg.o.get("browser_args") or "").split() + leg.args
        args += ["--"] + extra if extra else []
        info("running %s in '%s' (%s, %s)" % (leg.plan, self.ws, leg.cfg.name, leg.browser))
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


def _run_class(system):
    """A `--system <board>` run is run-benchmark here driving the board's browser (lib/wk/bench/board.py); every other
    system runs run-benchmark or the jsc shell directly, through the base `Run`."""
    from wk.bench import board
    return board.BoardRun if isinstance(system, board.BoardSystem) else Run


def nothing_left(reg, ws, plan, task):
    """A one-run task restarted with --task: whether it already holds its run ok. An A/B restarts through its own command."""
    d = os.path.join(record.leg_home(reg, ws, task)[1], task)
    doc = record.task_doc(d)
    if len(record.task_arms(doc)[0]) != 1:
        die("task %s is an A/B; restart it with its own command:\n    %s" % (task, doc.get("restart") or doc.get("commands", ["?"])[-1]))
    if plan not in doc.get("plans", []):
        die("task %s measures %s, not %s" % (task, ", ".join(doc.get("plans", [])), plan))
    st = record.task_state(d, False)
    if st["ok"] < st["planned"]:
        return False
    info("task %s already holds its run ok (%s); nothing is left to run" % (task, st["summary"]))
    return True


def run(root, reg, words, o, kill, clock):
    """`wk bench run <ws> <plan>`: the dispatcher resolved the workspace (WK_NAME) and dropped it from `words`."""
    ws, plan = ws_name(reg.env), (words[0] if words else "")
    if not ws or not (plan or kill):
        die("usage: wk bench run <workspace> <plan> [options]; see wk bench -h")
    ab = not kill and (o.get("ab") or o.get("ab_systems"))
    options = not kill and (o.get("a_args") is not None or o.get("b_args") is not None)
    if options and (ab or o.get("system")):
        die("--a-args and --b-args are an A/B of one build in this workspace; on a board the arms are\n"
            "    slots (--ab) or systems (--ab-systems)")
    alone = [k for k in (BOARD_AB_ONLY if options else () if ab else AB_ONLY) if o.get(k)]
    if alone:
        die("--%s belongs to an A/B on a board (--ab or --ab-systems)" % alone[0].replace("_", "-"))
    if options:
        from wk.bench import board_ab
        return board_ab.ArgsAB(root, reg, ws, plan, o, clock).go()
    if o.get("system") and reg.in_workspace() and (ab or o.get("collect")):
        die("an A/B or a collection on a board is not a request a workspace can make; run it on the workstation:\n"
            "    wk bench run %s %s --system %s ..." % (ws, plan, o["system"]))
    if ab:
        from wk.bench import board_ab
        return board_ab.run(root, reg, ws, plan, o, clock)
    if o.get("system") and reg.in_workspace():
        from wk.bench import board
        return board.request(root, reg, "run", ["machine=" + o["system"], "workspace=" + ws, "plan=" + plan, "slot=" + (o.get("slot") or ""),
                                                "count=" + (o.get("count") or "")], "wk bench run %s %s --system %s" % (ws, plan, o["system"]))
    if o.get("task") and not kill and nothing_left(reg, ws, plan, o["task"]):
        return 0
    system = systems.for_workspace(root, reg, ws, clock, o.get("system") or "")
    if o.get("collect") and system.kind != "board":
        die("--collect takes a PGO profile from a board's instrumented slot: --system <board> --slot <name>-instr")
    r = _run_class(system)(root, reg, system, clock, reg.env)
    if kill:
        return r.stop()
    if o.get("task") and not act.dry_run():
        r.lock.hold("bench-task-" + o["task"], timeout=5)
    return r.go(plan, o)

