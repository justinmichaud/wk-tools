"""Coverage for the WK_* overrides read (with a default) in this agent's
files: cmd/mcp, cmd/new, cmd/pi, cmd/pick, cmd/pr, cmd/profile, cmd/push,
cmd/quiesce, cmd/remote, cmd/remotes, cmd/rm, cmd/run, cmd/selftest,
cmd/session, cmd/skills, cmd/start, cmd/status, cmd/stop, cmd/sudo, cmd/sync,
cmd/test, cmd/verify, cmd/version, cmd/vm, cmd/zed, the `wk` dispatcher, and
`setup` (docs/HANDOFF-test-runner.md's "every WK_* override ... documented
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
import tempfile
import time
import unittest
from pathlib import Path

from tests.support import REPO, WkTest, bash

CMD_NEW = REPO / "cmd" / "new"
CMD_PI = REPO / "cmd" / "pi"
CMD_QUIESCE = REPO / "cmd" / "quiesce"
CMD_STATUS = REPO / "cmd" / "status"
CMD_SUDO = REPO / "cmd" / "sudo"
CMD_SYNC = REPO / "cmd" / "sync"
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
        header = "\n".join(WK.read_text().splitlines()[:40])
        names = [
            "WK_NAME", "WK_IN_VM", "WK_ROW_LABEL", "WK_HOST_SELF",
            "WK_NO_DELEGATE", "WK_QUIET", "WK_FORCE",
            "WK_TARGET", "WK_CONFIG",
        ]
        missing = [n for n in names if n not in header]
        self.assertEqual(missing, [], f"not documented in wk's header: {missing}")


class TestNewExitStatus(unittest.TestCase):
    """WK_EXIT_STATUS (cmd/new): published by lib/common.sh's atexit trap,
    not a knob a person sets -- documented at the read site so a reader
    does not mistake it for one."""

    def test_read_site_names_the_publisher(self):
        text = CMD_NEW.read_text()
        self.assertIn("WK_EXIT_STATUS: published by the atexit trap", text)


class TestNewTimeout(WkTest):
    """WK_NEW_TIMEOUT (cmd/new -h): how long `wk new` waits for its detached
    driver before giving up on watching it -- the driver itself is
    unaffected. cmd/new passes it straight to lib/detach.sh's detach_wait,
    so this drives that same function the way cmd/new's own read does."""

    def test_h_documents_it(self):
        cp = subprocess.run([str(WK), "new", "-h"], cwd=str(REPO),
                             capture_output=True, text=True, timeout=10)
        self.assertIn("WK_NEW_TIMEOUT", cp.stdout + cp.stderr)

    def test_override_shortens_the_wait(self):
        line = _grep_line(CMD_NEW, "detach_wait.*WK_NEW_TIMEOUT")
        self.assertIn("detach_wait", line)

        script = f'''
set -euo pipefail
WK_ROOT="{REPO}"
. "{REPO}/lib/common.sh"
. "{REPO}/lib/detach.sh"
SF=$(mktemp)
LOG=/nonexistent-log
_pid=$$
status_write "$SF" state=creating "pid=$_pid" stage=init
t0=$(date +%s)
{line.strip()}
d=$(( $(date +%s) - t0 ))
printf 'state=%s elapsed=%s\\n' "$_st" "$d"
rm -f "$SF"
'''
        cp = self.bash(script, env={"WK_NEW_TIMEOUT": "2"}, timeout=20)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("state=timeout", cp.stdout, cp.stdout)
        m = re.search(r"elapsed=(\d+)", cp.stdout)
        self.assertIsNotNone(m)
        # Default is 3600s; anything well under that proves the override
        # was read, not the default.
        self.assertLessEqual(int(m.group(1)), 8, cp.stdout)


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
    time 'quiesce on' sleeps before returning -- exercised through
    lib/common.sh's wk_sleep with the exact expression cmd/quiesce reads,
    never through a real 'quiesce on' (that mutates the host)."""

    def test_h_documents_it(self):
        cp = subprocess.run([str(WK), "quiesce", "-h"], cwd=str(REPO),
                             capture_output=True, text=True, timeout=10)
        self.assertIn("WK_SETTLE_SECONDS", cp.stdout + cp.stderr)

    def test_override_shortens_the_settle(self):
        line = _grep_line(CMD_QUIESCE, "WK_SETTLE_SECONDS:-30").strip()
        self.assertTrue(line.startswith("wk_sleep"), line)

        script = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
t0=$(date +%s)
{line}
printf '%s' $(( $(date +%s) - t0 ))
'''
        cp = self.bash(script, env={"WK_SETTLE_SECONDS": "1"}, timeout=10)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        # Default is 30s; well under that proves the override was read.
        self.assertLessEqual(int(cp.stdout.strip()), 4, cp.stdout)


class TestQuiesceStateReadSitesPointAtTheTest(unittest.TestCase):
    """WK_QUIESCE_STATE is a test hook (tests/test_quiesce.py sets it);
    both read sites say so, per the audit's rule (b)."""

    def test_cmd_quiesce_names_the_test(self):
        self.assertIn("tests/test_quiesce.py", CMD_QUIESCE.read_text())

    def test_cmd_status_names_the_test(self):
        self.assertIn("tests/test_quiesce.py", CMD_STATUS.read_text())

    def test_the_default_is_the_same_in_both(self):
        q = _grep_line(CMD_QUIESCE, "WK_QUIESCE_STATE:-").strip()
        s = _grep_line(CMD_STATUS, "WK_QUIESCE_STATE:-").strip()
        self.assertIn('WK_QUIESCE_STATE:-$(wk_state_dir)/quiesce', q)
        self.assertIn('WK_QUIESCE_STATE:-$(wk_state_dir)/quiesce', s)


class TestStatusFleetTimeout(unittest.TestCase):
    """WK_FLEET_TIMEOUT (cmd/status -h): already exercised end to end by
    tests/test_quick.py's test_no_fleet_probe_can_outlive_its_ceiling; this
    only checks the -h documents it (the audit's other half)."""

    def test_h_documents_it(self):
        cp = subprocess.run([str(WK), "status", "-h"], cwd=str(REPO),
                             capture_output=True, text=True, timeout=10)
        self.assertIn("WK_FLEET_TIMEOUT", cp.stdout + cp.stderr)


class TestStatusBridgeTimeout(unittest.TestCase):
    """WK_BRIDGE_TIMEOUT (cmd/status -h): the ceiling on one bridge phone's
    health check (_bridge_probe), which calls ssh directly rather than
    through report_fleet_device's probe wrapper -- so it needs its own
    ceiling test, built the same way test_quick.py's fleet-probe one is."""

    def test_h_documents_it(self):
        cp = subprocess.run([str(WK), "status", "-h"], cwd=str(REPO),
                             capture_output=True, text=True, timeout=10)
        self.assertIn("WK_BRIDGE_TIMEOUT", cp.stdout + cp.stderr)

    def test_a_wedged_ssh_cannot_outlive_the_ceiling(self):
        fns = ["_jesc", "rec_start", "note", "bridge_role_sum",
               "_bridge_probe_q", "_bridge_probe"]
        body = "\n".join(_lift_fn(CMD_STATUS, fn) for fn in fns)
        for fn in fns:
            self.assertIn(f"{fn}()", body, f"lift of {fn} failed")

        script = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
{body}
reach_tailnet() {{ return 1; }}
reach_without_tailnet() {{ return 1; }}
ssh() {{ sleep 30; }}
WK_FLEET_TIMEOUT=1
t0=$(date +%s)
out=$(_bridge_probe testphone /dev/null wantsum dev seg note 3>&1 2>/dev/null)
d=$(( $(date +%s) - t0 ))
printf 'elapsed=%s\\n%s\\n' "$d" "$out"
'''
        cp = subprocess.run(["bash", "-c", script], capture_output=True,
                             text=True, timeout=30,
                             env={**os.environ, "WK_BRIDGE_TIMEOUT": "1"})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        m = re.search(r"elapsed=(\d+)", cp.stdout)
        self.assertIsNotNone(m, cp.stdout)
        # Two ceilinged ssh calls at 1s each; the default (20s) would take
        # up to 40s -- well under that proves the override was read.
        self.assertLessEqual(int(m.group(1)), 10, cp.stdout)
        self.assertIn('"state":"unreachable"', cp.stdout)


class TestStatusWait(unittest.TestCase):
    """WK_WAIT_TIMEOUT and WK_WAIT_INTERVAL (cmd/status -h): --wait's
    default timeout and poll interval. Exercised by lifting the exact
    `if [ -n "$WAIT" ]; then ... fi` block and pointing its self-invocation
    ("$0") at a stub that always reports busy, rather than driving a real
    workspace -- 'wk status --wait' polls itself, so the stub takes that
    role directly."""

    def _block(self):
        block = _lift_range(CMD_STATUS,
                             r'^if \[ -n "\$WAIT" \]; then$', r'^fi$')
        self.assertTrue(block.startswith('if [ -n "$WAIT" ]; then'), block)
        self.assertIn("WK_WAIT_INTERVAL", block)
        return block

    def test_h_documents_both(self):
        cp = subprocess.run([str(WK), "status", "-h"], cwd=str(REPO),
                             capture_output=True, text=True, timeout=10)
        out = cp.stdout + cp.stderr
        self.assertIn("WK_WAIT_TIMEOUT", out)
        self.assertIn("WK_WAIT_INTERVAL", out)

    def test_timeout_and_interval_are_both_read(self):
        block = self._block()
        with tempfile.TemporaryDirectory(prefix="wk-test-wait-") as tmp:
            tmp = Path(tmp)
            count = tmp / "count"
            stub = tmp / "fakewk"
            stub.write_text(f'#!/bin/bash\necho x >> "{count}"\nexit 2\n')
            stub.chmod(0o755)

            script = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
WAIT=1
WAIT_TIMEOUT="${{WK_WAIT_TIMEOUT:-0}}"
{block}
'''
            env = dict(os.environ)
            env["WK_WAIT_INTERVAL"] = "1"
            env["WK_WAIT_TIMEOUT"] = "3"
            t0 = time.time()
            cp = subprocess.run(["bash", "-c", script, str(stub)],
                                 capture_output=True, text=True,
                                 timeout=20, env=env)
            elapsed = time.time() - t0
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("still busy after 3s", cp.stderr, cp.stderr)
            # The default interval is 5s and the default timeout is 0
            # (forever); a run this short, that stopped, proves both
            # overrides -- not the defaults -- were read.
            self.assertLess(elapsed, 8, cp.stderr)
            invocations = len(count.read_text().splitlines())
            self.assertEqual(invocations, 4, "expected one poll per second for 3s, plus the first")


class TestSudoTimeoutMin(unittest.TestCase):
    """WK_SUDO_TIMEOUT_MIN (cmd/sudo -h): the sudoers timestamp window, in
    minutes. WK_SUDO_TIMEOUT_DESC (the seconds spelling of the same value,
    printed in every verdict line) is derived from it so the two cannot
    disagree -- this is the regression the derivation guards against."""

    def test_h_documents_it_by_its_real_name(self):
        cp = subprocess.run([str(WK), "sudo", "-h"], cwd=str(REPO),
                             capture_output=True, text=True, timeout=10)
        out = cp.stdout + cp.stderr
        self.assertIn("WK_SUDO_TIMEOUT_MIN", out)

    def test_description_tracks_an_override(self):
        text = CMD_SUDO.read_text()
        m = re.search(
            r'^WK_SUDO_TIMEOUT_MIN=.*\n^WK_SUDO_TIMEOUT_DESC=(.*)$',
            text, re.M,
        )
        self.assertIsNotNone(m, "could not find the WK_SUDO_TIMEOUT_DESC assignment")
        script = f'''
set -euo pipefail
WK_SUDO_TIMEOUT_MIN="${{WK_SUDO_TIMEOUT_MIN:-0.5}}"
WK_SUDO_TIMEOUT_DESC={m.group(1)}
printf '%s' "$WK_SUDO_TIMEOUT_DESC"
'''
        default = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env={})
        self.assertEqual(default.stdout, "30 seconds", default.stdout + default.stderr)

        overridden = subprocess.run(["bash", "-c", script], capture_output=True,
                                     text=True, env={"WK_SUDO_TIMEOUT_MIN": "2"})
        self.assertEqual(overridden.stdout, "120 seconds", overridden.stdout + overridden.stderr)


class TestSyncBranch(unittest.TestCase):
    """WK_BRANCH (cmd/sync -h): publishes a snapshot from this branch
    instead of origin/main. Exercised as a substitution, the same way
    cmd/sync reads it -- driving a real 'wk sync --tools' would fetch
    all of WebKit."""

    def test_h_documents_it(self):
        cp = subprocess.run([str(WK), "sync", "-h"], cwd=str(REPO),
                             capture_output=True, text=True, timeout=10)
        self.assertIn("WK_BRANCH", cp.stdout + cp.stderr)

    def test_override_changes_the_published_branch(self):
        stmt = _grep_line(CMD_SYNC, 'BRANCH="\\${WK_BRANCH').strip()
        self.assertTrue(stmt.startswith("BRANCH="), stmt)

        default = subprocess.run(["bash", "-c", f'{stmt}; printf "%s" "$BRANCH"'],
                                  capture_output=True, text=True, env={})
        self.assertEqual(default.stdout, "origin/main")

        overridden = subprocess.run(
            ["bash", "-c", f'{stmt}; printf "%s" "$BRANCH"'],
            capture_output=True, text=True,
            env={"WK_BRANCH": "wpe-2.44"},
        )
        self.assertEqual(overridden.stdout, "wpe-2.44")


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
