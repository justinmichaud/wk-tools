"""A command is a module in lib/wk/cmd/ holding a table and one function:

    \"\"\"One line saying what it does.

    The rest of the docstring is what `wk <cmd> -h` prints.\"\"\"
    from wk.core.decl import SERVERS, Command, Opt

    COMMAND = Command(on=SERVERS, takes=("ws", "preset?"), opts=(Opt("--detach", "return at once"),),
                      tail=True, destructive=False)

    def main(args, inv):        # args.ws, args.preset (None when left out), args.detach, args.tail
        ...                     # returns an exit code, or None for 0

The module's name is the command: build.py is `wk build`, and key_set.py is the verb `wk key set` (so a command's
name has no underscore). One verb of a group may say `default=True`: it runs when the group is typed bare.

on       the kinds of machine it runs on; typed on another kind it is refused, naming where it runs. None: anywhere,
         a machine with no conf too.
takes    positionals in order; a trailing `?` makes one optional. The one named `ws` is the workspace.
opts     Opt(flag, help) is a boolean, Opt(flag, help, "name") takes a value; args.<flag without dashes>.
tail     the words after `--` belong to another program: args.tail.
readonly      it changes nothing; an `act` inside it is a Bug.
destructive   it removes, overwrites or revokes; it calls `confirm` before its first `act`, or that is a Bug."""

import dataclasses
import types

from wk.core.errors import Usage


@dataclasses.dataclass(frozen=True)
class Opt:
    flag: str
    help: str
    value: str = ""

    @property
    def dest(self):
        return self.flag.lstrip("-").replace("-", "_")


SERVERS = ("linux", "mac")
CONTROLLER = ("controller",)
WORKSTATION = ("workstation",)


@dataclasses.dataclass(frozen=True)
class Command:
    on: tuple = None
    takes: tuple = ()
    opts: tuple = ()
    tail: bool = False
    readonly: bool = False
    destructive: bool = False
    default: bool = False


def usage(words, cmd):
    parts = ["usage: wk"] + list(words)
    parts += ["[%s%s]" % (o.flag, o.value and " <%s>" % o.value) for o in cmd.opts]
    parts += ["[<%s>]" % t[:-1] if t.endswith("?") else "<%s>" % t for t in cmd.takes]
    return " ".join(parts + (["[-- <args>]"] if cmd.tail else []))


def parse(cmd, words, tail):
    """The command's own words (wk's global flags already taken out) and the tail after `--`, None when there is no `--`."""
    opts = {o.flag: o for o in cmd.opts}
    args = types.SimpleNamespace(**{o.dest: None if o.value else False for o in cmd.opts})
    given, words = [], list(words)
    while words:
        word = words.pop(0)
        if not word.startswith("-"):
            given.append(word)
        elif word not in opts:
            raise Usage("unknown option: %s" % word)
        elif not opts[word].value:
            setattr(args, opts[word].dest, True)
        elif not words:
            raise Usage("%s needs a value: %s <%s>" % (word, word, opts[word].value))
        else:
            setattr(args, opts[word].dest, words.pop(0))
    if len(given) > len(cmd.takes):
        raise Usage("unexpected argument: %s" % given[len(cmd.takes)])
    for i, take in enumerate(cmd.takes):
        if i >= len(given) and not take.endswith("?"):
            raise Usage("missing <%s>" % take)
        setattr(args, take.rstrip("?"), given[i] if i < len(given) else None)
    if tail is not None and not cmd.tail:
        raise Usage("nothing follows `--` here")
    if cmd.tail:
        args.tail = tail or []
    return args
