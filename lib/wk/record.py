"""The task record: one directory per long-running command under
<record dir>/task/, one file per field. Liveness is asked of the process table at read time, never stored."""

import glob
import os
import re
import sys
from pathlib import Path

from wk import act, store
from wk.clock import Clock
from wk.lock import Lock
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
REQUIRED = ("kind", "where", "name")
UNREADABLE = object()


def slug(text):
    return re.sub(r"[^A-Za-z0-9._-]", "-", text)


def host_name(machine=None):
    """`hostname -s` lowercased as ssh aliases, confs and lock paths spell it, or ""."""
    return (machine or here()).run(["hostname", "-s"]).out.strip().lower()


def row_label(env):
    return env.get("WK_ROW_LABEL", "")


def host_self(env):
    return bool(env.get("WK_HOST_SELF"))


def machine_name(env=None, machine=None):
    """A row's machine, in the VM the forwarding workstation; never a lock's, whose pid is the VM's."""
    env = os.environ if env is None else env
    if store.in_vm(env) and row_label(env):
        return row_label(env)
    return host_name(machine) or "here"


STALL_SECONDS = 300
ASK_SECONDS = 5   # how long a report waits for a workspace to say whether its job's pid lives


def watchdog_stall(env, default=STALL_SECONDS):
    return float(env.get("WK_STALL_SECONDS") or default)


def watchdog_abort(env, default=0):
    return float(env.get("WK_ABORT_SECONDS") or default)


def default_watchdog(env, stall=None, abort=None):
    if stall is not None:
        env["WK_STALL_SECONDS"] = "%g" % watchdog_stall(env, stall)
    if abort is not None:
        env["WK_ABORT_SECONDS"] = "%g" % watchdog_abort(env, abort)


def normalised(path, machine=None):
    return (machine or here()).read(path).replace("\r", "\n")


def first_error(path, machine=None):
    out = []
    try:
        lines = normalised(path, machine).split("\n")
    except OSError:
        return out
    for n, line in enumerate(lines, 1):
        if ERROR.search(line) and not NOT_ERROR.search(line):
            out.append("%d:%s" % (n, line))
            if len(out) == 5:
                break
    return out


def workspace_log(driver, name):
    """The newest log written for a workspace: its build's, an image stage's under home/, or a task's naming it."""
    ws_dir = driver.store.ws_dir(name)
    logs = [os.path.join(ws_dir, "build.log")]
    logs += glob.glob(os.path.join(ws_dir, "home", "yocto-*.log")) + glob.glob(os.path.join(ws_dir, "home", "buildroot-*.log"))
    logs += [t.field("log") for t in of_driver(driver).list() if t.field("name") == name and t.field("log")]
    there = [p for p in logs if os.path.isfile(p)]
    if not there:
        act.die("no log for '%s': nothing has made, built or staged it here ('wk new', 'wk build', 'wk sysimage build')" % name)
    return max(there, key=os.path.getmtime)


def show_log(driver, name, mode, hint):
    path = workspace_log(driver, name)
    if os.path.basename(path) != "build.log":
        act.info("showing %s" % path)
    if mode == "follow":
        here().exec(["tail", "-f", path])
    if mode == "all":
        sys.stdout.write(normalised(path))
        return 0
    act.log("errors:")
    errors = first_error(path)
    for e in errors:
        act.log("  " + e)
    if not errors:
        act.log("  (none)")
    act.log("")
    act.log("last output:")
    for line in [l for l in normalised(path).split("\n") if l][-15:]:
        act.log("  " + line)
    act.log("")
    act.log("  wk status%s --log --all     full log" % hint)
    act.log("  wk status%s --log --follow  live" % hint)
    act.log("  wk status%s --log --gates   the readings this build was collected under" % hint)
    return 0


def progress_line(path, machine=None):
    """What the last 64 KiB of a log says it reached: a ninja step, a benchmark iteration, an Xcode phase."""
    try:
        tail = (machine or here()).read_bytes(path, -65536).decode(errors="replace").replace("\r", "\n")
    except OSError:
        return ""
    shown = (lambda m: m.group(0), lambda m: "iteration %s/%s" % m.groups(),
             lambda m: "%s %s" % (m.group(1), os.path.basename(m.group(2))))
    for pattern, show in zip(PROGRESS, shown):
        found = list(pattern.finditer(tail))
        if found:
            return show(found[-1])
    return ""


def log_age(path, clock, machine=None):
    try:
        return int(clock.now() - (machine or here()).mtime(path))
    except (OSError, TypeError):
        return None


class Task:
    """One record, on `machine`; `ask_place(name, pid, cap)` answers True, False or None (no answer in cap seconds)."""

    def __init__(self, path, clock=None, ask_place=None, machine=None, env=None):
        self.env = os.environ if env is None else env
        self.path = Path(path)
        self.id = self.path.name
        self.clock = clock or Clock()
        self.ask_place = ask_place
        self.machine = machine or here()

    def _at(self, name):
        return str(self.path / name)

    def has(self, name):
        return self.machine.exists(self._at(name))

    def field(self, name):
        got = self.raw(name)
        return got if isinstance(got, str) else ""

    def raw(self, name):
        """A field's text, None where it is absent, UNREADABLE where it is there and cannot be read."""
        try:
            return self.machine.read(self._at(name)).replace("\r", "").rstrip("\n")
        except (OSError, UnicodeDecodeError):
            return UNREADABLE if self.has(name) else None

    def set(self, name, value):
        self.machine.write_own(self._at(name), str(value) + "\n")

    def unreadable(self):
        """What keeps this record from a verdict: a required field absent (an older shape), unreadable or unknown, or an unreadable plan."""
        out = [f for f in REQUIRED if not self.field(f)]
        if self.field("where") and self.field("where") not in ("here", "place"):
            out.append("where")
        if self.raw("plan") is UNREADABLE:
            out.append("plan")
        return out

    def pid(self, pid, machine=None):
        self.set("pid", pid)
        if machine:
            self.set("machine", machine)

    def plan(self):
        plan = self.raw("plan")
        return plan.split("\n") if isinstance(plan, str) else []

    def step_state(self, index, state):
        self.set("steps/%d" % index, state)

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
        return next((i for i, state in self.steps() if state == "running"), None)

    def stage(self):
        plan = self.plan()
        return [plan[i - 1] for i, state in self.steps() if state == "running"]

    def end(self, status):
        """The first verdict stands, and a stop asked for (`stopping`) outranks the status it caused."""
        if self.has("exit"):
            return
        if str(status) != "0" and self.field("stopping"):
            status = self.field("stopping")
        self.set("finished", self.clock.iso())
        self.set("exit", status)

    def alive(self, cap=None):
        """The one liveness question: True or False, or None where it cannot be told (no answer within `cap`, an unreadable pid)."""
        if self.has("exit"):
            return False
        pid = self.raw("pid")
        if pid is None:
            return False
        if not isinstance(pid, str) or not pid.isdigit():
            return None
        if self.field("where") == "place":
            if self.ask_place is None:
                raise RuntimeError("%s runs inside a workspace and no place is loaded" % self.id)
            return self.ask_place(self.field("name"), int(pid), cap)
        return self.machine.alive(int(pid))

    def holds(self):
        got = self.raw("holds")
        return None if got is UNREADABLE else (got or "").split()

    def line(self):
        return "\t".join((self.id, self.field("machine"), "%s %s" % (self.field("kind"), self.field("name")), self.field("kill")))

    def verdict(self, how="pid", stall_seconds=None, ask_seconds=None):
        if how not in ("pid", "capped"):
            raise ValueError("the pid is asked for as long as it takes or capped, not '%s'" % how)
        if self.unreadable():
            return "unreadable"
        rc = self.field("exit")
        if rc:
            if rc == "0":
                return "ok"
            return "failed" if rc.isdigit() else rc
        if not self.field("pid"):
            return "starting"
        cap = None
        if how == "capped" and self.field("where") == "place":
            cap = ASK_SECONDS if ask_seconds is None else ask_seconds
        alive = self.alive(cap)
        if alive is None:
            return "unanswered"
        if not alive:
            return "died"
        if not self.field("abort_after"):
            return "running"
        try:
            age = self.clock.now() - self.machine.mtime(self.field("log"))
        except OSError:
            return "running"
        stall = float(stall_seconds if stall_seconds is not None else watchdog_stall(self.env))
        return "running" if age <= stall else "silent"


def of_driver(driver, clock=None, machine=None, env=None):
    return Records(driver.store.records_dir(), clock=clock, ask_place=driver.pid_alive, env=driver.env if env is None else env,
                   machine=machine)


class Records:
    def __init__(self, root=None, clock=None, ask_place=None, env=None, machine=None):
        self.env = os.environ if env is None else env
        self.root = Path(root or store.Store(self.env).records_dir()) / "task"
        self.clock = clock or Clock()
        self.ask_place = ask_place
        self.machine = machine or here()

    def _task(self, path):
        return Task(path, self.clock, self.ask_place, self.machine, self.env)

    def list(self):
        if not self.machine.isdir(str(self.root)):
            return []
        tasks = [self._task(self.root / n) for n in self.machine.listdir(str(self.root))]
        return [t for t in tasks if t.has("plan")]

    def stamp_of(self, record_id, kind, name):
        m = re.fullmatch(r"%s-%s-(\d{8}T\d{6}Z)(-\d+)?" % (re.escape(slug(kind)), re.escape(slug(name))), record_id)
        return m.group(1) if m else None

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
                self.machine.remove_own(str(t.path))

    def begin(self, kind, where, name, kill, log, plan, holds=(), pid=None, argv=None, fields=None):
        if where not in ("here", "place"):
            raise ValueError("where is here or place, not '%s'" % where)
        if not plan:
            raise ValueError("%s/%s declared no plan" % (kind, name))
        if not kill:
            raise ValueError("%s/%s named no kill command" % (kind, name))
        if holds and where != "here":
            raise ValueError("%s/%s: a hold names the pid on this machine that took it, and a workspace's is not one" % (kind, name))
        pid = os.getpid() if pid is None else pid
        path = self.root / ("%s-%s-%s-%d" % (slug(kind), slug(name), self.clock.stamp(), pid))
        self.prune(kind, name, keep=path)
        t = self._task(path)
        if where != "place":
            t.set("pid", pid)
        if holds:
            t.set("holds", "\n".join(holds))
        for k, v in (fields or {}).items():
            t.set(k, v)
        t.set("kind", kind)
        t.set("where", where)
        t.set("name", name)
        t.set("kill", kill)
        t.set("log", log)
        t.set("machine", machine_name(self.env, self.machine))
        t.set("argv", " ".join(argv if argv is not None else sys.argv))
        t.set("started", self.clock.iso())
        if watchdog_abort(self.env):
            t.set("abort_after", "%g" % watchdog_abort(self.env))
        for f in ("exit", "finished", "steps"):
            self.machine.remove_own(str(path / f))
        # The plan publishes the record, so every field, the claim first among them, is there before it.
        self.machine.write_own(str(path / "plan"), "".join(s + "\n" for s in plan))
        return t

    def holders(self, resource):
        """The live records holding `resource`; a parent's (WK_TASK_PARENT) holds are its child's own."""
        parent = os.path.realpath(self.env.get("WK_TASK_PARENT") or "/nonexistent")
        out = []
        for t in self.list():
            held = t.holds()
            if held is not None and resource not in held or os.path.realpath(str(t.path)) == parent:
                continue
            if t.alive() is not False:
                out.append(t)
        return out

    def locked(self):
        return Lock(store.Store(self.env), self.machine, self.clock).held("holds")

    def claim(self, kind, name, kill, log, plan, holds, refuse, pid=None, fields=None):
        """`begin` holding `holds` under the store's lock: `refuse(resource, holders)` dies or crosses for each one held."""
        with self.locked():
            for resource in holds:
                held = self.holders(resource)
                if held:
                    refuse(resource, held)
            if act.dry_run():
                return None
            return self.begin(kind, "here", name, kill, log, plan, holds=holds, pid=pid, fields=fields)

    def wait(self, kind, name, log, timeout=0, pid=None, floor="", stream=None):
        """The verdict it ended on, or crashed/timeout; `stream` gets the log as it grows."""
        offset, st = 0, ["starting"]

        def pump():
            nonlocal offset
            if stream is None:
                return
            try:
                data = self.machine.read_bytes(log, offset)
            except OSError:
                return
            offset += len(data)
            if data:
                stream.write(data.decode(errors="replace"))
                stream.flush()

        def verdict():
            t = self.find(kind, name, floor)
            return "starting" if t is None else t.verdict("pid")

        def ended():
            pump()
            st[0] = verdict()
            if st[0] not in ("starting", "running", "silent"):
                return True
            if pid is not None and not self.machine.alive(pid):
                self.clock.sleep(1)   # the child may be mid-write of its final state
                st[0] = verdict()
                return True
            return False

        if not self.clock.wait_until(ended, timeout or None, 1):
            st[0] = "timeout"
        pump()
        return "crashed" if st[0] in ("starting", "running", "silent", "died") else st[0]


def fleet_holders(resource, stores):
    rows = []
    for a in stores(["status", "--holds", resource]):
        if a.side == "none":
            continue
        if a.why or a.rc:
            rows.append(("?", a.name, "unknown", a.why or "its 'wk status --holds' exited %d" % a.rc))
        else:
            rows += [tuple(line.split("\t")) for line in a.out.splitlines() if line]
    return rows


def fleet_stores(root, env, machine):
    """Every other store a fleet resource is held in: the podman machine's where it is not this one, and each peer's."""
    from wk import fleet
    from wk.places import Registry
    reg = Registry(root, env, machine)
    names = ([] if store.Store(env).is_local() else ["container"]) + [m for m in reg.machines() if reg.fleet.kind(m) == "peer"]
    return lambda argv: fleet.each(reg, argv, names, env)


def hold(records, fleet_rows, machine, kind, name, kill, log, plan, pid, env):
    """A device claim on the holder's own record; None under --dry-run or when a holding parent passed it down (WK_DEVICE_HELD)."""
    res = "device:%s" % machine
    if env.get("WK_DEVICE_HELD") == res:
        return None
    rows = fleet_rows(res)
    quiet = "".join("\n    %s -- %s" % (who, why) for _, who, what, why in rows if what == "unknown")
    if quiet:
        act.warn("a machine that could be driving %s could not be asked:%s" % (machine, quiet))

    def refuse(rows):
        act.barrier("%s is a fleet resource and another live task holds it:%s\n    Two drivers on one board make both results junk."
                    % (machine, "".join("\n    %s on %s -- stop it there:  %s" % (what, who, stop) for _, who, what, stop in rows)))
    far = [r for r in rows if r[2] != "unknown"]
    if far:
        refuse(far)
    return records.claim(kind, name, kill, log, plan, [res], lambda _, held: refuse([t.line().split("\t") for t in held]), pid=pid)
