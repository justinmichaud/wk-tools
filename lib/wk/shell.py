"""The bridge into the bash library, until the target layer is Python.

`ask` runs one library function and returns its stdout; `run` runs one and
returns its exit status with output passing through; `exec_fn` replaces this
process with one. The libraries sourced are the ones the bash dispatcher
sourced, so a function answers exactly as it did there.
"""

import os
import subprocess
import sys

LIBS = ("lib/common.sh", "lib/target.sh", "lib/store.sh")


def _script(fn, root):
    src = "".join('. "%s/%s"\n' % (root, lib) for lib in LIBS)
    return src + fn + ' "$@"\n'


def ask(root, fn, *args, env=None):
    """The function's stdout, stripped; None when it failed."""
    cp = subprocess.run(["bash", "-c", _script(fn, root), "wk", *args],
                        stdout=subprocess.PIPE, text=True, env=env or os.environ)
    if cp.returncode != 0:
        return None
    return cp.stdout.strip()


def run(root, fn, *args, env=None):
    """The function's exit status; its output goes where ours does."""
    return subprocess.call(["bash", "-c", _script(fn, root), "wk", *args],
                           env=env or os.environ)


def exec_fn(root, fn, *args):
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvp("bash", ["bash", "-c", _script(fn, root), "wk", *args])


def sh_quote(*args):
    return " ".join("'" + a.replace("'", "'\\''") + "'" for a in args)
