#!/usr/bin/env python3
"""One digest over a directory's file names, contents, symlinks and exec bits, so a tree copied onto a machine can be compared with the tree it came from; travels on stdin, so the far side needs no copy of wk-tools to run it."""
import argparse
import hashlib
import os
import sys

CHUNK = 1 << 20


def digest(root, exclude):
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in exclude)
        for name in sorted(filenames):
            if name in exclude:
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root).encode()
            if os.path.islink(path):
                h.update(b"l\0" + rel + b"\0" + os.readlink(path).encode() + b"\0")
                continue
            h.update(b"f\0" + rel + b"\0")   # the exec bit is content: a `wk` that arrived unexecutable is a tree that looks right and runs nothing
            h.update(b"x" if os.access(path, os.X_OK) else b"-")
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(CHUNK), b""):
                    h.update(chunk)
            h.update(b"\0")
    return h.hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("root")
    p.add_argument("--exclude", action="append", default=[], metavar="NAME",
                   help="a name to leave out wherever it appears; repeatable, and "
                        "both sides must exclude the same ones")
    args = p.parse_args()
    if not os.path.isdir(args.root):
        return 1
    print(digest(args.root, set(args.exclude)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
