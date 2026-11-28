"""The suite's guard, installed before any test is imported: no WK_* variable of the caller's reaches a test, HOME is
an empty directory, and every real machine tool on PATH is a shim that fails loudly."""

import atexit
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MACHINE_TOOLS = ("ssh", "scp", "podman", "pueue", "pueued", "tailscale", "zed", "ansible-playbook")
REACHED = 97
REAL_PATH = os.environ["PATH"]      # where a test marked live finds a real tool

sys.path.insert(0, os.path.join(ROOT, "lib"))
# The shell's own git settings (GIT_CONFIG_COUNT and its pairs, GIT_CONFIG_PARAMETERS) reach a real git as its command line.
for _var in [v for v in os.environ if v.startswith(("WK_", "GIT_CONFIG"))]:
    del os.environ[_var]
_scratch = tempfile.mkdtemp(prefix="wk-selftest-")
atexit.register(shutil.rmtree, _scratch)
os.environ["HOME"] = os.path.join(_scratch, "home")
os.makedirs(os.path.join(_scratch, "shims"))
os.makedirs(os.environ["HOME"])
for _tool in MACHINE_TOOLS:
    _path = os.path.join(_scratch, "shims", _tool)
    with open(_path, "w") as _f:
        _f.write('#!/bin/sh\necho "a unit test reached the real %s: $*" >&2\nexit %d\n' % (_tool, REACHED))
    os.chmod(_path, 0o755)
os.environ["PATH"] = os.path.join(_scratch, "shims") + os.pathsep + os.environ["PATH"]
