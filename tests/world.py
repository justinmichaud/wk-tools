"""A World: fake machines, a fake clock and a fleet, into which a test types a command.

    w = World()                              # desk (workstation), lin (linux), mac, box (mac), vault (controller)
    w.workspace("x", on="lin")               # lin now holds x: its files, and its driver's part made
    r = w.wk("build", "x", "jsc-release", on="lin")    # typed on the server `lin`
    r.code, r.out, r.err                     # exit code and the two streams
    r.calls                                  # every command and effect, in order: "[lin] git fetch"; "[vault] PUT /ws/x" is a
                                             # request to the controller's API, which the world's controller answers
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
from unittest import mock

import wk.cmd
from tests import ROOT
from wk.api import client, server
from wk.core import act, conf, dispatch, fakes, seams
from wk.core.fleet import Fleet
from wk.core.inv import Inv
from wk.core.machine import Result
from wk.creds import net

CONFS = {"desk": "kind=workstation", "lin": "kind=linux", "mac": "kind=mac", "box": "kind=mac", "vault": "kind=controller"}


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
        self.secrets, self.made = fakes.FakeSecrets(self.journal), set()
        self._tasks = {name: fakes.FakeTasks(machine, self.clock, self.journal) for name, machine in self.machines.items()}
        self.seams = {"driver": lambda inv, kind, machine: fakes.FakeDriver(kind, machine, self.journal, self.made),
                      "tasks": lambda inv, machine: self.tasks(machine.name), "secrets": lambda inv: self.secrets}
        self.seams.update(seams or {})
        for name, machine in self.machines.items():
            machine.on(("wk",), lambda call, name=name: self._far(name, call))
            machine.on(("pgrep",), Result(1))       # no `wk new` is in flight
            machine.on(("tailscale", "ip", "-4"), lambda call: call.argv[-1] + "\n")     # a node's address is its name
            machine.on(("tailscale", "whois", "--json"), lambda call: json.dumps({"Node": {"Name": call.argv[-1] + ".tail.ts.net."}}))

    def reserve(self, name, on):
        """The controller's record says `on` holds the name."""
        for controller in [c.name for c in self.confs.values() if c.kind == "controller"]:
            self.machines[controller].files["/home/u/wk/ws/" + name] = on

    def tasks(self, name):
        return self._tasks[name]

    def workspace(self, name, on="lin", state=seams.READY):
        """The files of a ready workspace, or of a broken one: a checkout with no `.ready`; and its name reserved."""
        path = "/home/u/wk/ws/" + name
        self.reserve(name, on)
        self.machines[on].files[path + "/src/.git/HEAD"] = "ref: refs/heads/main\n"
        if state == seams.READY:
            self.machines[on].files[path + "/.ready"] = ""
            self.made.add((on, name))
        return seams.Workspace(name, on, path, self.confs[on].driver, state)

    def state(self):
        return {"files": {n: dict(m.files) for n, m in self.machines.items()},
                "spawned": {n: list(m.spawned) for n, m in self.machines.items()},
                "tasks": {n: t.status() for n, t in self._tasks.items()},
                "secrets": dict(self.secrets.values), "made": set(self.made)}

    def inv(self, on, env=None, stdin=None):
        fleet = Fleet(lambda: self.confs, on, self.machines.get, self.machines.get(on))
        return Inv(ROOT, env or {}, fleet, self.clock, stdin, self.commands, self.seams)

    def _type(self, words, on, env, stdin):
        out, err, flags = io.StringIO(), io.StringIO(), act.flags
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(net, "request", self._api(on)):
                code = dispatch.main(list(words), self.inv(on, env, stdin))
        finally:
            act.begin(flags)
        return code, out.getvalue(), err.getvalue()

    def _api(self, caller):
        """net.request, with the controller's API answered by this world's controller as `wk serve` there would, for a
        request from `caller` (the fake whois names a node by its address). A request is one effect or read of the
        controller's; what serving it does there is not journalled. Any other URL goes on to the net.request before."""
        before = net.request
        controller = next((c.name for c in self.confs.values() if c.kind == "controller"), None)
        base = controller and "http://%s:%d/" % (controller, client.port(self.confs[controller]))

        def request(method, url, headers=None, body=None, timeout=net.TIMEOUT):
            if not base or not url.startswith(base):
                return before(method, url, headers, body, timeout)
            if self.machines[controller].down:
                raise net.NoAnswer("the fake is down")
            path = url[len(base) - 1:]
            (self.journal.read if method == "GET" else self.journal.effect)(controller, "%s %s" % (method, path))
            flags, self.journal.muted = act.flags, self.journal.muted + 1
            act.begin(act.Flags())
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    status, doc = server.answer(self.inv(controller), method, path, caller)
            finally:
                act.begin(flags)
                self.journal.muted -= 1
            return status, json.dumps(doc).encode()
        return request

    def _far(self, name, call):
        return Result(*self._type(call.argv[1:], name, call.env or {}, Stdin(call.input or "", call.tty)))

    def wk(self, *words, on=None, stdin="", tty=False, env=None):
        mark = len(self.journal.entries)
        code, out, err = self._type(words, on or self.here, env or {}, Stdin(stdin, tty))
        entries = self.journal.entries[mark:]
        return Run(code, out, err, [line for line, _ in entries], [line for line, changed in entries if changed],
                   [line[len("would: "):] for line in err.splitlines() if line.startswith("would: ")])
