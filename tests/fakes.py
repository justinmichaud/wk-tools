"""The fakes the suite shares, one per seam: a detached child, a registry over a fake machine, and a target that
answers a probe and its own wk."""

import shlex
import sys

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import job, targets  # noqa: E402


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


class FakeRegistry(targets.Registry):
    """targets.Registry over a fake machine. `make(name, env)` builds each target -- any name, or only those `names`
    lists, which are then the whole fleet and the first its default; without `make` the real drivers load.
    `ws_target`, `default` and `in_workspace`, when given, replace those methods."""

    def __init__(self, env, machine, make=None, names=None, ws_target=None, default=None, in_workspace=None):
        super().__init__(REPO, env=env, machine=machine)
        self.make, self.names = make, names
        for method, given in (("ws_target", ws_target), ("default", default), ("in_workspace", in_workspace)):
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
            raise LookupError("unknown target '%s'.\n    The built-in ones are container, vm, remote and local." % name)
        return self.make(name, dict(self.env))


class FakeTarget:
    """A machine behind a target: it answers a probe with (`side`, `why`) and its own wk with (`rc`, `out`), recording
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
