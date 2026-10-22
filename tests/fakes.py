"""The fakes the suite shares, one per seam: a detached child, a registry over a fake machine, and a place that
answers a probe and its own wk."""

import contextlib
import fnmatch
import functools
import io
import os
import shlex
import shutil
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import act, job, places, record  # noqa: E402
from wk.clock import Clock  # noqa: E402
from wk.machine import LOGIN, Local, Machine, Planted, Result, matches  # noqa: E402


class FakeClock(Clock):
    def __init__(self, start=1_700_000_000.0):
        self.t = float(start)
        self.slept = []

    def now(self):
        return self.t

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


class Exited:
    def __init__(self, pid, rc):
        self.pid, self.returncode = pid, rc

    def poll(self):
        return self.returncode

    def wait(self):
        return self.returncode


class Killed(BaseException):
    """The Fake's process died: raised in place of the effect `stop_after` names, and no `except Exception` outlives it."""


class Fake(Machine):
    """An in-memory host: a command answers what `answer()` or `react()`
    registered for the longest matching argv prefix (the latest among
    equals), and every effect lands in `effects`; with `stop_after` set, the
    effect after that many raises Killed instead of landing."""

    def __init__(self, name="fake"):
        self.name = name
        self.files = {}
        self.dirs = set()
        self.links = set()
        self.mtimes = {}
        self.pids = {os.getpid()}
        self.answers = []
        self.effects = []
        self.next_pid = 1000
        self.stop_after = None
        self.applied = 0
        self.streamed = []
        self._acting = False

    def answer(self, prefix, rc=0, out="", err=""):
        self.answers.append((tuple(prefix), Result(rc, out, err)))

    def react(self, prefix, fn):
        """`fn(argv, fake)` returns the Result and may move the fake's files and pids."""
        self.answers.append((tuple(prefix), fn))

    def effect(self, record):
        if self.stop_after is not None and self.applied >= self.stop_after:
            raise Killed(record)
        self.applied += 1
        self.effects.append(record)

    def _record(self, record):
        if self._acting:
            self.effect(record)
        else:
            self.effects.append(record)

    def record_run(self, argv):
        self._record(("run", tuple(argv)))

    def _answer(self, argv):
        best = None
        for prefix, result in self.answers:
            if tuple(argv[:len(prefix)]) == prefix and (best is None or len(prefix) >= len(best[0])):
                best = (prefix, result)
        if best is None:
            return Result(127, "", "%s: no answer registered" % argv[0])
        if not callable(best[1]):
            return best[1]
        seen = list(argv)
        if seen[0] == "ssh" and seen[-1].startswith(LOGIN):
            seen[-1] = shlex.split(seen[-1])[-1]
        return best[1](seen, self)

    def act_run(self, argv, **kw):
        self._acting = True
        try:
            return super().act_run(argv, **kw)
        finally:
            self._acting = False

    def run(self, argv, input=None, timeout=None, stream=False):
        self.record_run(argv)
        r = self._answer(argv)
        if not stream:
            return r
        self.streamed.append(tuple(argv))
        sys.stderr.write(r.out + r.err)
        return Result(r.rc)

    def run_tty(self, argv, cwd=None, timeout=None):
        self._record(("run_tty", tuple(argv), cwd))
        return self._answer(argv)

    def read(self, path):
        if path not in self.files:
            raise OSError("no such file: %s" % path)
        return self.files[path]

    def read_tree(self, anchor, rel, patterns, depth=3):
        top = os.path.join(anchor, rel).rstrip("/")
        path = anchor.rstrip("/")
        for part in (p for p in rel.split("/") if p):
            path += "/" + part
            if path in self.links:
                raise Planted(path)
        if top not in self.dirs:
            raise OSError("no such directory: %s" % top)
        out = {}
        for p in sorted(self.files):
            r = p[len(top) + 1:]
            if not p.startswith(top + "/") or r.count("/") >= depth:
                continue
            if p in self.links:
                raise Planted(p)
            if matches(r, patterns):
                data = self.files[p]
                out[r] = data if isinstance(data, bytes) else data.encode()
        return out

    def exists(self, path):
        return path in self.files or path in self.dirs

    def isdir(self, path):
        return path in self.dirs

    def listdir(self, path):
        if path not in self.dirs:
            raise OSError("no such directory: %s" % path)
        prefix = path.rstrip("/") + "/"
        names = set()
        for p in list(self.files) + list(self.dirs):
            if p.startswith(prefix):
                names.add(p[len(prefix):].split("/")[0])
        return sorted(names)

    def alive(self, pid):
        return pid in self.pids

    def readlink(self, path):
        return self.files[path] if path in self.files else None

    def mtime(self, path):
        if path not in self.files:
            raise OSError("no such file: %s" % path)
        return self.mtimes.get(path, 0.0)

    def read_bytes(self, path, start=0):
        data = self.read(path)
        return (data if isinstance(data, bytes) else data.encode())[start:]

    def _set_file(self, path, text):
        self.files[path] = text
        self.links.discard(path)
        parent = os.path.dirname(path)
        while parent and parent != "/":
            self.dirs.add(parent)
            parent = os.path.dirname(parent)

    def write(self, path, text):
        self.effect(("write", path))
        if act.dry_run():
            return
        self._set_file(path, text)

    def write_own(self, path, text):
        self._set_file(path, text)

    def remove_own(self, path):
        self._drop(path)

    def _drop(self, path):
        self.files = {p: t for p, t in self.files.items() if p != path and not p.startswith(path.rstrip("/") + "/")}
        self.links &= set(self.files)
        self.dirs = {d for d in self.dirs if d != path and not d.startswith(path.rstrip("/") + "/")}

    def remove(self, path):
        self.effect(("remove", path))
        if act.dry_run():
            return
        self._drop(path)

    def remove_now(self, path):
        self.effect(("remove", path))
        self._drop(path)

    def mkdir(self, path):
        self.effect(("mkdir", path))
        if act.dry_run():
            return
        self.dirs.add(path)

    def mkdir_now(self, path):
        self.effect(("mkdir", path))
        self.dirs.add(path)

    def symlink(self, to, path):
        self.effect(("symlink", path))
        parent = os.path.dirname(path)
        if path in self.files or path in self.dirs or (parent not in ("", "/") and parent not in self.dirs):
            return False
        self.files[path] = to
        self.links.add(path)
        return True

    def rename(self, path, dst):
        self.effect(("rename", path, dst))
        if path not in self.files:
            return False
        self.files[dst] = self.files.pop(path)
        self.links.discard(dst)
        if path in self.links:
            self.links.discard(path)
            self.links.add(dst)
        return True

    def kill(self, pid, sig=signal.SIGTERM):
        self.effect(("kill", pid, int(sig)))
        if act.dry_run():
            return True
        if pid in self.pids:
            self.pids.discard(pid)
            return True
        return False

    def spawn(self, argv, log):
        self.effect(("spawn", tuple(argv), log))
        if act.dry_run():
            return 0
        self.next_pid += 1
        self.pids.add(self.next_pid)
        self.files.setdefault(log, "")
        return self.next_pid

    def start(self, argv, out, cwd=None):
        self.effect(("start", tuple(argv)))
        r = self._answer(argv)
        out.write((r.out + r.err).encode())
        self.next_pid += 1
        return Exited(self.next_pid, r.rc)

    def _exec(self, argv, cwd, env):
        self.effect(("exec", tuple(argv), cwd))

    def forward(self, port, log=os.devnull):
        from contextlib import contextmanager

        @contextmanager
        def held():
            self.effect(("forward", port))
            pid = 0
            if not act.dry_run():
                self.next_pid += 1
                pid = self.next_pid
                self.pids.add(pid)
            try:
                yield pid
            finally:
                self.pids.discard(pid)
        return held()

    # A real file on disk crosses into this fake host's in-memory files, and back.
    def copy_in(self, src, dest):
        self.effect(("copy_in", src, dest))
        if act.dry_run():
            return
        with open(src, "rb") as f:
            data = f.read()
        self._set_file(dest, data)

    def copy_out(self, src, dest):
        self.effect(("copy_out", src, dest))
        if act.dry_run():
            return
        data = self.read(src)
        with open(dest, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode())

    def copy_tree_in(self, src, dest):
        self.effect(("copy_tree_in", src, dest))
        if act.dry_run():
            return
        self._drop(dest)
        self.dirs.add(dest)
        for root, _, filenames in os.walk(src):
            rel = os.path.relpath(root, src)
            base = dest if rel == "." else os.path.join(dest, rel)
            self.dirs.add(base)
            for fn in filenames:
                with open(os.path.join(root, fn), "rb") as f:
                    self.files[os.path.join(base, fn)] = f.read()

    def copy_tree_out(self, src, dest, exclude=()):
        self.effect(("copy_tree_out", src, dest) + tuple(exclude))
        if act.dry_run():
            return
        if src not in self.dirs:
            raise OSError("no such directory: %s" % src)
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        os.makedirs(dest, exist_ok=True)
        prefix = src.rstrip("/") + "/"
        for p, data in self.files.items():
            if not p.startswith(prefix):
                continue
            rel = p[len(prefix):]
            if any(fnmatch.fnmatchcase(part, x) for part in rel.split("/") for x in exclude):
                continue
            out = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as f:
                f.write(data if isinstance(data, bytes) else data.encode())


class World(Fake):
    """The one fake world a module builds its answers on: a Fake host with a FakeClock and `env`; given `tmp`, a
    scratch directory under it holds HOME, the store, the lock directory and XDG state. Every act_run is recorded as
    ("act", argv), dry or wet, so a dry run's plan holds against a wet run's (`mutations`)."""

    MUTATING = ("act", "write", "mkdir", "remove", "kill", "spawn", "copy_in", "copy_out", "copy_tree_in", "copy_tree_out", "forward")

    def __init__(self, tmp=None, name="here", **env):
        super().__init__(name)
        self.fake, self.clock = self, FakeClock()
        self.tmp, self.env = None, {}
        if tmp is not None:
            self.tmp = Path(tempfile.mkdtemp(dir=str(tmp)))
            self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": str(self.tmp / "store"),
                        "WK_LOCK_DIR": str(self.tmp / "locks"), "XDG_STATE_HOME": str(self.tmp / "state")}
            self.dirs.add(self.env["WK_LOCK_DIR"])
        self.env.update(env)
        self.answer(["hostname"], out=name + "\n")

    def act_run(self, argv, **kw):
        self.effects.append(("act", tuple(argv)))
        return super().act_run(argv, **kw)

    def rel(self, value):
        """A path or effect with the scratch directory taken out, so two worlds compare."""
        if isinstance(value, tuple):
            return tuple(self.rel(v) for v in value)
        return value.replace(str(self.tmp), "") if self.tmp and isinstance(value, str) else value

    def work(self):
        """The effects that are the command's work: a lock's are process coordination, not state."""
        locks = self.env.get("WK_LOCK_DIR")
        return [e for e in self.effects if not (locks and len(e) > 1 and isinstance(e[1], str) and e[1].startswith(locks))]

    def mutations(self):
        return [self.rel(e) for e in self.work() if e[0] in self.MUTATING]

    def state(self):
        return sorted(self.rel(p) for p in self.files), sorted(self.rel(d) for d in self.dirs)


class FlowTest(unittest.TestCase):
    """A scratch directory `tmp`, os.environ restored after with wk's mode variables and the bench knobs unset, and
    this host named HOST."""
    HOST = "here"
    UNSET = ("WK_DRY_RUN", "WK_YES", "WK_QUIET", "WK_DESTRUCTIVE", "WK_CONFIRMED", "WK_CMD", "WK_DEBUG", "WK_FORCE",
             "WK_DEVICE_HELD", "WK_BENCH_ASLR", "WK_BENCH_PATH_PAD", "WK_BENCH_ENV_PAD")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-flow-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        for p in (mock.patch.dict(os.environ), mock.patch.object(record, "host_name", return_value=self.HOST)):
            p.start()
            self.addCleanup(p.stop)
        for v in self.UNSET:
            os.environ.pop(v, None)

    def stderr(self, fn):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            result = fn()
        return result, err.getvalue()

    def refused(self, fn, status=1):
        with self.assertRaises(act.Refused) as cm:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                fn()
        self.assertEqual(cm.exception.status, status, err.getvalue())
        return err.getvalue()

    def dry_run(self):
        os.environ["WK_DRY_RUN"] = "1"


class FakeProc:
    """A detached child: it raises job.Interrupted(`interrupt`) on its first poll, runs for `polls` polls (for ever when
    None) calling `grow` on each, then exits `rc`; a wait on one still running kills it (-9)."""

    def __init__(self, rc=0, polls=0, interrupt=None, grow=None):
        self.pid, self.rc, self.polls, self.interrupt, self.grow, self.returncode = 4242, rc, polls, interrupt, grow, None

    def poll(self):
        if self.interrupt is not None:
            signum, self.interrupt = self.interrupt, None
            raise job.Interrupted(signum)
        if self.returncode is None and (self.polls is None or self.polls > 0):
            if self.polls:
                self.polls -= 1
            if self.grow:
                self.grow()
            return None
        self.returncode = self.rc if self.returncode is None else self.returncode
        return self.returncode

    def wait(self):
        self.returncode = -9 if self.returncode is None else self.returncode
        return self.returncode


class FakeRegistry(places.Registry):
    """places.Registry over a fake machine. `make(name, env)` builds each place -- any name, or only those `names`
    lists, which are then the whole fleet and the first its default; without `make` the real drivers load.
    `ws_place`, `default` and `in_workspace`, when given, replace those methods."""

    def __init__(self, env, machine, make=None, names=None, ws_place=None, default=None, in_workspace=None):
        super().__init__(REPO, env=env, machine=machine)
        self.make, self.names = make, names
        for method, given in (("ws_place", ws_place), ("default", default), ("in_workspace", in_workspace)):
            if given is not None:
                setattr(self, method, given)

    def all(self):
        return super().all() if self.names is None else list(self.names)

    def default(self):
        return super().default() if self.names is None else self.names[0]

    def vm_listed(self):
        return super().vm_listed() if self.names is None else "vm" in self.names

    def load(self, name):
        if self.make is None:
            return super().load(name)
        if self.names is not None and name not in self.names:
            raise LookupError("unknown place '%s'.\n    The built-in ones are container, vm, remote and local." % name)
        return self.make(name, dict(self.env))


class FakeDriver:
    """A machine behind a place: it answers a probe with (`side`, `why`) and its own wk with (`rc`, `out`), recording
    each ask's (args, env, quiet) in `asked`; `far_store` is its task store and `here` whether it is this machine. A
    command for its wk is `PEER <name> <args>`, for a world to answer."""

    def __init__(self, name, kind="remote", here=False, side="answering", why="", rc=0, out="", far_store=None,
                 peer=False, machine=None):
        self.name, self.kind, self.is_local, self.peer, self.machine, self.env = name, kind, here, peer, machine, {}
        self.side, self.why, self.rc, self.out, self.far_store, self.asked = side, why, rc, out, far_store, []

    def probe(self):
        return self.side, self.why

    def is_here(self):
        return self.is_local

    def wk(self, *args, env=None, quiet=False):
        self.asked.append((args, dict(env or {}), quiet))
        return self.rc, self.out

    def task_store(self):
        return self.far_store

    def has_wk(self):
        return True

    def machine_state(self):
        return "stopped"

    def podman_machine(self):
        return "wk"

    def wk_cmd(self, args, env):
        return "PEER %s %s" % (self.name, shlex.join(args))





@functools.lru_cache(maxsize=None)
def bench_lib(argv):
    """This tree's bench libraries, run for real: a reading of them is a pure function of its arguments."""
    return Local().run(list(argv))


class BenchHere(World):
    """This host: the bench libraries (`bash -c`) run for real, and nothing else answers."""

    def __init__(self, name="here"):
        super().__init__(name=name)
        self.react(["bash", "-c"], lambda a, f: bench_lib(tuple(a)))


class WsDriver(places.Driver):
    """A present, running workspace on a fake machine: a command in it runs there as `exec <ws> <argv>` (`exec-tty` on
    a terminal), and `kind`, `src`, `home`, `os` and `mirror` are what it answers."""

    def __init__(self, name, root, env, machine, kind="container", src="/src/WebKit", home="/home/u", os="linux", mirror=""):
        super().__init__(name, root, env, machine)
        self.kind, self._src, self._home, self._os, self._mirror = kind, src, home, os, mirror
        self.host = "box.example" if kind == "remote" else ""

    def info(self, ws):
        return "running"

    def state(self, ws, info=None):
        return "present"

    def src(self, ws):
        return self._src

    def home(self):
        return self._home

    def os(self):
        return self._os

    def mirror_dir(self):
        return self._mirror

    def exec_argv(self, ws, argv, tty=False, person=False):
        return ["exec", ws] + list(argv), None

    def exec_tty(self, ws, argv, timeout=None):
        return self.machine.run_tty(["exec-tty", ws] + list(argv))

    def sync_tools(self, ws):
        return self.machine.act_run(["sync-tools", ws]).ok


class JobDriver(WsDriver):
    """A WsDriver on the JobWorld behind it, which answers its size and its OS."""

    def build_size(self, ws):
        return self.machine.size

    def os(self):
        return self.machine.place_os


class JobWorld(World):
    """This host running a job in workspace `ws` on place `box` (a JobDriver of `kind`): the job writes `out` to its
    log and exits `rc`, or runs for ever (`hang`), or is interrupted (`interrupt`)."""

    def __init__(self, tmp, kind="container", out=b""):
        super().__init__(tmp, WK_PLACE="box", WK_NAME="ws", WK_IN_VM="1", WK_AVAIL_MB="65536", WK_JOB_PID_TRIES="0",
                         WK_KILL_WAIT="2")
        self.conf, self.kind, self.in_ws, self.place_os = {}, kind, False, "linux"
        self.size = (8, 32768, 2 if kind == "remote" else None)
        self.out, self.rc, self.hang, self.interrupt = out, 0, False, None
        self.answer(["df", "-Pk"], out="Filesystem 1024-blocks Used Available Capacity Mounted\n/dev/x 1 1 209715200 1% /\n")
        self.react(["exec", "ws", "kill", "-0"], lambda a, f: Result(0 if int(a[-1]) in f.pids else 1))
        self.answer(["sync-tools"])
        self.reg = FakeRegistry(self.env, self, lambda n, e: JobDriver("box", str(REPO), dict(e, **self.conf), self, self.kind, mirror="/mirror"),
                                ws_place=lambda ws: "box", in_workspace=lambda: self.in_ws)
        self.ws_dir = os.path.join(self.env["WK_STORE"], "ws", "ws")
        os.makedirs(self.ws_dir)

    def start(self, argv, out, cwd=None):
        self.effect(("watch", tuple(argv)))
        out.write(self.out)
        return FakeProc(self.rc, None if self.hang else 0, self.interrupt)

    def recs(self):
        return job.records_of(self.reg.load("box"), self.clock, self)
