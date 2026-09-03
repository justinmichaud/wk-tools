"""What the dispatcher hands the command it runs: the target the name
resolved to.

`resolve_target` walks every target that could hold a workspace name -- the
environments on this machine, then every machine of its own over ssh
(ws_locate, lib/target.sh) -- and `wk` does it once per invocation. The
answer is then handed on in WK_TARGET, which `ws_target` reads before it
asks anybody, so the `ws_target "$WK_NAME"` in the command that runs
(cmd/enter, cmd/build, cmd/logs, ...) is answered by that one walk instead
of starting a second.

Only for the command that runs *here*: a command forwarded into the podman
VM or delegated to the machine that owns the workspace is resolved over
there, where the target is that machine's own, and a lifecycle command
(`wk new`, `wk rm <a> <b>`) is handed a name with nothing behind it yet and
a list of names of its own.

The fixtures are tests/test_dispatch_speed.py's: fake machine confs in a
registry of this test's own, a stub `ssh` that records being asked, and one
local target whose store really holds the workspace. What runs is a probe
command in a scratch WK_ROOT of symlinks (the technique tests/test_peer.py
uses for a fake fleet): it prints the environment it was given and then
calls `ws_target` itself, reporting whether that call walked anything.

Run: python3 -m unittest tests.test_dispatch_handoff -v
"""
import os
import subprocess
import unittest

from tests.support import REPO, WkTest, stub_path
from tests.test_dispatch_speed import _LOCAL_CONF, _MACHINE_CONF, _WITNESS_SSH

# The command under test's stand-in: no real command prints its own
# environment, and the question here is exactly what the dispatcher put in
# it. Declared like any other workspace command, so the dispatcher treats it
# like one.
#
# `walked` is the property that matters: the witness file every stub on PATH
# appends to grows when a target is asked anything, so its size either side
# of this command's own `ws_target` says whether that call did a walk of its
# own or was answered from what it was handed.
_PROBE = '''#!/usr/bin/env bash
#
# wk probe <workspace> -- print what the dispatcher handed over
# wk: where=workspace name=required group=other readonly
#
# A test probe (tests/test_dispatch_handoff.py), never installed.
set -euo pipefail
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/target.sh"
printf 'WK_NAME=%s\\n' "${WK_NAME:-unset}"
printf 'WK_TARGET=%s\\n' "${WK_TARGET:-unset}"
_w="${WK_TEST_WITNESS:-/dev/null}"
_before=$( (wc -c < "$_w") 2>/dev/null || echo 0)
printf 'ws_target=%s\\n' "$(ws_target "${WK_NAME:-}")"
_after=$( (wc -c < "$_w") 2>/dev/null || echo 0)
if [ "$_before" = "$_after" ]; then printf 'walked=no\\n'; else printf 'walked=yes\\n'; fi
'''

# Every target driver reaches its environments through one of these; each
# records that it was asked and then answers nothing, so a walk is visible
# and no real container, guest or machine is touched.
_WITNESS = '''#!/bin/sh
echo "$0 $*" >> "${WK_TEST_WITNESS:-/dev/null}"
exit 1
'''


class TestTheResolvedTargetIsHandedOn(WkTest):
    """the target the name resolved to reaches the command, and it walks nothing"""

    def setUp(self):
        super().setUp()
        # A WK_ROOT of symlinks to this checkout, with a cmd/ of its own so
        # the probe can sit beside the real commands. `wk` decides its root
        # from the directory it is *in* when that directory is a checkout,
        # which is what makes this work (see the top of `wk`).
        self.root = self.tmp / "wk-root"
        (self.root / "cmd").mkdir(parents=True)
        for entry in REPO.iterdir():
            if entry.name in ("cmd", ".git", "__pycache__"):
                continue
            (self.root / entry.name).symlink_to(entry)
        for entry in (REPO / "cmd").iterdir():
            if entry.name == "__pycache__":
                continue
            (self.root / "cmd" / entry.name).symlink_to(entry)
        probe = self.root / "cmd" / "probe"
        probe.write_text(_PROBE)
        probe.chmod(0o755)

        # One target on this machine that really holds the workspace, and one
        # machine of its own that could only be asked over ssh.
        self.registry = self.tmp / "hosts"
        self.registry.mkdir()
        store = self.tmp / "store"
        store.mkdir()
        wsroot = self.tmp / "root"
        (wsroot / "ws" / "handoff-ws").mkdir(parents=True)
        (wsroot / "ws" / "handoff-ws" / ".wk-ready").write_text("")
        (self.registry / "fakelocal.conf").write_text(
            _LOCAL_CONF.format(root=wsroot, store=store))
        (self.registry / "fakemachine.conf").write_text(
            _MACHINE_CONF.format(host="fakemachine.invalid"))
        self.witness = self.tmp / "witness"

    def _probe(self, *args):
        with stub_path({"ssh": _WITNESS_SSH, "podman": _WITNESS,
                        "tart": _WITNESS}) as binp:
            cp = subprocess.run(
                [str(self.root / "wk"), *args],
                cwd=str(self.root),
                env={
                    "PATH": f"{binp}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                    "HOME": str(self.tmp / "home"),
                    "WK_TARGET_REGISTRY": str(self.registry),
                    "XDG_STATE_HOME": str(self.tmp / "state"),
                    "WK_TEST_WITNESS": str(self.witness),
                    "WK_TEST_SSH_WITNESS": str(self.witness),
                    "WK_SSH_TIMEOUT": "5",
                },
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=120,
            )
        fields = dict(
            line.split("=", 1) for line in cp.stdout.splitlines() if "=" in line
            and line.split("=", 1)[0] in ("WK_NAME", "WK_TARGET", "ws_target", "walked")
        )
        return cp, fields

    def test_the_command_is_told_which_target_the_name_is_on(self):
        """WK_TARGET reaches the command as the target the walk resolved"""
        cp, f = self._probe("probe", "handoff-ws")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual(f.get("WK_NAME"), "handoff-ws", cp.stdout)
        self.assertEqual(f.get("WK_TARGET"), "fakelocal", cp.stdout)

    def test_the_command_does_not_walk_a_second_time(self):
        """`ws_target "$WK_NAME"` in the command asks nothing"""
        cp, f = self._probe("probe", "handoff-ws")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual(f.get("ws_target"), "fakelocal", cp.stdout)
        self.assertEqual(
            f.get("walked"), "no",
            "the command re-walked the targets to learn what it was handed:\n"
            + (self.witness.read_text() if self.witness.exists() else "")
            + cp.stdout,
        )

    def test_a_workspace_on_this_machine_never_reaches_the_fleet(self):
        """no ssh at all: the walk stops at this machine's own targets"""
        cp, _ = self._probe("probe", "handoff-ws")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        asked = self.witness.read_text() if self.witness.exists() else ""
        self.assertNotIn(
            "ssh", asked,
            f"a machine of its own was asked over ssh:\n{asked}\n{cp.stdout}")

    def test_an_explicit_target_is_what_the_command_gets(self):
        """`--target` and a set WK_TARGET are the same answer, handed on unchanged"""
        cp, f = self._probe("probe", "handoff-ws", "--target", "fakelocal")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual(f.get("WK_TARGET"), "fakelocal", cp.stdout)


class TestWhatIsNotToldTheTarget(unittest.TestCase):
    """The two paths the export must not be on, read from `wk` itself: a
    forwarded child resolves inside the podman VM and a delegated one on the
    machine that owns the workspace, so WK_TARGET travels on neither, and a
    lifecycle command resolves each of its own names."""

    def test_the_forwarded_environment_carries_no_target(self):
        text = (REPO / "wk").read_text()
        cmd = [l for l in text.splitlines() if l.strip().startswith('_cmd="WK_IN_VM=1')]
        self.assertEqual(len(cmd), 1, "forward_to_vm's command line moved")
        self.assertNotIn("WK_TARGET=", cmd[0],
                         "a forwarded command is told a target it must resolve itself")

    def test_the_export_is_on_the_running_here_path_only(self):
        text = (REPO / "wk").read_text()
        exports = [l for l in text.splitlines() if 'WK_TARGET="$resolved"' in l]
        self.assertEqual(len(exports), 1, f"more than one place exports it: {exports}")
        self.assertIn("D_LIFECYCLE", exports[0],
                      "a lifecycle command is handed a target it resolves per name")


if __name__ == "__main__":
    unittest.main()
