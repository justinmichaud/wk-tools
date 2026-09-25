"""The fleet: one `machines/<name>.conf` per machine, with a KIND; ~/.config/wk/machines/ overlays it.
Exit 1: no such machine of those kinds; 2: a conf that does not parse."""

import os
import re
import shlex
import sys

KINDS = ("build", "peer", "board", "mac", "guest", "bridge")
TARGET_KINDS = ("build", "peer")
BENCH_KINDS = ("board", "mac", "guest")
BRIDGE_DEFAULTS = {"BR_SSH": lambda n: n, "BR_HOSTNAME": lambda n: n, "BR_TAG": "tag:bridge", "BR_IF": "lan0",
                   "BR_EGRESS": "none", "BR_CAMERA": "off", "BR_USER": "user", "BR_BATTERY_LIMIT": "80"}
USAGE = "usage: python3 -m wk.fleet load <name> [--kind K]... | list [--kind K]... | get <name> <KEY> | path <name> | self"
KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


class ConfError(ValueError):
    pass


def parse(path):
    with open(path, errors="replace") as f:
        return parse_text(f.read(), path)


def parse_text(text, path="<conf>"):
    out = {}
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = KEY.match(s)
        if not m:
            raise ConfError("%s:%d: not a KEY=value line: %s" % (path, i, s))
        k, raw = m.groups()
        if "$" in raw or "`" in raw:
            raise ConfError("%s:%d: %s is not a literal (a default belongs in code)" % (path, i, k))
        try:
            words = shlex.split(raw, comments=True)
        except ValueError as e:
            raise ConfError("%s:%d: %s: %s" % (path, i, k, e))
        if len(words) > 1:
            raise ConfError("%s:%d: %s is more than one word; quote it" % (path, i, k))
        out[k] = words[0] if words else ""
    return out


def config_home(env):
    return env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".config")


class Fleet:
    def __init__(self, root, env=None):
        self.root = str(root)
        self.env = os.environ if env is None else env

    def dir(self):
        return self.env.get("WK_MACHINES_DIR") or os.path.join(self.root, "machines")

    def local_dir(self):
        return os.path.join(config_home(self.env), "wk", "machines")

    def conf_path(self, name):
        return os.path.join(self.dir(), name + ".conf")

    def _files(self, name):
        return [p for p in (self.conf_path(name), os.path.join(self.local_dir(), name + ".conf")) if os.path.isfile(p)]

    def path(self, name):
        files = self._files(name)
        return files[0] if files else self.conf_path(name)

    def _declared(self):
        names = set()
        for d in (self.dir(), self.local_dir()):
            try:
                names |= {f[:-5] for f in os.listdir(d) if f.endswith(".conf")}
            except OSError:
                pass
        return sorted(names)

    def load(self, name):
        files = self._files(name)
        if not files:
            return None
        conf = {}
        for p in files:
            conf.update(parse(p))
        kind = conf.get("KIND", "")
        if kind not in KINDS:
            raise ConfError("%s: KIND=%s is not one of %s" % (files[0], kind or "(unset)", ", ".join(KINDS)))
        if kind == "mac" and self.env.get("WK_BENCH_VOLUME"):
            conf["NODE_VOLUME"] = self.env["WK_BENCH_VOLUME"]
        if kind == "bridge":
            for k, v in BRIDGE_DEFAULTS.items():
                if not conf.get(k):
                    conf[k] = v(name) if callable(v) else v
        return conf

    def kind(self, name):
        try:
            conf = self.load(name)
        except ConfError:
            return None
        return conf["KIND"] if conf else None

    def names(self, kinds=KINDS):
        return [n for n in self._declared() if self.kind(n) in kinds]

    def named_by_host(self, host):
        """The target this host is, by `hostname -s`: two machines sharing one home share every file there."""
        host = (host or "").lower()
        for n in self.names(TARGET_KINDS):
            if (self.load(n).get("WK_REMOTE_HOSTNAME") or n).lower() == host:
                return n
        raise LookupError("this host, %s, is the far end of a target (~/.wk-remote), and no machines/<name>.conf\n"
                          "    names it. Set WK_REMOTE_HOSTNAME=%s in the conf of the machine it is." % (host, host))

    def old_local_dir(self):
        return os.path.join(config_home(self.env), "wk", "bridges")


def _kinds(args):
    kinds = []
    while args[:1] == ["--kind"] and len(args) > 1:
        kinds.append(args[1])
        args = args[2:]
    if args or any(k not in KINDS for k in kinds):
        raise SystemExit(USAGE)
    return tuple(kinds) or KINDS


def main(argv, env=None):
    fleet = Fleet(os.environ.get("WK_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), env)
    verb, args = (argv[0], argv[1:]) if argv else ("", [])
    try:
        if verb == "list":
            for n in fleet.names(_kinds(args)):
                print(n)
            return 0
        if verb == "self" and not args:
            from wk import record
            try:
                print(fleet.named_by_host(record.host_name()))
            except LookupError as e:
                print(e, file=sys.stderr)
                return 1
            return 0
        if verb == "get" and len(args) == 2:
            conf = fleet.load(args[0])
            if not conf:
                return 1
            print(conf.get(args[1], ""))
            return 0
        if verb == "path" and len(args) == 1:
            print(fleet.path(args[0]))
            return 0
        if verb == "load" and args:
            kinds = _kinds(args[1:])
            conf = fleet.load(args[0])
            if not conf or conf["KIND"] not in kinds:
                return 1
            for k, v in sorted(conf.items()):
                if k != "KIND":
                    print("%s=%s" % (k, shlex.quote(v)))
            return 0
    except ConfError as e:
        print(e, file=sys.stderr)
        return 2
    raise SystemExit(USAGE)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
