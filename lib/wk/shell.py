"""The bridge into the bash library: `ask` returns a function's stdout, `run` its exit status, `exec_fn` becomes it."""

import os
import signal
import subprocess
import sys

from wk import act
from wk.machine import Result, TIMED_OUT

LIBS = ("lib/common.sh", "lib/target.sh", "lib/store.sh")


def prelude(root):
    return "".join('. "%s/%s"\n' % (root, lib) for lib in LIBS)


def _script(fn, root):
    return prelude(root) + fn + ' "$@"\n'


def argv(root, fn, *args):
    return ["bash", "-c", _script(fn, root), "wk", *args]


def ask(root, fn, *args, env=None, quiet=False):
    cp = subprocess.run(argv(root, fn, *args), stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL if quiet else None, text=True, env=env or os.environ)
    if cp.returncode != 0:
        return None
    return cp.stdout.strip()


def need(root, fn, *args, env=None):
    """`ask` for text a caller runs or acts on: a failing function refuses rather than answer ""."""
    out = ask(root, fn, *args, env=env)
    if out is None:
        act.die("the bash function %s failed (its error is above); fix what it names and re-run" % fn)
    return out


def run(root, fn, *args, env=None, quiet=False):
    return subprocess.call(argv(root, fn, *args),
                           stdout=subprocess.DEVNULL if quiet else None, stderr=subprocess.DEVNULL if quiet else None,
                           env=env or os.environ)


def exec_fn(root, fn, *args):
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvp("bash", argv(root, fn, *args))


def sh_quote(*args):
    return " ".join("'" + a.replace("'", "'\\''") + "'" for a in args)


def caller_shell(env):
    state = env.get("WK_CALLER_SHELL")
    return CallerShell(state) if state else None


class CallerShell:
    """A bash caller's own `t_exec`, re-entered from the shell state its shim dumped (lib/detach.sh's _caller_shell)."""

    def __init__(self, state):
        self.state = state

    def call(self, fn, args, timeout=None, quiet=False):
        argv = ["bash", "-c", '. "$0" 2>/dev/null; %s "$@"' % fn, self.state] + [str(a) for a in args]
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL if quiet else None, text=True, start_new_session=True)
        try:
            out, _ = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.communicate()
            return Result(TIMED_OUT)
        return Result(p.returncode, out)

    def exec(self, ws, argv, timeout=None):
        return self.call("t_exec", [ws] + list(argv), timeout, quiet=True)

    def act_exec(self, ws, argv):
        if act.dry_run():
            sys.stderr.write("would run in %s: %s\n" % (ws, " ".join(argv)))
            return Result(0)
        return self.exec(ws, argv)

    def task_put(self, ws, task):
        self.call("t_task_put", [ws, str(task.path)])

    def ask(self, ws, pid, cap):
        r = self.exec(ws, ["kill", "-0", str(pid)], timeout=cap)
        return True if r.rc == 0 else False if r.rc == 1 else None


def gh_authenticated(root, env=None):
    """Whether `gh` is on PATH and its token still answers: `gh auth status` exits 0 for a configured
    account whose token has expired, so the api call is the one that means anything."""
    env = os.environ if env is None else env
    exe = None
    for d in (env.get("PATH") or "").split(os.pathsep):
        p = os.path.join(d, "gh")
        if os.path.isfile(p) and os.access(p, os.X_OK):
            exe = p
            break
    if not exe:
        return False
    return subprocess.run([exe, "api", "user"], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, env=env).returncode == 0





def priv_helpers(root, env=None):
    out = ask(root, "_ph() { wk_priv_helpers | while read -r n w what; do if [ -n \"$n\" ]; then "
                    "printf '%s\\t%s\\t%s\\t%s\\t%s\\n' \"$n\" \"$w\" \"$what\" \"$(wk_priv_path \"$n\")\" \"$(wk_priv_sudoers \"$n\")\"; fi; done; }; _ph",
              env=env)
    return [tuple(line.split("\t")) for line in (out or "").splitlines() if line.count("\t") == 4]


def priv_answers(root, path, env=None):
    return run(root, "wk_priv_answers", path, env=env, quiet=True) == 0


GPU_FLAGS_FN = '. "$WK_ROOT/host/linux/gpu.sh"; gpu_flags'


def gpu_flags(root, machine):
    r = machine.run(argv(root, GPU_FLAGS_FN))
    sys.stderr.write(r.err)
    return r.out.split() if r.ok else []


def ccache_conf(root, env=None):
    return need(root, "ccache_conf_render", env=env) + "\n"






def bench_arms(root, *args):
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvp("bash", ["bash", os.path.join(str(root), "lib", "bench-arms.sh"), *args])


def sysimage_arms(root, *args):
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvp("bash", ["bash", os.path.join(str(root), "lib", "sysimage-arms.sh"), *args])
