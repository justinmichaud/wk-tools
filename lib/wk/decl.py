"""A command's table: cmd/<name> is a module whose COMMAND is a Command and whose main(args, inv) is the command.

A verb in `subs` and an option's `sets` are dicts of the Command fields they change. `parse` is the one argv parser:
argparse built from the table, after the verbs are walked and each given option's `sets` applied."""

import argparse
import dataclasses
import importlib.machinery
import importlib.util
import re
from pathlib import Path

GLOBALS = {"--force": "WK_FORCE", "--quiet": "WK_QUIET", "--dry-run": "WK_DRY_RUN", "-n": "WK_DRY_RUN",
           "--yes": "WK_YES", "-y": "WK_YES"}
HELP = ("-h", "--help", "--explain")
TAILS = {"": [], "--": ["[-- <args>]"], "rest": ["[<command>...]"], "all": ["[<args>...]"]}


@dataclasses.dataclass(frozen=True)
class Opt:
    flag: str
    help: str
    value: str = ""
    optional: bool = False
    sets: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class Command:
    where: object = "workspace"
    name: object = "none"
    takes: tuple = ()
    opts: tuple = ()
    subs: dict = dataclasses.field(default_factory=dict)
    default: str = ""
    tail: str = ""
    group: str = "other"
    readonly: bool = False
    destructive: bool = False
    dryrun: bool = True
    ready: bool = False
    lifecycle: bool = False
    here: bool = False
    forward: bool = True
    session: bool = False
    outside: bool = False
    broker: bool = False
    needs: tuple = ()
    preset: str = ""
    values: str = ""
    repos: tuple = ()

    def named(self):
        return self.name in ("required", "optional")

    def takes_a_word(self):
        return self.named() or bool(self.takes)


class Usage(Exception):
    line = ""   # the usage of the verb it reached


def load(root, name):
    path = Path(root, "cmd", name)
    if not path.is_file():
        return None
    loader = importlib.machinery.SourceFileLoader("wk_cmd_" + name, str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader, origin=str(path)))
    loader.exec_module(module)
    return module


def commands(root):
    return [(p.name, load(root, p.name)) for p in sorted(Path(root, "cmd").iterdir()) if p.is_file()]


def summary(module):
    return (module.__doc__ or "").strip().split("\n")[0]


def body(module):
    return (module.__doc__ or "").strip().partition("\n")[2].strip("\n")


def under(table, fields):
    return dataclasses.replace(table, **{"subs": {}, "default": "", **fields})


def walk(name, table, words):
    verbs = []
    while table.subs:
        pos = next((i for i, w in enumerate(words) if w == "--" or not w.startswith("-")), len(words))
        word = words[pos] if pos < len(words) and words[pos] != "--" else None
        if word in table.subs:
            stray = [w for w in words[:pos] if w not in GLOBALS and w not in HELP]
            if stray:
                raise Usage("%s comes before the verb: a verb's options follow it (wk %s %s ...)" % (stray[0], name, word))
            verbs.append(word)
            words = words[:pos] + words[pos + 1:]
            table = under(table, table.subs[word])
        elif table.default and (word is None or under(table, table.subs[table.default]).takes_a_word()):
            verbs.append(table.default)
            table = under(table, table.subs[table.default])
        elif word is None:
            raise Usage("'wk %s' needs one of: %s" % (name, ", ".join(table.subs)))
        else:
            raise Usage("unknown verb: %s (one of %s)" % (word, ", ".join(table.subs)))
    return table, verbs, words


def split(table, words, count):
    """(wk's words, each valued option joined to its value; the other program's words; whether `--` parted them)."""
    valued = {o.flag for o in table.opts if o.value and not o.optional}
    bare = {o.flag for o in table.opts if o.optional}
    head, n, i = [], 0, 0
    while i < len(words):
        w = words[i]
        if table.tail in ("rest", "all") and n == count and (table.tail == "all" or not w.startswith("-")):
            return head, words[i:], False
        if w == "--":
            if not table.tail:
                raise Usage("'--' means nothing to this command")
            return head, words[i + 1:], True
        if w in valued and i + 1 < len(words):
            w, i = w + "=" + words[i + 1], i + 1
        elif w in bare:
            w += "="
        elif not w.startswith("-"):
            n += 1
        head.append(w)
        i += 1
    return head, [], False


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        m = re.match(r"argument (\S+): (expected one argument|ignored explicit argument)", message)
        if m:
            raise Usage("%s %s" % (m.group(1), "needs a value" if m.group(2).startswith("expected") else "takes no value"))
        raise Usage("" if message.startswith("the following arguments are required") else message)


class _Seen(argparse.Action):
    def __call__(self, parser, ns, value, option_string=None):
        bare = isinstance(value, list) or (self.nargs == "?" and not value)
        ns.seen = (ns.seen or []) + [(self.option_strings[0], None if bare else value)]


def parse(name, table, words, inside=False, name_known=False):
    """(the table this invocation makes, its Args, the words a far wk is handed). Raises Usage."""
    typed = list(words)
    table, verbs, words = walk(name, table, list(words))
    try:
        return _parse(name, table, verbs, words, typed, inside, name_known)
    except Usage as e:
        e.line = usage(name, table, verbs)
        raise


def _parse(name, table, verbs, words, typed, inside, name_known):
    given = {w.split("=")[0] for w in words[:words.index("--") if "--" in words else len(words)]}
    for o in table.opts:
        if o.flag in given and o.sets:
            table = dataclasses.replace(table, **o.sets)
    named = table.named() and not inside
    head, tail, dashed = split(table, words, int(named) + len(table.takes))
    p = _Parser(prog="wk " + name, add_help=False, allow_abbrev=False)
    for flag in list(GLOBALS) + list(HELP):
        p.add_argument(flag, action=_Seen, nargs=0, dest="seen")
    for o in table.opts:
        p.add_argument(o.flag, action=_Seen, dest="seen", nargs=("?" if o.optional else None) if o.value else 0)
    known = set(GLOBALS) | set(HELP) | {o.flag for o in table.opts}
    stray = next((w for w in head if w.startswith("-") and len(w) > 1 and w.split("=")[0] not in known), None)
    if stray:
        raise Usage("unknown option: %s" % stray)
    if named:
        p.add_argument("ws", nargs="?" if table.name == "optional" or name_known else None)
    for i, t in enumerate(table.takes):
        p.add_argument("p%d" % i, nargs={(False, False): None, (True, False): "?", (False, True): "+", (True, True): "*"}[
            (t.startswith("["), t.rstrip("]").endswith("..."))])
    ns, extra = p.parse_known_intermixed_args(head)
    if extra:
        raise Usage("unexpected argument: %s" % extra[0])
    end = len(typed) - len(tail) - dashed
    return table, Args(table, verbs, ns, tail), [w for w in typed[:end] if w not in GLOBALS] + typed[end:]


class Args:
    """The one reader of an invocation's arguments, as its table parsed them."""

    def __init__(self, table, verbs, ns, tail=()):
        seen = getattr(ns, "seen", None) or []
        self.table = table
        self.given = [(f, v) for f, v in seen if f not in GLOBALS and f not in HELP]
        self.globals = [f for f, _ in seen if f in GLOBALS]
        self.help = any(f in HELP for f, _ in seen)
        self.order = [f for f, _ in self.given]
        takes = [w for i in range(len(table.takes)) for w in _words(getattr(ns, "p%d" % i, None))]
        self.preset = self.value("--preset") if table.preset == "--preset" else None
        if table.preset == "arg" and takes:
            self.preset = takes.pop(0)
        self.name = getattr(ns, "ws", None)
        self.verbs = list(verbs)
        self.positionals = self.verbs + takes
        self.tail = list(tail)

    def flag(self, opt):
        return any(f == opt and v is None for f, v in self.given)

    def value(self, opt):
        given = self.values(opt)
        return given[-1] if given else None

    def values(self, opt):
        return [v for f, v in self.given if f == opt and v is not None]

    def options(self):
        return [w for f, v in self.given for w in ([f] if v is None else [f, v])]

    def argv(self):
        """The options as given, the positionals past the verbs, then `--` and the tail."""
        return self.options() + self.positionals[len(self.verbs):] + (["--"] + self.tail if self.tail else [])


def _words(value):
    return [] if value is None else value if isinstance(value, list) else [value]


def usage(name, table, verbs=()):
    words = ["wk", name, *verbs]
    if table.subs:
        tails = {usage("", under(table, f))[4:] for f in table.subs.values()}
        return " ".join(words + ["|".join(table.subs)] + ([tails.pop()] if len(tails) == 1 else ["..."])).rstrip()
    words += {"required": ["<workspace>"], "optional": ["[<workspace>]"]}.get(table.name, []) if isinstance(table.name, str) else []
    return " ".join(words + list(table.takes) + (["[options]"] if table.opts else []) + TAILS[table.tail])


def leaves(name, table, verbs=()):
    if not table.subs or not usage(name, table, verbs).endswith("..."):
        return [usage(name, table, verbs)]
    return [line for verb, fields in table.subs.items() for line in leaves(name, under(table, fields), (*verbs, verb))]


def variants(name, table, verbs=()):
    """(usage, table, options) for the command and each verb under it, then (flag usage, table) for each option's `sets`."""
    yield usage(name, table, verbs), table, table.opts
    for o in table.opts:
        if o.sets:
            yield "%s %s" % (" ".join(["wk", name, *verbs]), o.flag), dataclasses.replace(table, **o.sets), ()
    for verb, fields in table.subs.items():
        yield from variants(name, under(table, fields), (*verbs, verb))
