"""The fakes the suite shares, one per seam: a detached child, a registry over a fake machine, and a place that
answers a probe and its own wk."""

import shlex
import sys

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import job, places  # noqa: E402


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

    def wk_cmd(self, args, env):
        return "PEER %s %s" % (self.name, shlex.join(args))



import functools  # noqa: E402
from wk.machine import Fake, Local  # noqa: E402


@functools.lru_cache(maxsize=None)
def bench_lib(argv):
    """This tree's bench libraries, run for real: a reading of them is a pure function of its arguments."""
    return Local().run(list(argv))


class BenchHere(Fake):
    """This host: the bench libraries (`bash -c`) run for real, and nothing else answers."""

    def __init__(self, name="here"):
        super().__init__(name)
        self.react(["bash", "-c"], lambda a, f: bench_lib(tuple(a)))


class JobDriver(places.Driver):
    """A present workspace `ws` whose commands run as `exec ws ...` on the JobWorld behind it."""

    def __init__(self, name, root, env, machine, kind="container"):
        super().__init__(name, root, env, machine)
        self.kind = kind
        self.host = "box.example" if kind == "remote" else ""

    def info(self, ws):
        return "running"

    def state(self, ws, info=None):
        return "present"

    def exec(self, ws, argv, tty=False, timeout=None):
        return self.machine.run(["exec", ws] + list(argv))

    def exec_argv(self, ws, argv, tty=False):
        return ["exec", ws] + list(argv), None

    def exec_tty(self, ws, argv, timeout=None):
        return self.machine.run_tty(["exec-tty", ws] + list(argv))

    def build_size(self, ws):
        return self.machine.size

    def sync_tools(self, ws):
        return self.machine.act_run(["sync-tools", ws]).ok

    def mirror_dir(self):
        return "/mirror"

    def os(self):
        return self.machine.place_os


from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402


class JobWorld(Fake):
    """This host running a job in workspace `ws` on place `box` (a JobDriver of `kind`): the job writes `out` to its
    log and exits `rc`, or runs for ever (`hang`), or is interrupted (`interrupt`)."""

    def __init__(self, tmp, kind="container", out=b""):
        import os
        import tempfile
        from pathlib import Path
        super().__init__("here")
        self.tmp = Path(tempfile.mkdtemp(dir=str(tmp)))
        self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": str(self.tmp / "store"), "WK_LOCK_DIR": str(self.tmp / "locks"),
                    "XDG_STATE_HOME": str(self.tmp / "state"), "WK_PLACE": "box", "WK_NAME": "ws", "WK_IN_VM": "1",
                    "WK_AVAIL_MB": "65536", "WK_JOB_PID_TRIES": "0", "WK_KILL_WAIT": "2"}
        self.conf, self.kind, self.in_ws, self.place_os = {}, kind, False, "linux"
        self.size = (8, 32768, 2 if kind == "remote" else None)
        self.clock = FakeClock()
        self.dirs.add(self.env["WK_LOCK_DIR"])
        self.out, self.rc, self.hang, self.interrupt = out, 0, False, None
        self.answer(["hostname"], out="here\n")
        self.answer(["df", "-Pk"], out="Filesystem 1024-blocks Used Available Capacity Mounted\n/dev/x 1 1 209715200 1% /\n")
        self.react(["exec", "ws", "kill", "-0"], lambda a, f: Result(0 if int(a[-1]) in f.pids else 1))
        self.answer(["sync-tools"])
        self.reg = FakeRegistry(self.env, self, lambda n, e: JobDriver("box", str(REPO), dict(e, **self.conf), self, self.kind),
                                ws_place=lambda ws: "box", in_workspace=lambda: self.in_ws)
        self.ws_dir = os.path.join(self.env["WK_STORE"], "ws", "ws")
        os.makedirs(self.ws_dir)

    @property
    def fake(self):
        return self

    def start(self, argv, out, cwd=None):
        self.effect(("watch", tuple(argv)))
        out.write(self.out)
        return FakeProc(self.rc, None if self.hang else 0, self.interrupt)

    def recs(self):
        from wk import build
        return build.records_of(self.reg.load("box"), self.clock, self)
