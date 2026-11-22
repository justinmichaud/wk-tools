"""What `wk doctor` and `wk gc` collect: any package wk.<lane> may hold a module `hooks` defining

    def doctor(inv, conf, ws): ...    # -> [DoctorRow]  ws is a Workspace, or None for the machine itself
    def gc(inv, conf): ...            # -> [GcRow]
    def gc_flags(): ...               # -> [Opt]        the flags of `wk gc` that take this lane's flagged rows
    def pins(inv, conf): ...          # -> [str]        the names in <wk_home>/cache this lane uses; `wk gc` reclaims the rest

`conf` is the machine's wk.core.conf.Conf; its Machine is inv.machine(conf.name). A hook lets Unreachable propagate:
the command that collects turns it into that machine's one row. `collect` finds the hooks; no list of lanes exists
anywhere."""

import dataclasses
import importlib
import importlib.util
import pkgutil
from typing import Callable

OK, MISSING, UNKNOWN = "ok", "missing", "unknown"
LISTED = {"d755": "drwxr-xr-x", "d700": "drwx------", "600": "-rw-------", "644": "-rw-r--r--"}


@dataclasses.dataclass(frozen=True)
class DoctorRow:
    """One thing a machine or workspace needs. `status` is ok | missing | unknown; unknown is a reading that could
    not be taken and is never reported as ok. `remedy` is the command that fixes a row that is not ok."""
    name: str
    status: str
    detail: str = ""
    remedy: str = ""


@dataclasses.dataclass(frozen=True)
class GcRow:
    """One thing `wk gc` can reclaim. `reclaim()` removes it. `flag` is empty when removing it loses no work, so a
    plain run takes it; otherwise it names the flag that does, and a plain run only prints the row."""
    name: str
    detail: str
    reclaim: Callable
    flag: str = ""


def collect(hook, *args, package="wk"):
    """Everything `hook` yields across the lanes, in lane-name order."""
    rows = []
    for lane in sorted(m.name for m in pkgutil.iter_modules(importlib.import_module(package).__path__) if m.ispkg):
        name = "%s.%s.hooks" % (package, lane)
        if importlib.util.find_spec(name):
            rows += getattr(importlib.import_module(name), hook, lambda *a: [])(*args)
    return rows


def lacking(m, want):
    """The paths of `want`, {path: a key of LISTED}, that `ls -ld` on `m` shows absent or moded otherwise."""
    lines = m.run(["ls", "-ld"] + list(want)).out.splitlines()
    return [path for path, mode in want.items() if not any(line.startswith(LISTED[mode]) and line.endswith(" " + path) for line in lines)]
