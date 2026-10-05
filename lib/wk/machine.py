"""The one seam for every effect: a Machine runs processes and touches files
on this host, a host over ssh, or an in-memory fake; a mutating call prints
under --dry-run and refuses before a destructive command has asked."""

import base64
import errno
import fnmatch
import io
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile

from wk import act


def is_macos():
    return os.uname().sysname == "Darwin"


def is_linux():
    return not is_macos()


PODMAN_MACHINE = "/etc/containers/podman-machine"   # written by podman into every machine it provisions


def in_podman_machine():
    return os.path.exists(PODMAN_MACHINE)


class Planted(OSError):
    def __init__(self, path):
        super().__init__("%s is a symbolic link or not a regular file, and nothing is read through one here" % path)
        self.path = path


def matches(rel, patterns):
    return any(fnmatch.fnmatch(rel, p) for p in patterns)


def replace_file(path, data, mode=None):
    """`path` holds `data` whole or keeps what it held: the temp is mkstemp's (O_EXCL, never a planted name), and the
    rename replaces a link at `path` rather than writing through it. `mode` is the file's, else the umask's."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".%s." % os.path.basename(path))
    try:
        if mode is None:
            mask = os.umask(0)
            os.umask(mask)
            mode = 0o666 & ~mask
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb" if isinstance(data, bytes) else "w") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise


def lib_argv(root, rel, fn, *args):
    return ["bash", "-c", '. "$0"; %s "$@"' % fn, os.path.join(str(root), rel), *args]


ISOLATED = "import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); runpy.run_module('wk', run_name=__name__, alter_sys=True)"


def isolated_module(lib, module, python="python3"):
    """-I keeps the working directory (a checkout's wk/ shadows ours) off sys.path; -P is 3.11+, a Mac's python3 3.9."""
    return [python, "-I", "-c", ISOLATED, str(lib), module]


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
HAVE = ("sh", "-c", 'command -v "$1" >/dev/null', "sh")


class Machine:
    """What every transport implements beyond these: run (`stream` sends the output to stderr as it arrives), run_tty,
    read, read_tree ({path: bytes} under anchor/rel matching `patterns`; Planted names a link met), exists, isdir,
    listdir, alive, readlink, mtime, read_bytes, and the effects --dry-run prints. write_own/remove_own (the task
    record) and symlink/rename/remove_now/mkdir_now (a lock) run whatever --dry-run says, with no destructive gate."""

    name = "machine"

    def have(self, tool):
        return self.run(list(HAVE) + [tool]).ok

    def _would(self, verb, what):
        if act.dry_run():
            sys.stderr.write("would %s%s: %s\n" % (verb, self._where(), what))
        return act.dry_run()

    def act_run(self, argv, **kw):
        if self._would("run", shlex.join(argv)):
            return Result(0)
        if act.destructive() and not act.asked():
            act.die("BUG: this command is declared destructive and acted before asking:\n    %s"
                    % shlex.join(argv))
        if kw.pop("tty", False):
            return self.run_tty(argv, **kw)
        return self._effect_run(argv, **kw)

    def _effect_run(self, argv, **kw):
        return self.run(argv, **kw)

    def remove(self, path):
        if not self._would("remove", path):
            self.remove_now(path)

    def remove_own(self, path):
        self.remove_now(path)

    def mkdir(self, path):
        if not self._would("create", path):
            self.mkdir_now(path)

    def exec(self, argv, cwd=None, env=None):
        """Replaces this process, so the far side's tty and job control are the caller's own; a dry run prints it and ends."""
        if act.dry_run():
            sys.stderr.write("would run: %s%s\n" % ("cd %s && " % shlex.quote(cwd) if cwd else "", shlex.join(argv)))
            raise SystemExit(0)
        self._exec(argv, cwd, env)

    def _where(self):
        return "" if self.name == "here" else " on " + self.name

class Local(Machine):
    name = "here"

    def run(self, argv, input=None, timeout=None, stream=False):
        if stream:
            sys.stdout.flush()
            sys.stderr.flush()
        try:
            p = subprocess.Popen(argv, stdin=subprocess.PIPE if input is not None else None,
                                 stdout=2 if stream else subprocess.PIPE, stderr=None if stream else subprocess.PIPE,
                                 text=True, start_new_session=timeout is not None)
        except FileNotFoundError as e:
            return Result(127, "", str(e))
        try:
            out, err = p.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.communicate()
            return Result(TIMED_OUT, "", "timed out after %ss" % timeout)
        return Result(p.returncode, out or "", err or "")

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

    def read_tree(self, anchor, rel, patterns, depth=3):
        top = os.path.join(anchor, rel)
        fd = os.open(anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            path = anchor
            for part in (p for p in rel.split("/") if p):
                path = os.path.join(path, part)
                fd, parent = _open_at(fd, part, path, True), fd
                os.close(parent)
            out = {}
            _walk(fd, top, "", depth, patterns, out)
            return out
        finally:
            os.close(fd)

    def exists(self, path):
        return os.path.exists(path)

    def isdir(self, path):
        return os.path.isdir(path)

    def listdir(self, path):
        return sorted(os.listdir(path))

    def alive(self, pid):
        try:
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                return False
        except ChildProcessError:
            pass
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def have(self, tool):
        return shutil.which(tool) is not None

    def readlink(self, path):
        try:
            return os.readlink(path)
        except OSError:
            return None

    def mtime(self, path):
        return os.path.getmtime(path)

    def read_bytes(self, path, start=0):
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() + start) if start < 0 else start)
            return f.read()

    def write(self, path, text):
        if not self._would("write", path):
            replace_file(path, text)

    def write_own(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        replace_file(path, text)

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
        if not (act.dry_run() and os.path.isdir(path)):
            super().mkdir(path)

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
        if self._would("signal", "%d %s" % (pid, signal.Signals(sig).name)):
            return True
        try:
            os.kill(pid, sig)
            return True
        except ProcessLookupError:
            return False

    def spawn(self, argv, log):
        if self._would("start", "%s > %s" % (shlex.join(argv), log)):
            return 0
        with open(log, "ab") as f:
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        return p.pid

    def start(self, argv, out, cwd=None):
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, cwd=cwd)

    def _exec(self, argv, cwd, env):
        if cwd is not None:
            os.chdir(cwd)
        sys.stdout.flush()
        sys.stderr.flush()
        if env is None:
            os.execvp(argv[0], argv)
        else:
            os.execvpe(argv[0], argv, env)

    # Both ends are this host's own filesystem, so a real copy needs no transport.
    def copy_in(self, src, dest):
        if not self._would("copy", "%s -> %s" % (src, dest)):
            shutil.copyfile(src, dest)

    copy_out = copy_in

    def copy_tree_in(self, src, dest):
        self.copy_tree_out(src, dest)

    def copy_tree_out(self, src, dest, exclude=()):
        if self._would("copy", "%s -> %s/" % (src, dest)):
            return
        cp = subprocess.run(["rsync", "-a", "--delete", *excludes(exclude), src.rstrip("/") + "/", dest.rstrip("/") + "/"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if cp.returncode != 0:
            raise OSError(cp.stderr.decode(errors="replace").strip() or "rsync failed")


def _open_at(dirfd, name, path, directory):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_DIRECTORY if directory else 0), dir_fd=dirfd)
    except OSError as e:
        if e.errno != errno.ENOENT and stat.S_ISLNK(os.stat(name, dir_fd=dirfd, follow_symlinks=False).st_mode):
            raise Planted(path)
        raise
    mode = os.fstat(fd).st_mode
    if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
        os.close(fd)
        raise Planted(path)
    return fd


def _walk(dirfd, top, prefix, depth, patterns, out):
    with os.scandir(dirfd) as it:
        entries = sorted(it, key=lambda e: e.name)
    for e in entries:
        rel = prefix + e.name
        if e.is_symlink():
            raise Planted(os.path.join(top, rel))
        if e.is_dir(follow_symlinks=False):
            if rel.count("/") < depth - 1:
                sub = _open_at(dirfd, e.name, os.path.join(top, rel), True)
                try:
                    _walk(sub, top, rel + "/", depth, patterns, out)
                finally:
                    os.close(sub)
        elif matches(rel, patterns):
            fd = _open_at(dirfd, e.name, os.path.join(top, rel), False)
            with os.fdopen(fd, "rb") as f:
                out[rel] = f.read()


# The far side's half of read_tree: tar archives a link as a link, so the reading side refuses it off the archive.
FAR_TREE = r"""set -e
export COPYFILE_DISABLE=1
cd -- "$1"
d=$2 w=$1
shift 2
for p in "$@"; do
    w=$w/$p
    if [ -L "$p" ]; then printf 'wk-planted %s\n' "$w" >&2; exit 3; fi
    cd -- "$p"
done
t=$(mktemp)
trap 'rm -f "$t"' EXIT
find . -maxdepth "$d" \( -type f -o -type l \) -print0 | tar -cf "$t" --null -T -
base64 < "$t"
"""

# The far side's half of write: never a rename onto a link, which `mv` follows into the directory it names.
FAR_WRITE = r"""set -e
if [ -L "$1" ]; then printf '%s is a symbolic link, and nothing is written through one here\n' "$1" >&2; exit 3; fi
t=$(mktemp "$(dirname "$1")/.wk-write.XXXXXX")
trap 'rm -f "$t"' EXIT
chmod "$(umask -S)" "$t"
chmod a-x "$t"
cat > "$t"
mv -f -- "$t" "$1"
trap - EXIT
"""


def far_tree(out, top, patterns):
    tree = {}
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(out))) as t:
        for m in t.getmembers():
            rel = os.path.normpath(m.name)
            if os.path.isabs(rel) or rel.split("/")[0] == "..":
                raise OSError("the far side's archive names %s, outside %s" % (m.name, top))
            if m.issym() or not (m.isfile() or m.islnk()):
                raise Planted(os.path.join(top, rel))
            if matches(rel, patterns):
                try:
                    tree[rel] = t.extractfile(m).read()
                except KeyError:
                    raise OSError("the far side's archive links %s to %s, which it does not hold" % (m.name, m.linkname))
    return tree


def far_side_start(cmd, out_path, tail):
    """`cmd` detached, output in `out_path`, then `tail`; the subshell makes init its reaper (tart's guest agent reaps nothing)."""
    return "(nohup %s > %s 2>&1 < /dev/null & %s)" % (cmd, shlex.quote(out_path), tail)


LOGIN = '"$SHELL" -lc '
MTIME = "import os, sys; print(os.path.getmtime(sys.argv[1]))"


class Ssh(Machine):
    """A host over ssh, each call one bounded non-interactive round trip run by `via` (this host)."""

    def __init__(self, dest, opts=None, timeout=10, via=None, control_dir=None):
        self.dest = dest
        self.name = dest
        self.control_dir = control_dir
        self.opts = list(opts or []) + ["-o", "BatchMode=yes", "-o", "ConnectTimeout=%d" % timeout]
        self.via = via or Local()

    def _up(self):
        """ssh creates no ControlPath directory; it is made on `via` before the first ssh that names it."""
        if self.control_dir:
            self.via.mkdir_now(self.control_dir)

    def argv(self, remote, tty=False):
        # The far sshd runs a command under a non-login shell, so ~/.local/bin and Homebrew are off its PATH.
        return ["ssh"] + (["-t"] if tty else []) + self.opts + [self.dest, LOGIN + shlex.quote(remote)]

    def _ssh(self, remote, input=None, timeout=None):
        self._up()
        # An empty stdin, not the caller's: ssh drinks whatever it is handed.
        return self.via.run(self.argv(remote), input="" if input is None else input, timeout=timeout)

    def _via(self, how, argv, input, timeout, stream):
        self._up()
        return how(self.argv(shlex.join(argv)), input="" if input is None else input,
                   timeout=timeout, **({"stream": True} if stream else {}))

    def run(self, argv, input=None, timeout=None, stream=False):
        return self._via(self.via.run, argv, input, timeout, stream)

    def _effect_run(self, argv, input=None, timeout=None, stream=False):
        return self._via(self.via.act_run, argv, input, timeout, stream)

    def run_tty(self, argv, cwd=None, timeout=None):
        self._up()
        remote = shlex.join(argv)
        if cwd:
            remote = "cd %s && %s" % (shlex.quote(cwd), remote)
        return self.via.run_tty(self.argv(remote, tty=True), timeout=timeout)

    def read(self, path):
        r = self._ssh("cat %s" % shlex.quote(path))
        if not r.ok:
            raise OSError(r.err.strip() or "cannot read %s on %s" % (path, self.dest))
        return r.out

    def read_tree(self, anchor, rel, patterns, depth=3):
        parts = [p for p in rel.split("/") if p]
        r = self._ssh(shlex.join(["sh", "-c", FAR_TREE, "sh", anchor, str(depth)] + parts))
        if r.rc == 3 and "wk-planted " in r.err:
            raise Planted(r.err.split("wk-planted ", 1)[1].splitlines()[0])
        if not r.ok:
            raise OSError(r.err.strip() or "cannot read %s on %s" % (os.path.join(anchor, rel), self.dest))
        return far_tree(r.out, os.path.join(anchor, rel), patterns)

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

    def _far(self, remote, path):
        r = self._ssh(remote)
        if not r.ok:
            raise OSError(r.err.strip() or "cannot read %s on %s" % (path, self.dest))
        return r.out

    def mtime(self, path):
        return float(self._far(shlex.join(["python3", "-c", MTIME, path]), path))

    def read_bytes(self, path, start=0):
        # base64 on the far side: a byte range can split a character, and the round trip carries text.
        cut = "+%d" % (start + 1) if start >= 0 else "%d" % -start
        q = shlex.quote(path)
        return base64.b64decode(self._far("test -r %s && tail -c %s %s | base64" % (q, cut, q), path))

    def write(self, path, text):
        if not self._would("write", path):
            self.write_own(path, text, "")

    def write_own(self, path, text, mkdir='mkdir -p "$(dirname "$1")"\n'):
        r = self._ssh(shlex.join(("sh", "-c", mkdir + FAR_WRITE, "sh", path)), input=text)
        if not r.ok:
            raise OSError(r.err.strip())

    def remove_now(self, path):
        self._ssh("rm -rf %s" % shlex.quote(path))

    def mkdir_now(self, path):
        self._ssh("mkdir -p %s" % shlex.quote(path))

    def symlink(self, target, path):
        return self._ssh("ln -s %s %s" % (shlex.quote(target), shlex.quote(path))).ok

    def rename(self, path, dst):
        return self._ssh("mv -f %s %s" % (shlex.quote(path), shlex.quote(dst))).ok

    def kill(self, pid, sig=signal.SIGTERM):
        if self._would("signal", "%d %s" % (pid, signal.Signals(sig).name)):
            return True
        return self._ssh("kill -%d %d" % (sig, pid)).ok

    def spawn(self, argv, log):
        line = far_side_start(shlex.join(argv), log, "echo $!")
        if self._would("start", line):
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
            self._up()
            pid = self.via.spawn(["ssh", *self.opts, "-o", "ExitOnForwardFailure=yes", "-N", "-R",
                                  "127.0.0.1:%d:127.0.0.1:%d" % (port, port), self.dest], log)
            try:
                yield pid
            finally:
                if pid:
                    self.via.kill(pid)
        return held()

    # scp/rsync, run by `via` (this host) reaching `self.dest`: a byte pipe through this Machine's own `run` would
    # corrupt binary data both ways. --chmod=go-w: a tree crossing machines does not carry the pushing machine's umask.
    def _copy(self, argv, src, dest, tree=""):
        if self._would("copy", "%s -> %s%s" % (src, dest, tree)):
            return
        self._up()
        r = self.via.run(argv)
        if not r.ok:
            raise OSError(r.err.strip() or "copy between here and %s failed" % self.dest)

    def _rsync(self, src, dest, exclude=()):
        return ["rsync", "-a", "--chmod=go-w", "--delete", *excludes(exclude), "-e", "ssh " + shlex.join(self.opts),
                src.rstrip("/") + "/", dest.rstrip("/") + "/"]

    def copy_in(self, src, dest):
        self._copy(["scp", "-q", *self.opts, src, "%s:%s" % (self.dest, dest)], src, dest)

    def copy_out(self, src, dest):
        self._copy(["scp", "-q", *self.opts, "%s:%s" % (self.dest, src), dest], src, dest)

    def copy_tree_in(self, src, dest):
        self._copy(self._rsync(src, "%s:%s" % (self.dest, dest)), src, dest, "/")

    def copy_tree_out(self, src, dest, exclude=()):
        self._copy(self._rsync("%s:%s" % (self.dest, src), dest, exclude), src, dest, "/")


class PodmanVm(Ssh):
    """A macOS host's podman machine, where the container store is, over `podman machine ssh`: reads and small writes only."""

    def argv(self, remote, tty=False):
        return ["podman", "machine", "ssh", self.dest, "--", remote]

    def _refused(self, *a, **kw):
        raise NotImplementedError("a copy into or out of the podman machine goes through its mounts, not %s" % self.dest)

    run_tty = forward = copy_in = copy_out = copy_tree_in = copy_tree_out = _refused


class TartExec(Ssh):
    """A macOS guest through `tart exec`, the guest agent's channel: no network, so macOS local-network privacy never applies."""

    def __init__(self, tart, vm, via=None):
        super().__init__(vm, via=via)
        self.tart = tart

    def argv(self, remote, tty=False):
        return [self.tart, "exec", "-i"] + (["-t"] if tty else []) + [self.dest, "/bin/zsh", "-lc", remote]

    def _pipe(self, line, src, dest, *args):
        if self._would("copy", "%s -> %s" % (src, dest)):
            return
        r = self.via.run(["sh", "-c", "set -o pipefail; " + line, self.tart, self.dest, src, dest, *args])
        if not r.ok:
            raise OSError(r.err.strip() or "copy between here and %s failed" % self.dest)

    def copy_in(self, src, dest):
        self._pipe('"$0" exec -i "$1" /bin/sh -c \'cat > "$0"\' "$3" < "$2"', src, dest)

    def copy_out(self, src, dest):
        self._pipe('"$0" exec "$1" /bin/cat "$2" > "$3"', src, dest)

    def copy_tree_in(self, src, dest):
        self._pipe('tar -C "$2" -cf - . | "$0" exec -i "$1" /bin/sh -c \'rm -rf "$0" && mkdir -p "$0" && tar -C "$0" -xf -\' "$3"',
                   src, dest)

    def copy_tree_out(self, src, dest, exclude=()):
        """bsdtar matches an --exclude unanchored, so a pattern without a slash names a path at any depth, as rsync's does."""
        self._pipe('t=$0 g=$1 s=$2 d=$3; shift 3; "$t" exec "$g" /usr/bin/tar -cf - -C "$s" "$@" . '
                   '| { rm -rf "$d" && mkdir -p "$d" && tar -C "$d" -xf -; }',
                   src, dest, *excludes(exclude))

    def _refused(self, *a, **kw):
        raise NotImplementedError("a port forward into %s goes over its sshd (Vm.ssh_transport), not tart exec" % self.dest)

    forward = _refused


def excludes(patterns):
    return [w for x in patterns for w in ("--exclude", x)]


class Exited:
    def __init__(self, pid, rc):
        self.pid, self.returncode = pid, rc

    def poll(self):
        return self.returncode

    def wait(self):
        return self.returncode


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
        self.links = set()
        self.mtimes = {}
        self.pids = {os.getpid()}
        self.answers = []
        self.effects = []
        self.next_pid = 1000
        self.stop_after = None
        self.applied = 0
        self.streamed = []
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
        if not callable(best[1]):
            return best[1]
        seen = list(argv)
        if seen[0] == "ssh" and seen[-1].startswith(LOGIN):
            seen[-1] = shlex.split(seen[-1])[-1]
        return best[1](seen, self)

    def act_run(self, argv, **kw):
        self._acting = True
        try:
            return super().act_run(argv, **kw)
        finally:
            self._acting = False

    def run(self, argv, input=None, timeout=None, stream=False):
        self.record_run(argv)
        r = self._answer(argv)
        if not stream:
            return r
        self.streamed.append(tuple(argv))
        sys.stderr.write(r.out + r.err)
        return Result(r.rc)

    def run_tty(self, argv, cwd=None, timeout=None):
        self._record(("run_tty", tuple(argv), cwd))
        return self._answer(argv)

    def read(self, path):
        if path not in self.files:
            raise OSError("no such file: %s" % path)
        return self.files[path]

    def read_tree(self, anchor, rel, patterns, depth=3):
        top = os.path.join(anchor, rel).rstrip("/")
        path = anchor.rstrip("/")
        for part in (p for p in rel.split("/") if p):
            path += "/" + part
            if path in self.links:
                raise Planted(path)
        if top not in self.dirs:
            raise OSError("no such directory: %s" % top)
        out = {}
        for p in sorted(self.files):
            r = p[len(top) + 1:]
            if not p.startswith(top + "/") or r.count("/") >= depth:
                continue
            if p in self.links:
                raise Planted(p)
            if matches(r, patterns):
                data = self.files[p]
                out[r] = data if isinstance(data, bytes) else data.encode()
        return out

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

    def mtime(self, path):
        if path not in self.files:
            raise OSError("no such file: %s" % path)
        return self.mtimes.get(path, 0.0)

    def read_bytes(self, path, start=0):
        data = self.read(path)
        return (data if isinstance(data, bytes) else data.encode())[start:]

    def _set_file(self, path, text):
        self.files[path] = text
        self.links.discard(path)
        parent = os.path.dirname(path)
        while parent and parent != "/":
            self.dirs.add(parent)
            parent = os.path.dirname(parent)

    def write(self, path, text):
        self.effect(("write", path))
        if act.dry_run():
            return
        self._set_file(path, text)

    def write_own(self, path, text):
        self._set_file(path, text)

    def remove_own(self, path):
        self._drop(path)

    def _drop(self, path):
        self.files = {p: t for p, t in self.files.items() if p != path and not p.startswith(path.rstrip("/") + "/")}
        self.links &= set(self.files)
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
        self.links.add(path)
        return True

    def rename(self, path, dst):
        self.effect(("rename", path, dst))
        if path not in self.files:
            return False
        self.files[dst] = self.files.pop(path)
        self.links.discard(dst)
        if path in self.links:
            self.links.discard(path)
            self.links.add(dst)
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

    def start(self, argv, out, cwd=None):
        self.effect(("start", tuple(argv)))
        r = self._answer(argv)
        out.write((r.out + r.err).encode())
        self.next_pid += 1
        return Exited(self.next_pid, r.rc)

    def _exec(self, argv, cwd, env):
        self.effect(("exec", tuple(argv), cwd))

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
