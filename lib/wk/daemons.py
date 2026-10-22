"""The one supervisor of wk's daemons: a row as a systemd user unit or a launchd job, restarted when its rendering (which
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
from wk.act import debug, die, warn
from wk.clock import Clock

PROGRAMS = {"proxy": "container/proxy/wk-proxy.py", "push": "container/push/wk-push.py",
            "github-inject": "container/proxy/github-inject.py", "broker": "container/broker/wk-broker.py"}
WORDS = tuple(PROGRAMS)
GUEST_WORDS = ("nosleep", "mirror", "display")
LOSS = {"proxy": "workspaces will have no egress", "push": "no workspace here can push",
        "github-inject": "a workspace's credentials and claude.ai login will fail", "broker": "workspaces cannot ask for a bench machine",
        "nosleep": "the guest sleeps between builds", "mirror": "no checkout in the guest resolves its objects",
        "display": "the guest keeps its default resolution"}
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


class Daemon(collections.namedtuple("Daemon", "word env socket sandbox boot lock argv domain keep",
                                    defaults=(None, (), True, None, None, "user", True))):
    """`socket`: what answers once it serves, else being loaded is serving; `boot`: RunAtLoad; `keep`: KeepAlive, True, False
    or {"SuccessfulExit": False}; `lock`: a login whose lock a stop holds; `argv`: the command, else /usr/bin/python3 over the
    word's program under PROGRAMS, whose stamp the rendering carries; `domain`: `user` (gui/<uid>, systemd --user) or `system`."""

    def command(self, root):
        return list(self.argv) if self.argv else ["/usr/bin/python3", os.path.join(root, PROGRAMS[self.word])]


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


def guest_rows(home, display):
    """A guest's own: kept awake, the mirror tag mounted by the installed vm/mount-mirror.sh (retried until it is), the display set at login."""
    from wk.places import GUEST_MIRROR_MOUNT, GUEST_MOUNT_MIRROR, MIRROR_TAG
    w, h = display.split("x")
    return {"nosleep": Daemon("nosleep", {}, argv=("/usr/bin/caffeinate", "-dimsu"), domain="system"),
            "mirror": Daemon("mirror", {}, argv=(GUEST_MOUNT_MIRROR, MIRROR_TAG, GUEST_MIRROR_MOUNT), domain="system",
                             keep={"SuccessfulExit": False}),
            "display": Daemon("display", {}, argv=(home + "/.local/bin/wk-set-display", w, h), keep=False)}


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
        return self.render(d, d.env if d.argv else dict(d.env, WK_PROGRAM_STAMP=stamp(PROGRAMS[d.word])))

    def installed(self, d):
        try:
            return self.m.read(self.path(d))
        except OSError:
            return None

    def put(self, d, body):
        self.m.mkdir(self.dir(d.domain))
        self.m.write(self.path(d), body)

    def drop(self, d):
        self.m.remove(self.path(d))

    def ensure(self, d):
        body, loaded = self.body(d), self.loaded(d)
        same = self.installed(d) == body
        if same and loaded and self.serving(d):
            debug("%s running" % self.name(d.word))
            return True
        if not same:
            if loaded:
                with claudelogin.locked(d.lock) if d.lock else contextlib.nullcontext():
                    self.stop(d)
            self.put(d, body)
        if not self.start(d):
            warn("%s did not reach readiness -- %s\n  why: %s" % (self.name(d.word), LOSS[d.word], self.why(d)))
            return False
        self.changes.append("%s%s %s" % ("would " if act.dry_run() else "", "start" if same or not loaded else "restart",
                                         self.name(d.word)))
        return True

    def prune(self, words):
        for domain in self.DOMAINS:
            try:
                names = self.m.listdir(self.dir(domain))
            except OSError:
                continue
            for name in names:
                word = self.word_of(name)
                if word is None or word in words:
                    continue
                d = Daemon(word, {}, domain=domain)
                self.stop(d)
                self.drop(d)
                self.changes.append("%sstopped and removed %s (no row names it)" % ("would have " if act.dry_run() else "", name))


class Systemd(Supervisor):
    DOMAINS = ("user",)

    def __init__(self, machine, root, journal="", clock=None):
        super().__init__(machine, root, clock)
        self.journal = journal

    def name(self, word):
        return "wk-%s.service" % word

    def dir(self, domain):
        return self.home() + "/.config/systemd/user"

    def path(self, d):
        return "%s/%s" % (self.dir(d.domain), self.name(d.word))

    def word_of(self, name):
        return name[3:-8] if name.startswith("wk-") and name.endswith(".service") else None

    def render(self, d, env):
        """Type=notify is the readiness every program under PROGRAMS signals, and a user unit has no system domain."""
        if d.argv or d.domain != "user":
            die("wk.daemons: a systemd user unit cannot hold %s: it %s" % (d.word, "runs a command outside PROGRAMS" if d.argv
                                                                             else "is in the system domain"))
        lines = ["[Unit]", "Description=wk " + d.word, "RequiresMountsFor=" + self.root, "", "[Service]", "Type=notify",
                 "NotifyAccess=all", "ExecStart=" + " ".join(d.command(self.root))] \
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

    def stop(self, d):
        self.ctl("disable", "--now", self.name(d.word))

    def start(self, d):
        self.ctl("daemon-reload")
        self.ctl("reset-failed", self.name(d.word))
        return self.ctl("enable", "--now", self.name(d.word)).ok

    def why(self, d):
        return "%sjournalctl --user -u %s -e" % (self.journal, self.name(d.word))


class Launchd(Supervisor):
    """launchd re-reads a plist only at bootstrap; a bootout is done once `launchctl print` stops answering. System jobs go through `sudo -n`."""

    DOMAINS = {"user": ("{home}/Library/LaunchAgents", "gui/%d" % os.getuid(), ()),
               "system": ("/Library/LaunchDaemons", "system", ("sudo", "-n"))}

    def __init__(self, machine, root, logs, clock=None):
        super().__init__(machine, root, clock)
        self.logs = logs

    def name(self, word):
        return "com.wk." + word

    def dir(self, domain):
        return self.DOMAINS[domain][0].format(home=self.home())

    def path(self, d):
        return "%s/%s.plist" % (self.dir(d.domain), self.name(d.word))

    def word_of(self, name):
        return name[7:-6] if name.startswith("com.wk.") and name.endswith(".plist") else None

    def render(self, d, env):
        log = os.path.join(self.logs, d.word + ".log")
        return plistlib.dumps({"Label": self.name(d.word), "ProgramArguments": d.command(self.root),
                               "EnvironmentVariables": dict(env, PATH=LAUNCHD_PATH), "RunAtLoad": d.boot, "KeepAlive": d.keep,
                               "StandardOutPath": log, "StandardErrorPath": log}).decode()

    def svc(self, d):
        return "%s/%s" % (self.DOMAINS[d.domain][1], self.name(d.word))

    def ctl(self, d, *argv):
        return [*self.DOMAINS[d.domain][2], "launchctl", *argv]

    def put(self, d, body):
        if d.domain == "user":
            return super().put(d, body)
        self.m.act_run(["sudo", "-n", "tee", self.path(d)], input=body)

    def drop(self, d):
        if d.domain == "user":
            return super().drop(d)
        self.m.act_run(["sudo", "-n", "rm", "-f", self.path(d)])

    def loaded(self, d):
        return self.m.run(self.ctl(d, "print", self.svc(d))).ok

    def serving(self, d):
        return d.socket is None or answers(self.m, d.socket)

    def stop(self, d):
        self.m.act_run(self.ctl(d, "bootout", self.svc(d)))
        if not act.dry_run():
            self.clock.wait_until(lambda: not self.loaded(d), READY_WAIT, 0.2)

    def start(self, d):
        self.m.mkdir(self.logs)
        if not self.loaded(d) and not self.m.act_run(self.ctl(d, "bootstrap", self.DOMAINS[d.domain][1], self.path(d))).ok:
            return False
        if not self.serving(d):
            self.m.act_run(self.ctl(d, "kickstart", self.svc(d)))
        return act.dry_run() or self.clock.wait_until(lambda: self.serving(d), READY_WAIT, 0.2)

    def why(self, d):
        return "%s; the log is %s" % (shlex.join(self.ctl(d, "print", self.svc(d))), os.path.join(self.logs, d.word + ".log"))


def place(where, env):
    from wk.machine import Local, Ssh
    from wk.store import Store
    store = Store(env)
    if where == "vm":
        name = store.podman_machine()
        return (Systemd(Ssh.podman(name), VM_ROOT, "podman machine ssh %s -- " % name), systemd_rows(VM_ROOT, VM_STORE, ""),
                ("proxy", "push", "github-inject"))
    if where == "guest":
        from wk import guest
        return Launchd(Local(), TREE, store.state_dir()), guest_rows(store.home(), guest.display(env)), GUEST_WORDS
    if store.macos_host:
        from wk import guest
        host = guest.inject_host(TREE, env)
        return host.supervisor(), host.daemons(), WORDS
    return Systemd(Local(), TREE), systemd_rows(TREE, store.store_dir(), store.keyring_claude_login()), WORDS


def main(argv, env):
    if len(argv) < 2 or argv[0] not in ("here", "vm", "guest"):
        sys.exit("usage: python3 -m wk.daemons here|vm|guest <%s>..." % "|".join(WORDS + GUEST_WORDS))
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
