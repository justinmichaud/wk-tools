"""One image-build lifecycle: a builder's stage as a task with `wk build`'s record, detach, watchdog and stop."""

import hashlib
import os
import re
import sys

from wk import act, decl, images, job
from wk.machine import Local, isolated_module
from wk.act import Refused, die, info, log, warn
from wk.resources import Budget, Resources, build_jobs, disk_gb
from wk.store import Store, dispatch_place

MB_PER_JOB = 2048   # a WPE compile runs about 2 GB a job
TAIL_LINES = 20


def image_root(env):
    """On a macOS host the store is the podman VM's /var/lib/wk, a path the Mac cannot create."""
    s = Store(env)
    return s.state_dir() if s.macos_host else s.store_dir()


def cache_dir(env):
    return os.path.join(image_root(env), "cache", "images")


def sha256(machine, path):
    words = machine.run(["sha256sum", path]).out.split()
    return words[0] if words else ""


def du(machine, path, absent="?"):
    words = machine.run(["du", "-sh", path]).out.split()
    return words[0] if words else absent


def fetch_pinned(machine, url, dest, sha):
    """`url` kept at `dest` on `machine` only once its sha256 is `sha`, resumed from `dest`.part; "" or why it is not."""
    if machine.exists(dest) and sha256(machine, dest) == sha:
        return ""
    part = dest + ".part"
    info("fetching %s" % os.path.basename(url))
    r = machine.act_run(["curl", "-fsSL", "--retry", "5", "-C", "-", "-o", part, url])
    if not r.ok:
        return "could not fetch %s\n    %s" % (url, r.err.strip())
    if act.dry_run():
        return ""
    got = sha256(machine, part)
    if got != sha:
        machine.act_run(["rm", "-f", part])
        return "%s does not match its pin (sha256 %s, expected %s); if a re-run mismatches too, the pin is stale" % (url, got, sha)
    return "" if machine.act_run(["mv", part, dest]).ok else "could not keep %s" % dest


def check_commit(commit):
    if not re.match(r"^[0-9a-f]{40}$", commit):
        die("--commit takes a full sha (40 hex digits), got '%s'" % commit)


def show(title, rows, did="built"):
    """The one dry-run report; with no `did`, the caller's own steps follow it and it ends them."""
    log("would " + title)
    for label, value in rows:
        log("  %-12s %s" % (label, value))
    if did:
        log("dry run -- nothing was %s." % did)
    return 0


def off_wall(path):
    """PATH without every directory under container/bin, so no configure step records a wall as the tool it found."""
    return ":".join(d for d in path.split(":") if d and not re.search(r"/container/bin(/|$)", d))


def in_workspace(tools, label, argv):
    return isolated_module(tools + "/lib", "wk.sysimage.task") + ["stage", label, "--"] + list(argv)


class _Declared:
    def __init__(self, flags, valued):
        self.opts = ",".join(list(flags) + [v + "=" for v in valued])

    def opts_for(self, argv):
        return self.opts


def options(rest, flags, valued, usage):
    args = decl.Args(_Declared(flags, valued), list(rest))
    if args.positionals:
        die("%s; %s is not an option of this build" % (usage, args.positionals[0]))
    got = {o: True for o in flags if args.flag(o)}
    got.update((o, args.value(o)) for o in valued if args.value(o) is not None)
    return got


class Stage:
    """`kind` is the builder and the record's kind; `stage` names the log; its record holds the workspace and the budget."""

    def __init__(self, reg, driver, ws, kind, stage, kill, clock):
        self.reg, self.driver, self.ws, self.kind, self.stage, self.kill = reg, driver, ws, kind, stage, kill
        self.here, self.env, self.clock = reg.machine, reg.env, clock
        self.recs = job.records_of(driver, clock, self.here)
        self.ws_dir = driver.store.ws_dir(ws)
        self.log = os.path.join(self.ws_dir, "home", "%s-%s.log" % (kind, stage))
        self.watchdog = {}

    def refuse_busy(self, held=None):
        held = self.recs.holders("ws:" + self.ws) if held is None else held
        if held:
            t = held[0]
            die("a build is still running in '%s': %s (pid %s, %s).\n    Follow it:  wk status %s --log -f\n    Stop it:    %s"
                % (self.ws, t.field("kind"), t.field("pid"), t.field("machine"), self.ws, t.field("kill")))

    def detach(self, argv, what):
        pid = job.detached(self.here, self.recs, self.clock, self.kind, self.ws, argv,
                           os.path.join(self.ws_dir, "detached-%s.log" % self.stage), what)
        info("running detached in '%s' as pid %d -- this end can go away" % (self.ws, pid))
        log("  follow:  wk status %s --log -f\n  state:   wk status %s\n  stop it: %s" % (self.ws, self.ws, self.kill))
        return 0

    def size(self, max_jobs=None, mb=MB_PER_JOB):
        env = dict(self.env, WK_MB_PER_JOB=str(mb), **({"WK_MAX_JOBS": str(max_jobs)} if max_jobs else {}))
        budget, self.res = Budget(self.here, env), Resources(self.here, env)
        running = budget.running(self.recs)
        return budget, running, build_jobs(self.res, budget, running)

    def admit(self, budget, need_gb=None, what=None):
        store = self.reg.store.admission_dir()
        need = disk_gb(self.env) if need_gb is None else need_gb
        budget.disk_admit(what or "the %s build" % self.stage, need, budget.free_gb(store), "%s's filesystem" % store)

    def begin(self, plan, budget, jobs, mb):
        def refuse(resource, held):
            if resource.startswith("ws:"):
                self.refuse_busy(held)
            budget.admit("the %s build" % self.stage, jobs, budget.rows(held))
        self.here.mkdir(os.path.dirname(self.log))
        t = self.recs.claim(self.kind, self.ws, self.kill, self.log, plan, ["ws:" + self.ws, budget.resource()], refuse,
                            fields={"stage": self.stage, "jobs": jobs, "budget_mb": mb})
        self.here.write(self.log, "")
        return t

    def run(self, task, argv, pattern):
        log("  log: %s\n  stop: %s" % (self.log, self.kill))
        watcher = job.PidWatch(self.driver, self.ws, task, self.log, self.kind, pattern, job.pid_tries(self.env))
        watcher.start()
        try:
            cmd, cwd = self.driver.build_argv(self.ws, in_workspace(self.driver.tools(self.ws), self.kind, argv))
            rc = job.watch(cmd, self.log, self.here, self.clock, self.env, cwd, **self.watchdog)
        except job.Interrupted as e:
            watcher.stop()
            warn("interrupted -- stopping the %s build in '%s'" % (self.stage, self.ws))
            if not job.kill(self.driver, self.ws, task, "cancelled", self.here, self.clock, self.env):
                warn("it is still running; stop it with:  %s" % self.kill)
            raise Refused(job.EXIT_OF.get(e.signum, 130))
        finally:
            watcher.stop()
        return self.verdict(task, rc)

    def verdict(self, task, rc):
        """The child's own "stage 'x' done" line, not its exit status: make and the helper exit 0 having done nothing."""
        try:
            with open(self.log, errors="replace") as f:
                text = f.read()
        except OSError:
            text = ""
        if "stage '%s' done" % self.stage in text:
            task.end(0)
            return 0
        if task.field("stopping"):
            task.end(rc or 1)
            warn("the %s build in '%s' was stopped (by '%s')" % (self.stage, self.ws, self.kill))
            raise Refused(rc or 1)
        if rc == 124:
            task.end("stalled")
            die("the %s build in '%s' stalled: its watchdog killed it, and says why above.\n    log: %s"
                % (self.stage, self.ws, self.log))
        task.end(rc or 1)
        tail = [l for l in text.replace("\r", "\n").split("\n") if l][-TAIL_LINES:]
        die("the %s build in '%s' failed%s. Last lines:\n%s\n    log: %s"
            % (self.stage, self.ws, "" if rc else " (it exited 0 and never said it was done)",
               "\n".join("    " + l for l in tail), self.log))

    def stop(self):
        rc = job.stop(self.driver, self.recs, self.ws, self.kind, self.here, self.clock, self.env)
        if rc == 1:
            die("the %s build in '%s' outlived a TERM and a KILL.\n    Look at it:  wk enter %s" % (self.kind, self.ws, self.ws))
        return 0


class ContainerBuilder:
    """A builder in a container workspace made from its own host image. A subclass is its data -- its options, its
    dry-run rows -- and its stages; `build` is the one lifecycle every one of them runs through; ls.OUTPUTS its images."""

    KIND = TITLE = SPEC = BASE_IMAGE = BASE_VAR = PATTERN = ""
    NEEDS = NOT_HERE = IMAGE_NOTE = SURVIVES = USAGE = ""
    FLAGS, VALUED = ("--detach", "--stop"), ("--workspace",)
    verb = "build"

    def __init__(self, reg, preset, spec, clock):
        self.reg, self.p, self.spec, self.clock = reg, preset, spec, clock
        self.name = preset["IMG_PRESET"]
        self.here, self.env, self.root = reg.machine, reg.env, str(reg.root)
        self.store = Store(self.env)

    def build(self, rest):
        o = self.parse(rest)
        driver = self.driver()
        ws = o.get("--workspace") or images.image_ws(self.name, self.env)
        st = self.stage(driver, ws, o["stage"])
        if act.dry_run():
            return show(*self.report(driver, st, ws, o))
        if o.get("--stop"):
            return self.stop(driver, st, ws, o)
        self.refuse(driver, st, ws, o)
        if o.get("--detach"):
            return st.detach([os.path.join(self.root, "wk"), "sysimage", self.verb, self.spec]
                             + [a for a in rest if a != "--detach"], self.what(o))
        self.staged(st, **self.plan(driver, st, ws, o))
        return self.done(driver, ws, o)

    def parse(self, rest):
        return dict(options(rest, self.FLAGS, self.VALUED, self.USAGE % self.name), stage="image")

    def stop(self, driver, st, ws, o):
        return st.stop()

    def refuse(self, driver, st, ws, o):
        st.refuse_busy()

    def driver(self):
        try:
            t = self.reg.load(dispatch_place(self.env) or self.reg.default())
        except LookupError as e:
            die(str(e))
        if t.kind != "container":
            die("%s, and place '%s' is a %s one.\n    %s" % (self.NEEDS, t.name, t.kind, self.NOT_HERE % {"spec": self.name}))
        return t

    def host_image(self):
        """(base, tag): tagged by a digest of SPEC, so an edited spec is a new image."""
        base = self.env.get(self.BASE_VAR) or self.BASE_IMAGE
        with open(os.path.join(self.root, self.SPEC), "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()[:8]
        return base, "localhost/wk-%s-host:%s-%s" % (self.KIND, base.rsplit(":", 1)[-1], digest)

    def stage(self, driver, ws, stage):
        flag = "" if ws == images.image_ws(self.name, self.env) else " --workspace " + ws
        return Stage(self.reg, driver, ws, self.KIND, stage, self.kill_cmd(flag, stage), self.clock)

    def kill_cmd(self, flag, stage):
        return "wk sysimage build %s%s --stop" % (self.spec, flag)

    def cache(self, what):
        return os.path.join(self.store.store_dir(), "cache", self.KIND, what)

    def du(self, path):
        return du(self.here, path, "not created yet")

    def staged(self, st, sized, plan, steps, say, argv, at=None, subject="", mb=None, need_gb=None, what=None):
        """Under a record of `plan` holding the workspace: each of `steps` is a plan step (or, with `at`, all come
        before step `at`), a refusal in one ends the record, then `argv()` runs as the next step."""
        budget, _, jobs = sized
        st.admit(budget, need_gb, what)
        t = st.begin(plan, budget, jobs, jobs * MB_PER_JOB if mb is None else mb)
        if subject:
            t.set("subject", subject)
        try:
            for n, fn in enumerate(steps, 1):
                if at is None:
                    t.step(n)
                fn()
        except Refused as e:
            t.end(e.status)
            raise
        if at is None:
            t.step(len(steps) + 1)
        else:
            t.step_state(at, "running")
        info(say)
        st.run(t, argv(), self.PATTERN)

    def ensure_ws(self, driver, ws):
        base, tag = self.host_image()
        podman = driver.podman()
        if not self.here.run(podman + ["image", "exists", tag]).ok:
            info("building the %s workspace image %s (one layer on %s)" % (self.TITLE, tag, base))
            log(self.IMAGE_NOTE % {"spec": self.SPEC})
            spec = os.path.join(self.root, self.SPEC)
            if not self.here.run_tty(podman + ["build", "--build-arg", "BASE=" + base, "-t", tag, "-f", spec, os.path.dirname(spec)]).ok:
                die("could not build %s (on the host, so a failed apt or pull is not the workspace boundary)" % tag)
        if driver.info(ws) == "absent":
            info("creating workspace '%s' for the %s build" % (ws, self.TITLE))
            if not self.here.run_tty(["env", "WK_SDK_IMAGE=" + tag, os.path.join(self.root, "wk"), "new", ws, "--on", driver.name]).ok:
                die("could not create workspace '%s'" % ws)
            return
        was = self.here.run(podman + ["container", "inspect", driver.ctr(ws), "--format", "{{.ImageName}}"]).out.strip()
        if was and was != tag:
            die("workspace '%s' was made from %s, and %s now wants %s.\n    Remake it -- %s:\n"
                "        wk rm %s && wk sysimage build %s" % (ws, was, self.SPEC, tag, self.SURVIVES, ws, self.spec))


def stage_main(label, argv, environ=None):
    env = dict(os.environ if environ is None else environ)
    env["PATH"] = off_wall(env.get("PATH", ""))
    env["WK_BUILD"] = "1"
    sys.stderr.write("wk: %s pid %d\n" % (label, os.getpid()))
    Local().exec(argv, env=env)


def main(argv):
    if len(argv) > 3 and argv[0] == "stage" and argv[2] == "--":
        stage_main(argv[1], argv[3:])
        return 0
    sys.stderr.write("usage: python3 -m wk.sysimage.task stage <label> -- <argv>\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
