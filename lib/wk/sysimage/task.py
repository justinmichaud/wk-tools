"""An image build's stage as a task with `wk build`'s record, detach, watchdog and stop, done when its log carries
the stage script's own marker whatever its exit status says; and the pinned download a builder fetches."""

import hashlib
import os
import re
import subprocess
import sys

from wk import act, build, images, job
from wk.act import Refused, die, info, log, warn
from wk.buildconf import DISK_GB
from wk.lock import Lock
from wk.resources import Budget, Resources, build_jobs
from wk.store import Store
from wk.sysimage.ls import human_bytes

MB_PER_JOB = 2048   # a WPE compile runs about 2 GB a job
TAIL_LINES = 20


def image_root(env):
    """On a macOS host the store is the podman VM's /var/lib/wk, a path the Mac cannot create."""
    s = Store(env)
    return s.state_dir() if s.macos_host else s.root()


def cache_dir(env):
    return os.path.join(image_root(env), "cache", "images")


def sha256(machine, path):
    words = machine.run(["sha256sum", path]).out.split()
    return words[0] if words else ""


def fetch_base(machine, url, sha, env):
    cache = cache_dir(env)
    dest = os.path.join(cache, os.path.basename(url))
    if machine.exists(dest) and sha256(machine, dest) == sha:
        act.debug("base image already fetched: %s" % dest)
        return dest
    machine.mkdir(cache)
    info("fetching base image %s" % os.path.basename(url))
    r = machine.act_run(["curl", "-fsSL", "--retry", "5", "-C", "-", "-o", dest, url])
    if not r.ok:
        die("could not fetch %s\n    %s" % (url, r.err.strip()))
    if sha256(machine, dest) != sha:
        die("checksum mismatch on %s\n    expected %s\n    Delete it and re-run; if it mismatches again the spec's pin is stale." % (dest, sha))
    return dest


class Fetch:
    def __init__(self, machine, profile, env):
        self.machine, self.p, self.env = machine, profile, env

    def build(self, rest):
        p = self.p
        options(rest, ("--dry-run",), (), "usage: wk sysimage build %s [--dry-run]" % p["IMG_PROFILE"])
        if act.dry_run():
            cached = self.machine.exists(os.path.join(cache_dir(self.env), os.path.basename(p["FET_URL"])))
            log("would fetch image %s" % p["IMG_PROFILE"])
            log("  from        %s" % p["FET_URL"])
            log("              %s" % ("cached" if cached else "not cached -- would download"))
            log("  pinned to   %s" % p["FET_SHA256"])
            log("  what it is  %s" % p["FET_NOTE"])
            log("dry run -- nothing was fetched.")
            return 0
        src = fetch_base(self.machine, p["FET_URL"], p["FET_SHA256"], self.env)
        try:
            size = human_bytes(os.path.getsize(src))
        except OSError:
            size = "?"
        info("fetched %s  (%s)" % (os.path.basename(src), size))
        log("  write it:  wk sysimage write --from %s --disk <machine>:<device>" % src)
        return 0


def off_wall(path):
    """PATH without every directory under container/bin, so no configure step records a wall as the tool it found."""
    keep = [d for d in path.split(":") if d and not re.search(r"/container/bin(/|$)", d)]
    return ":".join(keep)


def in_workspace(tools, label, argv):
    """`argv` run in a workspace through `main stage`: off the wall, declared a wk build, its pid announced."""
    return ["env", "PYTHONPATH=%s/lib" % tools, "python3", "-m", "wk.sysimage.task", "stage", label, "--"] + list(argv)


def options(rest, flags, valued, usage):
    got, i = {}, 0
    while i < len(rest):
        a = rest[i]
        i += 1
        key, eq, val = a.partition("=")
        if key in valued:
            if not eq:
                if i >= len(rest):
                    die("%s; %s needs a value" % (usage, key))
                val, i = rest[i], i + 1
            got[key] = val
        elif a in flags:
            got[a] = True
        else:
            die("%s; unknown option: %s" % (usage, a))
    if got.pop("--dry-run", None):
        os.environ["WK_DRY_RUN"] = "1"
    return got


class Stage:
    """`kind` is the builder and the record's kind (one of build.EXCLUSIVE); `stage` names the log and the marker."""

    def __init__(self, reg, target, ws, kind, stage, kill, clock, popen=subprocess.Popen):
        self.reg, self.target, self.ws, self.kind, self.stage, self.kill = reg, target, ws, kind, stage, kill
        self.here, self.env, self.clock, self.popen = reg.machine, reg.env, clock, popen
        self.recs = build.records_of(target, clock, self.here)
        self.ws_dir = target.store.ws_dir(ws)
        self.log = os.path.join(self.ws_dir, "home", "%s-%s.log" % (kind, stage))
        self.label = kind
        self.watchdog = {}   # job.watch's abort and wedge, where a builder's silence is not a failure

    def refuse_busy(self):
        busy = build.busy_reason(self.target, self.recs, self.ws)
        if busy:
            die("a build is still running in '%s': %s.\n    One job per workspace: both move the checkout and the tree's output.\n"
                "    Follow it:  wk logs %s -f\n    Stop it:    %s" % (self.ws, busy, self.ws, self.kill))

    def detach(self, argv, what):
        pid = build.detached(self.here, self.recs, self.clock, self.kind, self.ws, argv,
                             os.path.join(self.ws_dir, "detached-%s.log" % self.stage), what)
        info("running detached in '%s' as pid %d -- this end can go away" % (self.ws, pid))
        log("  follow:  wk logs %s -f" % self.ws)
        log("  state:   wk status %s" % self.ws)
        log("  stop it: %s" % self.kill)
        return 0

    def size(self, max_jobs):
        env = dict(self.env, WK_MB_PER_JOB=str(MB_PER_JOB), WK_MAX_JOBS=str(max_jobs))
        budget = Budget(self.here, env, self.clock)
        running = budget.running(build.holder_alive(self.reg))
        return budget, running, build_jobs(Resources(self.here, env), budget, running)

    def admit(self, budget, running, jobs, need_gb=None, what=None):
        """The workspace's lock, refused rather than queued, then its jobs and this machine's budget."""
        lock = Lock(self.target.store, self.here, self.clock)
        holder = lock.holder_pid("ws-" + self.ws)
        if holder is not None and self.here.alive(holder):
            die("'%s' is already building -- its driver holds the ws-%s lock.\n    Follow it:  wk logs %s -f\n    Stop it:    %s"
                % (self.ws, self.ws, self.ws, self.kill))
        lock.hold("ws-" + self.ws, timeout=0)
        try:
            self.refuse_busy()
            store = self.env.get("WK_STORE") or self.env.get("HOME", "")
            need = int(self.env.get("WK_BUILD_DISK_GB") or DISK_GB) if need_gb is None else need_gb
            budget.disk_admit(what or "the %s build" % self.stage, need, budget.free_gb(store),
                              "%s's filesystem" % store)
            budget.admit("the %s build" % self.stage, jobs, running)
        except Refused:
            lock.release_all()
            raise
        return lock

    def begin(self, plan):
        self.here.mkdir(os.path.dirname(self.log))
        self.here.write(self.log, "")
        t = self.recs.begin(self.kind, "here", self.ws, self.kill, self.log, plan)
        t.set("stage", self.stage)
        self.target.task_put(self.ws, t)
        return t

    def step(self, task, n):
        task.step(n)
        self.target.task_put(self.ws, task)

    def run(self, task, budget, jobs, argv, pattern, mb=None):
        budget.record("wk sysimage %s %s" % (self.stage, self.ws), jobs, jobs * MB_PER_JOB if mb is None else mb, "pid:%d" % os.getpid())
        log("  log: %s" % self.log)
        log("  stop: %s" % self.kill)
        watcher = job.PidWatch(self.target, self.ws, task, self.log, self.label, pattern, int(self.env.get("WK_JOB_PID_TRIES") or 900))
        watcher.start()
        try:
            cmd, cwd = self.target.build_argv(self.ws, in_workspace(self.target.tools(self.ws), self.label, argv))
            rc = job.watch(cmd, self.log, self.here, self.clock, self.env, cwd, self.popen, **self.watchdog)
        except job.Interrupted as e:
            watcher.stop()
            warn("interrupted -- stopping the %s build in '%s'" % (self.stage, self.ws))
            if not job.kill(self.target, self.ws, task, "cancelled", self.here, self.clock, self.env):
                warn("it is still running; stop it with:  %s" % self.kill)
            self.target.task_put(self.ws, task)
            raise Refused(job.EXIT_OF.get(e.signum, 130))
        finally:
            watcher.stop()
        return self.verdict(task, rc)

    def verdict(self, task, rc):
        try:
            with open(self.log, errors="replace") as f:
                text = f.read()
        except OSError:
            text = ""

        def end(word):
            task.end(word)
            self.target.task_put(self.ws, task)

        if "stage '%s' done" % self.stage in text:
            end(0)
            return 0
        if task.field("stopping"):
            end(rc or 1)
            warn("the %s build in '%s' was stopped (by '%s')" % (self.stage, self.ws, self.kill))
            raise Refused(rc or 1)
        if rc == 124:
            end("stalled")
            die("the %s build in '%s' stalled: its watchdog killed it, and says why above.\n    log: %s"
                % (self.stage, self.ws, self.log))
        end(rc or 1)
        tail = [l for l in text.replace("\r", "\n").split("\n") if l][-TAIL_LINES:]
        die("the %s build in '%s' failed%s. Last lines:\n%s\n    log: %s"
            % (self.stage, self.ws, "" if rc else " (it exited 0 and never said it was done)",
               "\n".join("    " + l for l in tail), self.log))

    def stop(self):
        """TERM to the job's process tree in the workspace, KILL after WK_KILL_WAIT; reported once it is gone."""
        rc = job.stop(self.target, self.recs, self.ws, self.kind, self.here, self.clock, self.env)
        if rc == 1:
            die("the %s build in '%s' outlived a TERM and a KILL.\n    Look at it:  wk enter %s" % (self.kind, self.ws, self.ws))
        return 0


class ContainerBuilder:
    """A builder in a container workspace made from its own host image; a subclass is its data and its stages."""

    KIND = TITLE = SPEC = BASE_IMAGE = BASE_VAR = ""
    NEEDS = NOT_HERE = IMAGE_NOTE = SURVIVES = ""

    def __init__(self, reg, profile, spec, clock, popen=subprocess.Popen):
        self.reg, self.p, self.spec, self.clock, self.popen = reg, profile, spec, clock, popen
        self.name = profile["IMG_PROFILE"]
        self.here, self.env, self.root = reg.machine, reg.env, str(reg.root)
        self.store = Store(self.env)

    def target(self):
        try:
            t = self.reg.load(self.env.get("WK_TARGET") or self.reg.default())
        except LookupError as e:
            die(str(e))
        if t.kind != "container":
            die("%s, and target '%s' is a %s one.\n    %s" % (self.NEEDS, t.name, t.kind, self.NOT_HERE % {"spec": self.name}))
        return t

    def host_image(self):
        """(base, tag): tagged by a digest of SPEC, so an edited spec is a new image."""
        base = self.env.get(self.BASE_VAR) or self.BASE_IMAGE
        with open(os.path.join(self.root, self.SPEC), "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()[:8]
        return base, "localhost/wk-%s-host:%s-%s" % (self.KIND, base.rsplit(":", 1)[-1], digest)

    def ws_flag(self, ws):
        return "" if ws == images.image_ws(self.name, self.env) else " --workspace " + ws

    def ensure_ws(self, target, ws, base, tag):
        """The image first, so an edited Containerfile changes the wanted tag on every run."""
        podman = target.podman()
        if self.here.run(podman + ["image", "exists", tag]).ok:
            act.debug("workspace image %s already built" % tag)
        else:
            info("building the %s workspace image %s (one layer on %s)" % (self.TITLE, tag, base))
            log(self.IMAGE_NOTE % {"spec": self.SPEC})
            spec = os.path.join(self.root, self.SPEC)
            if not self.here.run_tty(podman + ["build", "--build-arg", "BASE=" + base, "-t", tag, "-f", spec, os.path.dirname(spec)]).ok:
                die("could not build %s.\n    This runs on the host, where there is a network; if apt or the pull failed,\n"
                    "    that is a host-side problem and not the workspace boundary." % tag)
        if target.info(ws) == "absent":
            info("creating workspace '%s' for the %s build" % (ws, self.TITLE))
            if not self.here.run_tty(["env", "WK_SDK_IMAGE=" + tag, os.path.join(self.root, "wk"), "new", ws, "--target", target.name]).ok:
                die("could not create workspace '%s'" % ws)
            return
        was = self.here.run(podman + ["container", "inspect", target.ctr(ws), "--format", "{{.ImageName}}"])
        if was.ok and was.out.strip() and was.out.strip() != tag:
            die("workspace '%s' was made from %s, and the spec now wants\n    %s. A container cannot be moved between images, "
                "so this build\n    would use host packages %s no longer describes.\n    Remake it -- %s:\n"
                "        wk rm %s && wk sysimage build %s" % (ws, was.out.strip(), tag, self.SPEC, self.SURVIVES, ws, self.spec))


def stage_main(label, argv, environ=None):
    env = dict(os.environ if environ is None else environ)
    env["PATH"] = off_wall(env.get("PATH", ""))
    env["WK_BUILD"] = "1"
    sys.stderr.write("wk: %s pid %d\n" % (label, os.getpid()))
    sys.stderr.flush()
    os.execvpe(argv[0], argv, env)


def main(argv):
    verb, a = (argv[0], argv[1:]) if argv else ("", [])
    try:
        if verb == "stage" and len(a) > 2 and a[1] == "--":
            stage_main(a[0], a[2:])
        else:
            die("usage: python3 -m wk.sysimage.task stage <label> -- <argv>", 2)
    except Refused as e:
        return e.status
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
