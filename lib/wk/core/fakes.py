"""In-memory stand-ins for every seam. They share one Journal, which records what happened in order and is where a
kill-point test stops a command."""

import dataclasses
import shlex
import sys

from wk.core import act, seams
from wk.core.clock import Clock
from wk.core.errors import Unreachable
from wk.core.machine import Machine, Result


class Killed(BaseException):
    """The process died here. Not an Exception, so no command's handler survives it."""


class Journal:
    def __init__(self):
        self.entries = []           # (line, whether it changed state)
        self.stop_after = None      # the number of effects after which the next one raises Killed

    def read(self, who, what):
        self.entries.append(("[%s] %s" % (who, what), False))

    def effect(self, who, what):
        if self.stop_after is not None and len(self.effects()) >= self.stop_after:
            raise Killed()
        self.entries.append(("[%s] %s" % (who, what), True))

    def effects(self):
        return [line for line, changed in self.entries if changed]

    def gate(self, who, what, do, dry=None):
        """`act`, with the effect journalled when it really happens."""
        def journalled():
            self.effect(who, what)
            return do()
        return act.act(who, what, journalled, dry)


class FakeClock(Clock):
    """Sleeping advances the time and runs what `after` scheduled for the interval slept through."""

    def __init__(self, start=1000.0):
        self.t, self.due = start, []

    def now(self):
        return self.t

    def after(self, seconds, fn):
        self.due.append((self.t + seconds, fn))

    def sleep(self, seconds):
        self.t += seconds
        for item in sorted((d for d in self.due if d[0] <= self.t), key=lambda d: d[0]):
            self.due.remove(item)
            item[1]()


@dataclasses.dataclass(frozen=True)
class Call:
    argv: tuple
    input: object = None
    cwd: object = None
    env: object = None
    forward: object = None


class Fake(Machine):
    """A machine in memory.

    files     {path: text or bytes}; a directory exists while a file is under it
    modes     {path: mode} as written
    calls     every Call run or acted, in order
    spawned   [(argv, log)]
    down      True makes every method raise Unreachable
    on(prefix, answer)   scripts every command whose argv starts with `prefix` (the longest match wins). `answer` is
              a str (its stdout), a Result, or a function of the Call returning either or None; the function may
              edit `files`. A `run` nothing scripts is an AssertionError; an `act` nothing scripts succeeds."""

    home = "/home/u"

    def __init__(self, name, journal=None):
        self.name, self.journal = name, journal or Journal()
        self.files, self.modes, self.calls, self.spawned, self.scripts, self.down = {}, {}, [], [], {}, False

    def on(self, prefix, answer):
        self.scripts[tuple(prefix)] = answer

    def _up(self):
        if self.down:
            raise Unreachable(self.name, "the fake is down")

    def _gate(self, what, do, dry=None):
        self._up()
        return self.journal.gate(self.name, what, do, dry)

    def run(self, argv, **how):
        self._up()
        self.journal.read(self.name, shlex.join(argv))
        return self._run(argv, strict=True, **how)

    def _run(self, argv, input=None, timeout=None, cwd=None, env=None, stream=False, tty=False, forward=None, strict=False):
        call = Call(tuple(argv), input, cwd, env, forward)
        self.calls.append(call)
        match = max((p for p in self.scripts if call.argv[:len(p)] == p), key=len, default=None)
        if match is None and strict:
            raise AssertionError("an unscripted read on %s: %s\n    script it: fake.on(prefix, answer)" % (self.name, shlex.join(argv)))
        answer = self.scripts.get(match)
        if callable(answer):
            answer = answer(call)
        return answer if isinstance(answer, Result) else Result(0, answer or "")

    def read(self, path, raw=False):
        self._up()
        data = self.files.get(path)
        if data is None:
            return None
        return (data.encode() if isinstance(data, str) else data) if raw else (data if isinstance(data, str) else data.decode(errors="replace"))

    def exists(self, path):
        self._up()
        return path in self.files or bool(self.listdir(path))

    def listdir(self, path):
        self._up()
        under = path.rstrip("/") + "/"
        return sorted({p[len(under):].split("/")[0] for p in self.files if p.startswith(under)})

    def _write(self, path, data, mode):
        self.files[path], self.modes[path] = data, mode

    def _remove(self, path):
        for p in [p for p in self.files if p == path or p.startswith(path.rstrip("/") + "/")]:
            del self.files[p]

    def _spawn(self, argv, log):
        self.spawned.append((tuple(argv), log))


class FakeDriver(seams.Driver):
    """`made` is the set of (server, workspace) the driver has created; share one set across a world."""

    def __init__(self, kind, machine, journal, made):
        self.kind, self.machine, self.journal, self.made = kind, machine, journal, made

    def create(self, ws):
        self.journal.gate(self.machine.name, "driver create %s" % ws.name, lambda: self.made.add((self.machine.name, ws.name)))

    def destroy(self, ws):
        self.journal.gate(self.machine.name, "driver destroy %s" % ws.name, lambda: self.made.discard((self.machine.name, ws.name)))

    def state(self, ws):
        return seams.READY if (self.machine.name, ws.name) in self.made else seams.ABSENT

    def exec_argv(self, ws, argv, cwd=None, tty=False, task=False):
        return ["fake-exec"] + ["-t"] * tty + [ws.name, cwd or ws.path + "/src"] + list(argv)

    def enter_argv(self, ws, argv=(), tty=False):
        return ["fake-enter"] + ["-t"] * tty + [ws.name] + list(argv)


class FakeTasks(seams.Tasks):
    """A task runs, printing nothing, from the moment it is added until a test calls `finish(id, rc, log)`."""

    def __init__(self, machine, clock, journal):
        self.machine, self.clock, self.journal, self.tasks, self.logs = machine, clock, journal, [], {}

    def _set(self, id, **changes):
        self.tasks[id] = dataclasses.replace(self.tasks[id], **changes)

    def add(self, argv, label, group="default", cwd=None):
        def do():
            self.tasks.append(seams.Task(len(self.tasks), label, group, seams.RUNNING, shlex.join(argv), at=self.clock.now()))
            return len(self.tasks) - 1
        return self.journal.gate(self.machine.name, "task add %s: %s" % (label, shlex.join(argv)), do)

    def finish(self, id, rc=0, log=""):
        self._set(id, state=seams.FAILED if rc else seams.DONE, rc=rc, at=self.clock.now())
        self.logs[id] = log

    def status(self):
        return list(self.tasks)

    def log(self, id):
        return self.logs.get(id, "")

    def kill(self, id):
        if not self.tasks[id].ended:
            self.journal.gate(self.machine.name, "task kill %d" % id, lambda: self._set(id, state=seams.KILLED))

    def wait(self, id, timeout=None):
        self.clock.wait_until(lambda: self.tasks[id].ended, timeout)
        return self.tasks[id]

    def follow(self, id):
        sys.stdout.write(self.logs.get(id, ""))

    def quiet(self):
        return {t.id: self.clock.now() - t.at for t in self.tasks if t.state == seams.RUNNING}


class FakeSecrets(seams.Secrets):
    def __init__(self, journal):
        self.journal, self.values = journal, {}

    def get(self, name):
        return self.values.get(name)

    def put(self, name, value):
        self.journal.gate("store", "secret put %s" % name, lambda: self.values.__setitem__(name, value))

    def list(self):
        return sorted(self.values)
