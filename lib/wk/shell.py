"""The bridge into the bash library: `ask` returns a function's stdout, `run` its exit status, `exec_fn` becomes it."""

import os
import subprocess
import sys

LIBS = ("lib/common.sh", "lib/target.sh", "lib/store.sh")


def prelude(root):
    return "".join('. "%s/%s"\n' % (root, lib) for lib in LIBS)


def _script(fn, root):
    return prelude(root) + fn + ' "$@"\n'


def ask(root, fn, *args, env=None, quiet=False):
    cp = subprocess.run(["bash", "-c", _script(fn, root), "wk", *args], stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL if quiet else None, text=True, env=env or os.environ)
    if cp.returncode != 0:
        return None
    return cp.stdout.strip()


def run(root, fn, *args, env=None, quiet=False):
    return subprocess.call(["bash", "-c", _script(fn, root), "wk", *args],
                           stdout=subprocess.DEVNULL if quiet else None, stderr=subprocess.DEVNULL if quiet else None,
                           env=env or os.environ)


def exec_fn(root, fn, *args):
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvp("bash", ["bash", "-c", _script(fn, root), "wk", *args])


def sh_quote(*args):
    return " ".join("'" + a.replace("'", "'\\''") + "'" for a in args)


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


def guest_stop(root, name):
    return run(root, '. "$WK_ROOT/targets/vm.sh"; t_stop', name)


def guest_start(root, name):
    return run(root, '. "$WK_ROOT/targets/vm.sh"; t_start', name)


def peer_src(root, target, name):
    return ask(root, "load_target %s >/dev/null 2>&1; t_src" % sh_quote(target), name)


def in_machine(root, command, env=None, quiet=False):
    return ask(root, "_in_machine", command, env=env, quiet=quiet)


def gh_authenticated(root, env=None):
    return run(root, "gh_authenticated", env=env, quiet=True) == 0


def mirror_branches(root, env=None):
    return (ask(root, "wk_mirror_branches", env=env) or "").split()


def local_state_paths(root, env=None):
    out = ask(root, "_paths() { printf 'push_held=%s\\nread_pat=%s\\ntailscale_api=%s\\nntfy_topic=%s\\n' "
                    '"$(wk_push_held_dir)" "$(push_agent_machine_read_pat)" "$(wk_tailscale_api_path)" "$(wk_ntfy_topic_path)"; '
                    "for n in $(wk_agent_secret_names); do printf 'secret.%s=%s\\n' \"$n\" \"$(wk_agent_secret_path \"$n\")\"; done; }; _paths",
              env=env)
    return dict(line.partition("=")[::2] for line in (out or "").splitlines() if "=" in line)


def cred_settable(root, env=None):
    return (ask(root, "wk_cred_settable", env=env) or "").split()


def cred_verdict(root, name, env=None):
    return ask(root, "wk_cred_check", name, "--stored", env=env) or ""


def peer_workstations(root, env=None):
    return (ask(root, "peer_workstations", env=env, quiet=True) or "").split()


def peer_cred_verdict(root, peer, name, env=None):
    """The verdict line either way: a peer that does not answer gets `unverified` with the reason."""
    return ask(root, '_pv() { peer_cred_verdict "$@" || true; }; _pv', peer, name, env=env, quiet=True) or ""


def priv_helpers(root, env=None):
    out = ask(root, "_ph() { wk_priv_helpers | while read -r n w what; do if [ -n \"$n\" ]; then "
                    "printf '%s\\t%s\\t%s\\t%s\\t%s\\n' \"$n\" \"$w\" \"$what\" \"$(wk_priv_path \"$n\")\" \"$(wk_priv_sudoers \"$n\")\"; fi; done; }; _ph",
              env=env)
    return [tuple(line.split("\t")) for line in (out or "").splitlines() if line.count("\t") == 4]


def priv_answers(root, path, env=None):
    return run(root, "wk_priv_answers", path, env=env, quiet=True) == 0


def remote_probe(root, target, env=None):
    return ask(root, '. "$WK_ROOT/remote/deps.sh"; load_target %s >/dev/null 2>&1; wk_remote_probe' % sh_quote(target), env=env, quiet=True) or ""


def remote_findings(root, probe, env=None):
    return ask(root, '. "$WK_ROOT/remote/deps.sh"; wk_remote_findings', probe, env=env) or ""


def remote_provision_stale(root, target, env=None):
    """Why the machine's provisioning predates this tree, or None when it does not."""
    return ask(root, "load_target %s >/dev/null 2>&1; remote_provision_stale" % sh_quote(target), env=env, quiet=True)


def vm_base_findings(root, env=None):
    return ask(root, "load_target vm >/dev/null 2>&1; vm_base_findings", env=env, quiet=True) or ""
