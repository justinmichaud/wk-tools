"""The one seam for every effect: a Machine runs processes and touches files
on this host, a host over ssh, or an in-memory fake; a mutating call prints
under --dry-run and refuses before a destructive command has asked."""

import fnmatch
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time

from wk import act


def is_macos():
    return os.uname().sysname == "Darwin"


def is_linux():
    return not is_macos()


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

    def run_tty(self, argv, cwd=None, timeout=None):
        """Blocking, with this process's own stdio inherited (a real pty for lldb/samply/xctrace)."""
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

    def readlink(self, path):
        raise NotImplementedError

    # -- effects
    def act_run(self, argv, **kw):
        if act.dry_run():
            sys.stderr.write("would run%s: %s\n" % (self._where(), " ".join(shlex.quote(a) for a in argv)))
            return Result(0)
        if os.environ.get("WK_DESTRUCTIVE") and not act.asked():
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

    # -- copy: the one path for moving bytes in or out of a workspace, a board or a card
    def copy_in(self, src, dest):
        raise NotImplementedError

    def copy_out(self, src, dest):
        raise NotImplementedError

    def copy_tree_in(self, src, dest):
        raise NotImplementedError

    def copy_tree_out(self, src, dest, exclude=()):
        """`exclude` holds rsync patterns: one without a slash names a file or directory at any depth."""
        raise NotImplementedError

    # -- lock effects: a resource lock is process coordination, not workspace
    # mutation, so these run whatever --dry-run says and take no destructive gate
    def symlink(self, target, path):
        raise NotImplementedError

    def rename(self, path, dst):
        raise NotImplementedError

    def remove_now(self, path):
        raise NotImplementedError

    def mkdir_now(self, path):
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

    def run_tty(self, argv, cwd=None, timeout=None):
        try:
            cp = subprocess.run(argv, cwd=cwd, timeout=timeout)
        except subprocess.TimeoutExpired:
            return Result(TIMED_OUT, "", "timed out after %ss" % timeout)
        except FileNotFoundError as e:
            return Result(127, "", str(e))
        return Result(cp.returncode)

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

    def readlink(self, path):
        try:
            return os.readlink(path)
        except OSError:
            return None

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
        self.remove_now(path)

    def remove_now(self, path):
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
        self.mkdir_now(path)

    def mkdir_now(self, path):
        os.makedirs(path, exist_ok=True)

    def symlink(self, target, path):
        try:
            os.symlink(target, path)
            return True
        except OSError:
            return False

    def rename(self, path, dst):
        try:
            os.replace(path, dst)
            return True
        except OSError:
            return False

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

    # Both ends are this host's own filesystem, so a real copy needs no transport.
    def copy_in(self, src, dest):
        if act.dry_run():
            sys.stderr.write("would copy: %s -> %s\n" % (src, dest))
            return
        shutil.copyfile(src, dest)

    copy_out = copy_in

    def copy_tree_in(self, src, dest):
        self.copy_tree_out(src, dest)

    def copy_tree_out(self, src, dest, exclude=()):
        if act.dry_run():
            sys.stderr.write("would copy: %s -> %s/\n" % (src, dest))
            return
        cp = subprocess.run(["rsync", "-a", "--delete", *excludes(exclude), src.rstrip("/") + "/", dest.rstrip("/") + "/"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if cp.returncode != 0:
            raise OSError(cp.stderr.decode(errors="replace").strip() or "rsync failed")


def far_side_start(cmd, out_path, tail):
    """The far-side start line: `cmd` detached, its output in `out_path`, ending in `tail` (a pid, or a disown)."""
    return "nohup %s > %s 2>&1 < /dev/null & %s" % (cmd, shlex.quote(out_path), tail)


class Ssh(Machine):
    """A host over ssh, each call one bounded non-interactive round trip run by `via` (this host)."""

    def __init__(self, dest, opts=None, timeout=10, via=None):
        self.dest = dest
        self.name = dest
        self.opts = list(opts or []) + ["-o", "BatchMode=yes", "-o", "ConnectTimeout=%d" % timeout]
        self.via = via or Local()

    def argv(self, remote, tty=False):
        return ["ssh"] + (["-t"] if tty else []) + self.opts + [self.dest, remote]

    def _ssh(self, remote, input=None, timeout=None):
        # An empty stdin, not the caller's: ssh drinks whatever it is handed.
        return self.via.run(self.argv(remote), input="" if input is None else input, timeout=timeout)

    def run(self, argv, input=None, timeout=None):
        return self._ssh(" ".join(shlex.quote(a) for a in argv), input=input, timeout=timeout)

    def run_tty(self, argv, cwd=None, timeout=None):
        remote = " ".join(shlex.quote(a) for a in argv)
        if cwd:
            remote = "cd %s && %s" % (shlex.quote(cwd), remote)
        return self.via.run_tty(self.argv(remote, tty=True), timeout=timeout)

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

    def readlink(self, path):
        r = self._ssh("readlink %s" % shlex.quote(path))
        return r.out.strip() if r.ok else None

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
        self.remove_now(path)

    def remove_now(self, path):
        self._ssh("rm -rf %s" % shlex.quote(path))

    def mkdir(self, path):
        if act.dry_run():
            sys.stderr.write("would create on %s: %s\n" % (self.dest, path))
            return
        self.mkdir_now(path)

    def mkdir_now(self, path):
        self._ssh("mkdir -p %s" % shlex.quote(path))

    def symlink(self, target, path):
        return self._ssh("ln -s %s %s" % (shlex.quote(target), shlex.quote(path))).ok

    def rename(self, path, dst):
        return self._ssh("mv -f %s %s" % (shlex.quote(path), shlex.quote(dst))).ok

    def kill(self, pid, sig=signal.SIGTERM):
        if act.dry_run():
            sys.stderr.write("would signal on %s: %d %s\n" % (self.dest, pid, signal.Signals(sig).name))
            return True
        return self._ssh("kill -%d %d" % (sig, pid)).ok

    def spawn(self, argv, log):
        line = far_side_start(" ".join(shlex.quote(a) for a in argv), log, "echo $!")
        if act.dry_run():
            sys.stderr.write("would start on %s: %s\n" % (self.dest, line))
            return 0
        r = self._ssh(line)
        if not r.ok or not r.out.strip().isdigit():
            raise OSError(r.err.strip() or "no pid came back")
        return int(r.out.strip())

    def forward(self, port, log=os.devnull):
        """A context holding `ssh -R`: the far side's 127.0.0.1:<port> is this host's while it is held; it yields the ssh's pid, 0 in a dry run."""
        from contextlib import contextmanager

        @contextmanager
        def held():
            pid = self.via.spawn(["ssh", *self.opts, "-o", "ExitOnForwardFailure=yes", "-N", "-R",
                                  "127.0.0.1:%d:127.0.0.1:%d" % (port, port), self.dest], log)
            try:
                yield pid
            finally:
                if pid:
                    self.via.kill(pid)
        return held()

    # scp/rsync, run by `via` (this host) reaching `self.dest`: a byte pipe
    # through this Machine's own `run` would corrupt binary data both ways.
    def _dest(self, path):
        return "%s:%s" % (self.dest, path)

    def copy_in(self, src, dest):
        if act.dry_run():
            sys.stderr.write("would copy on %s: %s -> %s\n" % (self.dest, src, dest))
            return
        r = self.via.run(["scp", "-q", *self.opts, src, self._dest(dest)])
        if not r.ok:
            raise OSError(r.err.strip() or "copy to %s failed" % self.dest)

    def copy_out(self, src, dest):
        if act.dry_run():
            sys.stderr.write("would copy on %s: %s -> %s\n" % (self.dest, src, dest))
            return
        r = self.via.run(["scp", "-q", *self.opts, self._dest(src), dest])
        if not r.ok:
            raise OSError(r.err.strip() or "copy from %s failed" % self.dest)

    # --chmod=go-w: a tree crossing machines does not carry the pushing
    # machine's umask.
    def copy_tree_in(self, src, dest):
        if act.dry_run():
            sys.stderr.write("would copy on %s: %s -> %s/\n" % (self.dest, src, dest))
            return
        r = self.via.run(["rsync", "-a", "--chmod=go-w", "--delete", "-e", "ssh " + " ".join(shlex.quote(o) for o in self.opts),
                          src.rstrip("/") + "/", self._dest(dest.rstrip("/") + "/")])
        if not r.ok:
            raise OSError(r.err.strip() or "copy to %s failed" % self.dest)

    def copy_tree_out(self, src, dest, exclude=()):
        if act.dry_run():
            sys.stderr.write("would copy on %s: %s -> %s/\n" % (self.dest, src, dest))
            return
        r = self.via.run(["rsync", "-a", "--chmod=go-w", "--delete", *excludes(exclude), "-e", "ssh " + " ".join(shlex.quote(o) for o in self.opts),
                          self._dest(src.rstrip("/") + "/"), dest.rstrip("/") + "/"])
        if not r.ok:
            raise OSError(r.err.strip() or "copy from %s failed" % self.dest)


def excludes(patterns):
    return [w for x in patterns for w in ("--exclude", x)]


class Killed(Exception):
    """The Fake's process died: raised in place of the effect `stop_after` names."""


class Fake(Machine):
    """An in-memory host: a command answers what `answer()` or `react()`
    registered for the longest matching argv prefix (the latest among
    equals), and every effect lands in `effects`; with `stop_after` set, the
    effect after that many raises Killed instead of landing."""

    def __init__(self, name="fake"):
        self.name = name
        self.files = {}
        self.dirs = set()
        self.pids = set()
        self.answers = []
        self.effects = []
        self.next_pid = 1000
        self.stop_after = None
        self.applied = 0
        self._acting = False

    def answer(self, prefix, rc=0, out="", err=""):
        self.answers.append((tuple(prefix), Result(rc, out, err)))

    def react(self, prefix, fn):
        """`fn(argv, fake)` returns the Result and may move the fake's files and pids."""
        self.answers.append((tuple(prefix), fn))

    def effect(self, record):
        if self.stop_after is not None and self.applied >= self.stop_after:
            raise Killed(record)
        self.applied += 1
        self.effects.append(record)

    def _record(self, record):
        if self._acting:
            self.effect(record)
        else:
            self.effects.append(record)

    def record_run(self, argv):
        self._record(("run", tuple(argv)))

    def _answer(self, argv):
        best = None
        for prefix, result in self.answers:
            if tuple(argv[:len(prefix)]) == prefix and (best is None or len(prefix) >= len(best[0])):
                best = (prefix, result)
        if best is None:
            return Result(127, "", "%s: no answer registered" % argv[0])
        return best[1](list(argv), self) if callable(best[1]) else best[1]

    def act_run(self, argv, **kw):
        self._acting = True
        try:
            return super().act_run(argv, **kw)
        finally:
            self._acting = False

    def run(self, argv, input=None, timeout=None):
        self.record_run(argv)
        return self._answer(argv)

    def run_tty(self, argv, cwd=None, timeout=None):
        self._record(("run_tty", tuple(argv), cwd))
        return self._answer(argv)

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

    def readlink(self, path):
        return self.files[path] if path in self.files else None

    def _set_file(self, path, text):
        self.files[path] = text
        parent = os.path.dirname(path)
        while parent and parent != "/":
            self.dirs.add(parent)
            parent = os.path.dirname(parent)

    def write(self, path, text):
        self.effect(("write", path))
        if act.dry_run():
            return
        self._set_file(path, text)

    def _drop(self, path):
        self.files = {p: t for p, t in self.files.items() if p != path and not p.startswith(path.rstrip("/") + "/")}
        self.dirs = {d for d in self.dirs if d != path and not d.startswith(path.rstrip("/") + "/")}

    def remove(self, path):
        self.effect(("remove", path))
        if act.dry_run():
            return
        self._drop(path)

    def remove_now(self, path):
        self.effect(("remove", path))
        self._drop(path)

    def mkdir(self, path):
        self.effect(("mkdir", path))
        if act.dry_run():
            return
        self.dirs.add(path)

    def mkdir_now(self, path):
        self.effect(("mkdir", path))
        self.dirs.add(path)

    def symlink(self, target, path):
        self.effect(("symlink", path))
        parent = os.path.dirname(path)
        if path in self.files or path in self.dirs or (parent not in ("", "/") and parent not in self.dirs):
            return False
        self.files[path] = target
        return True

    def rename(self, path, dst):
        self.effect(("rename", path, dst))
        if path not in self.files:
            return False
        self.files[dst] = self.files.pop(path)
        return True

    def kill(self, pid, sig=signal.SIGTERM):
        self.effect(("kill", pid, int(sig)))
        if act.dry_run():
            return True
        if pid in self.pids:
            self.pids.discard(pid)
            return True
        return False

    def spawn(self, argv, log):
        self.effect(("spawn", tuple(argv), log))
        if act.dry_run():
            return 0
        self.next_pid += 1
        self.pids.add(self.next_pid)
        self.files.setdefault(log, "")
        return self.next_pid

    def forward(self, port, log=os.devnull):
        from contextlib import contextmanager

        @contextmanager
        def held():
            self.effect(("forward", port))
            pid = 0
            if not act.dry_run():
                self.next_pid += 1
                pid = self.next_pid
                self.pids.add(pid)
            try:
                yield pid
            finally:
                self.pids.discard(pid)
        return held()

    # A real file on disk crosses into this fake host's in-memory files, and back.
    def copy_in(self, src, dest):
        self.effect(("copy_in", src, dest))
        if act.dry_run():
            return
        with open(src, "rb") as f:
            data = f.read()
        self._set_file(dest, data)

    def copy_out(self, src, dest):
        self.effect(("copy_out", src, dest))
        if act.dry_run():
            return
        data = self.read(src)
        with open(dest, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode())

    def copy_tree_in(self, src, dest):
        self.effect(("copy_tree_in", src, dest))
        if act.dry_run():
            return
        self._drop(dest)
        self.dirs.add(dest)
        for root, _, filenames in os.walk(src):
            rel = os.path.relpath(root, src)
            base = dest if rel == "." else os.path.join(dest, rel)
            self.dirs.add(base)
            for fn in filenames:
                with open(os.path.join(root, fn), "rb") as f:
                    self.files[os.path.join(base, fn)] = f.read()

    def copy_tree_out(self, src, dest, exclude=()):
        self.effect(("copy_tree_out", src, dest) + tuple(exclude))
        if act.dry_run():
            return
        if src not in self.dirs:
            raise OSError("no such directory: %s" % src)
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        os.makedirs(dest, exist_ok=True)
        prefix = src.rstrip("/") + "/"
        for p, data in self.files.items():
            if not p.startswith(prefix):
                continue
            rel = p[len(prefix):]
            if any(fnmatch.fnmatchcase(part, x) for part in rel.split("/") for x in exclude):
                continue
            out = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as f:
                f.write(data if isinstance(data, bytes) else data.encode())


def here():
    return Local()
