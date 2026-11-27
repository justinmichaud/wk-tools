"""The machines `wk` knows, which one this is, and the one way to ask every server something at once.

    inv.fleet.here                    # this machine's Conf; refused when it has no conf
    inv.fleet.servers()               # the Confs that hold workspaces
    inv.fleet.machine("name")         # its Machine: this host itself, or ssh
    found, unknown = inv.fleet.workspaces()     # every Workspace on every server that answered; the servers that did not
    answers = inv.fleet.ask(lambda m: m.exists(path))     # {name: answer, or UNKNOWN when unreachable}"""

import concurrent.futures
import functools
import json

from wk.core.conf import THE
from wk.core.errors import Refused, Unreachable
from wk.core.seams import Workspace


class Unknown:
    """What an unreachable server answers. It is never read as absent or empty."""

    def __repr__(self):
        return "UNKNOWN"


UNKNOWN = Unknown()


class Fleet:
    def __init__(self, load, hostname, connect, local, workers=16):
        """`load()` returns {name: Conf}; `connect(name)` makes a remote Machine; `local` is this host's."""
        self._load, self.hostname, self.connect, self.local, self.workers = load, hostname, connect, local, workers

    @functools.cached_property
    def confs(self):
        return self._load()

    def machines(self):
        return [self.confs[name] for name in sorted(self.confs)]

    def get(self, name):
        if name not in self.confs:
            raise Refused("no machine named %s: machines/ has %s" % (name, ", ".join(sorted(self.confs)) or "none"))
        return self.confs[name]

    @property
    def here(self):
        if self.hostname not in self.confs:
            raise Refused("this machine, %s, has no conf: add machines/%s.conf with its kind=" % (self.hostname, self.hostname))
        return self.confs[self.hostname]

    def servers(self):
        return [c for c in self.machines() if c.holds_workspaces]

    def server(self, name):
        conf = self.get(name)
        if not conf.holds_workspaces:
            raise Refused("%s holds no workspace: it is %s\n    servers: %s"
                          % (name, THE[conf.kind], ", ".join(c.name for c in self.servers())))
        return conf

    @property
    def controller(self):
        found = [c for c in self.machines() if c.kind == "controller"]
        if not found:
            raise Refused("no controller: set kind=controller in one machines/<name>.conf")
        return found[0]

    def machine(self, name):
        return self.local if self.get(name).name == self.hostname else self.connect(name)

    def ask(self, fn, names=None):
        """`fn(machine)` on every server (or `names`) in parallel: {name: its answer, or UNKNOWN if unreachable}."""
        names = [c.name for c in self.servers()] if names is None else list(names)

        def one(name):
            try:
                return fn(self.machine(name))
            except Unreachable:
                return UNKNOWN
        with concurrent.futures.ThreadPoolExecutor(self.workers) as pool:
            return dict(zip(names, pool.map(one, names)))

    def workspaces(self, names=None):
        """(every Workspace on every server, or on `names`, that answered; the names of the servers that did not).
        A server answers `wk ls --json` with one JSON object, {"workspaces": [{"name", "driver", "state", "path"}]},
        listing its own alone; anything else it prints makes that server unknown."""
        def listing(machine):
            try:
                rows = json.loads(machine.run(["wk", "ls", "--json"], timeout=30).out)["workspaces"]
            except ValueError:
                return UNKNOWN
            return [Workspace(server=machine.name, **row) for row in rows]
        answers = self.ask(listing, names)
        return ([ws for found in answers.values() if found is not UNKNOWN for ws in found],
                [name for name, found in answers.items() if found is UNKNOWN])
