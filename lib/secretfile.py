#!/usr/bin/env python3
"""Read, write and test one credential file, refusing anything that is not one.

    secretfile.py read    <path>    its bytes on stdout, nothing for absent
    secretfile.py present <path>    exit 0 if there is one and it is not empty
    secretfile.py write   <path>    stdin into it, 0600

Why this is not `cat`, `[ -s ]` and a redirect. One of the directories these
paths land in -- `agent-rw` (wk_agent_rw_dir, lib/store.sh) -- is mounted
read-write into every container, because the Claude CLI rotates the claude.ai
login in place and every holder has to be looking at one set of bytes. It is a
sibling of `push-keys`, which holds the private deploy keys and the GitHub
token and is mounted nowhere. A workspace that replaces a file in the writable
directory with a link

    ln -sfn ../push-keys/github-pat /agent-rw/.credentials.json

turns every host-side read of it into a read of the token -- `wk vm start`
copies what it reads into a guest -- and every host-side write into a write
through the link, so `wk key set claude-login` would overwrite the token.

So every read and every write opens with O_NOFOLLOW and then asks the open
file descriptor, not the path, three questions: is it a regular file, is it
this user's, and is it the only name for its inode. A symlink fails the open;
a hard link to a file outside the directory fails st_nlink. Refusing is the
whole answer -- there is nothing to repair, because nothing wk wrote is ever
any of those things.

structured/unsafe IO in python and not in bash (CLAUDE.md): O_NOFOLLOW has no
spelling in the shell at all.
"""

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
    """The descriptor, or None for a path there is nothing at. Absent is not an
    error: every caller reports its own absence in its own words. Unreadable is
    absent too, which is what `[ -r ]` said before this."""
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
    # Checked before the truncate, so a refusal leaves whatever is there
    # exactly as it was.
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
