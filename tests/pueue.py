"""A pueue daemon for a Fake machine, and a World whose commands reach it through the real client.

    w = world()                         # World(seams={"tasks": wk.tasks.tasks}), each server with a daemon and a probe
    w.pueue["lin"].tasks                # {id: the task as `status --json` prints it}
    w.pueue["lin"].finish(0, rc=1, log="...")
    w.pueue["lin"].ending(rc, log)      # the next task followed ends that way while it is followed
    w.pueue["lin"].grew[0] = t          # when task 0's log last grew (default: when it started)
    w.probe["lin"] = "cores 4\\n..."    # what the sizing probe prints in a workspace there
    w.missing["lin"] = "fast/x.html\\n" # what the layout-test existence check prints there
    added(w, "lin")                     # the argv of every task added there

The daemon's state is the file <wk home>/pueue/state.json, so World.state() and the kill-point checks see it."""

import datetime
import json
import shlex
import types

from tests.world import World
from wk import webkit
from wk.core.machine import Result
from wk.tasks import sizing, tasks
from wk.tasks.pueue import MTIMES, Pueue

LINUX = "cores 16\navailable_kb 67108864\n"
MAC = "cores 10\ntotal_bytes 34359738368\n"


def iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat()


def bare(clock):
    """As much of an inv as the client asks: a clock, and where a machine's wk home is."""
    return types.SimpleNamespace(wk_home_on=lambda machine: machine.home + "/wk", clock=clock)


class FakePueue:
    def __init__(self, machine, clock, up=True):
        self.m, self.clock, self.logs, self.grew, self.endings = machine, clock, {}, {}, []
        self.client = Pueue(bare(clock), machine)
        self.path = self.client.dir + "/state.json"
        machine.on(self.client.argv(), self.answer)
        machine.on(["python3", "-I", "-c", MTIMES], lambda call: "".join("%d %r\n" % item for item in sorted(self.grew.items())))
        if up:
            self.start()

    def start(self):
        self.save({"tasks": {}, "groups": {"default": {"status": "Running", "parallel_tasks": 1}}})

    @property
    def state(self):
        return json.loads(self.m.files[self.path])

    @property
    def tasks(self):
        return {int(id): task for id, task in self.state["tasks"].items()}

    def save(self, state):
        self.m.files[self.path] = json.dumps(state, sort_keys=True)

    def _schedule(self, state):
        for task in sorted(state["tasks"].values(), key=lambda t: t["id"]):
            running = sum(1 for t in state["tasks"].values() if t["group"] == task["group"] and "Running" in t["status"])
            if "Queued" in task["status"] and running < state["groups"][task["group"]]["parallel_tasks"]:
                task["status"] = {"Running": {"enqueued_at": task["created_at"], "start": iso(self.clock.now())}}
                self.grew[task["id"]] = self.clock.now()
        self.save(state)

    def finish(self, id, rc=0, log="", result=None):
        state = self.state
        task = state["tasks"][str(id)]
        task["status"] = {"Done": {"enqueued_at": task["created_at"], "start": task["status"]["Running"]["start"],
                                   "end": iso(self.clock.now()), "result": result or ({"Failed": rc} if rc else "Success")}}
        self.logs[id] = log
        self._schedule(state)

    def ending(self, rc=0, log=""):
        self.endings.append((rc, log))

    def answer(self, call):
        words = list(call.argv[3:])
        if self.path not in self.m.files:
            return Result(1, "", "Error: Failed to connect to the daemon")
        state = self.state
        if words == ["status", "--json"]:
            return json.dumps(state)
        if words[:2] == ["group", "add"]:
            if words[2] in state["groups"]:
                return Result(1, "", 'Group "%s" already exists' % words[2])
            state["groups"][words[2]] = {"status": "Running", "parallel_tasks": int(words[4])}
        elif words[0] == "parallel":
            state["groups"][words[3]]["parallel_tasks"] = int(words[1])
        elif words[0] == "add":
            flags = dict(zip(words[2:8:2], words[3:8:2]))
            if flags["--group"] not in state["groups"]:
                return Result(1, "", 'Group "%s" does not exist' % flags["--group"])
            id = max([t["id"] for t in state["tasks"].values()] + [-1]) + 1
            now = iso(self.clock.now())
            state["tasks"][str(id)] = {"id": id, "created_at": now, "original_command": words[9], "command": words[9],
                                       "path": flags["--working-directory"], "envs": {}, "group": flags["--group"], "dependencies": [],
                                       "priority": 0, "label": flags["--label"], "status": {"Queued": {"enqueued_at": now}}}
            self._schedule(state)
            return "%d\n" % id
        elif words[0] == "kill":
            self.finish(int(words[1]), result="Killed")
            return None
        elif words[0] == "remove":
            del state["tasks"][words[1]]
        elif words[0] == "log":
            return json.dumps({words[1]: {"task": state["tasks"][words[1]], "output": self.logs.get(int(words[1]), "")}})
        elif words[0] == "follow":
            if self.endings:
                self.finish(int(words[1]), *self.endings.pop(0))
            return self.logs.get(int(words[1]), "")
        else:
            raise AssertionError("the fake pueue does not know: %s" % shlex.join(words))
        self.save(state)


def world(here="lin", **how):
    w = World(here=here, seams={"tasks": tasks}, **how)
    w.pueue = {name: FakePueue(machine, w.clock) for name, machine in w.machines.items() if w.confs[name].kind != "client"}
    w.probe = {name: MAC if w.confs[name].kind == "mac" else LINUX for name in w.pueue}
    w.missing = {name: "" for name in w.pueue}

    def inside(call, name):
        inner = list(call.argv[3:])
        return w.probe[name] if inner == sizing.PROBE else w.missing[name] if inner[:3] == webkit.missing_argv("", [])[:3] else None
    for name in w.pueue:
        w.machines[name].on(["fake-exec"], lambda call, name=name: inside(call, name))
    return w


def added(w, name):
    """What each task added on `name` runs, as an argv."""
    return [shlex.split(call.argv[-1]) for call in w.machines[name].calls if call.argv[3:4] == ("add",)]
