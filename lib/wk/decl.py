"""A command's declaration: the `# wk:` lines in the first 15 lines of
cmd/<name> and the `# wk <name> ... -- <summary>` synopsis. Keys: where=,
name= (with @N for the slot), takes=, ready=yes, group=, lifecycle, readonly,
destructive, dryrun, opts, passthrough[=tail], broker, outside, forward=no,
here, bare=merged, post=, values=, needs; `sub`/`flag` lines override per subverb or flag."""

import re
from pathlib import Path

WHERE_VALUES = ("host", "store", "local", "workspace", "dynamic")
NAME_VALUES = ("required", "optional", "none", "derived")
FLAGS = ("lifecycle", "readonly", "destructive", "broker", "needs", "opts",
         "passthrough", "dryrun", "passthrough=tail", "forward=no", "here",
         "outside", "bare=merged")
LIST_KEYS = ("needs", "opts", "readonly", "destructive", "dryrun", "broker")


class DeclError(Exception):
    pass


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
        self.destructive = ""
        self.opts = ""
        self.passthrough = ""
        self.dryrun = ""
        self.sub = []    # (verbs, {key: value})
        self.flag = []   # (flags, {key: value})
        self.synopsis = ""
        self._load()

    def _load(self):
        with open(self.path, errors="replace") as f:
            head = [next(f, "") for _ in range(15)]
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
            if key in ("where", "name", "takes", "ready", "group", "values", "post") and eq:
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
                elif tok == "dryrun":
                    self.dryrun = "yes"
                    pending = "dryrun"
                elif tok == "passthrough=tail":
                    self.passthrough = "tail"
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
            if in_list(a + "=", opts):
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
