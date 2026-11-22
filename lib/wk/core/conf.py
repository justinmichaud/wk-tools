"""machines/<name>.conf: one machine, as `key=value` lines of one literal word each.

    kind=linux            client | linux | mac | borrowed      (required)
    role=credential       the credential server                (optional)

The file is named for the machine's tailnet name, which is also its hostname. Any other lowercase key is a literal
a lane reads with `conf.get("key")`."""

import dataclasses
import os
import re

from wk.core.errors import Refused

KINDS = ("client", "linux", "mac", "borrowed")
DRIVERS = {"linux": "container", "mac": "plain", "borrowed": "plain"}
NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
LINE = re.compile(r"([a-z][a-z0-9_]*)=(.*)")
WORD = re.compile(r"[A-Za-z0-9_.,:/@+=-]+")


class ConfError(Refused):
    pass


@dataclasses.dataclass(frozen=True)
class Conf:
    name: str
    kind: str
    role: str = ""
    values: dict = dataclasses.field(default_factory=dict)

    def get(self, key, default=None):
        return self.values.get(key, default)

    @property
    def holds_workspaces(self):
        return self.kind != "client" and not self.role

    @property
    def driver(self):
        return DRIVERS[self.kind]


def parse(name, text, path="<conf>"):
    values = {}
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        at = "%s:%d:" % (path, n)
        m = LINE.fullmatch(line)
        if not m:
            raise ConfError("%s not a key=value line with a lowercase key: %s" % (at, line))
        key, value = m.groups()
        if key in values:
            raise ConfError("%s %s is set twice: delete one line" % (at, key))
        if not WORD.fullmatch(value):
            raise ConfError("%s %s is not one literal word: no spaces, quotes, $ or backticks (a default belongs in code)" % (at, key))
        values[key] = value
    kind, role = values.pop("kind", None), values.pop("role", "")
    if kind not in KINDS:
        raise ConfError("%s: kind=%s is not a kind: set kind= to one of %s" % (path, kind, ", ".join(KINDS)))
    if role not in ("", "credential"):
        raise ConfError("%s: role=%s is not a role: the only role is credential" % (path, role))
    return Conf(name, kind, role, values)


def load(directory):
    """Every machine in `directory`, by name."""
    if not os.path.isdir(directory):
        raise ConfError("no machines directory at %s: create it with one <name>.conf per machine" % directory)
    confs = {}
    for entry in sorted(os.listdir(directory)):
        name, ext = os.path.splitext(entry)
        if ext != ".conf":
            continue
        path = os.path.join(directory, entry)
        if not NAME.fullmatch(name):
            raise ConfError("%s: a machine is named for its tailnet name: lowercase letters, digits and hyphens" % path)
        with open(path) as f:
            confs[name] = parse(name, f.read(), path)
    for what, some in (("kind=client", [c.name for c in confs.values() if c.kind == "client"]),
                       ("role=credential", [c.name for c in confs.values() if c.role])):
        if len(some) > 1:
            raise ConfError("%s: %s say %s and only one machine may" % (directory, " and ".join(some), what))
    return confs
