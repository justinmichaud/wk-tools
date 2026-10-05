"""Shared test support for the wk-tools unittest suite.

Run the whole suite:      python3 tests/run.py -v      (wk selftest)
Run one module:            python3 -m unittest tests.test_dispatcher -v
A live test (requires_container_place and the other gates below) runs only when
the runner selected the live tier and its machine is up; it never starts
one. Every test that touches real state cleans up after itself.
"""

import atexit
import contextlib
import functools
import importlib.machinery
import importlib.util
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

# The suite is fleet-blind: BLIND_FLEET is this repo's machines less every build machine and peer, so no test
# reaches a real place. A test that wants a fleet passes its own directory, NO_REGISTRY, or REAL_MACHINES.
REAL_MACHINES = REPO / "machines"
NO_REGISTRY = tempfile.mkdtemp(prefix="wk-test-no-registry-")
BLIND_FLEET = tempfile.mkdtemp(prefix="wk-test-blind-fleet-")
for _conf in REAL_MACHINES.glob("*.conf"):
    if not re.search(r"^kind=(build|peer)$", _conf.read_text(), re.M):
        os.symlink(_conf, os.path.join(BLIND_FLEET, _conf.name))
atexit.register(shutil.rmtree, NO_REGISTRY, True)
atexit.register(shutil.rmtree, BLIND_FLEET, True)
# This machine's config home less its `wk` (git and podman read theirs there).
NO_CONFIG = tempfile.mkdtemp(prefix="wk-test-no-config-")
_REAL_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
for _entry in (_REAL_CONFIG.iterdir() if _REAL_CONFIG.is_dir() else ()):
    if _entry.name != "wk":
        os.symlink(_entry, os.path.join(NO_CONFIG, _entry.name))
atexit.register(shutil.rmtree, NO_CONFIG, True)
FLEET_ENV = {"XDG_CONFIG_HOME": NO_CONFIG}
# The name a fake place conf gives this host (hostname=) to be its far end.
THIS_HOST = subprocess.run(["hostname", "-s"], stdout=subprocess.PIPE, universal_newlines=True).stdout.strip().lower()


def real_confs(*kinds):
    """This repo's machines/<name>.conf of those KINDs."""
    return sorted(p for p in REAL_MACHINES.glob("*.conf")
                  if re.search(r"^kind=(%s)$" % "|".join(kinds), p.read_text(), re.M))

# Scratch secrets, state and API endpoints, so no test reads or writes the real ones; port 1 refuses at once.
NO_SECRETS = tempfile.mkdtemp(prefix="wk-test-no-secrets-")
atexit.register(shutil.rmtree, NO_SECRETS, True)

NO_STATE = tempfile.mkdtemp(prefix="wk-test-no-state-")
atexit.register(shutil.rmtree, NO_STATE, True)

NO_GITHUB = "http://127.0.0.1:1"

# A tailnet that lists no peer, so a stubbed `ssh` decides reachability.
TAILSCALE_KNOWS_NOTHING = "echo '{}'\n"
os.environ["WK_GITHUB_API"] = NO_GITHUB
os.environ["WK_TAILNET_API"] = NO_GITHUB
os.environ["WK_NTFY_API"] = NO_GITHUB
os.environ["WK_ANTHROPIC_API"] = NO_GITHUB
os.environ["WK_LITELLM_API"] = NO_GITHUB
os.environ["WK_BUGZILLA_API"] = NO_GITHUB

os.environ["WK_TS_AUTHKEY"] = os.path.join(NO_SECRETS, "tailscale-authkey")
os.environ["WK_TS_API_SECRET"] = os.path.join(NO_SECRETS, "tailscale-api-key")


sys.path.insert(0, str(REPO / "lib"))
from wk.dispatch import DISPATCH_VARS  # noqa: E402


# Variables wk sets for its own child processes: a protocol between wk's processes, owed no README line.
INTERNAL_VARS = DISPATCH_VARS + (
    "WK_AB_ROOT", "WK_MIRROR", "WK_DEV", "WK_DEVICE_HELD", "WK_DO", "WK_FILE", "WK_OCT", "WK_PART", "WK_PATH", "WK_SRC",
    "WK_TASK_HELD", "WK_TASK_PARENT", "WK_TS_API_SECRET_FILE",
    "WK_BOARD_CLASS", "WK_BOARD_EVIDENCE", "WK_BOARD_EXPECT", "WK_BOARD_JIT_TIERS",
    "WK_BOARD_PROFILE", "WK_BOARD_WARMUP", "WK_BOARD_KILL", "WK_BOARD_LAUNCH", "WK_BOARD_PGO", "WK_BOARD_RESET",
    "WK_BOARD_DEST", "WK_BOARD_OPTS", "WK_BOARD_LIB", "WK_BOARD_URL",
)

# A shell from `wk enter` inherits the dispatcher's variables; they go here and in _clean_env.
for _leaked in DISPATCH_VARS:
    os.environ.pop(_leaked, None)

# Debian's bash (SSH_SOURCE_BASHRC) sources ~/.bashrc when stdin is a socket; /dev/null is not one.
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
    or peer (BLIND_FLEET above) so nothing reaches a real place, and a scratch
    keyring (NO_SECRETS above) so nothing reads or writes the real
    ~/.config/wk/secrets, plus whatever the caller adds -- including a
    WK_MACHINES_DIR or WK_HOST_SECRETS of its own."""
    env = dict(os.environ)
    for var in DISPATCH_VARS:
        env.pop(var, None)
    env.pop("WK_MARKER", None)
    env.pop("WK_STORE", None)
    env["XDG_STATE_HOME"] = NO_STATE
    env["WK_MACHINES_DIR"] = BLIND_FLEET
    env["XDG_CONFIG_HOME"] = NO_CONFIG
    env["WK_REMOTE_MARKER"] = os.path.join(NO_STATE, "no-wk-remote")   # a real ~/.wk-remote makes this host a place's far end
    env["WK_HOST_SECRETS"] = NO_SECRETS
    env["WK_GITHUB_API"] = NO_GITHUB
    env["WK_TAILNET_API"] = NO_GITHUB
    env["WK_NTFY_API"] = NO_GITHUB
    env["WK_ANTHROPIC_API"] = NO_GITHUB
    env["WK_LITELLM_API"] = NO_GITHUB
    env["WK_BUGZILLA_API"] = NO_GITHUB
    env["WK_TS_AUTHKEY"] = os.environ["WK_TS_AUTHKEY"]
    env["WK_TS_API_SECRET"] = os.environ["WK_TS_API_SECRET"]
    if wk_root:
        env["WK_ROOT"] = str(REPO)
    if extra:
        env.update(extra)
    env["PATH"] = shimmed_path(env.get("PATH", ""))
    return env


SYSTEM_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin", "/usr/local/bin", "/opt/homebrew/bin")


def _real_dirs():
    shims = os.environ.get("WK_TEST_SHIMS", "")
    given = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p and p != shims]
    return {os.path.realpath(p) for p in given + list(SYSTEM_DIRS)}


@functools.lru_cache(maxsize=None)
def _shims_for(tools):
    d = tempfile.mkdtemp(prefix="wk-test-shims-")
    atexit.register(shutil.rmtree, d, True)
    for tool in tools:
        shutil.copy2(os.path.join(os.environ["WK_TEST_SHIMS"], tool), os.path.join(d, tool))
    return d


def shimmed_path(path):
    """`path` with the runner's shim first for each machine tool it would find installed, so a test's own PATH --
    one that leaves out the runner's shim directory, or names only system directories -- still reaches no machine;
    a tool it lacks stays absent, and a test's own stub stays first."""
    shims = os.environ.get("WK_TEST_SHIMS")
    if not shims:
        return path
    real = _real_dirs()
    reached = tuple(sorted(t for t in os.listdir(shims)
                           if shutil.which(t, path=path) and os.path.realpath(os.path.dirname(shutil.which(t, path=path))) in real))
    return _shims_for(reached) + os.pathsep + path if reached else path


def clean_env(extra=None, wk_root=True):
    """`_clean_env` for a test that invokes a cmd/* file directly instead of
    through ./wk -- the fleet-blindness above is the suite's, not the
    dispatcher's: `wk run --profile` resolving a workspace name against the real
    registry asked moose over ssh three times before refusing an argument,
    and outlived a 30s timeout while moose was down (2026-09-17)."""
    return _clean_env(extra, wk_root=wk_root)


def run(*args, env=None, check=False, timeout=120, input=None):
    """Run ./wk <args> and return a CompletedProcess with text output."""
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


def as_dispatched(cmd, argv, env):
    """argv as the dispatcher hands it to cmd/<cmd> (lib/wk/dispatch.py): the verb first, and a
    declared build preset -- `--preset <p>`, or build's positional -- lifted into env's WK_PRESET."""
    sys.path.insert(0, str(REPO / "lib"))
    from wk import decl, dispatch
    env.pop("WK_PRESET", None)
    d = decl.Decl(REPO / "cmd" / cmd)
    inv = dispatch.Invocation(cmd, d, list(argv))
    args, joined, valued = inv.verb_first(), [], d.valued_opts()
    while args:
        a = args.pop(0)
        if a == "--":
            joined += [a] + args
            break
        joined.append(a + "=" + args.pop(0) if a in valued and args else a)
    rest = inv.take_preset(joined, env)
    return dispatch.argv_split(d.opts_for(rest), rest)


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


def builds_on_the_books_env(tmp, *labels):
    """An environment in which builds_on_the_books() reads exactly `labels`:
    a stub podman answers `machine ssh` with them and reports no running
    machine (macOS), and the records hold one each (Linux)."""
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
    """Every file git does not ignore, tracked or not, as absolute paths: what a commit would hold."""
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                         capture_output=True, text=True, check=True).stdout
    return [REPO / name for name in out.split("\0")
            if name and (REPO / name).is_file()]


def load_cmd(name):
    """cmd/<name> as a module: a file with no extension needs its loader spelled out."""
    path = str(REPO / "cmd" / name)
    loader = importlib.machinery.SourceFileLoader("wk_cmd_" + name.replace("-", "_"), path)
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader, origin=path))
    m.__file__ = path
    loader.exec_module(m)
    return m


def shell_files():
    """Every shell file in the tree, by shebang or `.sh` suffix -- the same
    rule cmd/selftest's shell_files() uses, and for the same reason: most of
    what bash loads here (lib/, host/, bench/, image/) is sourced and has
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


def guest_step(env, step, ws="demo", secrets=None):
    """One of lib/wk/guest.py's converge steps run on this host, as a macOS host drives a guest: `env` over the
    clean environment is the whole of os.environ, so stubs first on its PATH stand in for tart. `secrets` patches
    Secrets methods (`{"bugzilla_user": fn}`). A CompletedProcess's returncode and stderr."""
    import io
    import types
    from unittest import mock
    sys.path.insert(0, str(REPO / "lib"))
    from wk import act, guest, places
    from wk.secrets import Secrets
    from wk.store import Store
    err = io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.dict(os.environ, _clean_env(env, wk_root=True), clear=True))
        stack.enter_context(mock.patch.object(Store, "macos_host", new_callable=mock.PropertyMock, return_value=True))
        for name, fn in (secrets or {}).items():
            stack.enter_context(mock.patch.object(Secrets, name, fn))
        stack.enter_context(contextlib.redirect_stderr(err))
        vm = places.Registry(str(REPO), env=dict(os.environ)).load("vm")
        try:
            rc = 0 if getattr(guest.Guest(guest.Host(vm), ws, vm.guest_of(vm.vm(ws))), step)() else 1
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


def git_run(*args, cwd, check=True):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=check)


def git_commit(repo, name):
    """A commit adding the file `name`; its sha."""
    (Path(repo) / name).write_text(name + "\n")
    git_run("add", name, cwd=repo)
    git_run("-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-q", "-m", name, cwd=repo)
    return git_run("rev-parse", "HEAD", cwd=repo).stdout.strip()


class GitMirror:
    """A mirror under `tmp` made by the real mirror_refresh_script (lib/wk/git.py) of two local upstreams -- origin
    (`up.git`, main at `sha1`) and a fork (`fk.git`, branch `side`) -- pushed to from the clone `seed`."""

    def __init__(self, tmp):
        from wk import git
        tmp = Path(tmp)
        self.upstream, self.fork, self.seed, self.mirror = tmp / "up.git", tmp / "fk.git", tmp / "seed", tmp / "m.git"
        for bare in (self.upstream, self.fork):
            git_run("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp)
        git_run("clone", "-q", str(self.upstream), str(self.seed), cwd=tmp)
        self.sha1 = git_commit(self.seed, "a")
        git_run("push", "-q", "origin", "main", cwd=self.seed)
        git_run("checkout", "-q", "-b", "side", cwd=self.seed)
        git_commit(self.seed, "side")
        git_run("push", "-q", str(self.fork), "side", cwd=self.seed)
        git_run("checkout", "-q", "main", cwd=self.seed)
        # A fork's default branch is not the mirror's (WPEWebKit's is wpe-2.46), which a bare fetch takes as its HEAD.
        git_run("symbolic-ref", "HEAD", "refs/heads/side", cwd=self.fork)
        self.remotes = (("origin", str(self.upstream)), ("fork", str(self.fork)))
        cp = subprocess.run(["sh", "-c", git.mirror_refresh_script(str(self.mirror), ["main"], self.remotes)],
                            capture_output=True, text=True)
        assert cp.returncode == 0, cp.stdout + cp.stderr

    def fetch(self):
        git_run("fetch", "--prune", "-q", "origin", cwd=self.mirror)

    def advance(self, name="b"):
        """One more commit on the upstream's main, and into the mirror."""
        sha = git_commit(self.seed, name)
        git_run("push", "-q", "origin", "main", cwd=self.seed)
        self.fetch()
        return sha


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
            [sys.executable, "-m", "wk.places", "podman-vm", "State"],
            capture_output=True, text=True, timeout=15,
            env=dict(os.environ, PYTHONPATH=str(REPO / "lib"), WK_MACHINE=machine),
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


def requires(need, *args):
    """A live gate of a test module's own: `need(*args)` is the reason to skip, or nothing."""
    return _live(need, *args)


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


def requires_tool(name):
    """Skip a unit test, by name, on a machine where the real tool `name` is not installed; the tier is unchanged."""
    return unittest.skipUnless(shutil.which(name), "needs %s on PATH" % name)


def requires_container_place():
    """Gate for a test that needs the real container place: on macOS the
    podman VM this repo drives must already be up (never started here), on
    Linux podman itself; skipped while the live tier is out, and while this
    machine has a build on its books."""
    return _live(_needs_container_place)


def requires_machine(name, timeout=5):
    """Gate for a test that reaches a configured machine over ssh: it never
    provisions, reboots or otherwise mutates the machine, and skips rather
    than hangs when the machine does not answer."""
    return _live(_needs_machine, name, timeout)


def container_target_missing():
    """Why this machine has no real container place to test against, or None."""
    if sys.platform == "darwin":
        return None if podman_vm_running("wk") else "podman machine 'wk' is not running"
    return None if shutil.which("podman") else "podman is not installed"


@functools.lru_cache(maxsize=None)
def _needs_container_place():
    if not live_selected():
        return "live tier not selected: needs the container place"
    return container_target_missing() or _build_in_the_way()


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
# A record whose `pid:` holder is gone is a killed build (lib/wk/build.py's
# holder_alive); any other holder is kept, since it cannot be read from here.
_BUILD_RECORDS = r'''for f in "${XDG_STATE_HOME:-$HOME/.local/state}"/wk/builds/*; do
    [ -f "$f" ] || continue
    h=$(sed -n 's/^holder=//p' "$f")
    case "$h" in pid:*) kill -0 "${h#pid:}" 2>/dev/null || continue ;; esac
    cat "$f"
done; true'''


def builds_on_the_books():
    """The builds recorded where a container workspace is really built: inside
    the podman VM on macOS, on this machine on Linux (Budget.record,
    lib/wk/resources.py). Read and never pruned -- `builds_running` deletes the
    record of a holder it cannot see, and a reading may not mutate what it
    reports on -- so a dead holder's record is skipped, not removed."""
    out = container_side(_BUILD_RECORDS).stdout
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
    disk-logic tests use to drive real driver code against a filesystem-only fake of
    `podman`/`tart`/`sfdisk`/`lsblk` rather than real hardware or a real VM.
    `body` is wrapped in a `#!/bin/sh` shebang unless it supplies its own.
    Yields the bin directory; the caller puts it first on PATH, e.g.
        env={"PATH": f"{binp}:{os.environ['PATH']}"}"""
    d = tempfile.mkdtemp(prefix="wk-test-stub-bin-")
    try:
        for name, body in scripts.items():
            p = Path(d) / name
            p.write_text(body if body.startswith("#!") else f"#!/bin/sh\n{body}")
            p.chmod(0o755)
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def container_side(command, timeout=60):
    """Run one shell command where the container place keeps its store and its
    drivers: inside the podman VM on macOS, on this host on Linux. For a test
    that has to kill a real driver pid, or read the store, without forwarding
    a second whole `wk` command to do it."""
    argv = ["podman", "machine", "ssh", "wk", "--", command] if sys.platform == "darwin" else ["sh", "-c", command]
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def container_store():
    """The container place's store as container_side sees it."""
    if sys.platform == "darwin":
        return "/var/lib/wk"
    sys.path.insert(0, str(REPO / "lib"))
    from wk.store import Store
    return Store(_clean_env()).store_dir()


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
        self.addCleanup(self.assert_no_process_left)
        self.tmp = Path(self._tmp)

    def assert_no_process_left(self):
        """A daemon a test started under its scratch dir is stopped by that test, so none outlives the run."""
        left = subprocess.run(["pgrep", "-fl", re.escape(self._tmp)], stdout=subprocess.PIPE, text=True).stdout
        self.assertEqual("", left, "processes this test started are still running")

    def run_wk(self, *args, env=None, **kwargs):
        return run(*args, env=env, **kwargs)

    def bash(self, script, env=None, **kwargs):
        return bash(script, env=env, **kwargs)
