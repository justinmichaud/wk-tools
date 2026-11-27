"""Reclaim what loses no work, and name the flag for the rest.

Lists what each lane can reclaim with its size, asks once, and removes the rows that need no flag. A row that costs
something to take is printed with the flag that takes it. It reclaims on the machine it is typed on."""

from wk.core import hooks
from wk.core.act import confirm
from wk.core.decl import CONTROLLER, SERVERS, Command


def __getattr__(name):
    # The flags are the lanes' own, so the table is made when it is read: hooks.gc_flags() -> [Opt].
    if name != "COMMAND":
        raise AttributeError(name)
    return Command(on=CONTROLLER + SERVERS, opts=tuple(hooks.collect("gc_flags")))


def main(args, inv):
    here, chosen = inv.fleet.here, []
    for row in hooks.collect("gc", inv, here):
        taken = not row.flag or getattr(args, row.flag.lstrip("-").replace("-", "_"), False)
        print("%-28s %s%s" % (row.name, row.detail, "" if taken else "    (%s takes it)" % row.flag))
        chosen += [row] if taken else []
    if not chosen:
        print("nothing to reclaim")
        return
    confirm("reclaim the %d rows on %s not marked with a flag?" % (len(chosen), here.name))
    for row in chosen:
        row.reclaim()
