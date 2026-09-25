"""The task record: one directory per long-running command under
<record dir>/task/, one file per field, the same directory lib/task.sh
writes. Liveness is asked of the process table at read time, never stored."""

import os
import re
import sys
from pathlib import Path

from wk import act, store
from wk.clock import Clock
from wk.machine import here

RUNNING = ("starting", "running", "silent", "unanswered")
ERROR = re.compile(r"(^FAILED:|^error:|: error:|: fatal error:|ninja: build stopped|No such file or directory)")
NOT_ERROR = re.compile(r"Performing Test|-- Failed|check for working|(^|[: ])warning:")
PROGRESS = (re.compile(r"\[[0-9]+/[0-9]+\]"),
            re.compile(r"Start the iteration ([0-9]+) of ([0-9]+)"),
            re.compile(r"^(CompileC|CompileSwiftSources|SwiftCompile|SwiftDriver|Ld|Libtool|CodeSign|ScanDependencies"
                       r"|ProcessInfoPlistFile|GenerateDSYMFile) ([^ ]+)", re.M))
STEP_EVENTS = {"start": "running", "ok": "done", "already": "done", "failed": "failed",
               "skipped": "skipped", "unneeded": "skipped", "refused": "pending"}
_STAMP = re.compile(r"^\d{8}T\d{6}Z$")
UNREADABLE = object()


def slug(text):
    return re.sub(r"[^A-Za-z0-9._-]", "-", text)


def record_dir(env=None):
    return store.Store(env).record_dir()


def host_name(machine=None):
    """`hostname -s` lowercased as ssh aliases, confs and lock paths spell it (bash: wk_host_name), or ""."""
    return (machine or here()).run(["hostname", "-s"]).out.strip().lower()


def machine_name(env=None, machine=None):
    """A row's machine, in the VM the forwarding workstation; never a lock's, whose pid is the VM's."""
    env = os.environ if env is None else env
    if env.get("WK_IN_VM") and env.get("WK_ROW_LABEL"):
        return env["WK_ROW_LABEL"]
    return host_name(machine) or "here"


def normalised(path):
    with open(path, errors="replace") as f:
        return f.read().replace("\r", "\n")


def first_error(path):
    out = []
    try:
        lines = normalised(path).split("\n")
    except OSError:
        return out
    for n, line in enumerate(lines, 1):
        if ERROR.search(line) and not NOT_ERROR.search(line):
            out.append("%d:%s" % (n, line))
            if len(out) == 5:
                break
    return out


def progress_line(path):
    """What the last 64 KiB of a log says it reached: a ninja step, a benchmark iteration, an Xcode phase."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 65536))
            tail = f.read().decode(errors="replace").replace("\r", "\n")
    except OSError:
        return ""
    m = None
    for m in PROGRESS[0].finditer(tail):
        pass
    if m:
        return m.group(0)
    for m in PROGRESS[1].finditer(tail):
        pass
    if m:
        return "iteration %s/%s" % (m.group(1), m.group(2))
    for m in PROGRESS[2].finditer(tail):
        pass
    return "%s %s" % (m.group(1), os.path.basename(m.group(2))) if m else ""


def log_age(path, clock):
    try:
        return int(clock.now() - os.path.getmtime(path))
    except (OSError, TypeError):
        return None


def _put(path, value):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as f:
        f.write(str(value) + "\n")
    os.replace(tmp, path)


class Task:
    """One record; `ask_target(name, pid, cap)` answers True, False or None
    (no answer within cap seconds) for a pid inside the workspace."""

    def __init__(self, path, clock=None, ask_target=None, machine=None):
        self.path = Path(path)
        self.id = self.path.name
        self.clock = clock or Clock()
        self.ask_target = ask_target
        self.machine = machine or here()

    def field(self, name):
        try:
            return (self.path / name).read_text().replace("\r", "").rstrip("\n")
        except OSError:
            return ""

    def raw(self, name):
        """A field's text, None where it is absent, UNREADABLE where it is there and cannot be read."""
        try:
            return (self.path / name).read_text().replace("\r", "").rstrip("\n")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError):
            return UNREADABLE

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
        """The first verdict stands, and a stop asked for (`stopping`) outranks the status it caused."""
        if (self.path / "exit").is_file():
            return
        if str(status) != "0" and self.field("stopping"):
            status = self.field("stopping")
        self.set("finished", self.clock.iso())
        self.set("exit", status)

    def job_exit(self):
        f = self.field("exit_file")
        if not f or not os.path.isfile(f) or os.path.getsize(f) == 0:
            return ""
        return re.sub(r"[^0-9]", "", Path(f).read_text())

    def alive(self, cap=None):
        if (self.path / "exit").is_file() or self.job_exit():
            return False
        pid = self.field("pid")
        if not pid:
            return False
        if self.field("where") == "target":
            if self.ask_target is None:
                raise RuntimeError("%s runs inside a workspace and no target is loaded" % self.id)
            return self.ask_target(self.field("name"), int(pid), cap)
        return self.machine.alive(int(pid))

    def holder_gone(self):
        """True only where the process that took this record's claim is provably gone."""
        if (self.path / "exit").is_file() or self.job_exit():
            return True
        pid = self.raw("pid")
        if self.field("where") != "here" or not isinstance(pid, str) or not pid.isdigit():
            return False
        return not self.machine.alive(int(pid))

    def verdict(self, how="pid", stall_seconds=None, ask_seconds=None):
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
            cap = float(ask_seconds if ask_seconds is not None else os.environ.get("WK_TASK_ASK_SECONDS") or 5)
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
        stall = float(stall_seconds if stall_seconds is not None else os.environ.get("WK_STALL_SECONDS") or 300)
        return "running" if age <= stall else "silent"

    def running(self, how="capped"):
        return self.verdict(how) in RUNNING


def of_target(target, clock=None, machine=None, env=None):
    """`target`'s records; a pid in a workspace is asked there, None where the workspace does not answer in time."""
    return Records(target.store.record_dir(), clock=clock, ask_target=target.pid_alive, env=target.env if env is None else env,
                   machine=machine)


class Records:
    def __init__(self, root=None, clock=None, ask_target=None, env=None, machine=None):
        self.env = os.environ if env is None else env
        self.root = Path(root or record_dir(self.env)) / "task"
        self.clock = clock or Clock()
        self.ask_target = ask_target
        self.machine = machine or here()

    def _task(self, path):
        return Task(path, self.clock, self.ask_target, self.machine)

    def list(self):
        if not self.root.is_dir():
            return []
        return [self._task(p) for p in sorted(self.root.iterdir()) if (p / "plan").is_file()]

    def stamp_of(self, record_id, kind, name):
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
        if where not in ("here", "target"):
            raise ValueError("where is here or target, not '%s'" % where)
        if not plan:
            raise ValueError("%s/%s declared no plan" % (kind, name))
        if not kill:
            raise ValueError("%s/%s named no kill command" % (kind, name))
        if holds and where != "here":
            raise ValueError("%s/%s: a hold names the pid on this machine that took it, and a workspace's is not one" % (kind, name))
        pid = os.getpid() if pid is None else pid
        path = self.root / ("%s-%s-%s-%d" % (slug(kind), slug(name), self.clock.stamp(), pid))
        self.prune(kind, name, keep=path)
        path.mkdir(parents=True, exist_ok=True)
        t = self._task(path)
        if where != "target":
            t.set("pid", pid)
        # The claim goes on with its holder, before the plan that publishes the record.
        if holds:
            t.set("holds", holds)
        tmp = path / ("plan.tmp.%d" % os.getpid())
        tmp.write_text("".join(s + "\n" for s in plan))
        os.replace(tmp, path / "plan")
        t.set("kind", kind)
        t.set("where", where)
        t.set("name", name)
        t.set("kill", kill)
        t.set("log", log)
        t.set("machine", machine_name(self.env, self.machine))
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
        """Every record naming `resource`, or whose claim cannot be read, with its holder not provably gone."""
        out = []
        for t in self.list():
            holds = t.raw("holds")
            if holds is None or (holds is not UNREADABLE and holds != resource):
                continue
            if t.holder_gone():
                continue
            out.append((t.id, t.field("machine"), "%s %s" % (t.field("kind"), t.field("name")), t.field("kill")))
        return out

    def wait(self, kind, name, log, timeout=0, pid=None, floor="", stream=None):
        """The verdict it ended on, or crashed/timeout; `stream` gets the log as it grows."""
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
            if pid is not None and not self.machine.alive(pid):
                # The child may be mid-write of its final state.
                self.clock.sleep(1)
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


def fleet_holders(resource, records, stores):
    """This store's live holders of `resource`, then every other store's; one that could not be asked is a row
    of its own (`unknown`), since an unread machine is not a free board."""
    rows = list(records.holders(resource))
    for name, ask in stores:
        got, why = ask(resource)
        if got is None:
            rows.append(("?", name, "unknown", why.replace("\t", " ")))
        else:
            rows += [tuple(l.split("\t")) for l in got.replace("\r", "").splitlines() if l]
    return rows


def fleet_stores(root, env, machine):
    """(name, ask) for the podman machine's store where this one is not it, and for each peer through its own wk."""
    from wk import status
    from wk.targets import Registry
    reg = Registry(root, env, machine)

    def holds(target, resource, why):
        rc, out = target.wk("status", "--holds", resource, quiet=True)
        return (out, "") if rc == 0 else (None, why)

    def peer(name):
        def ask(resource):
            t = reg.load(name)
            side, why = t.probe()
            if side != "answering":
                return None, status.far_side_reason(t, side, why)
            return holds(t, resource, "it answers, but its wk does not read --holds: wk sync --tools %s" % name)
        return ask

    stores = []
    if not store.Store(env).is_local():
        stores.append(("%s's podman machine" % machine_name(env, machine),
                       lambda r: holds(reg.load("container"), r, "it did not answer: wk start, or wk sync --tools")))
    return stores + [(p, peer(p)) for p in reg.peer_workstations()]


def hold(records, fleet, machine, kind, name, kill, log, plan, pid, env):
    """A hold on the holder's own record, or None under --dry-run or a driver's own claim (WK_DEVICE_HELD)."""
    res = "device:%s" % machine
    if env.get("WK_DEVICE_HELD") == res:
        return None
    rows = fleet(res)
    quiet = "".join("\n    %s -- %s" % (who, why) for _, who, what, why in rows if what == "unknown")
    held = "".join("\n    %s on %s -- stop it there:  %s" % (what, who, stop) for _, who, what, stop in rows if what != "unknown")
    if quiet:
        act.warn("a machine that could be driving %s could not be asked:%s" % (machine, quiet))
    if held:
        act.barrier("%s is a fleet resource and another live task holds it:%s\n"
                    "    Two drivers on one board make both results junk." % (machine, held))
    if act.dry_run():
        return None
    return records.begin(kind, "here", name, kill, log, plan, holds=res, pid=pid)


def _begin_args(args):
    """[--holds R] [--pid P] [--argv A] and the positionals, the shape lib/task.sh's task_begin takes."""
    kw = {}
    while args and args[0] in ("--holds", "--pid", "--argv"):
        kw[args[0][2:]] = args[1]
        args = args[2:]
    if "pid" in kw:
        kw["pid"] = int(kw["pid"])
    if "argv" in kw:
        kw["argv"] = [kw["argv"]]
    return kw, args


def _out(text):
    if text:
        sys.stdout.write(str(text))


def main(argv, env=None):
    """The record for a bash caller (lib/task.sh): `python3 -m wk.record <verb> ...`."""
    from wk.shell import caller_shell
    env = os.environ if env is None else env
    shell_ = caller_shell(env)
    records = Records(env=env, ask_target=shell_.ask if shell_ else None)
    verb, a = argv[0], argv[1:]
    task = (lambda: records._task(a[0])) if a else None
    try:
        if verb == "field":
            v = task().field(a[1])
            _out(v + "\n" if v else "")
        elif verb == "begin":
            kw, pos = _begin_args(a)
            _out(records.begin(*pos[:5], plan=pos[5:], **kw).path)
        elif verb == "pid":
            task().pid(a[1], a[2] if len(a) > 2 else None)
        elif verb == "set":
            task().set(a[1], a[2])
        elif verb == "step-state":
            task().step_state(a[1], a[2])
        elif verb == "step-event":
            task().step_event(a[1], a[2])
        elif verb == "step":
            task().step(int(a[1]))
        elif verb == "step-named":
            task().step_named(a[1])
        elif verb == "step-now":
            _out(task().step_now())
        elif verb == "stage":
            _out("".join(s + "\n" for s in task().stage()))
        elif verb == "end":
            if not a or not os.path.isdir(a[0]):
                act.die("task_end: '%s' is no task record -- the caller holds none to end (an unset YOCTO_TASK/PGO_TASK "
                        "reads like this)" % (a[0] if a else ""))
            task().end(a[1])
        elif verb == "alive":
            return 0 if task().alive(None) else 1
        elif verb == "verdict":
            _out(task().verdict(a[1] if len(a) > 1 else "pid"))
        elif verb == "find":
            t = records.find(a[0], a[1])
            _out(t.path if t else "")
        elif verb == "list":
            _out("".join("%s\n" % t.path for t in records.list()))
        elif verb == "first-error":
            _out("".join(l + "\n" for l in first_error(a[0])))
        elif verb == "log-age":
            age = log_age(a[0], records.clock) if os.path.isfile(a[0]) else None
            if age is None:
                return 1
            sys.stdout.write("%d" % age)
        elif verb == "hold":
            kw, pos = _begin_args(a)
            root = env.get("WK_ROOT") or str(Path(__file__).resolve().parents[2])
            t = hold(records, lambda r: fleet_holders(r, records, fleet_stores(root, env, records.machine)),
                     pos[0], pos[1], pos[2], pos[3], pos[4], pos[5:], kw.get("pid"), env)
            _out(t.path if t else "")
        else:
            act.die("wk.record: no verb '%s'" % verb, 2)
    except (ValueError, RuntimeError) as e:
        act.err("%s: %s" % (verb, e))
        return 1
    except act.Refused as e:
        return e.status
    return 0


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


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
