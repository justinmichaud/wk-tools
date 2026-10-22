"""A configured machine name resolves as a place and its driver answers."""
import os
import sys
import unittest

from tests.support import REAL_MACHINES, REPO, WkTest, live_selected, machine_reachable

sys.path.insert(0, str(REPO / "lib"))
from wk import places  # noqa: E402


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
