"""The dispatcher: the one place argv is parsed, usage is printed, global flags are read and exit codes are decided."""

import importlib
import os
import pkgutil
import socket
import sys

from wk.core import act, decl, route
from wk.core.clock import Clock
from wk.core.conf import load
from wk.core.errors import Refused, Usage
from wk.core.fleet import Fleet
from wk.core.inv import Inv
from wk.core.machine import Local, Ssh

GLOBALS = {"--dry-run": "dry_run", "-n": "dry_run", "--yes": "yes", "-y": "yes", "--force": "force", "--quiet": "quiet"}
GLOBAL_HELP = """global flags:
  --dry-run, -n   print each state change and make none
  --yes, -y       answer yes to the one question a destructive command asks
  --force         cross a refusal that exists because of a rule
  --quiet         print warnings and errors only
  -h, --help      this text"""
HELP = ("-h", "--help")


def discover(package):
    """{("key", "set"): "wk.cmd.key_set"} for every module of the package."""
    return {tuple(m.name.split("_")): "%s.%s" % (package.__name__, m.name) for m in pkgutil.iter_modules(package.__path__)}


def resolve(commands, words):
    """(the command's key, the words after it)."""
    for n in (2, 1):
        if tuple(words[:n]) in commands:
            return tuple(words[:n]), words[n:]
    verbs = sorted(key for key in commands if len(key) == 2 and key[0] == words[0])
    if not verbs:
        raise Usage("unknown command: %s" % words[0])
    if len(words) == 1 or words[1].startswith("-"):
        for key in verbs:
            if importlib.import_module(commands[key]).COMMAND.default:
                return key, words[1:]
    raise Usage("wk %s needs one of: %s" % (words[0], ", ".join(key[1] for key in verbs)))


def help_text(key, module):
    cmd = module.COMMAND
    width = max([len(o.flag) for o in cmd.opts] or [0])
    options = ["options:"] + ["  %-*s  %s" % (width, o.flag, o.help) for o in cmd.opts] + [""] if cmd.opts else []
    return "\n".join([decl.usage(key, cmd), "", module.__doc__.strip(), ""] + options + [GLOBAL_HELP])


def target(args, inv):
    if getattr(args, "on", None):
        return inv.fleet.server(args.on).name
    if getattr(args, "ws", None):
        return route.locate(inv, args.ws).server
    raise Usage("names no workspace, so no server to run on")


def main(argv, inv):
    """Run one command line; returns its exit code."""
    flags = act.Flags(stdin=inv.stdin, **{flag: bool(inv.env.get(var)) for flag, var in route.ENV.items()})
    act.begin(flags)
    usage = "usage: wk <command> [<args>]    (`wk help` lists the commands)"
    try:
        cut = argv.index("--") if "--" in argv else len(argv)
        for word in argv[:cut]:
            if word in GLOBALS:
                setattr(flags, GLOBALS[word], True)
        words = [w for w in argv[:cut] if w not in GLOBALS and w not in HELP]
        key, rest = resolve(inv.commands, words or ["help"])
        module = importlib.import_module(inv.commands[key])
        cmd = module.COMMAND
        usage = decl.usage(key, cmd)
        if any(w in HELP for w in argv[:cut]):
            print(help_text(key, module))
            return 0
        args = decl.parse(cmd, rest, argv[cut + 1:] if cut < len(argv) else None)
        flags.readonly, flags.destructive = cmd.readonly, cmd.destructive
        inv.words = list(key) + rest + argv[cut:]
        here = inv.fleet.here.kind if cmd.where != "any" else ""
        if cmd.where == "client" and here != "client":
            raise Refused("wk %s runs on the client, and this is the server %s: type it there"
                          % (" ".join(key), inv.fleet.hostname))
        if cmd.where == "server" and here == "client":
            return route.hand_over(inv, target(args, inv))
        return module.main(args, inv) or 0
    except Usage as e:
        sys.stderr.write("error: %s\n%s\n" % (e, usage))
        return 1
    except Refused as e:
        sys.stderr.write("error: %s\n" % e)
        return e.status
    except KeyboardInterrupt:
        return 130
    finally:
        for message in flags.forced:
            act.warn("this command was forced past: %s" % message)


def entry(root):
    import wk.cmd
    env = os.environ
    fleet = Fleet(lambda: load(env.get("WK_MACHINES_DIR") or os.path.join(root, "machines")),
                  socket.gethostname().split(".")[0].lower(), Ssh, Local())
    sys.exit(main(sys.argv[1:], Inv(root, env, fleet, Clock(), sys.stdin, discover(wk.cmd))))
