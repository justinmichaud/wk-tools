"""`wk build` as a flow over a Target, its Records, a Lock and a Clock: the front refuses, stops, or detaches;
the driver sizes, admits, and runs build/build-in-target.sh in the workspace under the watchdog, stepping
its record; the babysitter re-runs the build with a Claude fix between failures."""

import os
import re
import shlex
import subprocess
import sys

from wk import act, buildconf, job, record, shell
from wk.act import Refused, die, info, log, warn
from wk.clock import Clock
from wk.lock import Lock
from wk.resources import Budget, Resources, parse_df

PID_MATCH = "*build-in-target.sh* *Tools/Scripts/build-*"   # build-in-target.sh execs the port's script; a PGO config stays in it across phases
EXCLUSIVE = ("build", "babysit", "yocto", "buildroot")   # jobs that hold a checkout: two at once corrupt it
BABYSIT_MODEL = "haiku"
BABYSIT_ATTEMPTS = 5


def records_of(target, clock, machine):
    """The target's records, each begun with the watchdog's deadline, so `wk status` tells a quiet job from one whose watchdog is gone."""
    env = dict(target.env)
    env.setdefault("WK_ABORT_SECONDS", str(job.ABORT_SECONDS))
    return record.of_target(target, clock, machine, env)


def kill_cmd(in_ws, name):
    return "wk build --kill" if in_ws else "wk build %s --kill" % name


def busy_reason(target, records, name, skip=""):
    """What already holds the checkout: a record by its kind alone, else a pid file in the workspace's home
    whose pid no target-side record names and which is alive in there."""
    recorded = set()
    for t in records.list():
        if skip and os.path.realpath(str(t.path)) == os.path.realpath(skip):
            continue
        if t.field("name") != name:
            continue
        if t.field("where") == "target":
            recorded.add(t.field("pid"))
        if t.field("kind") in EXCLUSIVE and t.alive(None):
            return "%s (pid %s, %s)  stop it: %s" % (t.field("kind"), t.field("pid"), t.field("machine"), t.field("kill"))
    home = os.path.join(target.store.ws_dir(name), "home")
    here = records.machine
    if not here.isdir(home):
        return None
    for n in here.listdir(home):
        if not n.endswith(".pid"):
            continue
        try:
            pid = re.sub(r"[^0-9]", "", here.read(os.path.join(home, n)))
        except OSError:
            continue
        if pid and pid not in recorded and target.exec(name, ["kill", "-0", pid]).ok:
            return "%s (pid %s in the workspace)" % (n[:-4], pid)
    return None


def forward(argv, drop=(), drop_valued=(), add=()):
    """`argv` without the flags named, `add` placed before any `--` tail."""
    out, i = [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            break
        if a in drop_valued:
            i += 2
            continue
        if a not in drop:
            out.append(a)
        i += 1
    return out + list(add) + argv[i:]


def holder_alive(reg):
    """A budget record's holder: `pid:<n>` on this machine, `ws:<name>:<pidfile>` in that workspace's home."""
    def alive(h):
        if h.startswith("pid:") and h[4:].isdigit():
            return reg.machine.alive(int(h[4:]))
        if h.startswith("ws:") and ":" in h[3:]:
            name, pidf = h[3:].split(":", 1)
            try:
                t = reg.load(reg.ws_target(name))
                pid = re.sub(r"[^0-9]", "", reg.machine.read(os.path.join(t.store.ws_dir(name), "home", pidf)))
            except (LookupError, OSError, Refused):
                return False
            return bool(pid) and t.exec(name, ["kill", "-0", pid]).ok
        return False
    return alive


def _exited(pid):
    """A spawned child that exited is a zombie `kill -0` still answers for until it is reaped."""
    try:
        return os.waitpid(pid, os.WNOHANG)[0] == pid
    except ChildProcessError:
        return False


def size_for(reg, target, name, cfg, clock):
    """(budget, the jobs already running, jobs, MB a job, nice), from the whole machine once: a remote target's
    own numbers, else this machine's free memory under the target's envelope."""
    cores, mem, load = target.build_size(name)
    polite = target.kind == "remote"
    benv = dict(reg.env, WK_BUILD_MACHINE=target.name) if polite else reg.env
    if polite:
        avail = int(reg.env.get("WK_AVAIL_MB") or mem)
    else:
        avail = Resources(reg.machine, reg.env).avail_mem_mb(cgroup_mb=mem)
    if target.env.get("WK_REMOTE_MAX_JOBS"):
        warn("WK_REMOTE_MAX_JOBS is set in %s's conf and is ignored: the job count\n"
             "  is derived per build from what that machine has free. Delete the line." % target.name)
    budget = Budget(reg.machine, benv, clock)
    running = budget.running(holder_alive(reg))
    mbpj = buildconf.mb_per_job(cfg, reg.env)
    max_jobs = int(target.env.get("WK_MAX_JOBS") or 0) or None
    jobs = budget.explain(cores, avail, mbpj, load if polite else None, max_jobs, running)
    return budget, running, jobs, mbpj, (19 if polite else 10)


class Build:
    def __init__(self, reg, name, opts, argv=(), clock=None, popen=subprocess.Popen):
        self.reg, self.name, self.opts, self.argv = reg, name, opts, list(argv)
        self.here, self.root, self.env = reg.machine, reg.root, reg.env
        self.clock = clock or Clock()
        self.popen = popen
        self.in_ws = reg.in_workspace()
        self.kill = kill_cmd(self.in_ws, name)
        try:
            self.target = reg.load(self.env.get("WK_TARGET") or reg.ws_target(name))
        except LookupError as e:
            die(str(e))
        self.recs = records_of(self.target, self.clock, self.here)
        self.ws_dir = self.target.store.ws_dir(name)
        self.cfg = None

    # -- the front

    def front(self):
        if self.opts.get("kill"):
            return self.stop()
        name, o = self.name, self.opts
        if not o.get("config"):
            log("usage: wk build <config>" if self.in_ws else "usage: wk build <workspace> <config>")
            for line in buildconf.LIST_TEXT.splitlines():
                log("  " + line)
            raise Refused(2)
        try:
            self.cfg = buildconf.resolve(o["config"], self.target.os(), self.target.kind, self.target.env)
        except LookupError:
            die("unknown config '%s' (wk build --list)" % o["config"])
        for e in o.get("env", []):
            if not re.match(r"^[A-Za-z_][^=]*=", e):
                die("--env takes NAME=VALUE, got %s" % shlex.quote(e))
        if o.get("cmakeargs") is not None:
            act.barrier("--cmakeargs would replace the config's CMake flags rather than add to\n"
                        "    them: build-webkit takes one --cmakeargs, so the last one on the command\n"
                        "    line wins and '%s' would lose %s.\n    Use --cmake instead, which is added to them:\n"
                        "        wk build%s %s --cmake %s" % (self.cfg.name, self.cfg.cmake_summary(), "" if self.in_ws else " " + name,
                                                           self.cfg.name, shlex.quote(o["cmakeargs"])))
            o.setdefault("pass", []).insert(0, "--cmakeargs=" + o["cmakeargs"])
        if o.get("sysroot") is not None:
            die("--sysroot is not implemented (docs/Nice to have/HANDOFF-cross-compile.md).\n"
                "    This workspace is %s; a --sysroot build would be a *cross* build from a\n"
                "    native workspace, which is a different mechanism -- an aarch64 clang, -m32,\n"
                "    CMAKE_LIBRARY_ARCHITECTURE and another rootfs to link against." % self.target.arch(name))
        if o.get("babysit_driver"):
            return self.babysit()
        if o.get("babysit"):
            return self.babysit_front()
        if o.get("detach") and not act.dry_run():
            return self.detach()
        return self.driver()

    def stop(self):
        name = self.name
        stopped, left = False, []
        # The babysitter first: left alone it starts the next build itself.
        for kind in ("babysit", "build"):
            rc = job.stop(self.target, self.recs, name, kind, self.here, self.clock, self.env)
            stopped = stopped or rc == 0
            if rc == 1:
                left.append(kind)
        if left:
            die("'%s's %s outlived a TERM and a KILL.\n    Look at it:  wk enter %s  and then  pgrep -af ninja" % (name, " ".join(left), name))
        if stopped:
            log("  the build directory keeps what it compiled, so\n  'wk build%s <config>' resumes rather than starts over."
                % ("" if self.in_ws else " " + name))
        return 0

    def child_argv(self, drop, drop_valued=(), add=()):
        return [os.path.join(self.root, "wk"), "build"] + ([] if self.in_ws else [self.name]) + forward(self.argv, drop, drop_valued, add)

    def detach(self):
        """Handed to the machine that runs it; this end returns once the child's own record exists."""
        path = os.path.join(self.ws_dir, "detached.log")
        self.here.mkdir(self.ws_dir)
        self.here.write(path, "")
        since = self.clock.stamp()
        pid = self.here.spawn(self.child_argv(("--detach",)), path)
        if self.wait_own_record(pid, since, path) is None:
            die("the detached build of '%s' ended before it started -- what it said is\n    above, in full in %s" % (self.name, path))
        info("building %s in '%s', detached as pid %d -- this end can go away" % (self.cfg.name, self.name, pid))
        log("  follow:  wk logs %s -f" % self.name)
        log("  state:   wk status %s" % self.name)
        log("  stop it: %s" % self.kill)
        return 0

    def wait_own_record(self, pid, since, path):
        offset = [0]

        def pump():
            try:
                with open(path, "rb") as f:
                    f.seek(offset[0])
                    data = f.read()
            except OSError:
                return
            offset[0] += len(data)
            sys.stderr.write(data.decode(errors="replace"))

        while True:
            pump()
            t = self.recs.find("build", self.name, floor=since)
            if t is not None and t.id.endswith("-%d" % pid):
                return t
            if _exited(pid) or not self.here.alive(pid):
                pump()
                return None
            self.clock.sleep(1)

    # -- the babysitter

    def babysit_front(self):
        name, env = self.name, self.env
        if self.in_ws:
            die("--babysit runs on the host: it re-runs 'wk build' and starts\n    Claude in the workspace, neither of which works from in here")
        if self.target.kind == "remote":
            die("refusing to babysit on a remote target: the fixer is Claude, and\n"
                "    a shared machine has no sandbox to run it in (the same rule as 'wk ai claude')")
        if self.target.kind == "local":
            die("already inside a workspace -- run the build and claude directly")
        blog = os.path.join(self.ws_dir, "babysit.log")
        t = self.recs.find("babysit", name)
        if t is not None and t.alive(None):
            die("a babysitter is already running for '%s' (pid %s).\n    Follow it:  tail -f %s\n    Stop it:    %s"
                % (name, t.field("pid"), blog, self.kill))
        model = env.get("WK_BABYSIT_MODEL") or BABYSIT_MODEL
        attempts = int(env.get("WK_BABYSIT_ATTEMPTS") or BABYSIT_ATTEMPTS)
        argv = self.child_argv(("--babysit", "--detach"), add=("--_babysit",))
        if act.dry_run():
            log("dry run -- nothing was built, no babysitter started.")
            log("  would run detached: %s" % " ".join(shlex.quote(a) for a in argv))
            return 0
        self.here.mkdir(self.ws_dir)
        self.here.write(blog, "")
        pid = self.here.spawn(argv, blog)
        branch = self.opts.get("branch")
        info("babysitter started for %s in '%s' (pid %d, model %s%s)" % (self.cfg.name, name, pid, model, ", branch " + branch if branch else ""))
        log("  it survives this terminal; up to %d fixes, then it closes itself" % attempts)
        log("  state:   wk status %s" % name)
        log("  follow:  tail -f %s" % blog)
        log("  report:  %s" % os.path.join(self.ws_dir, "babysit.report"))
        log("  stop it: %s" % self.kill)
        return 0

    def babysit(self):
        """The detached loop: build; on a failure a Claude fix from inside the workspace, then build again."""
        name, cfg, here, env = self.name, self.cfg, self.here, self.env
        model = env.get("WK_BABYSIT_MODEL") or BABYSIT_MODEL
        most = int(env.get("WK_BABYSIT_ATTEMPTS") or BABYSIT_ATTEMPTS)
        report, blog = os.path.join(self.ws_dir, "babysit.report"), os.path.join(self.ws_dir, "build.log")
        here.write(report, "")
        plan = ["build %s" % cfg.name] + ["fix %d of %d, build again" % (i, most) for i in range(1, most + 1)]
        task = self.recs.begin("babysit", "here", name, "wk build %s --kill" % name, os.path.join(self.ws_dir, "babysit.log"), plan)

        def note(title, body):
            try:
                old = here.read(report)
            except OSError:
                old = ""
            here.write(report, old + "=== %s  %s ===\n%s\n\n" % (title, self.clock.iso(), body))

        def give_up(word, title, body, why):
            task.end(word)
            note(title, body)
            die(why)

        with job.Signals():
            try:
                info("babysitting '%s' in '%s' (model %s, up to %d fixes)" % (cfg.name, name, model, most))
                task.step(1)
                branch = self.opts.get("branch")
                if branch:   # once, before the loop: a checkout would take a fix from under the model that made it
                    info("checking out '%s'" % branch)
                    if not self.checkout(branch):
                        give_up("error", "gave up before building", "could not check out branch '%s'" % branch,
                                "could not check out '%s' in '%s'" % (branch, name))
                    note("checkout", "built from branch '%s'" % branch)
                inner = ["env", "WK_TASK_PARENT=%s" % task.path] + self.child_argv(("--_babysit", "--babysit", "--detach"), ("--branch",))
                attempt = 0
                while True:
                    since = self.clock.stamp()
                    r = here.run_tty(inner)
                    if r.ok:
                        task.end(0)
                        note("done", "build succeeded on its own -- nothing to fix" if attempt == 0 else "build succeeded after %d fix(es)" % attempt)
                        info("BUILD OK after %d fix(es) -- report: %s" % (attempt, report))
                        return 0
                    built = self.recs.find("build", name, floor=since)
                    if built is not None and built.field("exit") == "stalled":
                        give_up("stalled", "gave up", "the build stalled (no output). That is load or\nmemory, not source -- "
                                "nothing for a fix attempt to act on. See %s" % blog, "build stalled; not something a fix can reach")
                    attempt += 1
                    if attempt > most:
                        give_up("gave-up", "gave up", "still failing after %d fix attempt(s); last exit %d.\n"
                                "The log is %s; the attempts above say what was tried." % (most, r.rc, blog), "gave up after %d fix attempts" % most)
                    task.step(attempt + 1)
                    info("build failed (exit %d) -- fix attempt %d of %d" % (r.rc, attempt, most))
                    fix = here.run(["env", "WK_NAME=%s" % name, os.path.join(self.root, "cmd", "ai"), "claude",
                                    "--model", model, "-p", self.fix_prompt(r.rc, attempt, most, blog)])
                    sys.stderr.write(fix.err)
                    note("fix attempt %d (exit %d)" % (attempt, fix.rc), fix.out)
                    if not fix.ok and not fix.out.strip():   # the substrate gone, not a failed fix: a retry fails the same forever
                        give_up("error", "gave up", "claude did not run (exit %d) -- see %s/babysit.log" % (fix.rc, self.ws_dir),
                                "claude did not run (exit %d)" % fix.rc)
            except job.Interrupted as e:
                note("stopped", "a person stopped the babysitter (wk build %s --kill)" % name)
                task.end("cancelled")
                raise Refused(job.EXIT_OF.get(e.signum, 130))

    def fix_prompt(self, rc, attempt, most, blog):
        errs = "\n".join(record.first_error(blog))
        try:
            with open(blog, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 8000))
                tail = f.read().decode(errors="replace")
        except OSError:
            tail = ""
        return ("You are an unattended build-fixer. The '%s' build of the WebKit\ncheckout in the current directory failed (exit %d); this is fix attempt\n"
                "%d of %d. Find the cause and fix it in the checkout, then stop.\n\n"
                "Rules: make the smallest change that fixes the build, following the\nrepository's house rules. Do not run the full build -- the babysitter reruns\n"
                "it when you finish -- but you may syntax-check or compile a single file. If\nthe failure is not fixable from inside the workspace (toolchain, disk,\n"
                "network), say so plainly and change nothing.\n\nEnd your reply with a short paragraph: what failed, what you changed, and\n"
                "which files you touched.\n\nBuild errors (classified):\n%s\n\nLog tail:\n%s" % (self.cfg.name, rc, attempt, most, errs, tail))

    # -- the driver

    def checkout(self, branch):
        q = shlex.quote
        fetch = shell.origin_branch_fetch_step(self.root, self.here, branch, self.target.mirror_dir())
        script = ("cd %s && {\n    git checkout -q %s 2>/dev/null ||\n    { %s &&\n      git checkout -q %s; }; }"
                  % (q(self.target.src(self.name)), q(branch), fetch, q(branch)))
        return self.target.act_exec(self.name, ["bash", "-c", script]).ok

    def admit(self, budget, running, jobs):
        name, here = self.name, self.here
        lock = Lock(self.target.store, here, self.clock)
        holder = lock.holder_pid("ws-" + name)
        if holder is not None and here.alive(holder):   # refused, not queued: an hour on a lock names no remedy
            die("'%s' is already building -- its driver holds the ws-%s lock.\n    Follow it:  wk logs %s -f\n    Stop it:    %s"
                % (name, name, name, self.kill))
        lock.hold("ws-" + name, timeout=0)
        busy = busy_reason(self.target, self.recs, name, self.env.get("WK_TASK_PARENT", ""))
        if busy:
            act.barrier("'%s' already has a job running in it: %s\n    Two builds in one checkout corrupt both, and this one would be the second.\n"
                        "    See what it is:  wk logs %s --all" % (name, busy, name))
        store = self.env.get("WK_STORE") or self.env.get("HOME", "")
        budget.disk_admit("this build", self.cfg.disk_gb, budget.free_gb(store), "%s's filesystem" % store)
        if self.target.kind == "vm":   # the guest's disk, and the host image it grows, both fill
            free = parse_df(self.target.exec(name, ["df", "-Pk", self.target.src(name)]).out)
            budget.disk_admit("this build", self.cfg.disk_gb, free, "the disk inside '%s'" % name)
        budget.admit("this build", jobs, running)
        return lock

    def build_env(self, jobs, nice):
        o, tenv = self.opts, dict(self.target.env)
        if o.get("mem_budget"):
            tenv["WK_MEM_BUDGET_MB"] = o["mem_budget"]
        if o.get("mem_floor"):
            tenv["WK_MEM_FLOOR_MB"] = o["mem_floor"]
        defaults = "" if o.get("no_defaults") else tenv.get("WK_BUILD_ARGS", "")
        return tenv, defaults, buildconf.build_env(
            self.cfg, self.target.src(self.name), jobs, nice, self.target.arch(self.name), self.target.ccache_dir(self.name),
            tenv, " ".join(o.get("cmake", [])), o.get("env", []), defaults)

    def driver(self):
        name, cfg, here, o = self.name, self.cfg, self.here, self.opts
        budget, running, jobs, mbpj, nice = size_for(self.reg, self.target, self.name, self.cfg, self.clock)
        tenv, defaults, cfg_env = self.build_env(jobs, nice)
        budget_mb = int(tenv.get("WK_MEM_BUDGET_MB") or jobs * mbpj)
        bit = self.target.tools(name) + "/build/build-in-target.sh"
        passthru = o.get("pass", [])
        lock = None
        if act.dry_run():
            self.report(tenv, defaults, cfg_env, jobs, nice, budget_mb, bit, passthru)
        else:
            lock = self.admit(budget, running, jobs)
        try:
            return self.run(budget, jobs, nice, budget_mb, cfg_env, bit, passthru)
        finally:
            if lock is not None:
                lock.release_all()

    def report(self, tenv, defaults, cfg_env, jobs, nice, budget_mb, bit, passthru):
        name, cfg, o, t = self.name, self.cfg, self.opts, self.target
        arch = t.arch(name)
        label = buildconf.arch_label(arch)
        log("dry run -- nothing was built.")
        log("  workspace: %s (%s, %s%s)" % (name, t.name, t.state(name), ", " + label if label else ""))
        if o.get("branch"):
            log("  branch:    %s (would be checked out first)" % o["branch"])
        log("  config:    %s (%s%s%s)" % (cfg.name, cfg.buildsys, " " + cfg.port if cfg.port else "", " " + cfg.args if cfg.args else ""))
        if tenv.get("WK_TARGET_CMAKE"):
            log("  machine:   %s (WK_TARGET_CMAKE, from %s's conf)" % (tenv["WK_TARGET_CMAKE"], t.name))
        if defaults:
            log("  defaults:  %s (WK_BUILD_ARGS, from %s's conf; --no-defaults skips it)" % (defaults, t.name))
        if o.get("cmake"):
            log("  --cmake:   %s (added to the config's)" % " ".join(o["cmake"]))
        if o.get("env"):
            log("  --env:     %s(overrides the config's)" % "".join(e + " " for e in o["env"]))
        if passthru:
            log("  passed on: %s (straight to build-webkit)" % " ".join(passthru))
        if label:
            a = buildconf.ARCH[arch]
            log("  arch:      %s, native (%s %s)" % (label, a["wrapper"], a["cflags"]))
        log("  src:       %s" % t.src(name))
        log("  build dir: %s" % cfg.build_dir(t.src(name)))
        log("  jobs:      %d (nice %d)" % (jobs, nice))
        log("  memory:    budget %dMB, floor %sMB, watched every %ss"
            % (budget_mb, tenv.get("WK_MEM_FLOOR_MB") or 2048, self.env.get("WK_MEM_INTERVAL") or 30))
        log(("  would run: env %s %s %s" % (" ".join(cfg_env), bit, " ".join(passthru))).rstrip())
        if not t.exec(name, ["grep", "-q", "WK_DRY_RUN", bit]).ok:
            warn("  the wk-tools in '%s' predates --dry-run's target half, so the commands" % name)
            log("  it would run cannot be asked for without risking a real build.")
            log("  push this tree there first:  wk sync --tools %s" % t.name)
            return
        line = self.far_line(cfg_env, bit, passthru)
        if line is not None:
            log("  running:   %s" % line)
        else:
            warn("  could not ask '%s' itself what it would run -- the lines above are" % name)
            log("  this side's half of it. Is the workspace up? (wk status %s)" % name)

    def far_line(self, cfg_env, bit, passthru):
        """The command line the target half resolves, asked of it under WK_DRY_RUN; it knows ionice and the cgroup clamp."""
        r = self.target.exec(self.name, ["env"] + cfg_env + ["WK_DRY_RUN=1", bit] + passthru)
        return r.out.replace("\r", "").strip() if r.ok else None

    def run(self, budget, jobs, nice, budget_mb, cfg_env, bit, passthru):
        """The steps: the log is truncated before the record says running, so a reader of the record reads this build's log."""
        name, cfg, here, o, t = self.name, self.cfg, self.here, self.opts, self.target
        dry = act.dry_run()
        path = os.path.join(self.ws_dir, "build.log")
        here.mkdir(self.ws_dir)
        here.write(path, "")
        start = self.clock.now()
        label = buildconf.arch_label(t.arch(name))
        plan = (["check out %s" % o["branch"]] if o.get("branch") else []) + [
            "sync wk-tools into '%s'" % name, "compile %s%s with -j%d" % (cfg.name, " (%s)" % label if label else "", jobs)]
        task = None if dry else self.recs.begin("build", "here", name, self.kill, path, plan)
        step = [0]

        def advance():
            step[0] += 1
            if task is not None:
                task.step(step[0])
                t.task_put(name, task)

        def end(word):
            if task is not None:
                task.end(word)
                t.task_put(name, task)

        if task is not None:
            task.set("config", cfg.name)
            t.task_put(name, task)
        watcher = None
        with job.Signals():
            try:
                if o.get("branch"):
                    advance()
                    info("checking out '%s' in '%s'" % (o["branch"], name))
                    if not self.checkout(o["branch"]):
                        die("could not check out '%s' in '%s'" % (o["branch"], name))
                advance()
                if not shell.sync_tools(self.root, here, t.name, name):
                    die("pushing wk-tools into '%s' failed -- the reason is above" % name)
                advance()
                info("building %s%s in '%s' with -j%d (nice %d)" % (cfg.name, " (%s)" % label if label else "", name, jobs, nice))
                log("  log: %s" % path)
                log("  stop: %s" % self.kill)
                log("  stall warning after %ss of silence; abort after %ss"
                    % (self.env.get("WK_STALL_SECONDS") or 300, self.env.get("WK_ABORT_SECONDS") or job.ABORT_SECONDS))
                if not dry:
                    line = self.far_line(cfg_env, bit, passthru)
                    if line is not None:
                        log("running:   %s" % line)
                budget.record("wk build %s (%s)" % (name, cfg.name), jobs, budget_mb, "pid:%d" % os.getpid())
                if task is not None:
                    watcher = job.PidWatch(t, name, task, path, "build", PID_MATCH, int(self.env.get("WK_JOB_PID_TRIES") or 900))
                    watcher.start()
                argv, cwd = t.build_argv(name, ["env"] + cfg_env + [bit] + passthru)
                rc = job.watch(argv, path, here, self.clock, self.env, cwd, self.popen)
            except job.Interrupted as e:
                if watcher is not None:
                    watcher.stop()
                self.cancelled(task)
                raise Refused(job.EXIT_OF.get(e.signum, 130))
            except Refused as e:
                end(e.status)
                raise
            finally:
                if watcher is not None:
                    watcher.stop()
        if dry:
            return 0
        return self.verdict(rc, path, start, end, task)

    def cancelled(self, task):
        """^C reaches this driver, not the build: the building machine is told and the record converged before it exits."""
        if task is None:
            return
        warn("interrupted -- stopping the build in '%s'" % self.name)
        if not job.kill(self.target, self.name, task, "cancelled", self.here, self.clock, self.env):
            warn("it is still running; stop it with:  %s" % self.kill)
        self.target.task_put(self.name, task)

    def verdict(self, rc, path, start, end, task):
        name, cfg = self.name, self.cfg
        try:
            text = record.normalised(path)
        except OSError:
            text = ""
        peaks = re.findall(r"wk: memory: peak ([0-9]*)MB", text)
        limits = [l for l in text.split("\n") if "wk: MEMORY LIMIT" in l]
        secs = int(self.clock.now() - start)
        took = "%dm%ds" % (secs // 60, secs % 60)
        label = buildconf.arch_label(self.target.arch(name))
        sys.stderr.write("\n")
        if rc == 0:
            end(0)
            info("BUILD OK  %s%s in '%s'  (%s)" % (cfg.name, " (%s)" % label if label else "", name, took))
            return 0
        if task is not None and task.field("stopping"):
            end(rc)
            warn("BUILD STOPPED  %s in '%s'  (by '%s' after %s)" % (cfg.name, name, self.kill, took))
            raise Refused(rc)
        if rc == 124:
            end("stalled")
            die("BUILD STALLED  %s in '%s'  (killed after %s with no output)\n    log: %s\n"
                "    A stall is usually memory pressure -- check 'wk status %s' for OOM kills." % (cfg.name, name, took, path, name))
        if limits:
            end("oom")
            warn("BUILD KILLED FOR MEMORY  %s in '%s'  (after %s)" % (cfg.name, name, took))
            for l in limits[-2:]:
                sys.stderr.write(l.replace("wk: ", "  ", 1) + "\n")
            log("  peak %sMB. Fewer jobs: WK_MB_PER_JOB=3072 wk build %s %s" % (peaks[-1] if peaks else "?", name, cfg.name))
            log("  full log: %s" % path)
            raise Refused(rc)
        end(rc)
        warn("BUILD FAILED  %s in '%s'  (exit %d after %s)" % (cfg.name, name, rc, took))
        log("first error(s):")
        for e in record.first_error(path):
            log("  " + e)
        if "xcbuilddata/manifest.json" in text:
            log("")
            warn("that is Xcode's build description, not your code.")
            log("  It is derived, and an interrupted build can leave it unusable.")
            log("  The products survive; only the plan has to be rebuilt:")
            log("    wk enter %s rm -rf %s/WebKitBuild/%s/XCBuildData"
                % (name, self.target.src(name), os.path.basename(cfg.build_dir(self.target.src(name)))))
            log("    wk build %s %s" % (name, cfg.name))
        log("")
        log("  full log: %s" % path)
        raise Refused(rc)


def list_configs():
    log("available configs:")
    for line in buildconf.LIST_TEXT.splitlines():
        log("  " + line)
    return 0
