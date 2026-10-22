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
    """`path` holds `data` whole or keeps what it held, across a power loss too: the temp is mkstemp's (O_EXCL, never a
    planted name), synced before the rename, which replaces a link at `path` rather than writing through it and is synced
    in its directory. `mode` is the file's, else the umask's."""
    parent = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".%s." % os.path.basename(path))
    try:
        if mode is None:
            mask = os.umask(0)
            os.umask(mask)
            mode = 0o666 & ~mask
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb" if isinstance(data, bytes) else "w") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise
    dfd = os.open(parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def lib_argv(root, rel, fn, *args):
    return ["bash", "-c", '. "$0"; %s "$@"' % fn, os.path.join(str(root), rel), *args]


ISOLATED = "import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); runpy.run_module(sys.argv.pop(1), run_name=__name__, alter_sys=True)"


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
            act.die("BUG: this command is declared destructive and acted before asking:\n    %s" % shlex.join(argv))
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

    def ensure_dir_mode(self, path, mode):
        self.mkdir(path)
        if not self.run(["find", path, "-maxdepth", "0", "-perm", mode]).out.strip():
            self.act_run(["chmod", mode, path])

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
        except BaseException:
            if timeout is not None:     # a session of its own: an interrupt here never reached it
                os.killpg(p.pid, signal.SIGKILL)
            raise
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
    """A host over ssh, or `podman`'s or `tart`'s channel: each call one bounded round trip run by `via` (this host)."""

    def __init__(self, dest, opts=None, timeout=10, via=None, control_dir=None, channel=None, kind="ssh"):
        self.dest = dest
        self.name = dest
        self.control_dir = control_dir
        self.opts = list(opts or []) + ["-o", "BatchMode=yes", "-o", "ConnectTimeout=%d" % timeout]
        self.via = via or Local()
        self.channel = channel
        self.kind = kind

    @classmethod
    def podman(cls, name, via=None):
        return cls(name, via=via, kind="podman", channel=lambda tty: ["podman", "machine", "ssh", name, "--"])

    @classmethod
    def tart(cls, tart, vm, via=None):
        """A macOS guest through `tart exec`, the guest agent's channel: no network, so local-network privacy never applies."""
        return cls(vm, via=via, kind="tart",
                   channel=lambda tty: [tart, "exec", "-i"] + (["-t"] if tty else []) + [vm, "/bin/zsh", "-lc"])

    def _up(self):
        """ssh creates no ControlPath directory; it is made on `via` before the first ssh that names it."""
        if self.control_dir:
            self.via.mkdir_now(self.control_dir)

    def argv(self, remote, tty=False):
        if self.channel:
            return self.channel(tty) + [remote]
        # The far sshd runs a command under a non-login shell, so ~/.local/bin and Homebrew are off its PATH.
        return ["ssh"] + ({"force": ["-tt"], True: ["-t"]}.get(tty, [])) + self.opts + [self.dest, LOGIN + shlex.quote(remote)]

    def _ssh(self, remote, input=None, timeout=None, how=None, stream=False):
        self._up()
        return (how or self.via.run)(self.argv(remote), input="" if input is None else input, timeout=timeout,
                                     **({"stream": True} if stream else {}))

    def run(self, argv, input=None, timeout=None, stream=False):
        return self._ssh(shlex.join(argv), input, timeout, self.via.run, stream)

    def _effect_run(self, argv, input=None, timeout=None, stream=False):
        return self._ssh(shlex.join(argv), input, timeout, self.via.act_run, stream)

    def run_tty(self, argv, cwd=None, timeout=None):
        self._up()
        remote = shlex.join(argv)
        if cwd:
            remote = "cd %s && %s" % (shlex.quote(cwd), remote)
        return self.via.run_tty(self.argv(remote, tty=True), timeout=timeout)

    def read(self, path):
        return self._far("cat %s" % shlex.quote(path), path)

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

        if self.channel:
            raise NotImplementedError("a port forward into %s goes over its sshd, not %s" % (self.dest, self.kind))

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

    # A pipe run by `via` through the channel: bytes through this Machine's `run` would be decoded as text.
    def _pipe(self, line, local, remote, src, dest):
        if self._would("copy", "%s -> %s" % (src, dest)):
            return
        self._up()
        r = self.via.run(["bash", "-c", "set -o pipefail; f=$1; shift; " + line, "bash", local] + self.argv(remote))
        if not r.ok:
            raise OSError(r.err.strip() or "copy between here and %s failed" % self.dest)

    def copy_in(self, src, dest):
        self._pipe('"$@" < "$f"', src, "cat > %s" % shlex.quote(dest), src, dest)

    def copy_out(self, src, dest):
        self._pipe('"$@" < /dev/null > "$f"', dest, "cat %s" % shlex.quote(src), src, dest)

    def copy_tree_in(self, src, dest):
        q = shlex.quote(dest)
        self._pipe('COPYFILE_DISABLE=1 tar -C "$f" -cf - . | "$@"', src,
                   "rm -rf %s && mkdir -p %s && tar -C %s -xf -" % (q, q, q), src, dest + "/")

    def copy_tree_out(self, src, dest, exclude=()):
        self._pipe('rm -rf "$f" && mkdir -p "$f" && "$@" < /dev/null | tar -C "$f" -xf -', dest,
                   shlex.join(["env", "COPYFILE_DISABLE=1", "tar", "-cf", "-", "-C", src, *excludes(exclude), "."]),
                   src, dest + "/")


def excludes(patterns):
    return [w for x in patterns for w in ("--exclude", x)]


def here():
    return Local()
