"""The one seam for every effect: a Machine runs processes and touches files
on this host, a host over ssh, or an in-memory fake; a mutating call prints
under --dry-run and refuses before a destructive command has asked."""

import os
import shlex
import signal
import subprocess
import sys
import time

from wk import act


class Result:
    def __init__(self, rc, out="", err=""):
        self.rc = rc
        self.out = out
        self.err = err

    @property
    def ok(self):
        return self.rc == 0

    def __repr__(self):
        return "Result(%d, %r, %r)" % (self.rc, self.out[:60], self.err[:60])


TIMED_OUT = 124


class Machine:

    name = "machine"

    # -- reads
    def run(self, argv, input=None, timeout=None):
        raise NotImplementedError

    def read(self, path):
        raise NotImplementedError

    def exists(self, path):
        raise NotImplementedError

    def isdir(self, path):
        raise NotImplementedError

    def listdir(self, path):
        raise NotImplementedError

    def alive(self, pid):
        raise NotImplementedError

    # -- effects
    def act_run(self, argv, **kw):
        if act.dry_run():
            sys.stderr.write("would run%s: %s\n" % (self._where(), " ".join(shlex.quote(a) for a in argv)))
            return Result(0)
        if os.environ.get("WK_DESTRUCTIVE") and not os.environ.get("WK_CONFIRMED"):
            act.die("BUG: this command is declared destructive and acted before asking:\n    %s"
                    % " ".join(shlex.quote(a) for a in argv))
        return self.run(argv, **kw)

    def write(self, path, text):
        raise NotImplementedError

    def remove(self, path):
        raise NotImplementedError

    def mkdir(self, path):
        raise NotImplementedError

    def kill(self, pid, sig=signal.SIGTERM):
        raise NotImplementedError

    def spawn(self, argv, log):
        raise NotImplementedError

    def _where(self):
        return "" if self.name == "here" else " on " + self.name


class Local(Machine):
    name = "here"

    def run(self, argv, input=None, timeout=None):
        try:
            cp = subprocess.run(argv, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return Result(TIMED_OUT, "", "timed out after %ss" % timeout)
        except FileNotFoundError as e:
            return Result(127, "", str(e))
        return Result(cp.returncode, cp.stdout, cp.stderr)

    def read(self, path):
        with open(path, errors="replace") as f:
            return f.read()

    def exists(self, path):
        return os.path.exists(path)

    def isdir(self, path):
        return os.path.isdir(path)

    def listdir(self, path):
        return sorted(os.listdir(path))

    def alive(self, pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def write(self, path, text):
        if act.dry_run():
            sys.stderr.write("would write: %s\n" % path)
            return
        tmp = "%s.tmp.%d" % (path, os.getpid())
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, path)

    def remove(self, path):
        if act.dry_run():
            sys.stderr.write("would remove: %s\n" % path)
            return
        if os.path.isdir(path) and not os.path.islink(path):
            for root, dirs, files in os.walk(path, topdown=False):
                for n in files:
                    os.unlink(os.path.join(root, n))
                for n in dirs:
                    p = os.path.join(root, n)
                    os.rmdir(p) if not os.path.islink(p) else os.unlink(p)
            os.rmdir(path)
        elif os.path.lexists(path):
            os.unlink(path)

    def mkdir(self, path):
        if act.dry_run():
            if not os.path.isdir(path):
                sys.stderr.write("would create: %s\n" % path)
            return
        os.makedirs(path, exist_ok=True)

    def kill(self, pid, sig=signal.SIGTERM):
        if act.dry_run():
            sys.stderr.write("would signal: %d %s\n" % (pid, signal.Signals(sig).name))
            return True
        try:
            os.kill(pid, sig)
            return True
        except ProcessLookupError:
            return False

    def spawn(self, argv, log):
        if act.dry_run():
            sys.stderr.write("would start: %s > %s\n" % (" ".join(shlex.quote(a) for a in argv), log))
            return 0
        with open(log, "ab") as f:
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        return p.pid


class Ssh(Machine):
    """A host over ssh, each call one bounded non-interactive round trip run by `via` (this host)."""

    def __init__(self, dest, opts=None, timeout=10, via=None):
        self.dest = dest
        self.name = dest
        self.opts = list(opts or []) + ["-o", "BatchMode=yes", "-o", "ConnectTimeout=%d" % timeout]
        self.via = via or Local()

    def _ssh(self, remote, input=None, timeout=None):
        # An empty stdin, not the caller's: ssh drinks whatever it is handed.
        return self.via.run(["ssh", *self.opts, self.dest, remote], input="" if input is None else input, timeout=timeout)

    def run(self, argv, input=None, timeout=None):
        return self._ssh(" ".join(shlex.quote(a) for a in argv), input=input, timeout=timeout)

    def read(self, path):
        r = self._ssh("cat %s" % shlex.quote(path))
        if not r.ok:
            raise OSError(r.err.strip() or "cannot read %s on %s" % (path, self.dest))
        return r.out

    def exists(self, path):
        return self._ssh("test -e %s" % shlex.quote(path)).ok

    def isdir(self, path):
        return self._ssh("test -d %s" % shlex.quote(path)).ok

    def listdir(self, path):
        r = self._ssh("ls -1A %s" % shlex.quote(path))
        if not r.ok:
            raise OSError(r.err.strip())
        return sorted(r.out.split())

    def alive(self, pid):
        return self._ssh("kill -0 %d" % pid).ok

    def write(self, path, text):
        if act.dry_run():
            sys.stderr.write("would write on %s: %s\n" % (self.dest, path))
            return
        r = self._ssh("cat > %s.tmp.$$ && mv %s.tmp.$$ %s" % ((shlex.quote(path),) * 3), input=text)
        if not r.ok:
            raise OSError(r.err.strip())

    def remove(self, path):
        if act.dry_run():
            sys.stderr.write("would remove on %s: %s\n" % (self.dest, path))
            return
        self._ssh("rm -rf %s" % shlex.quote(path))

    def mkdir(self, path):
        if act.dry_run():
            sys.stderr.write("would create on %s: %s\n" % (self.dest, path))
            return
        self._ssh("mkdir -p %s" % shlex.quote(path))

    def kill(self, pid, sig=signal.SIGTERM):
        if act.dry_run():
            sys.stderr.write("would signal on %s: %d %s\n" % (self.dest, pid, signal.Signals(sig).name))
            return True
        return self._ssh("kill -%d %d" % (sig, pid)).ok

    def spawn(self, argv, log):
        line = "nohup %s > %s 2>&1 < /dev/null & echo $!" % (" ".join(shlex.quote(a) for a in argv), shlex.quote(log))
        if act.dry_run():
            sys.stderr.write("would start on %s: %s\n" % (self.dest, line))
            return 0
        r = self._ssh(line)
        if not r.ok or not r.out.strip().isdigit():
            raise OSError(r.err.strip() or "no pid came back")
        return int(r.out.strip())


class Fake(Machine):
    """An in-memory host: a command answers what `answer()` registered for the
    longest matching argv prefix, and every effect lands in `effects`."""

    def __init__(self, name="fake"):
        self.name = name
        self.files = {}
        self.dirs = set()
        self.pids = set()
        self.answers = []
        self.effects = []
        self.next_pid = 1000

    def answer(self, prefix, rc=0, out="", err=""):
        self.answers.append((tuple(prefix), Result(rc, out, err)))

    def run(self, argv, input=None, timeout=None):
        self.effects.append(("run", tuple(argv)))
        best = None
        for prefix, result in self.answers:
            if tuple(argv[:len(prefix)]) == prefix and (best is None or len(prefix) > len(best[0])):
                best = (prefix, result)
        if best is None:
            return Result(127, "", "%s: no answer registered" % argv[0])
        return best[1]

    def read(self, path):
        if path not in self.files:
            raise OSError("no such file: %s" % path)
        return self.files[path]

    def exists(self, path):
        return path in self.files or path in self.dirs

    def isdir(self, path):
        return path in self.dirs

    def listdir(self, path):
        if path not in self.dirs:
            raise OSError("no such directory: %s" % path)
        prefix = path.rstrip("/") + "/"
        names = set()
        for p in list(self.files) + list(self.dirs):
            if p.startswith(prefix):
                names.add(p[len(prefix):].split("/")[0])
        return sorted(names)

    def alive(self, pid):
        return pid in self.pids

    def write(self, path, text):
        self.effects.append(("write", path))
        if act.dry_run():
            return
        self.files[path] = text
        parent = os.path.dirname(path)
        while parent and parent != "/":
            self.dirs.add(parent)
            parent = os.path.dirname(parent)

    def remove(self, path):
        self.effects.append(("remove", path))
        if act.dry_run():
            return
        self.files = {p: t for p, t in self.files.items() if p != path and not p.startswith(path.rstrip("/") + "/")}
        self.dirs = {d for d in self.dirs if d != path and not d.startswith(path.rstrip("/") + "/")}

    def mkdir(self, path):
        self.effects.append(("mkdir", path))
        if act.dry_run():
            return
        self.dirs.add(path)

    def kill(self, pid, sig=signal.SIGTERM):
        self.effects.append(("kill", pid, int(sig)))
        if act.dry_run():
            return True
        if pid in self.pids:
            self.pids.discard(pid)
            return True
        return False

    def spawn(self, argv, log):
        self.effects.append(("spawn", tuple(argv), log))
        if act.dry_run():
            return 0
        self.next_pid += 1
        self.pids.add(self.next_pid)
        self.files.setdefault(log, "")
        return self.next_pid


def here():
    return Local()
