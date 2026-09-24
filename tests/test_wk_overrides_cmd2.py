"""Coverage for the WK_* overrides read (with a default) in this agent's
files: cmd/new, cmd/pi, cmd/pr, cmd/profile, cmd/push,
cmd/quiesce, cmd/remote, cmd/rm, cmd/run, cmd/selftest,
cmd/session, cmd/start, cmd/status, cmd/stop, cmd/key sudo, cmd/sync,
cmd/test, cmd/version, cmd/vm, cmd/zed, the `wk` dispatcher, and
`setup` (docs/PLAN.md's "every WK_* override ... documented
... and covered by a test, or removed").

Each override kept here is a genuine tunable, already documented in the -h
header (or a driver's own header) of the file that reads it; this exercises
the override actually changing behaviour, generally by lifting the exact
line or block from its source file (as tests/test_wifi_seed.py's `_lift`
does) rather than re-implementing the logic a second time.

Run: python3 -m unittest tests.test_wk_overrides_cmd2 -v
"""
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from tests.support import REPO, WkTest, bash

CMD_PI = REPO / "cmd" / "pi"
CMD_STATUS = REPO / "cmd" / "status"
WK = REPO / "wk"
SETUP = REPO / "setup"


def _lift_range(path, start_pat, end_pat):
    """The literal text between (and including) the first line matching
    start_pat and the next line matching end_pat, sed'd out of a shell file
    -- the block form of tests/test_wifi_seed.py's `_lift`, for a block of
    top-level code that is not itself a function."""
    return subprocess.run(
        ["sed", "-n", f"/{start_pat}/,/{end_pat}/p", str(path)],
        capture_output=True, text=True,
    ).stdout


def _lift_fn(path, name):
    return subprocess.run(
        ["sed", "-n", f"/^{name}()/,/^}}/p", str(path)],
        capture_output=True, text=True,
    ).stdout


def _grep_line(path, pattern):
    """The one line matching pattern -- there must be exactly one, so a
    test built on it cannot silently start reading the wrong line."""
    out = subprocess.run(
        ["grep", "-n", pattern, str(path)], capture_output=True, text=True
    ).stdout.splitlines()
    assert len(out) == 1, f"expected exactly one match for {pattern!r} in {path}, got {out}"
    return out[0].split(":", 1)[1]


class TestDispatcherProtocolDocumented(unittest.TestCase):
    """The dispatcher-set plumbing names are documented once, in the `wk`
    header, per CLAUDE.md ("Nothing is ad-hoc ... documented once")."""

    def test_wk_header_names_every_dispatcher_set_variable(self):
        header = "\n".join((REPO / "lib" / "wk" / "dispatch.py").read_text().splitlines()[:40])
        names = [
            "WK_NAME", "WK_IN_VM", "WK_ROW_LABEL", "WK_HOST_SELF",
            "WK_NO_DELEGATE", "WK_QUIET", "WK_FORCE",
            "WK_TARGET", "WK_CONFIG",
        ]
        missing = [n for n in names if n not in header]
        self.assertEqual(missing, [], f"not documented in the dispatcher's header: {missing}")


class TestNewTimeout(WkTest):
    """WK_NEW_TIMEOUT (cmd/new -h): how long `wk new` waits for its detached
    driver before giving up on watching it -- the driver itself is
    unaffected. The override shortening the wait is
    tests/test_wk_workspace.py's, on a fake clock."""

    def test_h_documents_it_and_every_other_override_new_reads(self):
        cp = subprocess.run([str(WK), "new", "-h"], cwd=str(REPO),
                             capture_output=True, text=True, timeout=10)
        for var in ("WK_NEW_TIMEOUT", "WK_READY_TIMEOUT", "WK_KILL_WAIT"):
            self.assertIn(var, cp.stdout + cp.stderr)


class TestPiTag(unittest.TestCase):
    """WK_PI_TAG (cmd/pi -h): the tailscale tag 'wk pi setup' advertises."""

    def test_h_documents_it(self):
        cp = subprocess.run([str(WK), "pi", "-h"], cwd=str(REPO),
                             capture_output=True, text=True, timeout=10)
        self.assertIn("WK_PI_TAG", cp.stdout + cp.stderr)

    def test_override_changes_the_tag(self):
        stmt = _grep_line(CMD_PI, 'TAG="\\${WK_PI_TAG').strip()
        self.assertTrue(stmt.startswith("TAG="), stmt)

        default = subprocess.run(
            ["bash", "-c", f'{stmt}; printf "%s" "$TAG"'],
            capture_output=True, text=True, env={},
        ).stdout
        self.assertEqual(default, "tag:wk")

        overridden = subprocess.run(
            ["bash", "-c", f'{stmt}; printf "%s" "$TAG"'],
            capture_output=True, text=True, env={"WK_PI_TAG": "tag:custom"},
        ).stdout
        self.assertEqual(overridden, "tag:custom")


class TestQuiesceSettleSeconds(WkTest):
    """WK_SETTLE_SECONDS (cmd/quiesce -h, already documented): the settle
    time 'quiesce on' sleeps before returning; tests/test_quiesce.py holds the
    sleep to it against a fake clock."""

    def test_h_documents_it(self):
        cp = subprocess.run([str(WK), "quiesce", "-h"], cwd=str(REPO),
                             capture_output=True, text=True, timeout=10)
        self.assertIn("WK_SETTLE_SECONDS", cp.stdout + cp.stderr)


class TestStatusFleetTimeout(unittest.TestCase):
    """WK_FLEET_TIMEOUT (cmd/status -h): already exercised end to end by
    tests/test_ceilings.py's test_no_fleet_probe_can_outlive_its_ceiling; this
    only checks the -h documents it (the audit's other half)."""

    def test_h_documents_it(self):
        cp = subprocess.run([str(WK), "status", "-h"], cwd=str(REPO),
                             capture_output=True, text=True, timeout=10)
        self.assertIn("WK_FLEET_TIMEOUT", cp.stdout + cp.stderr)


class TestStatusBridgeTimeout(unittest.TestCase):
    """WK_BRIDGE_TIMEOUT (cmd/status -h): the ceiling on one bridge phone's
    health check, which calls ssh directly rather than through the fleet
    probe -- so it has a ceiling of its own."""

    def test_h_documents_it(self):
        cp = subprocess.run([str(WK), "status", "-h"], cwd=str(REPO),
                             capture_output=True, text=True, timeout=10)
        self.assertIn("WK_BRIDGE_TIMEOUT", cp.stdout + cp.stderr)

    def test_a_wedged_ssh_cannot_outlive_the_ceiling(self):
        import sys
        sys.path.insert(0, str(REPO / "lib"))
        from wk import status
        with tempfile.TemporaryDirectory(prefix="wk-test-bridge-") as tmp:
            stub = Path(tmp) / "ssh"
            stub.write_text("#!/bin/sh\nsleep 30\n")
            stub.chmod(0o755)
            env = dict(os.environ, PATH="%s:%s" % (tmp, os.environ.get("PATH", "")))
            with unittest.mock.patch.dict(os.environ, env):
                out = status.bridge_ssh("testphone", status.BRIDGE_PROBE, True, 1, 1)
        self.assertEqual(out, "")
        rec = status.bridge_record("testphone", {}, "wantsum", status.kv(out), lambda n: ("", ""))
        self.assertEqual(rec["state"], "unreachable")

class TestStatusWait(unittest.TestCase):
    """WK_WAIT_TIMEOUT and WK_WAIT_INTERVAL (cmd/status -h): --wait's default
    timeout and poll interval, read where the command starts its wait."""

    def test_h_documents_both(self):
        cp = subprocess.run([str(WK), "status", "-h"], cwd=str(REPO),
                             capture_output=True, text=True, timeout=10)
        out = cp.stdout + cp.stderr
        self.assertIn("WK_WAIT_TIMEOUT", out)
        self.assertIn("WK_WAIT_INTERVAL", out)

    def test_timeout_and_interval_are_both_read(self):
        text = CMD_STATUS.read_text()
        self.assertIn('env.get("WK_WAIT_TIMEOUT", "0")', text)
        self.assertIn('env.get("WK_WAIT_INTERVAL", "5")', text)

class TestSudoTimeoutMin(unittest.TestCase):
    """WK_SUDO_TIMEOUT_MIN (wk key -h): the sudoers timestamp window, in
    minutes. Sudo.timeout_desc (lib/wk/sudo.py -- the seconds spelling of the
    same value, printed in every verdict line) is derived from it in the
    constructor, so the two cannot disagree."""

    def test_h_documents_it_by_its_real_name(self):
        cp = subprocess.run([str(WK), "key", "-h"], cwd=str(REPO),
                             capture_output=True, text=True, timeout=10)
        out = cp.stdout + cp.stderr
        self.assertIn("WK_SUDO_TIMEOUT_MIN", out)

    def test_description_tracks_an_override(self):
        sys.path.insert(0, str(REPO / "lib"))
        from wk.sudo import Sudo
        self.assertEqual(Sudo(None, {}).timeout_desc, "30 seconds")
        self.assertEqual(Sudo(None, {"WK_SUDO_TIMEOUT_MIN": "2"}).timeout_desc, "120 seconds")


class TestSyncBranch(unittest.TestCase):
    """WK_BRANCH (cmd/sync -h): publishes a snapshot from this branch
    instead of origin/main. Exercised on lib/wk/sync.py's reader -- driving
    a real 'wk sync --tools' would fetch all of WebKit."""

    def test_h_documents_it(self):
        cp = subprocess.run([str(WK), "sync", "-h"], cwd=str(REPO),
                             capture_output=True, text=True, timeout=10)
        self.assertIn("WK_BRANCH", cp.stdout + cp.stderr)

    def test_override_changes_the_published_branch(self):
        sys.path.insert(0, str(REPO / "lib"))
        from wk import sync
        self.assertEqual(sync.publish_branch({}), "origin/main")
        self.assertEqual(sync.publish_branch({"WK_BRANCH": "wpe-2.44"}), "wpe-2.44")


class TestSetupDryRun(unittest.TestCase):
    """WK_DRY_RUN (setup -h): --dry-run's own env spelling, also read by
    host/macos/sharing.sh. `setup -h` must actually print the line that
    documents it (a prior off-by-one in the sed range cut it off)."""

    def test_h_prints_the_dry_run_line(self):
        cp = subprocess.run([str(SETUP), "-h"], cwd=str(REPO),
                             capture_output=True, text=True, timeout=10)
        out = cp.stdout + cp.stderr
        self.assertIn("--dry-run", out)
        self.assertIn("WK_DRY_RUN", out)

    def test_sharing_sh_default_agrees_with_setup(self):
        setup_default = _grep_line(SETUP, 'export WK_DRY_RUN=""')
        self.assertIn('WK_DRY_RUN=""', setup_default)
        sharing = (REPO / "host" / "macos" / "sharing.sh").read_text()
        self.assertIn("${WK_DRY_RUN:-}", sharing)


if __name__ == "__main__":
    unittest.main()
