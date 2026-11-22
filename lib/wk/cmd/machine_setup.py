"""Converge a server: wk itself, then what every lane needs on it, stage by stage.

Each stage is idempotent and prints `changed` or `unchanged`; a second run changes nothing. The server gets exactly
this checkout's commit, so uncommitted changes are refused. A stage that fails stops the run; killed anywhere and
typed again, setup converges. Nothing is installed with a package manager: what a server must already have is named."""

from wk.core import hooks
from wk.core.act import die
from wk.core.decl import Command, Opt
from wk.core.errors import Refused

COMMAND = Command(where="client", takes=("server",), opts=(Opt("--stage", "run this stage alone", "name"),))


def main(args, inv):
    conf = inv.fleet.get(args.server)
    if conf.kind == "client":
        die("%s is the client, and setup converges a server: `wk doctor` reports what the client needs" % conf.name)
    stages = hooks.collect("stages", inv, conf)
    if args.stage:
        names = ", ".join(s.name for s in stages)
        stages = [s for s in stages if s.name == args.stage]
        if not stages:
            die("no stage named %s: the stages are %s" % (args.stage, names))
    changed, width = 0, max(len(s.name) for s in stages)
    for stage in stages:
        try:
            did = stage.run()
        except Refused as e:
            raise Refused("stage %s failed on %s: %s\n    fix that and re-run it alone: wk machine setup %s --stage %s\n"
                          "    then the rest: wk machine setup %s" % (stage.name, conf.name, e, conf.name, stage.name, conf.name), e.status)
        print("%-*s  %s" % (width, stage.name, "changed" if did else "unchanged"))
        changed += bool(did)
    print("%s: %s" % (conf.name, "%d changed" % changed if changed else "no changes"))
