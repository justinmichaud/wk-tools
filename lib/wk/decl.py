"""A command's declaration: the `# wk:` lines in the leading comment block of cmd/<name> and the
`# wk <name> ... -- <summary>` synopsis. Keys: where=, name= (with @N for the slot), takes=,
ready=yes, group=, lifecycle, readonly, destructive, dryrun, nodryrun, opts, passthrough[=tail|=all], broker,
outside, forward=no, here, bare=merged, post=, values=, config=, verbs=, default=, needs;
`sub` lines override per verb (a command with verbs= keeps its opts on them), `flag` lines per flag of a command without.
An option declared both bare and with `=` (`--x,--x=`) takes a value only as `--x=v`."""

import re
from pathlib import Path

WHERE_VALUES = ("host", "store", "local", "workspace", "dynamic")
NAME_VALUES = ("required", "optional", "none", "derived")
FLAGS = ("lifecycle", "readonly", "destructive", "broker", "needs", "opts",
         "passthrough", "dryrun", "nodryrun", "passthrough=tail", "passthrough=all", "forward=no", "here",
         "outside", "bare=merged")
CONFIG_VALUES = ("--config", "arg")
LIST_KEYS = ("needs", "opts", "readonly", "destructive", "dryrun", "broker")


class DeclError(Exception):
    pass


def leading_block(path):
    """The lines of `path` up to its first that is not a comment: where its declaration and its help live."""
    out = []
    with open(path, errors="replace") as f:
        for line in f:
            if not line.startswith("#"):
                break
            out.append(line)
    return out


def in_list(word, spec):
    """`word` is in the comma list `spec`; `*` is everything, empty is nothing."""
    if not spec:
        return False
    if spec == "*":
        return True
    return word in spec.split(",")


class Decl:
    def __init__(self, path):
        self.path = Path(path)
        self.name = self.path.name
        self.where = "workspace"
        self.name_decl = "none"
        self.ready = False
        self.group = "other"
        self.lifecycle = False
        self.readonly = ""
        self.broker = ""
        self.forward = True
        self.bare = ""
        self.post = ""
        self.outside = False
        self.needs = ""
        self.here = False
        self.takes = "0"
        self.values = ""
        self.config = ""
        self.verbs = ""
        self.default = ""
        self.destructive = ""
        self.opts = ""
        self.passthrough = ""
        self.dryrun = ""
        self.nodryrun = False
        self.sub = []    # (verbs, {key: value})
        self.flag = []   # (flags, {key: value})
        self.synopsis = ""
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
                self.sub.append(self._override(body[4:]))
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
            for verbs, _ in self.sub:
                raise DeclError("%s: 'sub %s' but the command declares no verbs=" % (self.name, verbs))
            return
        for verbs, _ in self.sub:
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
            if key in ("where", "name", "takes", "ready", "group", "values", "post", "config", "verbs", "default") and eq:
                pending = ""
                if key == "where":
                    if value not in WHERE_VALUES:
                        raise DeclError("%s: where=%s is not one of %s"
                                        % (self.name, value, "|".join(WHERE_VALUES)))
                    self.where = value
                elif key == "name":
                    if value.split("@")[0] not in NAME_VALUES:
                        raise DeclError("%s: name=%s is not one of %s"
                                        % (self.name, value, "|".join(NAME_VALUES)))
                    self.name_decl = value
                elif key == "takes":
                    self.takes = value
                elif key == "ready":
                    self.ready = value == "yes"
                elif key == "group":
                    self.group = value
                elif key == "values":
                    self.values = value
                elif key == "post":
                    self.post = value
                elif key == "config":
                    if value not in CONFIG_VALUES:
                        raise DeclError("%s: config=%s is not one of %s"
                                        % (self.name, value, "|".join(CONFIG_VALUES)))
                    self.config = value
                elif key == "verbs":
                    self.verbs = value
                elif key == "default":
                    self.default = value
            elif tok in FLAGS:
                pending = ""
                if tok == "lifecycle":
                    self.lifecycle = True
                elif tok == "readonly":
                    self.readonly = "yes"
                    pending = "readonly"
                elif tok == "destructive":
                    self.destructive = "yes"
                    pending = "destructive"
                elif tok == "broker":
                    self.broker = "*"
                    pending = "broker"
                elif tok == "needs":
                    pending = "needs"
                elif tok == "opts":
                    pending = "opts"
                elif tok == "passthrough":
                    self.passthrough = "yes"
                elif tok == "nodryrun":
                    self.nodryrun = True
                elif tok == "dryrun":
                    self.dryrun = "yes"
                    pending = "dryrun"
                elif tok in ("passthrough=tail", "passthrough=all"):
                    self.passthrough = tok.split("=")[1]
                elif tok == "forward=no":
                    self.forward = False
                elif tok == "here":
                    self.here = True
                    self.forward = False
                elif tok == "outside":
                    self.outside = True
                elif tok == "bare=merged":
                    self.bare = "merged"
            elif pending in LIST_KEYS:
                setattr(self, pending, tok)
                pending = ""
            else:
                raise DeclError("%s: '%s' is not a declaration this dispatcher knows"
                                % (self.name, tok))

    # -- per-invocation answers: a flag override wins, then the subverb's, then the command's

    def _flag_override(self, key, args):
        found = None
        for flags, spec in self.flag:
            for a in args:
                if in_list(a.split("=")[0], flags) and key in spec:
                    found = spec[key]
        return found

    def _sub_override(self, key, sub):
        for verbs, spec in self.sub:
            if in_list(sub, verbs) and key in spec:
                return spec[key]
        return None

    def _answer(self, key, default, args):
        v = self._flag_override(key, args)
        if v is None:
            v = self._sub_override(key, args[0] if args else "")
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
        v = self._sub_override("passthrough", args[0] if args else "")
        return self.passthrough if v is None else v

    def needs_for(self, args):
        v = self._sub_override("needs", args[0] if args else "")
        return self.needs if v is None else v

    def is_readonly(self, sub=""):
        if not self.readonly:
            return False
        if self.readonly == "yes":
            return True
        return in_list(sub, self.readonly)

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
        specs = [self.opts] + [spec.get("opts") or "" for _, spec in self.sub + self.flag]
        return {x[:-1] for spec in specs for x in spec.split(",") if x.endswith("=") and not in_list(x[:-1], spec)}

    def synopsis_line(self):
        return self.synopsis.split(" -- ")[0]

    def summary(self):
        return self.synopsis.split(" -- ", 1)[1] if " -- " in self.synopsis else ""

    def leading_comment(self):
        """The comment block after the header, what `wk <cmd> -h` prints."""
        out = []
        seen = False
        with open(self.path, errors="replace") as f:
            lines = f.read().splitlines()
        for line in lines[1:]:
            if line.startswith("# wk:"):
                continue
            if re.match(r"^# wk [a-z]", line) and not seen:
                continue
            if line.startswith("#"):
                text = re.sub(r"^# ?", "", line)
                if text == "" and not seen:
                    continue
                seen = True
                out.append("  " + text)
                continue
            break
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
