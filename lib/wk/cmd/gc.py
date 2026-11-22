"""Reclaim what loses no work, and name the flag for the rest.

Lists what each lane can reclaim with its size, asks once, and removes the rows that need no flag. A row that costs
something to take is printed with the flag that takes it. On the client: every server in turn. A server whose
listing failed is named, and the exit code is that failure's."""

from wk.core import hooks
from wk.core.act import confirm
from wk.core.decl import Command
from wk.core.errors import Refused


def __getattr__(name):
    # The flags are the lanes' own, so the table is made when it is read: hooks.gc_flags() -> [Opt].
    if name != "COMMAND":
        raise AttributeError(name)
    return Command(opts=tuple(hooks.collect("gc_flags")))


def main(args, inv):
    here = inv.fleet.here
    targets = inv.fleet.servers() if here.kind == "client" else [here]
    chosen, code = [], 0
    for conf in targets:
        try:
            rows = hooks.collect("gc", inv, conf)
        except Refused as e:
            print("%-8s not listed: %s" % (conf.name, str(e).split("\n")[0]))
            code = e.status
            continue
        for row in rows:
            taken = not row.flag or getattr(args, row.flag.lstrip("-").replace("-", "_"), False)
            print("%-8s %-28s %s%s" % (conf.name, row.name, row.detail, "" if taken else "    (%s takes it)" % row.flag))
            chosen += [row] if taken else []
    if not chosen:
        print("nothing to reclaim")
        return code
    confirm("reclaim the %d rows not marked with a flag?" % len(chosen))
    for row in chosen:
        row.reclaim()
    return code
