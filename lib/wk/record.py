"""The task record: one directory per long-running command under
<record dir>/task/, one file per field. Liveness is asked of the process table at read time, never stored."""

import glob
import os
import re
import shlex
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
REQUIRED = ("kind", "where", "name")
_STAMP = re.compile(r"^\d{8}T\d{6}Z$")
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


def watchdog_stall(env, default=STALL_SECONDS):
    return float(env.get("WK_STALL_SECONDS") or default)


def task_ask_seconds(env):
    return float(env.get("WK_TASK_ASK_SECONDS") or 5)


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


# The readings travel beside the products (lib/wk/bench/mac.py's `PgoCollect.evidence`), read from the build
# the workspace holds now, on the machine holding it, each verdict re-derived by its own checker.
GATES = r'''
set -u
shopt -s nullglob
seen=0
for f in "$SRC"/WebKitBuild/*/wk-browser-check.json; do
    seen=1; printf 'browser check %s\n' "$f"
    python3 "$TOOLS/bench/mac-browser-check.py" --read "$f" 2>&1 | sed 's/^/  /'
done
for f in "$SRC"/WebKitBuild/*/wk-profile-check.json "$SRC"/WebKitBuild/wk-pgo/*/profile-check.json; do
    [ -f "$f" ] || continue
    seen=1; printf 'profile check %s\n' "$f"
    PYTHONPATH="$TOOLS/lib" python3 -m wk.pgo check --read "$f" 2>&1 | sed 's/^/  /'
done
for f in "$SRC"/WebKitBuild/*/wk-payload-pins; do
    seen=1; printf 'benchmark payloads %s\n' "$f"
    sed 's/^/  /' "$f"
done
[ "$seen" = 1 ] || printf 'no readings under %s/WebKitBuild -- only a profile-guided build
collects them (build/mac-pgo.sh beside the products, a board cycle under
wk-pgo/); every other build has no gates.\n' "$SRC"
'''


def workspace_log(target, name):
    """(path, whether it is an image-stage log): build.log, else the newest log an image builder wrote under home/."""
    ws_dir = target.store.ws_dir(name)
    build_log = os.path.join(ws_dir, "build.log")
    if os.path.isfile(build_log):
        return build_log, False
    stage_logs = glob.glob(os.path.join(ws_dir, "home", "yocto-*.log")) + glob.glob(os.path.join(ws_dir, "home", "buildroot-*.log"))
    if not stage_logs:
        act.die("no build log and no image-stage log for '%s' -- has it been\n    built? ('wk build', 'wk sysimage build')" % name)
    return max(stage_logs, key=os.path.getmtime), True


def show_log(target, name, mode, hint):
    if mode == "gates":
        r = target.exec(name, ["bash", "-c", "SRC=%s TOOLS=%s\n%s" % (shlex.quote(target.src(name)), shlex.quote(target.tools(name)), GATES)])
        sys.stdout.write(r.out)
        sys.stderr.write(r.err)
        return r.rc
    path, stage = workspace_log(target, name)
    if stage:
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


def log_age(path, clock, machine=None):
    try:
        return int(clock.now() - (machine or here()).mtime(path))
    except (OSError, TypeError):
        return None


class Task:
    """One record, on `machine`; `ask_target(name, pid, cap)` answers True, False or None (no answer in cap seconds)."""

    def __init__(self, path, clock=None, ask_target=None, machine=None, env=None):
        self.env = os.environ if env is None else env
        self.path = Path(path)
        self.id = self.path.name
        self.clock = clock or Clock()
        self.ask_target = ask_target
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

    def pid(self, pid, machine=None):
        self.set("pid", pid)
        if machine:
            self.set("machine", machine)

    def unreadable(self):
        """What keeps this record from a verdict: a required field absent (an older shape), unreadable or unknown, or an unreadable plan."""
        out = [f for f in REQUIRED if not self.field(f)]
        if self.field("where") and self.field("where") not in ("here", "target"):
            out.append("where")
        if self.raw("plan") is UNREADABLE:
            out.append("plan")
        return out

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
        for i, state in self.steps():
            if state == "running":
                return i
        return None

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
        if self.has("exit"):
            return False
        pid = self.field("pid")
        if not pid.isdigit():
            return False
        if self.field("where") == "target":
            if self.ask_target is None:
                raise RuntimeError("%s runs inside a workspace and no target is loaded" % self.id)
            return self.ask_target(self.field("name"), int(pid), cap)
        return self.machine.alive(int(pid))

    def holder_gone(self):
        """True only where the process that took this record's claim is provably gone."""
        if self.has("exit"):
            return True
        pid = self.raw("pid")
        if self.field("where") != "here" or not isinstance(pid, str) or not pid.isdigit():
            return False
        return not self.machine.alive(int(pid))

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
        if how == "capped" and self.field("where") == "target":
            cap = ask_seconds if ask_seconds is not None else task_ask_seconds(self.env)
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

    def running(self, how="capped"):
        return self.verdict(how) in RUNNING


def of_target(target, clock=None, machine=None, env=None):
    """`target`'s records; a pid in a workspace is asked there, None where the workspace does not answer in time."""
    return Records(target.store.records_dir(), clock=clock, ask_target=target.pid_alive, env=target.env if env is None else env,
                   machine=machine)


class Records:
    def __init__(self, root=None, clock=None, ask_target=None, env=None, machine=None):
        self.env = os.environ if env is None else env
        self.root = Path(root or store.Store(self.env).records_dir()) / "task"
        self.clock = clock or Clock()
        self.ask_target = ask_target
        self.machine = machine or here()

    def _task(self, path):
        return Task(path, self.clock, self.ask_target, self.machine, self.env)

    def list(self):
        if not self.machine.isdir(str(self.root)):
            return []
        tasks = [self._task(self.root / n) for n in self.machine.listdir(str(self.root))]
        return [t for t in tasks if t.has("plan")]

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
                self.machine.remove_own(str(t.path))

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
        t = self._task(path)
        if where != "target":
            t.set("pid", pid)
        if holds:
            t.set("holds", holds)
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
