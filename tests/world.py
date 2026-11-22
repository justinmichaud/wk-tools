"""A World: fake machines, a fake clock and a fleet, into which a test types a command.

    w = World()                              # desk (client), lin (linux), mac, box (borrowed), vault (credential)
    w.workspace("x", on="lin")               # `wk ls --json` on lin now lists x
    r = w.wk("build", "x", "jsc-release")    # typed on the client; a hand-over really runs the command on the fake server
    r.code, r.out, r.err                     # exit code and the two streams
    r.calls                                  # every command and effect, in order: "[lin] git fetch"
    r.effects                                # the state changes among them
    r.plan                                   # what a --dry-run said it would do, in the same words as r.effects
    w.machines["lin"].files                  # final state; w.state() is all of it, for comparing two worlds
    w.wk("status", on="lin")                 # typed on a server instead
    w.clock, w.tasks("lin"), w.secrets, w.made   # the other fakes

World(here=, confs={name: conf text}, commands=[extra packages of command modules], seams={"tasks": factory})."""

import contextlib
import dataclasses
import io
import json

import wk.cmd
from tests import ROOT
from wk.core import act, conf, dispatch, fakes, seams
from wk.core.fleet import Fleet
from wk.core.inv import Inv
from wk.core.machine import Result

CONFS = {"desk": "kind=client", "lin": "kind=linux", "mac": "kind=mac", "box": "kind=borrowed",
         "vault": "kind=linux\nrole=credential"}


class Stdin(io.StringIO):
    def __init__(self, text, tty):
        super().__init__(text)
        self.tty = tty

    def isatty(self):
        return self.tty


@dataclasses.dataclass
class Run:
    code: int
    out: str
    err: str
    calls: list
    effects: list
    plan: list


class World:
    def __init__(self, here="desk", confs=CONFS, commands=(), seams=None):
        self.here, self.journal, self.clock = here, fakes.Journal(), fakes.FakeClock()
        self.confs = {name: conf.parse(name, text) for name, text in confs.items()}
        self.machines = {name: fakes.Fake(name, self.journal) for name in confs}
        self.commands = dict(dispatch.discover(wk.cmd))
        for package in commands:
            self.commands.update(dispatch.discover(package))
        self.secrets, self.made, self.listed = fakes.FakeSecrets(self.journal), set(), {}
        self._tasks = {name: fakes.FakeTasks(machine, self.clock, self.journal) for name, machine in self.machines.items()}
        self.seams = {"driver": lambda inv, kind, machine: fakes.FakeDriver(kind, machine, self.journal, self.made),
                      "tasks": lambda inv, machine: self.tasks(machine.name), "secrets": lambda inv: self.secrets}
        self.seams.update(seams or {})
        for name, machine in self.machines.items():
            machine.on(("wk",), lambda call, name=name: self._far(name, call))
            # TODO: the workspace lane's `wk ls` replaces this stub, and `workspace` then makes the files it reads.
            self.listed[name] = []
            machine.on(("wk", "ls", "--json"), lambda call, name=name: json.dumps({"workspaces": self.listed[name]}))

    def tasks(self, name):
        return self._tasks[name]

    def workspace(self, name, on="lin", state=seams.READY):
        row = {"name": name, "driver": self.confs[on].driver, "state": state, "path": "/home/u/wk/ws/" + name}
        self.listed[on].append(row)
        return seams.Workspace(server=on, **row)

    def state(self):
        return {"files": {n: dict(m.files) for n, m in self.machines.items()},
                "spawned": {n: list(m.spawned) for n, m in self.machines.items()},
                "tasks": {n: t.status() for n, t in self._tasks.items()},
                "secrets": dict(self.secrets.values), "made": set(self.made)}

    def _type(self, words, on, env, stdin):
        fleet = Fleet(lambda: self.confs, on, self.machines.get, self.machines.get(on), workers=1)
        inv = Inv(ROOT, env, fleet, self.clock, stdin, self.commands, self.seams)
        out, err, flags = io.StringIO(), io.StringIO(), act.flags
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = dispatch.main(list(words), inv)
        finally:
            act.begin(flags)
        return code, out.getvalue(), err.getvalue()

    def _far(self, name, call):
        return Result(*self._type(call.argv[1:], name, call.env or {}, Stdin(call.input or "", False)))

    def wk(self, *words, on=None, stdin="", tty=False, env=None):
        mark = len(self.journal.entries)
        code, out, err = self._type(words, on or self.here, env or {}, Stdin(stdin, tty))
        entries = self.journal.entries[mark:]
        return Run(code, out, err, [line for line, _ in entries], [line for line, changed in entries if changed],
                   [line[len("would: "):] for line in err.splitlines() if line.startswith("would: ")])
