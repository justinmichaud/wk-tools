"""The bridge into the bash library: `ask` returns a function's stdout, `run` its exit status, `exec_fn` becomes it."""

import os
import subprocess
import sys

from wk import act

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


def target_pid_alive(root, name, pid, cap):
    script = _script("load_target \"$(ws_target \"$1\")\" >/dev/null 2>&1; t_exec", root)
    try:
        cp = subprocess.run(["bash", "-c", script, "wk", name, "kill", "-0", str(pid)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=cap)
    except subprocess.TimeoutExpired:
        return None
    return cp.returncode == 0


def session_mode_warn(root, env=None):
    run(root, "session_mode_warn", env=env)


def session_mode(root, env=None):
    return ask(root, "session_mode", env=env) or ""


def bmc_drm_device(root, env=None):
    return ask(root, "bmc_drm_device", env=env)


def lldb_prelude(root):
    return need(root, "lldb_prelude")


# `~/.lldbinit` sets follow-fork-mode child; `-O` runs after the init file.
LLDB_PIN_OPTS = "-O 'settings set target.process.follow-fork-mode parent'"


def vm_login_note(root, env=None):
    run(root, '. "$WK_ROOT/targets/vm.sh"; vm_login_note', env=env)


def guest_admit(root, name, env=None):
    return run(root, _vm('_admit() { _check_guest_limit && _check_memory_budget "$1" "$(t_mem_mb "$1")" '
                         '&& _check_host_disk; }; _admit'), name, env=env)


def guest_step(root, fn, name, ip, env=None):
    return run(root, _vm(fn), name, ip, env=env) == 0


def in_machine(root, command, env=None, quiet=False):
    return ask(root, "_in_machine", command, env=env, quiet=quiet)


def gh_authenticated(root, env=None):
    return run(root, "gh_authenticated", env=env, quiet=True) == 0





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


def vm_base_findings(root, env=None):
    return ask(root, "load_target vm >/dev/null 2>&1; vm_base_findings", env=env, quiet=True) or ""


GPU_FLAGS_FN = '. "$WK_ROOT/host/linux/gpu.sh"; gpu_flags'


def gpu_flags(root, machine):
    r = machine.run(argv(root, GPU_FLAGS_FN))
    sys.stderr.write(r.err)
    return r.out.split() if r.ok else []


def ccache_conf(root, env=None):
    return need(root, "ccache_conf_render", env=env) + "\n"


# lib/wk/targets.py's, build.py's and doctor.py's names for lib/wk/git.py, imported here since git.py imports this module.
def mirror_branches(root, env=None):
    from wk import git
    return git.mirror_branches(env)


def mirror_refresh_script(root, mirror_dir, env=None):
    from wk import git
    return git.mirror_refresh_script(mirror_dir, git.mirror_branches(env))


def wiring_script(root, src, mirror_dir, extra_name="", extra_url="", ssh_config="", env=None):
    from wk import git
    forks = [tuple(line.split()) for line in need(root, "wk_push_forks", env=env).splitlines() if line.split()]
    return git.wiring_script(src, mirror_dir, forks, git.mirror_branches(env), extra_name, extra_url, ssh_config)


def _would(what):
    """A bash writer that does not go through `act` is named under --dry-run and not run."""
    if act.dry_run():
        sys.stderr.write("would run: %s\n" % what)
        return True
    return False


def _vm(fn):
    return '_vm() { load_target vm >/dev/null 2>&1 || return 1; %s "$@"; }; _vm' % fn


def vm_ensure_base(root, env=None):
    if _would("vm_ensure_base"):
        return 0
    return run(root, '. "$WK_ROOT/targets/vm.sh"; _ensure_base', env=env)


def vm_base_stale(root, env=None):
    return ask(root, '. "$WK_ROOT/targets/vm.sh"; vm_base_stale', env=env, quiet=True)


def _on(target, fn):
    return "load_target %s >/dev/null 2>&1; %s" % (sh_quote(target), fn)


def _said(r):
    sys.stderr.write(r.out + r.err)
    return r


def _refuse_unless(r):
    if not r.ok:
        raise act.Refused(r.rc or 1)


def arch_canon(root, machine, arch):
    r = machine.run(argv(root, '. "$WK_ROOT/lib/arch.sh"; arch_canon', arch))
    sys.stderr.write(r.err)
    _refuse_unless(r)
    return r.out.strip()






def _rows(r):
    return [line.split() for line in r.out.splitlines() if line.split()]


def agent_secrets(root, machine):
    """wk_agent_secrets' rows: name, store file, home path, variable, value|file, the target kinds it reaches."""
    return _rows(machine.run(argv(root, "wk_agent_secrets")))


def push_forks(root, machine):
    return _rows(machine.run(argv(root, "wk_push_forks")))


def agent_secret_stored(root, machine, secret):
    return machine.run(argv(root, "wk_agent_secret_present", secret)).ok


def agent_secret_store_remedy(root, machine, secret):
    return machine.run(argv(root, "agent_secret_store_remedy", secret)).out.strip()


def arch_has_gpu(root, machine, arch):
    return machine.run(argv(root, '. "$WK_ROOT/lib/arch.sh"; arch_has_gpu', arch)).ok






def store_is_local(root, machine):
    return machine.run(argv(root, "store_is_local")).ok


def tailnet_key_present(root, machine):
    return machine.run(argv(root, "wk_tailscale_authkey_present")).ok


def tailnet_api_present(root, machine):
    return machine.run(argv(root, "wk_tailscale_api_present")).ok


def tailnet_authkey(root, machine):
    """The auth key file, minted first when this machine holds the API credential; "" when there is none."""
    r = machine.act_run(argv(root, "wk_tailscale_authkey"))
    sys.stderr.write(r.err)
    return r.out.strip() if r.ok else ""


def tailnet_retire(root, machine, name):
    return machine.act_run(argv(root, "wk_tailnet_retire", name))



def target_size(root, machine, target, ws):
    r = machine.run(argv(root, _on(target, 'printf "%s %s\\n" "$(t_cores "$1")" "$(t_mem_mb "$1")"'), ws))
    _refuse_unless(r)
    cores, mem = (r.out.split() + ["", ""])[:2]
    if not (cores.isdigit() and mem.isdigit()):
        act.die("could not read how big '%s' is (t_cores/t_mem_mb said %r)" % (ws, r.out.strip()))
    return int(cores), int(mem)


def origin_branch_fetch_step(root, machine, branch, mirror):
    from wk import git
    return git.origin_branch_fetch_step(branch, mirror)


def bench_arms(root, *args):
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvp("bash", ["bash", os.path.join(str(root), "lib", "bench-arms.sh"), *args])


def sysimage_arms(root, *args):
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvp("bash", ["bash", os.path.join(str(root), "lib", "sysimage-arms.sh"), *args])
