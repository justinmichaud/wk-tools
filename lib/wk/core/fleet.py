"""The machines `wk` knows and which one this is.

    inv.fleet.here                    # this machine's Conf; refused when it has no conf
    inv.fleet.servers()               # the Confs that hold workspaces
    inv.fleet.controller              # the controller's Conf
    inv.fleet.machine("name")         # its Machine: this host itself, or ssh

What the fleet as a whole holds, its workspace names, the controller's API answers (wk.api.client)."""

import functools

from wk.core.conf import THE
from wk.core.errors import Refused


class Fleet:
    def __init__(self, load, hostname, connect, local):
        """`load()` returns {name: Conf}; `connect(name)` makes a remote Machine; `local` is this host's."""
        self._load, self.hostname, self.connect, self.local = load, hostname, connect, local

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
