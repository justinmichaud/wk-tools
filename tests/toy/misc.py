"""End in each of the ways a command can."""
from wk.core.act import barrier, die, info, warn
from wk.core.decl import Command, Opt

COMMAND = Command(opts=(Opt("--die", "refuse with this status", "status"), Opt("--interrupt", "be interrupted"),
                        Opt("--barrier", "meet a barrier"), Opt("--retry", "meet one that lifts"), Opt("--code", "return it", "n")))


def main(args, inv):
    info("starting")
    warn("careful")
    if args.die:
        die("it is busy: wait for it", int(args.die))
    if args.interrupt:
        raise KeyboardInterrupt
    if args.barrier or args.retry:
        barrier("the rule says no", 75 if args.retry else 1)
    return int(args.code) if args.code else None
