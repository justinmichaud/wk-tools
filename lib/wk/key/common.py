import io
import re
import sys
import termios
from concurrent.futures import ThreadPoolExecutor

from wk.act import debug, info, log, warn
from wk.secrets import first_line

LOCAL = "local"
LOGIN = "claude-login"
TITLE = "wk shared deploy key"
SAME_AS_HERE = "the one this machine holds"
LOGIN_FILES = (".credentials.json", ".claude.json")
STATES = {"ok": "ok", "wide": "wide", "unverified": "?", "absent": "none"}
RANKS = {"ok": 3, "wide": 2}
TOMBSTONES = {"register": "wk key deploy",
              "share": "wk key setup   (it elects the credential the fleet holds rather than pushing this machine's)",
              "claude": "wk key set claude   (the account login beside it is 'wk key set claude-login')",
              "tailnet": "wk key set tailnet", "tailnet-api": "wk key set tailnet-api"}


def verdict(line):
    return (line or "").split("\t", 1)[0]


def detail(line):
    return line.split("\t", 1)[1] if "\t" in (line or "") else (line or "")


def summary(line):
    return first_line(detail(line))


def fact(line, key):
    for text in (line or "").split("\n"):
        m = re.match(r"^ *%s: (.*)$" % re.escape(key), text)
        if m:
            return m.group(1)
    return ""


def cred_print(name, line):
    """A stored credential's verdict, to the log: True unless it cannot do its job."""
    v, d = verdict(line), detail(line)
    if v == "ok":
        log("  " + d)
    elif v == "wide":
        warn("%s reaches further than wk spends it: %s" % (name, d))
    elif v == "unverified":
        warn("%s is unverified: %s" % (name, d))
    elif v == "bad":
        warn("%s cannot do its job: %s" % (name, d))
        return False
    return True


def table_row(state, label, text):
    return "    %-4s %-28s %s\n" % (state, label, text)


def changed(msg):
    info(msg)


def unchanged(msg):
    debug("ok: " + msg)


class Indented(io.TextIOBase):
    """What `ensure` says, indented under the run that asked for it."""

    def __init__(self, out):
        self.out, self.fresh = out, True

    def write(self, s):
        for part in s.splitlines(True):
            self.out.write(("  " if self.fresh else "") + part)
            self.fresh = part.endswith("\n")
        return len(s)


def prompt_secret(what, url, how, tty):
    """What a person pasted with the terminal's echo off, or None."""
    if not tty():
        warn("wk needs %s, and there is no terminal to ask on. Re-run interactively." % what)
        return None
    sys.stderr.write("\n")
    info("wk needs %s." % what)
    if url:
        log("  " + url)
    if how:
        log("  " + how)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    new = termios.tcgetattr(fd)
    new[3] &= ~termios.ECHO
    termios.tcsetattr(fd, termios.TCSADRAIN, new)
    try:
        sys.stderr.write("  paste it (input hidden, empty to skip): ")
        sys.stderr.flush()
        val = sys.stdin.readline()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    sys.stderr.write("\n")
    return val.rstrip("\n") or None


class GitHub:
    def __init__(self, machine):
        self.machine = machine

    def _keys(self, repo, jq):
        r = self.machine.run(["gh", "api", "repos/%s/keys" % repo, "--jq", jq], input="")
        return r.out if r.ok else None

    def bodies(self, repo):
        return [" ".join(line.split()[:2]) for line in (self._keys(repo, ".[].key") or "").splitlines()]

    def read_only(self, repo, b64):
        return first_line(self._keys(repo, '.[] | select(.key | contains("%s")) | .read_only' % b64) or "")

    def titled(self, repo, title):
        return (self._keys(repo, '.[] | select(.title == "%s") | .id' % title) or "").split()

    def delete(self, repo, key_id):
        return self.machine.act_run(["gh", "api", "-X", "DELETE", "repos/%s/keys/%s" % (repo, key_id)], input="").ok

    def add(self, repo, title, pub):
        return self.machine.act_run(["gh", "api", "repos/%s/keys" % repo, "-f", "title=" + title, "-f", "key=" + pub,
                                     "-F", "read_only=false"], input="").ok


class Fleet:
    """The other workstations and the build machines, each asked through its own `wk key`."""

    def __init__(self, reg, env):
        self.reg, self.env = reg, env
        self.peers, self.boxes, self.targets = [], [], {}

    def _load(self, name):
        try:
            t = self.reg.load(name)
        except LookupError:
            return None
        return t if t.kind == "remote" and t.has_wk() else None

    def resolve(self):
        names = self.reg.machines()
        with ThreadPoolExecutor(max_workers=max(1, len(names))) as pool:
            loaded = list(pool.map(self._load, names))
        for name, t in zip(names, loaded):
            if t is not None:
                self.targets[name] = t
                (self.peers if t.peer else self.boxes).append(name)
        return self

    def _argv(self, name, args, env):
        return ["sh", "-c", self.targets[name].wk_cmd(["key"] + list(args), env)]

    def ask(self, name, *args):
        """(status, stdout): a read, so the far side is never told this run is a dry one."""
        env = {k: v for k, v in self.env.items() if k != "WK_DRY_RUN"}
        r = self.targets[name].machine.run(self._argv(name, args, env), input="")
        return r.rc, r.out.replace("\r", "")

    def tell(self, name, args, value):
        """The value on stdin, never an argument in `ps` over there."""
        r = self.targets[name].machine.act_run(self._argv(name, args, self.env), input=value)
        return r.ok, r.out.replace("\r", "")
