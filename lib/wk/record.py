"""The task record: one directory per long-running command under
<record dir>/task/, one file per field, each written by tmp+rename. The
same directory lib/task.sh writes, so a bash and a Python reader agree.
Liveness is asked of the process table at read time, never stored.

Fields: kind, where (here|target), name, kill, log, machine, pid, argv,
started, plan (one step per line), steps/<n>, holds, abort_after, exit,
finished, and a kind's own (config, subject, exit_file).
"""

import os
import re
import subprocess
import sys
from pathlib import Path

from wk.clock import Clock

RUNNING = ("starting", "running", "silent", "unanswered")
STEP_EVENTS = {"start": "running", "ok": "done", "already": "done", "failed": "failed",
               "skipped": "skipped", "unneeded": "skipped", "refused": "pending"}
_STAMP = re.compile(r"^\d{8}T\d{6}Z$")


def slug(text):
    return re.sub(r"[^A-Za-z0-9._-]", "-", text)


def record_dir(env=None):
    """Where this machine's records live: the store, except a macOS
    workstation's default store, which is the podman VM's and unwritable
    from the host, so that one is diverted to the state directory."""
    env = os.environ if env is None else env
    store = env.get("WK_STORE") or "/var/lib/wk"
    if not env.get("WK_IN_VM") and os.uname().sysname == "Darwin" and store == "/var/lib/wk":
        return os.path.join(env.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "wk")
    return store


def machine_name(env=None):
    env = os.environ if env is None else env
    if env.get("WK_IN_VM") and env.get("WK_ROW_LABEL"):
        return env["WK_ROW_LABEL"]
    cp = subprocess.run(["hostname", "-s"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return (cp.stdout.strip() or "here").lower()


def _put(path, value):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as f:
        f.write(str(value) + "\n")
    os.replace(tmp, path)


def _local_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class Task:
    """One record. `ask_target(name, pid, cap)` says whether a pid inside
    the workspace is alive: True, False, or None when the workspace did not
    answer within `cap` seconds."""

    def __init__(self, path, clock=None, ask_target=None):
        self.path = Path(path)
        self.id = self.path.name
        self.clock = clock or Clock()
        self.ask_target = ask_target

    def field(self, name):
        try:
            return (self.path / name).read_text().replace("\r", "").rstrip("\n")
        except OSError:
            return ""

    def set(self, name, value):
        _put(str(self.path / name), value)

    def pid(self, pid, machine=None):
        self.set("pid", pid)
        if machine:
            self.set("machine", machine)

    def plan(self):
        return self.field("plan").split("\n") if (self.path / "plan").is_file() else []

    def step_state(self, index, state):
        _put(str(self.path / "steps" / str(index)), state)

    def step_event(self, index, event):
        if event not in STEP_EVENTS:
            raise ValueError("'%s' is no scheduler event" % event)
        self.step_state(index, STEP_EVENTS[event])

    def step(self, index):
        for i in range(1, index):
            self.step_state(i, "done")
        self.step_state(index, "running")

    def step_named(self, name):
        plan = self.plan()
        if name not in plan:
            raise ValueError("'%s' is not a step of %s" % (name, self.id))
        self.step(plan.index(name) + 1)

    def steps(self):
        return [(i + 1, self.field("steps/%d" % (i + 1)) or "pending") for i in range(len(self.plan()))]

    def step_now(self):
        for i, state in self.steps():
            if state == "running":
                return i
        return None

    def stage(self):
        plan = self.plan()
        return [plan[i - 1] for i, state in self.steps() if state == "running"]

    def end(self, status):
        """The first verdict stands: a kill's `cancelled` is not overwritten
        by the failure the kill caused."""
        if (self.path / "exit").is_file():
            return
        self.set("finished", self.clock.iso())
        self.set("exit", status)

    def job_exit(self):
        f = self.field("exit_file")
        if not f or not os.path.isfile(f) or os.path.getsize(f) == 0:
            return ""
        return re.sub(r"[^0-9]", "", Path(f).read_text())

    def alive(self, cap=None):
        """True, False, or None when the workspace holding the pid did not
        answer within `cap` seconds."""
        if (self.path / "exit").is_file() or self.job_exit():
            return False
        pid = self.field("pid")
        if not pid:
            return False
        if self.field("where") == "target":
            if self.ask_target is None:
                raise RuntimeError("%s runs inside a workspace and no target is loaded" % self.id)
            return self.ask_target(self.field("name"), int(pid), cap)
        return _local_alive(int(pid))

    def verdict(self, how="pid", stall_seconds=None, ask_seconds=None):
        """starting|running|silent|died|unanswered|ok|failed|<the word end took>."""
        if how not in ("pid", "capped"):
            raise ValueError("the pid is asked for as long as it takes or capped, not '%s'" % how)
        rc = self.field("exit") if (self.path / "exit").is_file() else self.job_exit()
        if rc:
            if rc == "0":
                return "ok"
            return "failed" if rc.isdigit() else rc
        if not self.field("pid"):
            return "starting"
        cap = None
        if how == "capped" and self.field("where") == "target":
            cap = float(ask_seconds if ask_seconds is not None else os.environ.get("WK_TASK_ASK_SECONDS", 5))
        alive = self.alive(cap)
        if alive is None:
            return "unanswered"
        if not alive:
            return "died"
        log = self.field("log")
        try:
            age = self.clock.now() - os.path.getmtime(log)
        except OSError:
            return "running"
        if not self.field("abort_after"):
            return "running"
        stall = float(stall_seconds if stall_seconds is not None else os.environ.get("WK_STALL_SECONDS", 300))
        return "running" if age <= stall else "silent"

    def running(self, how="capped"):
        return self.verdict(how) in RUNNING


class Records:
    """Every record under one record directory."""

    def __init__(self, root=None, clock=None, ask_target=None, env=None):
        self.env = os.environ if env is None else env
        self.root = Path(root or record_dir(self.env)) / "task"
        self.clock = clock or Clock()
        self.ask_target = ask_target

    def _task(self, path):
        return Task(path, self.clock, self.ask_target)

    def list(self):
        """Every record, oldest id first."""
        if not self.root.is_dir():
            return []
        return [self._task(p) for p in sorted(self.root.iterdir()) if (p / "plan").is_file()]

    def stamp_of(self, record_id, kind, name):
        """The id's stamp, or None when the id is another task's."""
        prefix = "%s-%s-" % (slug(kind), slug(name))
        if not record_id.startswith(prefix):
            return None
        rest = record_id[len(prefix):]
        stamp = rest.split("-")[0]
        if not _STAMP.match(stamp):
            return None
        tail = rest[len(stamp):]
        if tail and not re.match(r"^-\d+$", tail):
            return None
        return stamp

    def find(self, kind, name, floor=""):
        """The newest record of this kind and name at or after the floor stamp."""
        last = None
        for t in self.list():
            stamp = self.stamp_of(t.id, kind, name)
            if stamp is None or (floor and stamp < floor):
                continue
            last = t
        return last

    def prune(self, kind, name, keep=None):
        for t in self.list():
            if keep is not None and t.path == keep:
                continue
            if self.stamp_of(t.id, kind, name) is not None and not t.alive(None):
                _rmtree(t.path)

    def begin(self, kind, where, name, kill, log, plan, holds=None, pid=None, argv=None):
        """A new record; prints nothing, returns the Task. `where` is `here`
        for this machine's pid, `target` for the workspace's."""
        if where not in ("here", "target"):
            raise ValueError("where is here or target, not '%s'" % where)
        if not plan:
            raise ValueError("%s/%s declared no plan" % (kind, name))
        if not kill:
            raise ValueError("%s/%s named no kill command" % (kind, name))
        pid = os.getpid() if pid is None else pid
        path = self.root / ("%s-%s-%s-%d" % (slug(kind), slug(name), self.clock.stamp(), pid))
        self.prune(kind, name, keep=path)
        path.mkdir(parents=True, exist_ok=True)
        t = self._task(path)
        if holds:
            t.set("holds", holds)   # before the plan, which is what publishes the record
        tmp = path / ("plan.tmp.%d" % os.getpid())
        tmp.write_text("".join(s + "\n" for s in plan))
        os.replace(tmp, path / "plan")
        t.set("kind", kind)
        t.set("where", where)
        t.set("name", name)
        t.set("kill", kill)
        t.set("log", log)
        t.set("machine", machine_name(self.env))
        if where != "target":
            t.set("pid", pid)
        t.set("argv", " ".join(argv if argv is not None else sys.argv))
        t.set("started", self.clock.iso())
        if self.env.get("WK_ABORT_SECONDS"):
            t.set("abort_after", self.env["WK_ABORT_SECONDS"])
        for f in ("exit", "finished"):
            try:
                (path / f).unlink()
            except OSError:
                pass
        _rmtree(path / "steps")
        (path / "steps").mkdir()
        return t

    def holders(self, resource):
        """(id, machine, 'kind name', kill) per live record here holding it."""
        out = []
        for t in self.list():
            if t.field("holds") != resource:
                continue
            if t.verdict("capped") not in RUNNING:
                continue
            out.append((t.id, t.field("machine"), "%s %s" % (t.field("kind"), t.field("name")), t.field("kill")))
        return out

    def wait(self, kind, name, log, timeout=0, pid=None, floor="", stream=None):
        """The verdict the task ended on, or crashed/timeout. `stream`, when
        given, receives the log's bytes as they arrive."""
        offset = 0
        waited = 0

        def pump():
            nonlocal offset
            if stream is None or not os.path.isfile(log):
                return
            with open(log, "rb") as f:
                f.seek(offset)
                data = f.read()
            offset += len(data)
            if data:
                stream.write(data.decode(errors="replace"))
                stream.flush()

        while True:
            pump()
            t = self.find(kind, name, floor)
            st = "starting" if t is None else t.verdict("pid")
            if st == "died":
                st = "crashed"
                break
            if st not in ("starting", "running", "silent"):
                break
            if pid is not None and not _local_alive(pid):
                self.clock.sleep(1)   # the child may be mid-write of its final state
                t = self.find(kind, name, floor)
                st = "starting" if t is None else t.verdict("pid")
                if st in ("starting", "running", "silent", "died"):
                    st = "crashed"
                break
            if timeout and waited >= timeout:
                st = "timeout"
                break
            self.clock.sleep(1)
            waited += 1
        pump()
        return st


def _rmtree(path):
    path = Path(path)
    if not path.exists():
        return
    for p in sorted(path.rglob("*"), reverse=True):
        if p.is_dir() and not p.is_symlink():
            p.rmdir()
        else:
            p.unlink()
    path.rmdir()

