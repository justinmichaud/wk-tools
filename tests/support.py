"""Shared test support for the wk-tools unittest suite.

Run the whole suite:      python3 tests/run.py -v      (wk selftest)
Run one module:            python3 -m unittest tests.test_dispatcher -v
A live test (requires_podman_vm and the other gates below) runs only when
the runner selected the live tier and its machine is up; it never starts
one. Every test that touches real state cleans up after itself.
"""

import atexit
import contextlib
import functools
import os
import random
import re
import shutil
import string
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WK = REPO / "wk"

# The suite is fleet-blind: every run()/bash() below points WK_MACHINES_DIR
# (lib/wk/fleet.py) at BLIND_FLEET, this repo's machines less every build
# machine and peer, so target_all knows only container and vm and no test ever
# ssh's to one of the maintainer's real targets or finds a workspace that
# happens to live there. A test that wants a fleet passes its own directory --
# fake machine confs of its own, NO_REGISTRY for none at all, or REAL_MACHINES
# when it is deliberately auditing the machines this repo ships.
REAL_MACHINES = REPO / "machines"
NO_REGISTRY = tempfile.mkdtemp(prefix="wk-test-no-registry-")
BLIND_FLEET = tempfile.mkdtemp(prefix="wk-test-blind-fleet-")
for _conf in REAL_MACHINES.glob("*.conf"):
    if not re.search(r"^KIND=(build|peer)$", _conf.read_text(), re.M):
        os.symlink(_conf, os.path.join(BLIND_FLEET, _conf.name))
atexit.register(shutil.rmtree, NO_REGISTRY, True)
atexit.register(shutil.rmtree, BLIND_FLEET, True)
# This machine's config home less its `wk`, so no test sees the ~/.config/wk/machines/ overlay
# (lib/wk/fleet.py); the rest stays, since git and podman read their own config there.
NO_CONFIG = tempfile.mkdtemp(prefix="wk-test-no-config-")
_REAL_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
for _entry in (_REAL_CONFIG.iterdir() if _REAL_CONFIG.is_dir() else ()):
    if _entry.name != "wk":
        os.symlink(_entry, os.path.join(NO_CONFIG, _entry.name))
atexit.register(shutil.rmtree, NO_CONFIG, True)
FLEET_ENV = {"XDG_CONFIG_HOME": NO_CONFIG}
# The name a fake target conf gives this host (WK_REMOTE_HOSTNAME) to be its far end.
THIS_HOST = subprocess.run(["hostname", "-s"], stdout=subprocess.PIPE, universal_newlines=True).stdout.strip().lower()


def real_confs(*kinds):
    """This repo's machines/<name>.conf of those KINDs."""
    return sorted(p for p in REAL_MACHINES.glob("*.conf")
                  if re.search(r"^KIND=(%s)$" % "|".join(kinds), p.read_text(), re.M))

# Same reasoning, for wk_secrets_dir (lib/store.sh): on a macOS host it reads
# WK_HOST_SECRETS rather than $WK_STORE, so without a default of its own a
# test would read and write the real ~/.config/wk/secrets. A test that wants
# a populated store passes its own directory.
NO_SECRETS = tempfile.mkdtemp(prefix="wk-test-no-secrets-")
atexit.register(shutil.rmtree, NO_SECRETS, True)

# Same reasoning, for wk_state_dir (lib/common.sh): on a macOS workstation
# that is where wk_record_dir sends a task record and where the mirror lives,
# so a suite that merely popped XDG_STATE_HOME wrote into the real one -- 85
# task records under ~/.local/state/wk/task, from tests about commands that
# record a task. A test that wants this machine's own passes REAL_STATE.
NO_STATE = tempfile.mkdtemp(prefix="wk-test-no-state-")
REAL_STATE = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
atexit.register(shutil.rmtree, NO_STATE, True)

# The credential rules (lib/credcheck.py) ask GitHub what a token can do, so
# without a default of its own a test that stores one would spend a request
# against the real API. Port 1 refuses at once, which is the same answer a
# machine with no network gives and the branch that reports a credential
# unverified. A test that wants answers points this at a stub of its own.
NO_GITHUB = "http://127.0.0.1:1"

# A tailnet that lists no peer, for stub_path: reach_offline (lib/reach.sh) then
# refuses nothing and the stubbed `ssh` decides reachability, whatever the real
# coordinator says about a board of that name.
TAILSCALE_KNOWS_NOTHING = "echo '{}'\n"
os.environ["WK_GITHUB_API"] = NO_GITHUB
os.environ["WK_TAILNET_API"] = NO_GITHUB
os.environ["WK_NTFY_API"] = NO_GITHUB
os.environ["WK_ANTHROPIC_API"] = NO_GITHUB
os.environ["WK_CLAUDE_OAUTH"] = NO_GITHUB
os.environ["WK_LITELLM_API"] = NO_GITHUB
os.environ["WK_BUGZILLA_API"] = NO_GITHUB

# The tailnet keys are the two credentials whose paths are not under
# wk_secrets_dir (lib/common.sh reads them from ~/.config/wk), so without these
# a test would read the maintainer's real ones and put them to the tailnet.
os.environ["WK_TS_AUTHKEY"] = os.path.join(NO_SECRETS, "tailscale-authkey")
os.environ["WK_TS_API_SECRET"] = os.path.join(NO_SECRETS, "tailscale-api-key")


def dispatch_vars():
    """The variables the dispatcher exports for the one command it runs, read
    from the file that defines them (`WK_DISPATCH_VARS` in lib/common.sh)
    rather than copied into a test -- the same way where_values() reads
    WK_WHERE_VALUES out of `wk`."""
    import re
    m = re.search(r'WK_DISPATCH_VARS="([^"]+)"',
                  (REPO / "lib" / "common.sh").read_text())
    assert m, "lib/common.sh no longer defines WK_DISPATCH_VARS"
    return tuple(m.group(1).split())


DISPATCH_VARS = dispatch_vars()

# A shell started from `wk zed`/`wk enter` inherits those variables and keeps
# them, so a test that inherits one is a test about whatever that person last
# worked on. They go at import as well as in _clean_env below: _clean_env is
# the door most tests use, and this is what the ones that build an environment
# out of os.environ themselves get. A test that wants one sets it through
# `env=`, which still wins.
for _leaked in DISPATCH_VARS:
    os.environ.pop(_leaked, None)

# Every subprocess a test starts inherits this process's stdin, and a bash
# built with SSH_SOURCE_BASHRC (Debian and Ubuntu ship one) sources ~/.bashrc
# in a *non-interactive* shell whose stdin is a connected socket and whose
# SHLVL is below 2 -- so a `bash -c` or a `./wk` handed a hand-built env
# (which drops SHLVL) had the machine's rc rewrite its PATH, whenever the
# runner itself was started with a socketpair on stdin. /dev/null is not a
# socket. A test that wants to feed a command bytes passes `input=`.
with open(os.devnull, "rb") as _devnull:
    os.dup2(_devnull.fileno(), 0)


def where_values():
    """The `where=` vocabulary, read from the module that enforces it
    (`WHERE_VALUES` in lib/wk/decl.py) rather than copied into a test."""
    import re
    m = re.search(r'WHERE_VALUES = \(([^)]+)\)', (REPO / "lib" / "wk" / "decl.py").read_text())
    assert m, "lib/wk/decl.py no longer defines WHERE_VALUES"
    return tuple(w.strip().strip('"') for w in m.group(1).split(",") if w.strip())


def _clean_env(extra=None, wk_root=False):
    """A predictable environment: this machine's own, minus the dispatcher's
    per-invocation variables (DISPATCH_VARS above) and anything else that
    would make the command under test think it is already a workspace or
    already pointed at a scratch store, and with a fleet of no build machine
    or peer (BLIND_FLEET above) so nothing reaches a real target, and a scratch
    secrets directory (NO_SECRETS above) so nothing reads or writes the real
    ~/.config/wk/secrets, plus whatever the caller adds -- including a
    WK_MACHINES_DIR or WK_HOST_SECRETS of its own.

    wk_root=True also sets WK_ROOT: every sourced lib in this tree that
    needs it (image/profiles.sh, boot/machines.sh, ...) gets it for free
    from lib/common.sh's own `WK_ROOT="${WK_ROOT:-$(cd ... )}"`, but a
    bash snippet that sources a lib *without* lib/common.sh first (as some
    of cmd/selftest's lifted checks do) needs it set explicitly.
    """
    env = dict(os.environ)
    for var in DISPATCH_VARS:
        env.pop(var, None)
    env.pop("WK_MARKER", None)
    env.pop("WK_STORE", None)
    env["XDG_STATE_HOME"] = NO_STATE
    env["WK_MACHINES_DIR"] = BLIND_FLEET
    env["XDG_CONFIG_HOME"] = NO_CONFIG
    env["WK_REMOTE_MARKER"] = os.path.join(NO_STATE, "no-wk-remote")   # a real ~/.wk-remote makes this host a target's far end
    env["WK_HOST_SECRETS"] = NO_SECRETS
    env["WK_GITHUB_API"] = NO_GITHUB
    env["WK_TAILNET_API"] = NO_GITHUB
    env["WK_NTFY_API"] = NO_GITHUB
    env["WK_ANTHROPIC_API"] = NO_GITHUB
    env["WK_CLAUDE_OAUTH"] = NO_GITHUB
    env["WK_LITELLM_API"] = NO_GITHUB
    env["WK_BUGZILLA_API"] = NO_GITHUB
    env["WK_TS_AUTHKEY"] = os.environ["WK_TS_AUTHKEY"]
    env["WK_TS_API_SECRET"] = os.environ["WK_TS_API_SECRET"]
    if wk_root:
        env["WK_ROOT"] = str(REPO)
    if extra:
        env.update(extra)
    return env


def clean_env(extra=None, wk_root=True):
    """`_clean_env` for a test that invokes a cmd/* file directly instead of
    through ./wk -- the fleet-blindness above is the suite's, not the
    dispatcher's: cmd/profile resolving a workspace name against the real
    registry asked moose over ssh three times before refusing an argument,
    and outlived a 30s timeout while moose was down (2026-09-17)."""
    return _clean_env(extra, wk_root=wk_root)


def run(*args, env=None, check=False, timeout=120, input=None):
    """Run ./wk <args> and return a CompletedProcess with text output.

    stdout and stderr are merged into .stdout (.stderr is always ""),
    mirroring cmd/selftest's `out=$(fn 2>&1)`: most of wk's reporting
    (dry runs, refusals) goes to stderr, and every check ported from that
    file greps the combined blob rather than one stream or the other.
    """
    cp = subprocess.run(
        [str(WK), *args],
        cwd=str(REPO),
        env=_clean_env(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        input=input,
        check=check,
    )
    cp.stderr = ""
    return cp


def run_here(*args, env=None, **kw):
    """`run`, as the machine that holds the store. WK_IN_VM=1 keeps the
    dispatcher from forwarding into the podman VM, and WK_STORE is a scratch
    store unless the caller names one, so no real machine's store answers."""
    e = {"WK_IN_VM": "1"}
    if not (env and env.get("WK_STORE")):
        store = tempfile.mkdtemp(prefix="wk-test-store-")
        atexit.register(shutil.rmtree, store, True)
        e["WK_STORE"] = store
    e.update(env or {})
    return run(*args, env=e, **kw)


def bash(script, env=None, timeout=60, cwd=None):
    """Run a bash script (mirrors cmd/selftest's `bash -c '...'` idiom for
    lifting a function out of a file and calling it). WK_ROOT is set in the
    environment (see _clean_env) so a script may source any lib directly
    without sourcing lib/common.sh first. Returns a CompletedProcess with
    stdout and stderr captured separately."""
    return subprocess.run(
        ["bash", "-c", script],
        cwd=cwd or str(REPO),
        env=_clean_env(env, wk_root=True),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def lock_bash(script, lock_dir, env=None, timeout=60):
    """Run a bash script with lib/common.sh sourced and every lock under
    `lock_dir`, so nothing it takes or breaks is this machine's."""
    e = {"WK_LOCK_DIR": str(lock_dir)}
    if env:
        e.update(env)
    return bash(f'. "{REPO}/lib/common.sh"; set +e\n{script}', env=e, timeout=timeout)


def builds_on_the_books_env(tmp, *labels):
    """An environment in which builds_on_the_books() reads exactly `labels`:
    a stub podman answers `machine ssh` with them and reports no running
    machine (macOS), and a state directory holds one record each (Linux)."""
    tmp = Path(tmp)
    records = tmp / "wk" / "builds"
    records.mkdir(parents=True, exist_ok=True)
    for i, label in enumerate(labels):
        (records / str(i)).write_text(f"label={label}\n")
    binp = tmp / "bin"
    binp.mkdir(exist_ok=True)
    lines = "".join(f"    echo 'label={label}'\n" for label in labels)
    (binp / "podman").write_text(
        '#!/bin/sh\ncase "$1 $2" in "machine ssh")\n' + lines + "    ;;\nesac\nexit 0\n")
    (binp / "podman").chmod(0o755)
    return {"PATH": f"{binp}:{os.environ['PATH']}", "XDG_STATE_HOME": str(tmp)}


def bench_ls_runs(stdout):
    """The run directories `wk bench ls` printed, oldest first: the indented
    lines under each task whose first word is a path containing /runs/. What
    a test hands to `wk bench report <run-a> <run-b>`."""
    out = []
    for line in stdout.splitlines():
        words = line.split()
        if words and "/runs/" in words[0]:
            out.append(words[0])
    return out


def rand_suffix(n=6):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def repo_files():
    """Every tracked file, as absolute paths.

    `git ls-files`, not a directory walk: an audit that counts how many times
    something is defined "in the tree" must not count a build directory, a
    scratch file, or an agent's git worktree -- Claude Code puts one under
    .claude/worktrees, which doubles every file in the repository and fails
    six of these audits at once."""
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"],
                         capture_output=True, text=True, check=True).stdout
    return [REPO / name for name in out.split("\0")
            if name and (REPO / name).is_file()]


def shell_files():
    """Every shell file in the tree, by shebang or `.sh` suffix -- the same
    rule cmd/selftest's shell_files() uses, and for the same reason: most of
    what bash loads here (lib/, boot/, targets/, image/) is sourced and has
    no shebang."""
    out = []
    for p in repo_files():
        if p.suffix == ".sh":
            out.append(p)
            continue
        try:
            with open(p, "rb") as f:
                first = f.readline(200)
        except OSError:
            continue
        if first.startswith(b"#!") and (b"bash" in first or b"/sh" in first):
            out.append(p)
    return out


def assert_guest_start_converges(case, step):
    """A guest start converges it through one `Guest.converge` over `lib/wk/guest.py`'s STEPS, from both arms --
    the guest that was already running and the one this start booted. `step` names it as targets/vm.sh did
    (`_set_guest_egress "$name" "$ip"`): the step is in STEPS once, and `start` converges once, after both arms."""
    import inspect
    sys.path.insert(0, str(REPO / "lib"))
    from wk import guest
    name = step.split()[0].lstrip("_")
    case.assertEqual(1, [s[0] for s in guest.STEPS].count(name), f"a guest start does not run {name!r} exactly once")
    body = inspect.getsource(guest.start)
    case.assertEqual(1, body.count(".converge()"), "a guest start no longer converges once, after both arms")
    case.assertIn('if state == "running":', body)


def guest_step(env, step, ws="demo", ip="1.2.3.4", secrets=None):
    """One of lib/wk/guest.py's converge steps run on this host, as a macOS host drives a guest: `env` over the
    clean environment is the whole of os.environ, so stubs first on its PATH stand in for ssh. `secrets` patches
    Secrets methods (`{"bugzilla_user": fn}`). A CompletedProcess's returncode and stderr."""
    import io
    import types
    from unittest import mock
    sys.path.insert(0, str(REPO / "lib"))
    from wk import act, guest, targets
    from wk.secrets import Secrets
    from wk.store import Store
    err = io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.dict(os.environ, _clean_env(env, wk_root=True), clear=True))
        stack.enter_context(mock.patch.object(Store, "macos_host", new_callable=mock.PropertyMock, return_value=True))
        for name, fn in (secrets or {}).items():
            stack.enter_context(mock.patch.object(Secrets, name, fn))
        stack.enter_context(contextlib.redirect_stderr(err))
        vm = targets.Registry(str(REPO), env=dict(os.environ)).load("vm")
        try:
            rc = 0 if getattr(guest.Guest(guest.Host(vm), ws, ip), step)() else 1
        except act.Refused as e:
            rc = e.status
    return types.SimpleNamespace(returncode=rc, stdout="", stderr=err.getvalue())


def func_body(text, name):
    """One shell function's body. The house style (`name() {` opening a line,
    a closing `}` alone on a line) is what makes this exact; an argument
    comment after the brace is part of the style and allowed."""
    m = re.search(r"^%s\(\) \{[^\n]*$(.*?)^\}$" % re.escape(name), text, re.M | re.S)
    assert m, f"no {name}() in the text given"
    return m.group(1)


class FakeWorkspace:
    """A workspace that is not one: a marker naming a checkout that exists,
    so in-workspace code paths run on a host without creating anything.
    Mirrors cmd/selftest's fake_marker()/as_workspace()."""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-ws-"))
        self.ws_dir = self.tmp / "ws"
        self.state_dir = self.tmp / "state"
        (self.ws_dir / "WebKit").mkdir(parents=True)
        self.state_dir.mkdir(parents=True)
        self.marker = self.ws_dir / "marker"
        self.marker.write_text(
            "# written by tests/support.py\n"
            "name=selftest-ws\n"
            f"src={self.ws_dir / 'WebKit'}\n"
        )

    def env(self, extra=None):
        e = {"WK_MARKER": str(self.marker), "XDG_STATE_HOME": str(self.state_dir)}
        if extra:
            e.update(extra)
        return e

    def run(self, *args, **kwargs):
        env = kwargs.pop("env", None)
        return run(*args, env=self.env(env), **kwargs)

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


@contextlib.contextmanager
def fake_workspace():
    ws = FakeWorkspace()
    try:
        yield ws
    finally:
        ws.cleanup()


@contextlib.contextmanager
def temp_store():
    """A scratch WK_STORE, so a test that writes through the store machinery
    cannot touch the real one."""
    d = tempfile.mkdtemp(prefix="wk-test-store-")
    try:
        yield {"WK_STORE": d, "path": Path(d)}
    finally:
        shutil.rmtree(d, ignore_errors=True)


@contextlib.contextmanager
def scratch_dir(prefix="wk-test-"):
    d = tempfile.mkdtemp(prefix=prefix)
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@contextlib.contextmanager
def glob_bait(patterns):
    """A directory holding one file per pattern word, named so the word
    pathname-expands there: `*Tools/Scripts/build-*` gets xTools/Scripts/build-x.
    Run a matcher from this cwd and a pattern that leaks through an unquoted
    expansion stops matching."""
    with scratch_dir("wk-glob-bait-") as d:
        for word in patterns.split():
            name = re.sub(r"\[[^\]]*\]", "x", word).replace("*", "x").replace("?", "x")
            path = d / name.lstrip("/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")
        yield d


def podman_vm_running(machine="wk"):
    try:
        cp = subprocess.run(
            ["podman", "machine", "inspect", machine, "--format", "{{.State}}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return cp.returncode == 0 and cp.stdout.strip() == "running"


def selected_tiers():
    """The tiers this run selected, as tests/run.py exports them in
    WK_TEST_TIERS; a module run on its own gets the runner's default."""
    return set(os.environ.get("WK_TEST_TIERS", "lint,unit").split(","))


def live_selected():
    """Whether the live tier is in: without it every test that needs a VM, a
    machine or a board skips by name, whatever is actually reachable."""
    return "live" in selected_tiers()


def owed(reason):
    """Mark a test for behaviour still owed: it is expected to fail, and the
    runner fails when it passes, naming this mark and `reason`."""
    def mark(test):
        test = unittest.expectedFailure(test)
        test.wk_owed = reason
        return test
    return mark


def _live(need, *args):
    """Mark a test or class live (wk_tier, read by tests/run.py) and skip it
    at run time with the reason `need(*args)` gives; nothing is probed at
    import, and each need is probed once per run."""
    def decorate(obj):
        obj.wk_tier = "live"
        if isinstance(obj, type):
            inherited = obj.setUpClass.__func__

            def setUpClass(cls):
                reason = need(*args)
                if reason:
                    raise unittest.SkipTest(reason)
                inherited(cls)
            obj.setUpClass = classmethod(setUpClass)
            return obj

        @functools.wraps(obj)
        def wrapper(self, *a, **kw):
            reason = need(*args)
            if reason:
                raise unittest.SkipTest(reason)
            return obj(self, *a, **kw)
        return wrapper
    return decorate


def requires_container_target():
    """Gate for a test that needs the real container target: on macOS the
    podman VM this repo drives must already be up (never started here), on
    Linux podman itself; skipped while the live tier is out, and while this
    machine has a build on its books."""
    return _live(_needs_container_target)


def requires_podman_vm(machine="wk"):
    """Gate for a test that needs a real container workspace: the podman VM
    this repo drives must already be up, and is never started here."""
    return _live(_needs_podman_vm, machine)


def requires_machine(name, timeout=5):
    """Gate for a test that reaches a configured machine over ssh: it never
    provisions, reboots or otherwise mutates the machine, and skips rather
    than hangs when the machine does not answer."""
    return _live(_needs_machine, name, timeout)


@functools.lru_cache(maxsize=None)
def _needs_container_target():
    if not live_selected():
        return "live tier not selected: needs the container target"
    if sys.platform == "darwin":
        return _needs_podman_vm("wk")
    if not shutil.which("podman"):
        return "podman is not installed"
    return _build_in_the_way()


@functools.lru_cache(maxsize=None)
def _needs_podman_vm(machine):
    if not live_selected():
        return "live tier not selected: needs the podman VM"
    if not podman_vm_running(machine):
        return f"podman machine '{machine}' is not running"
    return _build_in_the_way()


@functools.lru_cache(maxsize=None)
def _needs_machine(name, timeout):
    if not live_selected():
        return f"live tier not selected: needs '{name}'"
    if not machine_reachable(name, timeout=timeout):
        return f"'{name}' is not reachable over ssh (BatchMode)"
    return None


@functools.lru_cache(maxsize=None)
def _build_in_the_way():
    """A test that makes a real workspace shares the machine with whatever is
    building on it: `wk new` takes minutes where it takes seconds, and the
    build such a test asks for is refused because the memory is spoken for --
    build_admit working, not a fault to be read as a failure."""
    busy = builds_on_the_books()
    if busy:
        return ("a build is on this machine's books (%s): re-run this on an "
                "idle machine" % ", ".join(busy))
    return None


# wk_state_dir (lib/common.sh), spelled for the shell that reads the records.
# A record whose `pid:` holder is gone is a killed build (lib/resources.sh's
# _build_holder_alive); any other holder is kept, since it cannot be read from here.
_BUILD_RECORDS = r'''for f in "${XDG_STATE_HOME:-$HOME/.local/state}"/wk/builds/*; do
    [ -f "$f" ] || continue
    h=$(sed -n 's/^holder=//p' "$f")
    case "$h" in pid:*) kill -0 "${h#pid:}" 2>/dev/null || continue ;; esac
    cat "$f"
done; true'''


def builds_on_the_books():
    """The builds recorded where a container workspace is really built: inside
    the podman VM on macOS, on this machine on Linux (build_record,
    lib/resources.sh). Read and never pruned -- `builds_running` deletes the
    record of a holder it cannot see, and a reading may not mutate what it
    reports on -- so a dead holder's record is skipped, not removed."""
    if sys.platform == "darwin":
        out = podman_vm_ssh(_BUILD_RECORDS).stdout
    else:
        out = subprocess.run(["bash", "-c", _BUILD_RECORDS], capture_output=True,
                             text=True, timeout=60).stdout
    return [l.split("=", 1)[1] for l in out.splitlines() if l.startswith("label=")]


def machine_reachable(name, timeout=5):
    try:
        cp = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}", name, "true"],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return cp.returncode == 0


@contextlib.contextmanager
def stub_path(scripts):
    """A temp directory, first on PATH, holding one fake executable per
    `{name: body}` entry -- the technique 'un-managed clobbering' and the
    disk-logic tests use to drive real driver code (targets/container.sh,
    targets/vm.sh, boot/disk.sh) against a filesystem-only fake of
    `podman`/`tart`/`sfdisk`/`lsblk` rather than real hardware or a real VM.
    `body` is wrapped in a `#!/bin/sh` shebang unless it supplies its own.
    Yields the bin directory; the caller puts it first on PATH, e.g.
        env={"PATH": f"{binp}:{os.environ['PATH']}"}
    """
    d = tempfile.mkdtemp(prefix="wk-test-stub-bin-")
    try:
        for name, body in scripts.items():
            p = Path(d) / name
            p.write_text(body if body.startswith("#!") else f"#!/bin/sh\n{body}")
            p.chmod(0o755)
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def podman_vm_ssh(command, machine="wk", timeout=60):
    """Run one command inside the podman VM this repo drives -- the same
    machine `detach_run`'s driver process lives on once `wk new --target
    container` forwards there (lib/target.sh's forward_to_vm execs the whole
    command over `podman machine ssh`). For a test that has to reach in and
    kill a real driver pid, or read its store, without forwarding a second
    whole `wk` command to do it."""
    return subprocess.run(
        ["podman", "machine", "ssh", machine, "--", command],
        capture_output=True, text=True, timeout=timeout,
    )


class WkTest(unittest.TestCase):
    """Base class for tests that shell out to ./wk or to bash. Not required
    -- module-level functions above work standalone -- but it gives
    subclasses `self.repo`, `self.wk`, `self.run(...)` and a per-test scratch
    dir for free."""

    repo = REPO
    wk = WK

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="wk-test-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.tmp = Path(self._tmp)

    def run_wk(self, *args, env=None, **kwargs):
        return run(*args, env=env, **kwargs)

    def bash(self, script, env=None, **kwargs):
        return bash(script, env=env, **kwargs)
