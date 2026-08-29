"""Shared test support for the wk-tools unittest suite.

Run the whole suite:      python3 -m unittest discover -s tests -v
Run one module:            python3 -m unittest tests.test_dispatcher -v
Skip the podman-gated integration test: it self-skips when no `wk`
podman machine is `running` (see requires_podman_vm below); nothing extra
to pass. Every test that touches real state cleans up after itself.
"""

import contextlib
import os
import random
import shutil
import string
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WK = REPO / "wk"


def where_values():
    """The `where=` vocabulary, read from the dispatcher that enforces it
    (`WK_WHERE_VALUES` in ./wk) rather than copied into a test."""
    import re
    m = re.search(r'WK_WHERE_VALUES="([^"]+)"', (REPO / "wk").read_text())
    assert m, "wk no longer defines WK_WHERE_VALUES"
    return tuple(m.group(1).split())


def _clean_env(extra=None, wk_root=False):
    """A predictable environment: this machine's own, minus anything that
    would make the command under test think it is already a workspace or
    already pointed at a scratch store, plus whatever the caller adds.

    wk_root=True also sets WK_ROOT: every sourced lib in this tree that
    needs it (image/profiles.sh, boot/machines.sh, ...) gets it for free
    from lib/common.sh's own `WK_ROOT="${WK_ROOT:-$(cd ... )}"`, but a
    bash snippet that sources a lib *without* lib/common.sh first (as some
    of cmd/selftest's lifted checks do) needs it set explicitly.
    """
    env = dict(os.environ)
    env.pop("WK_MARKER", None)
    env.pop("XDG_STATE_HOME", None)
    env.pop("WK_STORE", None)
    if wk_root:
        env["WK_ROOT"] = str(REPO)
    if extra:
        env.update(extra)
    return env


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


def rand_suffix(n=6):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def shell_files():
    """Every shell file in the tree, by shebang or `.sh` suffix -- the same
    rule cmd/selftest's shell_files() uses, and for the same reason: most of
    what bash loads here (lib/, boot/, targets/, image/) is sourced and has
    no shebang."""
    out = []
    for p in REPO.rglob("*"):
        if not p.is_file():
            continue
        parts = p.parts
        if ".git" in parts or "__pycache__" in parts:
            continue
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
            "config=jsc-release\n"
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


def quick_run():
    """`wk selftest --quick` sets WK_TEST_QUICK=1: every test that needs a
    VM, a machine or a board skips by name, whatever is actually reachable."""
    return os.environ.get("WK_TEST_QUICK") == "1"


def requires_podman_vm(machine="wk"):
    """Skip decorator for a test that needs a real container workspace: it
    runs only when the podman VM this repo drives is already up, never
    starts it, and is skipped by --quick."""
    if quick_run():
        return unittest.skip("--quick: needs the podman VM")
    return unittest.skipUnless(
        podman_vm_running(machine),
        f"podman machine '{machine}' is not running",
    )


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


def requires_machine(name, timeout=5):
    """Skip decorator for a test that reaches a configured machine over
    ssh: it never provisions, reboots or otherwise mutates the machine, and
    self-skips rather than hanging when the machine does not answer, and is
    skipped by --quick."""
    if quick_run():
        return unittest.skip(f"--quick: needs '{name}'")
    return unittest.skipUnless(
        machine_reachable(name, timeout=timeout),
        f"'{name}' is not reachable over ssh (BatchMode)",
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
