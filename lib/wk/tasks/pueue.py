"""The Tasks seam over pueue: one daemon per machine, run from wk's pinned binaries under wk's own config, so its
socket and logs are at one path whatever XDG variables a login has.

Coded against pueue VERSION; tests/data/pueue-status.json is its pueue_lib/tests/data/v4.0.0_state.json:
    status --json            {"tasks": {"<id>": {id, created_at, command, group, label, status}}, "groups": {name: {parallel_tasks}}}
    status                   {"Queued" | "Stashed" | "Locked": {...}} | {"Running" | "Paused": {start}} | {"Done": {end, result}}
    result                   "Success" | {"Failed": rc} | "Killed" | {"FailedToSpawn": text} | "Errored" | "DependencyFailed"
    log <id> --json --full   {"<id>": {"output": text}}
    add --print-task-id      prints the id; the command is one string, run by `sh -c`
    <pueue_directory>/task_logs/<id>.log is the task's output as it grows"""

import datetime
import json
import re
import shlex
import sys

from wk.core import seams
from wk.core.act import die

VERSION = "4.0.5"
LIMITS = {"build": 1}
CONFIG = "shared:\n  pueue_directory: %s\n  runtime_directory: %s\n"
MTIMES = """import os, sys
for name in os.listdir(sys.argv[1]):
    if name.endswith(".log"):
        print(name[:-4], os.path.getmtime(os.path.join(sys.argv[1], name)))"""


def stamp(text):
    """pueue's RFC 3339 time. datetime (3.9) reads neither its nanoseconds nor its Z."""
    return datetime.datetime.fromisoformat(re.sub(r"(\.\d{6})\d*", r"\1", text).replace("Z", "+00:00")).timestamp()


def job(row):
    (kind, detail), = row["status"].items()
    rc, at = None, row["created_at"]
    if kind in ("Running", "Paused"):
        state, at = seams.RUNNING, detail["start"]
    elif kind != "Done":
        state = seams.QUEUED
    else:
        result, at = detail["result"], detail["end"]
        if result == "Success":
            state, rc = seams.DONE, 0
        elif result == "Killed":
            state = seams.KILLED
        else:
            state, rc = seams.FAILED, result.get("Failed", 1) if isinstance(result, dict) else 1
    return seams.Task(row["id"], row["label"] or "", row["group"], state, row["command"], rc, stamp(at))


def jobs(state):
    return sorted((job(row) for row in state["tasks"].values()), key=lambda j: j.id)


class Pueue(seams.Tasks):
    def __init__(self, inv, machine):
        home = inv.wk_home_on(machine)
        self.m, self.clock = machine, inv.clock
        self.bin, self.dir = "%s/cache/pueue-%s" % (home, VERSION), home + "/pueue"
        self.config = self.dir + "/pueue.yml"

    def argv(self, *words):
        return [self.bin + "/pueue", "--config", self.config] + list(words)

    def state(self):
        r = self.m.run(self.argv("status", "--json"))
        if not r.ok:
            die("pueue does not answer on %s: %s\n    `wk machine setup %s` installs and starts it"
                % (self.m.name, r.err.strip(), self.m.name))
        return json.loads(r.out)

    def _act(self, *words):
        r = self.m.act(self.argv(*words))
        if not r.ok:
            die("pueue %s failed on %s: %s\n    `wk doctor` says whether its daemon answers" % (words[0], self.m.name, r.err.strip()))
        return r.out

    def limit(self, group, parallel):
        """Make `group` run `parallel` tasks at once; True when that changed anything. A daemon that does not answer
        has no group yet: under --dry-run that is the daemon setup has still to start."""
        r = self.m.run(self.argv("status", "--json"))
        have = json.loads(r.out)["groups"].get(group) if r.ok else None
        if have is None:
            self._act("group", "add", group, "--parallel", str(parallel))
        elif have["parallel_tasks"] != parallel:
            self._act("parallel", str(parallel), "--group", group)
        return have is None or have["parallel_tasks"] != parallel

    def add(self, argv, label, group="default", cwd=None):
        if group in LIMITS:
            self.limit(group, LIMITS[group])
        out = self._act("add", "--print-task-id", "--group", group, "--label", label, "--working-directory", cwd or self.m.home,
                        "--", shlex.join(argv))
        return int(out) if out else None

    def status(self):
        return jobs(self.state())

    def _one(self, id):
        return [j for j in self.status() if j.id == id][0]

    def log(self, id):
        r = self.m.run(self.argv("log", str(id), "--json", "--full"))
        if not r.ok:
            die("pueue has no log of task %d on %s: %s" % (id, self.m.name, r.err.strip()))
        return json.loads(r.out)[str(id)]["output"]

    def kill(self, id):
        task = self._one(id)
        if task.state == seams.QUEUED:
            self._act("remove", str(id))    # pueue kills only what runs; a queued task is taken out of the queue
        elif not task.ended:
            self._act("kill", str(id))

    def wait(self, id, timeout=None):
        self.clock.wait_until(lambda: self._one(id).ended, timeout, interval=2)
        return self._one(id)

    def follow(self, id):
        r = self.m.run(self.argv("follow", str(id)), stream=True)
        sys.stdout.write(r.out)

    def quiet(self):
        r = self.m.run(["python3", "-I", "-c", MTIMES, self.dir + "/task_logs"])
        if not r.ok:
            die("cannot read when the task logs on %s last grew: %s" % (self.m.name, r.err.strip()))
        now = self.clock.now()
        return {int(id): now - float(mtime) for id, mtime in (line.split() for line in r.out.splitlines())}
