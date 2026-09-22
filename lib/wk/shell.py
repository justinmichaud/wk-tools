"""The bridge into the bash library, until the target layer is Python:
`ask` returns a function's stdout, `run` its exit status, `exec_fn` becomes it."""

import os
import subprocess
import sys

LIBS = ("lib/common.sh", "lib/target.sh", "lib/store.sh")


def prelude(root):
    return "".join('. "%s/%s"\n' % (root, lib) for lib in LIBS)


def _script(fn, root):
    return prelude(root) + fn + ' "$@"\n'


def ask(root, fn, *args, env=None):
    cp = subprocess.run(["bash", "-c", _script(fn, root), "wk", *args],
                        stdout=subprocess.PIPE, text=True, env=env or os.environ)
    if cp.returncode != 0:
        return None
    return cp.stdout.strip()


def run(root, fn, *args, env=None):
    return subprocess.call(["bash", "-c", _script(fn, root), "wk", *args],
                           env=env or os.environ)


def exec_fn(root, fn, *args):
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvp("bash", ["bash", "-c", _script(fn, root), "wk", *args])


def sh_quote(*args):
    return " ".join("'" + a.replace("'", "'\\''") + "'" for a in args)


# -- the target questions a Python command still asks the bash library

def _in_target(root, target, fn, *args, **kw):
    return ask(root, "load_target %s >/dev/null 2>&1; %s" % (sh_quote(target), fn), *args, **kw)


def ws_target(root, name):
    return ask(root, "ws_target", name)


def ws_info(root, target, name):
    return _in_target(root, target, "t_info", name)


def ws_stop(root, target, name):
    return run(root, "load_target %s >/dev/null 2>&1; t_stop" % sh_quote(target), name)


def rc_stop(root, target, name):
    return run(root, 'load_target %s >/dev/null 2>&1; . "$WK_ROOT/lib/watchdog.sh"; rc_stop' % sh_quote(target), name)


def target_pid_alive(root, name, pid, cap):
    script = _script("load_target \"$(ws_target \"$1\")\" >/dev/null 2>&1; t_exec", root)
    try:
        cp = subprocess.run(["bash", "-c", script, "wk", name, "kill", "-0", str(pid)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=cap)
    except subprocess.TimeoutExpired:
        return None
    return cp.returncode == 0


def machines(root):
    out = ask(root, "target_all")
    return [t for t in (out.split() if out else []) if t not in ("container", "vm", "local")]


def machine_answers(root, machine):
    cp = subprocess.run(["bash", "-c", _script("load_target %s >/dev/null 2>&1; machine_answers" % sh_quote(machine), root),
                         "wk", machine], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return cp.returncode == 0, cp.stdout


def machine_wk(root, machine, *args, env=None, quiet=False):
    cp = subprocess.run(["bash", "-c", _script("load_target %s >/dev/null 2>&1; t_wk" % sh_quote(machine), root),
                         "wk", *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL if quiet else subprocess.STDOUT,
                        text=True, env=env or os.environ)
    return cp.returncode, cp.stdout


def guests(root):
    out = ask(root, '. "$WK_ROOT/targets/vm.sh"; _tart_bin >/dev/null 2>&1 || exit 0; t_list 2>/dev/null')
    rows = []
    for line in (out or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            rows.append((parts[0], parts[1]))
    return rows


def guest_stop(root, name):
    return run(root, '. "$WK_ROOT/targets/vm.sh"; t_stop', name)


def machine_state(root, machine):
    return ask(root, "_machine_state", machine) or "absent"


def in_machine(root, command):
    return ask(root, "_in_machine", command)
