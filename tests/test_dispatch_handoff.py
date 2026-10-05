"""What the dispatcher hands the command it runs: the place the name resolved to."""
import os
import subprocess
import sys
import unittest

from tests.support import REPO, WkTest, stub_path
from tests.test_dispatch_speed import _LOCAL_CONF, _MACHINE_CONF, _WITNESS_SSH

sys.path.insert(0, str(REPO / "lib"))
from wk import places  # noqa: E402
from wk.machine import Fake  # noqa: E402

_PROBE = '''#!/usr/bin/env python3
#
# wk probe <workspace> -- print what the dispatcher handed over
# wk: where=workspace name=required group=other readonly opts --on=
#
# A test probe (tests/test_dispatch_handoff.py), never installed.
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
from wk import places


def witnessed():
    try:
        return os.path.getsize(os.environ.get("WK_TEST_WITNESS") or os.devnull)
    except OSError:
        return 0


print("WK_NAME=%s" % os.environ.get("WK_NAME", "unset"))
print("WK_PLACE=%s" % os.environ.get("WK_PLACE", "unset"))
before = witnessed()
print("ws_place=%s" % places.Registry(ROOT).ws_place(os.environ.get("WK_NAME", "")))
print("walked=%s" % ("no" if witnessed() == before else "yes"))
'''

_WITNESS = '''#!/bin/sh
echo "$0 $*" >> "${WK_TEST_WITNESS:-/dev/null}"
exit 1
'''


class TestTheResolvedPlaceIsHandedOn(WkTest):

    def setUp(self):
        super().setUp()
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
                    "WK_MACHINES_DIR": str(self.registry),
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
            and line.split("=", 1)[0] in ("WK_NAME", "WK_PLACE", "ws_place", "walked")
        )
        return cp, fields

    def test_the_command_is_told_which_target_the_name_is_on(self):
        cp, f = self._probe("probe", "handoff-ws")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual(f.get("WK_NAME"), "handoff-ws", cp.stdout)
        self.assertEqual(f.get("WK_PLACE"), "fakelocal", cp.stdout)

    def test_an_explicit_target_is_what_the_command_gets(self):
        cp, f = self._probe("probe", "handoff-ws", "--on", "fakelocal")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual(f.get("WK_PLACE"), "fakelocal", cp.stdout)


class TestWhatIsNotToldThePlace(unittest.TestCase):

    def test_the_forwarded_environment_carries_no_place(self):
        env = {"WK_PLACE": "box", "WK_STORE": "/here", "WK_ROW_LABEL": "host", "WK_YES": "1"}
        remote = {"WK_REMOTE_LOCAL": "1", "WK_REMOTE_TOOLS": "/opt/wk-tools"}
        for t in (places.Container("container", str(REPO), {}, Fake()), places.Remote("box", str(REPO), remote, Fake())):
            with self.subTest(kind=t.kind):
                line = t.wk_cmd(["status"], env)
                self.assertNotIn("WK_PLACE", line, "a forwarded command is told a place it must resolve itself")
                self.assertNotIn("WK_STORE", line, "the far side's store is its own")

    def test_the_sdk_image_override_is_carried_to_the_far_side(self):
        remote = {"WK_REMOTE_LOCAL": "1", "WK_REMOTE_TOOLS": "/opt/wk-tools"}
        line = places.Remote("box", str(REPO), remote, Fake()).wk_cmd(["new", "x"], {"WK_SDK_IMAGE": "ghcr.io/igalia/wkdev-sdk:2.55-v1-abc"})
        self.assertIn("WK_SDK_IMAGE=ghcr.io/igalia/wkdev-sdk:2.55-v1-abc ", line)


if __name__ == "__main__":
    unittest.main()
