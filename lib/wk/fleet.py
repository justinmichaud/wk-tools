"""The fleet: one `machines/<name>.conf` per machine, with a kind; ~/.config/wk/machines/ overlays it.
`python3 -m wk.fleet list` exits 2 on a conf that does not parse."""

import argparse
import os
import re
import sys

from wk import act, images, kv
from wk.kv import ConfError

KINDS = ("build", "peer", "board", "mac", "guest", "bridge")
TARGET_KINDS = ("build", "peer")
BENCH_KINDS = ("board", "mac", "guest")
BRIDGE_DEFAULTS = {"ssh": lambda n: n, "hostname": lambda n: n, "tag": "tag:bridge", "if": "lan0",
                   "egress": "none", "camera": "off", "user": "user", "battery_limit": "80"}
LOWER = re.compile(r"^[a-z][a-z0-9_]*$")


def _lowercase(key):
    if not LOWER.match(key):
        return "%s is not a key: a machine conf's keys are lowercase" % key
    return None


def parse_text(text, path="<conf>"):
    return kv.conf(text, path, _lowercase)


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
            conf.update(kv.conf_file(p, _lowercase))
        kind = conf.get("kind", "")
        if kind not in KINDS:
            raise ConfError("%s: kind=%s is not one of %s" % (files[0], kind or "(unset)", ", ".join(KINDS)))
        if kind == "mac" and self.env.get("WK_BENCH_VOLUME"):
            conf["volume"] = self.env["WK_BENCH_VOLUME"]
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
        return conf["kind"] if conf else None

    def names(self, kinds=KINDS):
        """A conf that does not parse is refused by name rather than left out of the listing."""
        out = []
        for n in self._declared():
            try:
                conf = self.load(n)
            except ConfError as e:
                act.die(str(e), 2)
            if conf and conf["kind"] in kinds:
                out.append(n)
        return out

    def named_by_host(self, host):
        """The target this host is, by `hostname -s`: two machines sharing one home share every file there."""
        host = (host or "").lower()
        for n in self.names(TARGET_KINDS):
            if (self.load(n).get("hostname") or n).lower() == host:
                return n
        raise LookupError("this host, %s, is the far end of a target (~/.wk-remote), and no machines/<name>.conf\n"
                          "    names it. Set hostname=%s in the conf of the machine it is." % (host, host))


def main(argv, env=None):
    p = argparse.ArgumentParser(prog="python3 -m wk.fleet")
    verbs = p.add_subparsers(dest="verb", required=True)
    verbs.add_parser("list", help="the names machines/ declares, for a setup stage (host/dotfiles.sh)").add_argument(
        "--kind", action="append", choices=KINDS, help="only machines of this kind (repeatable)")
    a = p.parse_args(argv)
    try:
        for n in Fleet(images.root(env), env).names(tuple(a.kind or KINDS)):
            print(n)
    except act.Refused as e:
        return e.status
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
