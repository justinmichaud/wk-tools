"""A setup stage's --dry-run changes nothing: the Mac's broker stage against a scratch HOME and a stand-in launchctl."""
import os
import subprocess
import sys
import unittest

from tests.support import REPO, WkTest


@unittest.skipUnless(sys.platform == "darwin", "host/macos/broker.sh is a Mac's stage")
class TestTheBrokerStage(WkTest):
    def run_stage(self, *flags):
        home = self.tmp / "home"
        (home / ".config").mkdir(parents=True, exist_ok=True)
        binp = self.tmp / "bin"
        binp.mkdir(exist_ok=True)
        (binp / "launchctl").write_text('#!/bin/sh\necho "$*" >> "%s"\n[ "$1" != print ]\n' % (self.tmp / "launchctl.log"))
        (binp / "launchctl").chmod(0o755)
        env = dict(os.environ, HOME=str(home), XDG_STATE_HOME=str(home / ".state"), PATH="%s:%s" % (binp, os.environ["PATH"]))
        cp = subprocess.run([str(REPO / "setup"), "--stage", "broker", *flags], cwd=str(REPO), env=env, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=120)
        log = self.tmp / "launchctl.log"
        return cp.stdout + cp.stderr, (log.read_text().split() if log.exists() else []), home

    def test_a_dry_run_installs_and_starts_nothing(self):
        out, calls, home = self.run_stage("--dry-run")
        self.assertIn("would install com.wk.broker.plist", out)
        self.assertIn("would start com.wk.broker", out)
        self.assertFalse((home / "Library" / "LaunchAgents" / "com.wk.broker.plist").exists())
        self.assertEqual(set(), {c for c in calls if c in ("bootstrap", "bootout", "kickstart")}, calls)
