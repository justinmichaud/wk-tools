#!/usr/bin/env python3
"""read|present|write one credential file. Every open is O_NOFOLLOW and the fd is
checked regular/ours/st_nlink==1: agent-rw is mounted read-write into every container, where a workspace could otherwise aim a credential at push-keys."""

import os
import stat
import sys


def _refuse(verb, path, what):
    sys.stderr.write(
        "wk: refusing to %s %s: %s.\n"
        "    A workspace put something there that is not a file. Remove it;\n"
        "    nothing wk writes to that path is ever a link or a shared inode.\n"
        % (verb, path, what))
    raise SystemExit(2)


def _check(fd, verb, path):
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        _refuse(verb, path, "it is not a regular file")
    if st.st_uid != os.geteuid():
        _refuse(verb, path, "it is owned by uid %d, not by this user (uid %d)"
                % (st.st_uid, os.geteuid()))
    if st.st_nlink != 1:
        _refuse(verb, path, "it has %d hard links, so the same bytes have "
                            "another name elsewhere" % st.st_nlink)
    return st


def _open_read(verb, path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NOCTTY)
    except FileNotFoundError:
        return None
    except NotADirectoryError:
        return None
    except PermissionError:
        return None
    except OSError as exc:
        _refuse(verb, path, "opening it failed: %s" % exc.strerror)
    _check(fd, verb, path)
    return fd


def read(path):
    fd = _open_read("read", path)
    if fd is None:
        return 0
    try:
        with os.fdopen(fd, "rb") as f:
            sys.stdout.buffer.write(f.read())
    finally:
        sys.stdout.buffer.flush()
    return 0


def present(path):
    fd = _open_read("read", path)
    if fd is None:
        return 1
    try:
        return 0 if os.fstat(fd).st_size > 0 else 1
    finally:
        os.close(fd)


def write(path):
    data = sys.stdin.buffer.read()
    try:
        fd = os.open(path,
                     os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NOCTTY,
                     0o600)
    except OSError as exc:
        _refuse("write", path, "opening it failed: %s" % exc.strerror)
    # Before the truncate, so a refusal leaves what is there exactly as it was.
    _check(fd, "write", path)
    with os.fdopen(fd, "wb") as f:
        os.ftruncate(f.fileno(), 0)
        f.write(data)
        os.fchmod(f.fileno(), 0o600)
    return 0


VERBS = {"read": read, "present": present, "write": write}


def main(argv):
    if len(argv) != 3 or argv[1] not in VERBS:
        sys.stderr.write("usage: secretfile.py read|present|write <path>\n")
        return 2
    return VERBS[argv[1]](argv[2])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
