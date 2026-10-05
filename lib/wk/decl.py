"""A command's declaration: the `# wk:` lines in the leading comment block of cmd/<name> (keys in VALUED and
FLAGS) and the `# wk <name> ... -- <summary>` synopsis. `sub <verbs> [<second words>]` lines override per verb
or verb pair (opts live on verbs), `flag` lines per flag. An option declared both bare and with `=` (`--x,--x=`)
takes a value only as `--x=v`."""

import re
from pathlib import Path

from wk import repos

WHERE_VALUES = ("host", "store", "local", "workspace", "dynamic")
NAME_VALUES = ("required", "optional", "none", "derived")
PRESET_VALUES = ("--preset", "arg")
VALUED = {"where": WHERE_VALUES, "name": NAME_VALUES, "preset": PRESET_VALUES, "takes": (), "ready": (), "group": (),
          "values": (), "post": (), "verbs": (), "default": (), "repos": ()}
FLAGS = {"lifecycle": {"lifecycle": True}, "readonly": {"readonly": "yes"}, "destructive": {"destructive": "yes"},
         "broker": {"broker": "*"}, "needs": {}, "opts": {}, "passthrough": {"passthrough": "yes"},
         "passthrough=tail": {"passthrough": "tail"}, "passthrough=all": {"passthrough": "all"},
         "dryrun": {"dryrun": "yes"}, "nodryrun": {"nodryrun": True}, "forward=no": {"forward": False},
         "here": {"here": True, "forward": False}, "outside": {"outside": True}, "bare=merged": {"bare": "merged"}}
DEFAULTS = {"where": "workspace", "name_decl": "none", "ready": False, "group": "other", "lifecycle": False,
            "readonly": "", "broker": "", "forward": True, "bare": "", "post": "", "outside": False, "needs": "",
            "here": False, "takes": "0", "values": "", "preset": "", "verbs": "", "default": "", "destructive": "",
            "opts": "", "passthrough": "", "dryrun": "", "nodryrun": False, "synopsis": "", "repos": ""}
LIST_KEYS = ("needs", "opts", "readonly", "destructive", "dryrun", "broker")


class DeclError(Exception):
    pass


def leading_block(path):
    out = []
    with open(path, errors="replace") as f:
        for line in f:
            if not line.startswith("#"):
                break
            out.append(line)
    return out


def in_list(word, spec):
    if not spec:
        return False
    if spec == "*":
        return True
    return word in spec.split(",")


class Decl:
    def __init__(self, path):
        self.path = Path(path)
        self.name = self.path.name
        self.__dict__.update(DEFAULTS)
        self.sub = []    # (verbs, second words, {key: value})
        self.flag = []   # (flags, {key: value})
        self._load()

    def _load(self):
        head = leading_block(self.path)
        for line in head[:5]:
            if line.startswith("# wk "):
                self.synopsis = line[len("# wk "):].rstrip("\n")
                break
        for line in head:
            if not line.startswith("# wk:"):
                continue
            body = line[len("# wk:"):].strip()
            if body.startswith("sub "):
                words = body[4:].split()
                seconds = words.pop(1) if len(words) > 1 and "=" not in words[1] else ""
                verbs, spec = self._override(" ".join(words))
                self.sub.append((verbs, seconds, spec))
                continue
            if body.startswith("flag "):
                self.flag.append(self._override(body[5:]))
                continue
            self._tokens(body.split())
        self._check_verbs()

    def _check_verbs(self):
        """A command takes verbs or it does not: options and flags belong to one verb, never to the command."""
        if self.default and not in_list(self.default, self.verbs):
            raise DeclError("%s: default=%s is not one of verbs=%s" % (self.name, self.default, self.verbs))
        if not self.verbs:
            for verbs, _, _ in self.sub:
                raise DeclError("%s: 'sub %s' but the command declares no verbs=" % (self.name, verbs))
            return
        for verbs, _, _ in self.sub:
            for v in verbs.split(","):
                if not in_list(v, self.verbs):
                    raise DeclError("%s: 'sub %s' names no verb in verbs=%s" % (self.name, v, self.verbs))
        if self.opts:
            raise DeclError("%s: opts belong on the verbs that take them ('sub <verb> opts=...'), not on a command with verbs=" % self.name)
        if self.flag:
            raise DeclError("%s: a 'flag' line is no verb's: put it on a verb ('sub <verb> ...')" % self.name)

    def _override(self, text):
        words = text.split()
        spec = {}
        for tok in words[1:]:
            key, _, value = tok.partition("=")
            spec[key] = value
        return (words[0], spec)

    def _tokens(self, tokens):
        pending = ""
        for tok in tokens:
            key, eq, value = tok.partition("=")
            if eq and key in VALUED:
                pending = ""
                allowed = VALUED[key]
                if allowed and (value.split("@")[0] if key == "name" else value) not in allowed:
                    raise DeclError("%s: %s=%s is not one of %s" % (self.name, key, value, "|".join(allowed)))
                unknown = [r for r in value.split(",") if r not in repos.names()] if key == "repos" else []
                if unknown:
                    raise DeclError("%s: repos=%s names no repo in REPOS (%s)" % (self.name, value, " ".join(repos.names())))
                setattr(self, "name_decl" if key == "name" else key, value == "yes" if key == "ready" else value)
            elif tok in FLAGS:
                self.__dict__.update(FLAGS[tok])
                pending = tok if tok in LIST_KEYS else ""
            elif pending:
                setattr(self, pending, tok)
                pending = ""
            else:
                raise DeclError("%s: '%s' is not a declaration this dispatcher knows" % (self.name, tok))

    # -- per-invocation answers: a flag override wins, then the subverb's, then the command's

    def _flag_override(self, key, args):
        found = None
        for flags, spec in self.flag:
            for a in args:
                if in_list(a.split("=")[0], flags) and key in spec:
                    found = spec[key]
        return found

    def _sub_override(self, key, args):
        first = args[0] if args else ""
        second = next((a for a in args[1:] if not a.startswith("-")), "")
        found = None
        for verbs, seconds, spec in self.sub:
            if key in spec and in_list(first, verbs):
                if seconds and in_list(second, seconds):
                    return spec[key]
                if not seconds and found is None:
                    found = spec[key]
        return found

    def overrides(self):
        return [(" ".join(filter(None, (v, s))), spec) for v, s, spec in self.sub] + self.flag

    def _answer(self, key, default, args):
        v = self._flag_override(key, args)
        if v is None:
            v = self._sub_override(key, args)
        return default if v is None else v

    def name_for(self, args):
        return self._answer("name", self.name_decl, args)

    def takes_for(self, args):
        return self._answer("takes", self.takes, args)

    def opts_for(self, args):
        return self._answer("opts", self.opts, args)

    def where_for(self, args):
        return self._answer("where", self.where, args)

    def here_for(self, args):
        v = self._answer("here", None, args)
        return self.here if v is None else v == "yes"

    def forward_for(self, args):
        v = self._answer("here", None, args)
        return self.forward if v is None else v != "yes"

    def passthrough_for(self, args):
        v = self._sub_override("passthrough", args)
        return self.passthrough if v is None else v

    def needs_for(self, args):
        v = self._sub_override("needs", args)
        return self.needs if v is None else v

    def is_readonly(self, args=()):
        v = self._sub_override("readonly", args)
        if v is not None:
            return v == "yes"
        return self.readonly == "yes" or in_list(args[0] if args else "", self.readonly)

    def _in_argv_list(self, spec, args):
        if not spec:
            return False
        if spec == "yes":
            return True
        return any(in_list(a.split("=")[0], spec) for a in args)

    def is_destructive(self, args):
        return self._in_argv_list(self._answer("destructive", self.destructive, args), args)

    def honours_dryrun(self, args):
        return self._in_argv_list(self._answer("dryrun", self.dryrun, args), args)

    def valued_opts(self):
        specs = [self.opts] + [spec.get("opts") or "" for _, spec in self.overrides()]
        return {x[:-1] for spec in specs for x in spec.split(",") if x.endswith("=") and not in_list(x[:-1], spec)}

    def serves(self, repo):
        """Whether a workspace holding `repo` is one this command acts on: every repo unless `repos=` names some."""
        return not self.repos or in_list(repo, self.repos)

    def synopsis_line(self):
        return self.synopsis.split(" -- ")[0]

    def summary(self):
        return self.synopsis.split(" -- ", 1)[1] if " -- " in self.synopsis else ""

    def leading_comment(self):
        out = []
        for line in leading_block(self.path)[1:]:
            line = line.rstrip("\n")
            if line.startswith("# wk:") or (re.match(r"^# wk [a-z]", line) and not out):
                continue
            text = re.sub(r"^# ?", "", line)
            if text or out:
                out.append("  " + text)
        return "\n".join(out)


class Args:
    """The one reader of a command's options: argv as the dispatcher hands it on (`--x <value>`), by its declaration."""

    def __init__(self, decl, argv):
        opts = decl.opts_for(argv)
        self._values, self._flags = {}, set()
        self.positionals, self.tail, self.order = [], [], []
        i = 0
        while i < len(argv):
            a = argv[i]
            i += 1
            if a == "--":
                self.tail = argv[i:]
                break
            key, eq, given = a.partition("=")
            if eq and in_list(key + "=", opts):
                self._values.setdefault(key, []).append(given)
                self.order.append(key)
            elif in_list(a + "=", opts) and not in_list(a, opts):
                self._values.setdefault(a, []).append(argv[i])
                self.order.append(a)
                i += 1
            elif in_list(a, opts):
                self._flags.add(a)
                self.order.append(a)
            else:
                self.positionals.append(a)

    def flag(self, opt):
        return opt in self._flags

    def value(self, opt):
        given = self._values.get(opt)
        return given[-1] if given else None

    def values(self, opt):
        return list(self._values.get(opt, ()))


def name_slot(decl_name):
    """Which positional is the name; 0 when none is."""
    base = decl_name.split("@")[0]
    if base in ("none", "derived"):
        return 0
    if "@" in decl_name:
        return int(decl_name.split("@")[1])
    return 1


def all_commands(root):
    for path in sorted(Path(root, "cmd").iterdir()):
        if path.is_file() and path.stat().st_mode & 0o111:
            yield Decl(path)
