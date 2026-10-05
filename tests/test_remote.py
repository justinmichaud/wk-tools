"""A configured machine name resolves as a place and its driver answers; a fan-out names a machine that is down."""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

from tests.support import REAL_MACHINES, REPO, WkTest, live_selected, machine_reachable, stub_path

sys.path.insert(0, str(REPO / "lib"))
from wk import places, sudo  # noqa: E402


class TestUnregisteredWorkspaceResolves(WkTest):
    def test_unregistered_remote_workspace_resolves(self):
        registry = self.tmp / "hosts"
        registry.mkdir()
        root = self.tmp / "root"
        (root / "ws" / "tws").mkdir(parents=True)
        (root / "ws" / "tws" / ".wk-ready").write_text("")
        store = self.tmp / "store"
        store.mkdir()
        # The store is apart from the root, so only Remote.info can find the workspace.
        (registry / "fakebox.conf").write_text(
            "kind=build\ndriver=remote\n"
            "local=1\n"
            f"root={root}\n"
            f"store={store}\n"
        )
        reg = places.Registry(REPO, env=dict(os.environ, WK_MACHINES_DIR=str(registry),
                                              XDG_STATE_HOME=str(self.tmp / "state")))
        self.assertEqual(reg.ws_place("tws"), "fakebox")


class TestMachineAnswers(WkTest):
    """A fan-out tells a machine that is down from one with no wk-tools, in one line."""

    def _registry(self, conf):
        registry = self.tmp / "hosts"
        registry.mkdir(exist_ok=True)
        (registry / "fakebox.conf").write_text(conf)
        return registry

    def _machine_answers(self, conf):
        ssh = ('echo "ssh: Could not resolve hostname wk-test-unreachable.invalid:'
               ' nodename nor servname provided, or not known" >&2; exit 255')
        with stub_path({"ssh": ssh}) as binp, \
                mock.patch.dict(os.environ, {"PATH": f"{binp}:{os.environ.get('PATH', '/usr/bin:/bin')}"}), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(out):
            env = dict(os.environ, WK_MACHINES_DIR=str(self._registry(conf)),
                       XDG_STATE_HOME=str(self.tmp / "state"), WK_SSH_TIMEOUT="3")
            answered = sudo.machine_answers(places.Registry(REPO, env=env).load("fakebox"), "fakebox")
        return answered, out.getvalue()

    def test_an_unreachable_machine_is_reported_as_unreachable(self):
        answered, out = self._machine_answers(
            "kind=build\ndriver=remote\nhost=wk-test-unreachable.invalid\n")
        self.assertFalse(answered, out)
        self.assertRegex(out, r"(?m)^fakebox\s+unreachable over ssh: \S", out)
        self.assertNotIn("no wk-tools", out)
        self.assertNotIn("not on the tailnet", out, "a guess where ssh's own reason belongs")
        self.assertEqual(len([l for l in out.splitlines() if "fakebox" in l]), 1,
                         "one line per machine, not ssh's whole stderr: " + out)

    def test_the_machine_itself_is_not_a_far_side(self):
        answered, out = self._machine_answers("kind=build\ndriver=remote\nlocal=1\n")
        self.assertFalse(answered, out)
        self.assertRegex(out, r"(?m)^fakebox\s+not a machine of its own", out)


def _configured_remote_machines():
    """{name: kind} for every place conf in machines/."""
    reg = places.Registry(REPO, env=dict(os.environ, WK_MACHINES_DIR=str(REAL_MACHINES)))
    return {n: reg.kind(n) for n in reg.known()}


class TestRemoteConfsResolve(WkTest):
    def test_remote_confs_resolve(self):
        machines = _configured_remote_machines()
        if not machines:
            self.skipTest(f"no machines configured in {REAL_MACHINES}")
        bad = [n for n, k in machines.items() if not k]
        self.assertEqual(bad, [], f"configured but unresolvable: {bad}")


class TestRemoteReachable(unittest.TestCase):
    wk_tier = "live"

    def test_each_configured_machine_that_answers_reports_on_a_workspace(self):
        machines = [n for n in _configured_remote_machines() if machine_reachable(n)] if live_selected() else []
        if not machines:
            self.skipTest("live tier not selected, or no configured machine answers over ssh")
        reg = places.Registry(REPO, env=dict(os.environ, WK_MACHINES_DIR=str(REAL_MACHINES)))
        for name in machines:
            with self.subTest(machine=name):
                self.assertEqual(reg.load(name).info("selftest-nonexistent"), "absent")


if __name__ == "__main__":
    unittest.main()
