"""`wk bench run`: one plan on one System -- refusals, preflight, pinned payload, task and progress record, the watched run,
collect, verdict. Every system goes through this one `Run`; what differs is the System's (lib/wk/bench/systems.py)."""

import os
import re
import shlex
import sys

from wk import act, job, project, record as progress, screen
from wk.act import Refused, die, info, log, warn
from wk.bench import record
from wk.lock import Lock

CORES_TOKEN = re.compile(r"^[0-9]+(-[0-9]+)?$")
# A benchmark reports once per subtest, far less often than a compiler does.
STALL_SECONDS, ABORT_SECONDS = "900", "5400"
SCORE = re.compile(r"^(Score|Total|.*Score:)", re.I)
VARIANCE = (("aslr", "WK_BENCH_ASLR"), ("env_pad", "WK_BENCH_ENV_PAD"), ("path_pad", "WK_BENCH_PATH_PAD"),
            ("shared_cache", "WK_BENCH_SHARED_CACHE"))


def check_cores(spec):
    if spec and not all(CORES_TOKEN.match(t) for t in spec.split(",")):
        die("--cores '%s' is not a valid Linux cpu list (e.g. 0-3, 2,3, 0-1,4, 7)" % spec)


def ab_fields(o):
    return dict({"ab.round": o["round"]}, **{"ab." + k: o.get(k, "") for k in ("arm", "slot_a", "slot_b")}) if o.get("round") else {}


def variance(env):
    return {key: env.get(name, "") for key, name in VARIANCE}


def knob(env, key):
    v = variance(env)[key]
    return int(v) if v.isdigit() else 0


def task_held(env, task):
    """The A/B that started this run holds its task's lock already."""
    return env.get("WK_TASK_HELD") == task


def configuration_fields(env):
    """The knobs, recorded into `configuration` for `wk bench report` to group by; path_len is the pad asked for."""
    v = variance(env)
    out = {"configuration.aslr": "off"} if v["aslr"] == "off" else {}
    if v["shared_cache"] == "avoid":
        out["configuration.shared_cache"] = False
    for key, field in (("env_pad", "env_pad_bytes"), ("path_pad", "path_len")):
        if knob(env, key):
            out["configuration." + field] = str(knob(env, key))
    return out


def check(ok, what, detail):
    word, colour = ("unk ", "33") if ok is record.UNKNOWN else ("ok  ", "32") if ok else ("FAIL", "31")
    mark = "\033[%sm%s\033[0m" % (colour, word) if sys.stderr.isatty() else word
    sys.stderr.write("  %s  %-34s %s\n" % (mark, what, detail))


class Run:
    def __init__(self, system, kit, env=None):
        s = self.system = system
        self.root, self.reg, self.clock, self.kit, self.here = s.root, s.reg, s.clock, kit, s.reg.machine
        self.env = dict(os.environ if env is None else env)
        progress.default_watchdog(self.env, STALL_SECONDS, ABORT_SECONDS)
        self.lock = Lock(self.reg.store, self.here, self.clock)
        self.task, self.dry_fails = None, 0
        self.kill_cmd = s.kill_cmd()

    def preflight(self, leg):
        info("preflight")
        rows, notes = self.system.checks(leg)
        fails = record.failed(rows)
        for ok, what, detail in rows:
            check(ok, what, detail)
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

    def begin(self, leg):
        """The task (task.json, under its lock) and its run directory, the env.json the report reads, and the progress record."""
        s, o, stamp = self.system, leg.o, self.clock.stamp()
        given, name = o.get("task") or "", s.name(leg)
        leg.id = "-".join([stamp, leg.plan, name] + (["r%s%s" % (o["round"], o.get("arm", ""))] if o.get("round") else [])
                          + (["settle"] if o.get("settle") else []))
        leg.task = given or "%s-%s" % (stamp, name)
        leg.machine, bench = s.tasks()
        if o.get("pgo_dir"):   # one leg of the profile the next build reads, in no task
            leg.task, leg.out = "", os.path.join(o["pgo_dir"], leg.plan)
        else:
            leg.out = os.path.join(bench, leg.task, "runs", leg.id)
        steps = ["run %s on %s (%s, %s iteration(s))" % (leg.plan, s.label(leg), leg.runner, leg.count or "default"), "collect into %s" % leg.out]
        if act.dry_run():
            return steps
        if given:
            if not task_held(self.env, leg.task):
                self.lock.hold("bench-task-" + leg.task, timeout=5)
        elif leg.task:
            kind, spec, device, flags = s.subject(leg)
            command = "wk bench run %s %s%s%s" % (s.ws, leg.plan, flags, " --count " + leg.count if leg.count else "")
            record.new_task(leg.machine, bench, leg.task, self.lock, self.clock.iso(), {
                "subject.kind": kind, "subject.spec": spec, "devices": device, "plans": leg.plan, "rounds": "1", "slots": spec,
                **({"count": leg.count} if leg.count else {})}, command)
        if o.get("pgo_dir"):
            leg.machine.remove(leg.out)
        leg.machine.mkdir(leg.out)
        fields = {"plan": leg.plan, "task": leg.task, "count": leg.count, "class": leg.klass, "runner": leg.runner, "arch": leg.arch,
                  "bench_host": s.bench_host, "local_copy": leg.payload, "preflight_notes": leg.notes, "cores.set": leg.cores,
                  "cores.pinned": bool(leg.cores), "software_reason": leg.software_reason, "software": leg.software,
                  "forced": bool(act.forced(self.env)), "subtests_excluded": o.get("excluded", ""),
                  **({"warmup": True, "warmup_kind": "settle" if o.get("settle") else "evidence"} if o.get("settle") or o.get("warmup") else {})}
        fields.update(ab_fields(o), **({"arm_args": shlex.join(leg.args)} if o.get("round") else {}))
        fields.update(configuration_fields(self.env))
        fields.update(s.env_fields(leg))
        fields.update(s.facts(leg))
        record.write_env(os.path.join(leg.out, "env.json"), fields, machine=leg.machine)
        reading = s.reading(leg)
        if reading:
            leg.machine.write(os.path.join(leg.out, "profile-check.json"), self.here.read(reading))
        self.task = s.progress(self.env, self.kill_cmd, os.path.join(leg.out, "run.log"), steps)
        return steps

    def step(self, n):
        if self.task is not None:
            self.task.step(n)

    def end(self, word):
        if self.task is not None:
            self.task.end(word)
        self.lock.release_all()

    def watched(self, argv, cwd, path):
        s, watcher = self.system, None
        if self.task is not None:
            self.task.set("log", path)
        if self.task is not None and s.pid_watch:
            watcher = job.PidWatch(s.ws_driver, s.ws, self.task, path, "bench", project.get("BENCH_PID_MATCH"), job.pid_tries(self.env))
            watcher.start()
        try:
            return job.watch(argv, path, self.here, self.clock, self.env, cwd)
        finally:
            if watcher is not None:
                watcher.stop()

    def pad(self, path):
        """The link a run goes through so the executable's own path, which also lands on the stack, is that many characters longer."""
        n = knob(self.env, "path_pad")
        if not n:
            return path
        link = os.path.join("/tmp/wk-bench-pad-" + "p" * n, os.path.basename(path))
        self.system.link(path, link)
        return link

    def script(self, leg, exports, cwd, argv):
        head = 'echo "wk: bench pid $$" >&2\n' + "".join("export %s\n" % e for e in exports)
        prefix = ""
        if self.system.local:
            n = knob(self.env, "env_pad")   # envp's size moves where the initial stack starts
            head += 'export WK_BENCH_ENV_PAD_DUMMY="$(head -c %d /dev/zero | tr "\\0" x)"; ' % n if n else ""
            prefix = ("taskset -c %s " % leg.cores if leg.cores else "") + (self.system.aslr_prefix() if variance(self.env)["aslr"] == "off" else "")
        return head + "cd %s && exec %s%s" % (shlex.quote(cwd), prefix, " ".join(argv))

    def run_shell(self, leg):
        """The JavaScript shell over the payload's own driver, one log per iteration, merged into one result."""
        s, preset = self.system, leg.preset
        shell, var, lib = self.pad(preset.jsc_path(s.src())), preset.run_var(), preset.run_dir(s.src())
        n, logs = int(leg.count or 1), []
        info("running %s in '%s' (%s, %s shell, %d iteration(s))" % (leg.plan, s.ws, preset.name, project.get("SHELL"), n))
        log("  results: %s" % leg.out)
        for i in range(1, n + 1):
            if n > 1:
                info("iteration %d/%d" % (i, n))
            logs.append(os.path.join(leg.out, "run-%d.log" % i))
            argv = self.kit.shell_argv(shlex.quote(shell), [shlex.quote(a) for a in leg.args], leg.subtests)
            rc = s.run(leg, self.script(leg, ['%s="%s${%s:+:${%s}}"' % (var, lib, var, var)], s.payload_dir(leg), argv), self.watched, logs[-1])
            if rc != 0:
                return rc, "%s exited %d on iteration %d" % (project.get("SHELL"), rc, i), logs[-1]
        if not act.dry_run():
            self.kit.merge_jsc_logs(os.path.join(leg.out, "result.json"), logs)
        return 0, "", logs[0]

    def run_browser(self, leg):
        s, path = self.system, os.path.join(leg.out, "run.log")
        exports, cwd, argv = s.runner(leg, self.kit, self.pad)
        info("running %s on %s (%s)" % (leg.plan, s.label(leg), leg.browser or leg.slot))
        log("  results: %s" % leg.out)
        # For as long as the browser is up: a dialog that draws mid-run covers every leg after it.
        watch = screen.Watch(self.here, self.root, self.clock, self.env) if s.local and not act.dry_run() else None
        if watch is not None:
            watch.start()
        rc = s.run(leg, self.script(leg, exports, cwd, argv), self.watched, path)
        drew = watch.stop() if watch is not None else []
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
        s = self.system
        s.refuse(o)
        leg = s.leg(plan, o, self.kit)
        given = o.get("task") and not o.get("pgo_dir")
        if given and not s.tasks()[0].exists(os.path.join(s.tasks()[1], o["task"], "task.json")):
            die("no such task '%s' on %s (%s); 'wk bench ls' lists the tasks and where each is" % (o["task"], s.label(leg), s.tasks()[1]))
        s.boot(leg)
        self.preflight(leg)
        leg.payload = s.payload(leg, self.kit, self.lock)
        steps = self.begin(leg)
        start = self.clock.now()
        with job.Signals():
            try:
                self.step(1)
                rc, why, path = (self.run_shell if leg.runner == project.get("SHELL") else self.run_browser)(leg)
                self.step(2)
                s.collect(leg, rc)
                result = os.path.join(leg.out, "result.json")
                if rc == 0 and not act.dry_run() and not (leg.machine.exists(result) and leg.machine.read(result).strip()):
                    rc, why = 1, "%s exited 0 with no result" % os.path.basename(project.get("BENCH_RUNNER"))
            except job.Interrupted as e:
                warn("interrupted -- stopping the benchmark on %s" % s.label(leg))
                if self.task is not None and not job.kill(s.ws_driver, s.ws, self.task, "cancelled", self.here, self.clock, self.env):
                    warn("it is still running; stop it with:  %s" % self.kill_cmd)
                self.lock.release_all()
                raise Refused(job.EXIT_OF.get(e.signum, 130))
            except Refused as e:
                self.end(e.status)
                raise
        if act.dry_run():
            info("dry run -- nothing was benchmarked")
            for n, step in enumerate(steps, 1):
                log("  %d. %s" % (n, step))
            return 1 if self.dry_fails else 0
        record.write_env(os.path.join(leg.out, "env.json"), dict({"wall_time_s": "%d" % int(self.clock.now() - start)}, **s.after(leg)),
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
            warn("BENCH STOPPED  %s on %s (by '%s')" % (leg.plan, self.system.label(leg), self.kill_cmd))
            raise Refused(rc)
        if rc == 124:
            self.end("stalled")
            die("BENCH STALLED  %s on %s (killed after %ss with no output)\n    log: %s"
                % (leg.plan, self.system.label(leg), progress.watchdog_abort(self.env), path))
        self.end(rc)
        warn(why)
        for e in progress.first_error(path):
            log("  " + e)
        log("  full log: %s" % path)
        raise Refused(rc)
