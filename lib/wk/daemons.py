"""The one supervisor of wk's daemons: a row as a systemd user unit or a launchd agent, restarted when its rendering (which
carries its program's stamp) changes, removed once no row names it; a stop precedes the write, so a killed run converges."""

import collections
import contextlib
import modulefinder
import os
import plistlib
import shlex
import sys
import zlib

from wk import act, claudelogin
from wk.act import debug, warn
from wk.clock import Clock

PROGRAMS = {"proxy": "container/proxy/wk-proxy.py", "push": "container/push/wk-push.py",
            "github-inject": "container/proxy/github-inject.py", "broker": "container/broker/wk-broker.py"}
WORDS = tuple(PROGRAMS)
LOSS = {"proxy": "workspaces will have no egress", "push": "no workspace here can push",
        "github-inject": "a workspace's credentials and claude.ai login will fail", "broker": "workspaces cannot ask for a bench machine"}
TREE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VM_ROOT, VM_STORE = "/opt/wk-tools", "/var/lib/wk"
LAUNCHD_PATH = "/opt/podman/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"   # podman's pkg is on no default PATH
READY_WAIT = 10

# An `nc -z -U` answers 1 for a served socket on macOS, so the connect is made in python; host:port is a TCP listener.
SOCKET_ANSWERS = """import socket, sys
a = sys.argv[1]
s = socket.socket(socket.AF_UNIX) if a.startswith("/") else socket.socket()
s.settimeout(2)
try:
    s.connect(a if a.startswith("/") else (a.rpartition(":")[0], int(a.rpartition(":")[2])))
except OSError:
    sys.exit(1)
"""


class Daemon(collections.namedtuple("Daemon", "word env socket sandbox boot lock", defaults=(None, (), True, None))):
    """`boot`: started at login and kept alive, else on demand; `lock`: a login whose lock a stop holds."""

    @property
    def program(self):
        return PROGRAMS[self.word]


def stamp(program):
    """crc32 over the program and every module of this tree it imports, its own directory searched first as python3 does."""
    path = os.path.join(TREE, program)
    tree = [os.path.dirname(path), os.path.join(TREE, "lib")]

    class InTree(modulefinder.ModuleFinder):
        def find_module(self, name, where, parent=None):
            return super().find_module(name, tree if where is None else where, parent)
    f = InTree(tree)
    f.run_script(path)
    crc = 0
    for p in sorted({path} | {m.__file__ for m in f.modules.values() if m.__file__}):
        with open(p, "rb") as h:
            crc = zlib.crc32(h.read(), crc)
    return str(crc)


def answers(machine, sock):
    return machine.run(["python3", "-c", SOCKET_ANSWERS, sock]).ok


def systemd_rows(root, store, login):
    """The proxy only opens sockets; push asks rootless podman, which cannot rejoin its user namespace from a sandbox's."""
    walled = ("PrivateTmp=yes", "ProtectSystem=strict", "ProtectHome=read-only")
    return {"proxy": Daemon("proxy", {"WK_STORE": store}, sandbox=walled),
            "push": Daemon("push", {"WK_ROOT": root, "WK_STORE": store}),
            "github-inject": Daemon("github-inject", {"WK_STORE": store, "WK_INJECT_CLAUDE_LOGIN": login}, sandbox=walled + ("ReadWritePaths=" + store,),
                             lock=login or None),
            "broker": Daemon("broker", {"WK_ROOT": root, "WK_STORE": store}, sandbox=("PrivateTmp=yes",))}


class Supervisor:
    def __init__(self, machine, root, clock=None):
        self.m, self.root, self.clock = machine, root, clock or Clock()
        self.changes = []
        self._home = None

    def home(self):
        if self._home is None:
            self._home = self.m.run(["sh", "-c", 'printf %s "$HOME"']).out.strip()
        return self._home

    def body(self, d):
        return self.render(d, dict(d.env, WK_PROGRAM_STAMP=stamp(d.program)))

    def installed(self, d):
        try:
            return self.m.read(self.path(d.word))
        except OSError:
            return None

    def ensure(self, d):
        body, loaded = self.body(d), self.loaded(d)
        same = self.installed(d) == body
        if same and loaded and self.serving(d):
            debug("%s running" % self.name(d.word))
            return True
        if not same:
            if loaded:
                with claudelogin.locked(d.lock) if d.lock else contextlib.nullcontext():
                    self.stop(d.word)
            self.m.mkdir(os.path.dirname(self.path(d.word)))
            self.m.write(self.path(d.word), body)
        if not self.start(d):
            warn("%s did not reach readiness -- %s\n  why: %s" % (self.name(d.word), LOSS[d.word], self.why(d.word)))
            return False
        self.changes.append("%s%s %s" % ("would " if act.dry_run() else "", "start" if same or not loaded else "restart",
                                         self.name(d.word)))
        return True

    def prune(self, words):
        try:
            names = self.m.listdir(self.dir())
        except OSError:
            return
        for name in names:
            word = self.word_of(name)
            if word is None or word in words:
                continue
            self.stop(word)
            self.m.remove(self.path(word))
            self.changes.append("%sstopped and removed %s (no row names it)" % ("would have " if act.dry_run() else "", name))


class Systemd(Supervisor):

    def __init__(self, machine, root, journal="", clock=None):
        super().__init__(machine, root, clock)
        self.journal = journal

    def name(self, word):
        return "wk-%s.service" % word

    def dir(self):
        return self.home() + "/.config/systemd/user"

    def path(self, word):
        return "%s/%s" % (self.dir(), self.name(word))

    def word_of(self, name):
        return name[3:-8] if name.startswith("wk-") and name.endswith(".service") else None

    def render(self, d, env):
        lines = ["[Unit]", "Description=wk " + d.word, "RequiresMountsFor=" + self.root, "", "[Service]", "Type=notify",
                 "NotifyAccess=all", "ExecStart=/usr/bin/python3 %s/%s" % (self.root, d.program)] \
            + ["Environment=%s=%s" % kv for kv in sorted(env.items())] \
            + ["Restart=on-failure", "RestartSec=2", "RuntimeDirectory=wk", "RuntimeDirectoryMode=0700",
               "RuntimeDirectoryPreserve=yes", *d.sandbox, "", "[Install]", "WantedBy=default.target"]
        return "\n".join(lines) + "\n"

    def ctl(self, *argv):
        return self.m.act_run(["systemctl", "--user", *argv])

    def loaded(self, d):
        return self.m.run(["systemctl", "--user", "is-active", "--quiet", self.name(d.word)]).ok

    def serving(self, d):
        return True

    def stop(self, word):
        self.ctl("disable", "--now", self.name(word))

    def start(self, d):
        self.ctl("daemon-reload")
        self.ctl("reset-failed", self.name(d.word))
        return self.ctl("enable", "--now", self.name(d.word)).ok

    def why(self, word):
        return "%sjournalctl --user -u %s -e" % (self.journal, self.name(word))


class Launchd(Supervisor):
    """launchd re-reads a plist only at bootstrap; a bootout is done once `launchctl print` stops answering."""

    def __init__(self, machine, root, logs, clock=None):
        super().__init__(machine, root, clock)
        self.logs, self.domain = logs, "gui/%d" % os.getuid()

    def name(self, word):
        return "com.wk." + word

    def dir(self):
        return self.home() + "/Library/LaunchAgents"

    def path(self, word):
        return "%s/%s.plist" % (self.dir(), self.name(word))

    def word_of(self, name):
        return name[7:-6] if name.startswith("com.wk.") and name.endswith(".plist") else None

    def render(self, d, env):
        log = os.path.join(self.logs, d.word + ".log")
        return plistlib.dumps({"Label": self.name(d.word), "ProgramArguments": ["/usr/bin/python3", os.path.join(self.root, d.program)],
                               "EnvironmentVariables": dict(env, PATH=LAUNCHD_PATH), "RunAtLoad": d.boot, "KeepAlive": d.boot,
                               "StandardOutPath": log, "StandardErrorPath": log}).decode()

    def svc(self, word):
        return "%s/%s" % (self.domain, self.name(word))

    def loaded(self, d):
        return self.m.run(["launchctl", "print", self.svc(d.word)]).ok

    def serving(self, d):
        return answers(self.m, d.socket)

    def stop(self, word):
        self.m.act_run(["launchctl", "bootout", self.svc(word)])
        if not act.dry_run():
            self.clock.wait_until(lambda: not self.m.run(["launchctl", "print", self.svc(word)]).ok, READY_WAIT, 0.2)

    def start(self, d):
        self.m.mkdir(self.logs)
        if not self.loaded(d) and not self.m.act_run(["launchctl", "bootstrap", self.domain, self.path(d.word)]).ok:
            return False
        if not self.serving(d):
            self.m.act_run(["launchctl", "kickstart", self.svc(d.word)])
        return act.dry_run() or self.clock.wait_until(lambda: self.serving(d), READY_WAIT, 0.2)

    def why(self, word):
        return "launchctl print %s; the log is %s" % (self.svc(word), os.path.join(self.logs, word + ".log"))


def place(where, env):
    from wk.machine import Local, PodmanVm
    from wk.store import Store
    store = Store(env)
    if where == "vm":
        name = store.podman_machine()
        return (Systemd(PodmanVm(name), VM_ROOT, "podman machine ssh %s -- " % name), systemd_rows(VM_ROOT, VM_STORE, ""),
                ("proxy", "push", "github-inject"))
    if store.macos_host:
        from wk import guest
        host = guest.inject_host(TREE, env)
        return host.supervisor(), host.daemons(), WORDS
    return Systemd(Local(), TREE), systemd_rows(TREE, store.store_dir(), store.keyring_claude_login()), WORDS


def main(argv, env):
    if len(argv) < 2 or argv[0] not in ("here", "vm"):
        sys.exit("usage: python3 -m wk.daemons here|vm <%s>..." % "|".join(WORDS))
    sup, rows, words = place(argv[0], env)
    unknown = [w for w in argv[1:] if w not in words]
    if unknown:
        sys.exit("wk.daemons: %s runs no %s" % (argv[0], " ".join(unknown)))
    sup.prune(words)
    for w in argv[1:]:
        sup.ensure(rows[w])
    sys.stdout.write("".join("changed %s\n" % shlex.quote(c) for c in sup.changes))


if __name__ == "__main__":
    main(sys.argv[1:], os.environ)
