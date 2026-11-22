"""The Machine seam: every process run and every file touched, on this host (`Local`) or a server (`Ssh`).

    r = m.run(["git", "status"], cwd=src)      # a read: runs under --dry-run too; r.rc, r.out, r.err, r.ok
    m.act(["git", "fetch"], cwd=src)           # a state change: printed, not run, under --dry-run
    m.write(path, text, mode="600")            # atomic; parents made; bytes travel on stdin, never argv
    m.read(path)  m.exists(path)  m.listdir(path)  m.remove(path)  m.copy(src, to_machine, dst)  m.spawn(argv, log)

Paths are absolute; `m.home` is the login's home. `run` and `act` take input= (str or bytes, on stdin), timeout=
(seconds; rc 124 and the whole process group killed), cwd=, env= (a dict added to the environment; never a secret, it
is argv on the far side), stream=True (output goes to the terminal, not the Result) and tty=True (interactive). A
server's also take forward= (Ssh.argv).
A machine that does not answer raises Unreachable from any method."""

import functools
import os
import shlex
import signal
import subprocess

from wk.core import act as gate
from wk.core.errors import Refused, Unreachable

TIMED_OUT = 124


class Result:
    def __init__(self, rc=0, out="", err="", raw=b""):
        self.rc, self.out, self.err, self.raw = rc, out, err, raw

    @property
    def ok(self):
        return self.rc == 0


def local_run(argv, input=None, timeout=None, cwd=None, env=None, stream=False, tty=False):
    """One process on this host, in a session of its own unless it is interactive."""
    if isinstance(input, str):
        input = input.encode()
    pipe = None if stream else subprocess.PIPE
    try:
        p = subprocess.Popen(argv, stdin=subprocess.PIPE if input is not None else None if stream else subprocess.DEVNULL,
                             stdout=pipe, stderr=pipe, cwd=cwd, env=dict(os.environ, **(env or {})), start_new_session=not tty)
    except OSError as e:
        return Result(127, "", str(e))
    with p:
        try:
            out, err = p.communicate(input, timeout)
        except BaseException as e:
            if tty:
                p.kill()
            else:
                os.killpg(p.pid, signal.SIGKILL)
            if isinstance(e, subprocess.TimeoutExpired):
                return Result(TIMED_OUT, "", "timed out after %ss" % timeout)
            raise
    return Result(p.returncode, (out or b"").decode(errors="replace"), (err or b"").decode(errors="replace"), out or b"")


class Machine:
    """The interface. An implementation supplies _run, _write, _remove, _spawn, read, exists, listdir and home."""

    name = ""

    def _gate(self, what, do, dry=None):
        return gate.act(self.name, what, do, dry)

    def run(self, argv, **how):
        return self._run(argv, **how)

    def act(self, argv, **how):
        return self._gate(shlex.join(argv), lambda: self._run(argv, **how), Result())

    def write(self, path, data, mode="644"):
        self._gate("write %s" % path, lambda: self._write(path, data, mode))

    def remove(self, path):
        """The file or the whole tree at `path`; an absent one is already removed."""
        self._gate("remove %s" % path, lambda: self._remove(path))

    def spawn(self, argv, log):
        """Start `argv` detached, its output in the file `log`; it outlives this process and its ssh session."""
        self._gate("spawn %s" % shlex.join(argv), lambda: self._spawn(argv, log))

    def copy(self, src, to, dst, mode="644"):
        """The file `src` here becomes `dst` on the machine `to`."""
        to.write(dst, self.read(src, raw=True), mode)


WRITE = 'mkdir -p "$(dirname "$1")" && (umask 077 && cat >"$1.tmp") && chmod "$2" "$1.tmp" && mv -f "$1.tmp" "$1"'
SPAWN = 'nohup "$@" >"$0" 2>&1 </dev/null &'


class Shell(Machine):
    """File operations as POSIX commands over `_run`, so this host and a server share one implementation."""

    @functools.cached_property
    def home(self):
        return self._run(["sh", "-c", 'printf %s "$HOME"']).out

    def read(self, path, raw=False):
        """The file's text (bytes when `raw`), or None when there is none."""
        r = self._run(["cat", path])
        return (r.raw if raw else r.out) if r.ok else None

    def exists(self, path):
        return self._run(["test", "-e", path]).ok

    def listdir(self, path):
        """The sorted names in a directory; none when there is no directory."""
        return sorted(self._run(["ls", "-1A", path]).out.splitlines())

    def _must(self, what, argv, input=None):
        r = self._run(argv, input=input)
        if not r.ok:
            raise Refused("cannot %s on %s: %s" % (what, self.name, r.err.strip()))

    def _write(self, path, data, mode):
        self._must("write %s" % path, ["sh", "-c", WRITE, "sh", path, mode], data)

    def _remove(self, path):
        self._must("remove %s" % path, ["rm", "-rf", path])

    def _spawn(self, argv, log):
        self._must("start %s" % argv[0], ["sh", "-c", SPAWN, log] + list(argv))


class Local(Shell):
    name = "here"

    def _run(self, argv, **how):
        return local_run(argv, **how)


# ConnectTimeout bounds a dead address; the keep-alives bound a session whose far end vanishes mid-command.
ALIVE = ["-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]
SSH = ["-o", "BatchMode=yes"] + ALIVE + ["-o", "ControlMaster=auto", "-o", "ControlPath=~/.ssh/wk-%C", "-o", "ControlPersist=60"]
# A forward lives as long as its own connection, so it shares no master; ssh takes the first value an option is given.
FORWARD = ["-o", "ControlPath=none", "-o", "ExitOnForwardFailure=yes"]


class Ssh(Shell):
    """A server, by its tailnet name."""

    def __init__(self, name):
        self.name = name

    def argv(self, argv, cwd=None, env=None, tty=False, forward=None):
        """`forward` is (port, "host:port"): 127.0.0.1:port on the server reaches host:port from this side."""
        remote = shlex.join((["env"] + ["%s=%s" % kv for kv in sorted(env.items())] if env else []) + list(argv))
        if cwd:
            remote = "cd %s && %s" % (shlex.quote(cwd), remote)
        # sshd runs a command under a non-login shell, whose PATH lacks ~/.local/bin and Homebrew.
        carried = FORWARD + ["-R", "127.0.0.1:%d:%s" % forward] if forward else []
        return ["ssh"] + carried + SSH + (["-t"] if tty else []) + [self.name, '"$SHELL" -lc ' + shlex.quote(remote)]

    def _run(self, argv, input=None, timeout=None, cwd=None, env=None, stream=False, tty=False, forward=None):
        r = local_run(self.argv(argv, cwd, env, tty, forward), input, timeout, stream=stream, tty=tty)
        if r.rc == 255:     # ssh's own failure; a remote command's status is its own
            raise Unreachable(self.name, r.err)
        return Result(r.rc, r.out.replace("\r", ""), r.err.replace("\r", ""), r.raw)
