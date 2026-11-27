"""`inv`, the one object a command reaches the world through. The dispatcher builds it; a test's World builds it of fakes.

    inv.here                       this machine's Machine
    inv.machine("name")            another machine's
    inv.fleet                      wk.core.fleet.Fleet
    inv.clock                      wk.core.clock.Clock
    inv.driver(kind, machine)      a wk.core.seams.Driver        (wk.workspace.driver(inv, kind, machine))
    inv.tasks(machine)             a wk.core.seams.Tasks         (wk.tasks.tasks(inv, machine))
    inv.secrets()                  a wk.core.seams.Secrets       (wk.creds.secrets(inv))
    inv.wk_home_on(machine)        its wk home: <its login's home>/wk, or $WK_HOME on this machine (inv.wk_home) alone
    inv.env  inv.stdin  inv.root   the environment, the terminal, the checkout wk runs from
    inv.commands                   {("key", "set"): "wk.cmd.key_set"}"""

import importlib


def lane(package, name):
    """The factory a lane's package exports, imported when it is first asked for."""
    return lambda *args: getattr(importlib.import_module(package), name)(*args)


SEAMS = {"driver": lane("wk.workspace", "driver"), "tasks": lane("wk.tasks", "tasks"), "secrets": lane("wk.creds", "secrets")}


class Inv:
    def __init__(self, root, env, fleet, clock, stdin, commands, seams=SEAMS):
        self.root, self.env, self.fleet, self.clock, self.stdin, self.commands = root, env, fleet, clock, stdin, commands
        self.seams = seams

    @property
    def here(self):
        return self.fleet.local

    def machine(self, name):
        return self.fleet.machine(name)

    def wk_home_on(self, machine):
        return machine is self.here and self.env.get("WK_HOME") or machine.home + "/wk"

    @property
    def wk_home(self):
        return self.wk_home_on(self.here)

    def driver(self, kind, machine):
        return self.seams["driver"](self, kind, machine)

    def tasks(self, machine):
        return self.seams["tasks"](self, machine)

    def secrets(self):
        return self.seams["secrets"](self)
