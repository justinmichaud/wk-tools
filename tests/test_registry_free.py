"""Registry.ws_target derives a workspace's target from evidence -- its store directory, or a live `wk new` record --
and the completion list walks the same stores."""
import os
import sys
import tempfile
import unittest

from tests.support import REPO, rand_suffix

sys.path.insert(0, str(REPO / "lib"))
from wk import completion, targets  # noqa: E402
from wk.record import Records  # noqa: E402


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="wk-registry-free-")
        self.tmp = self._tmp.name
        self.machines = os.path.join(self.tmp, "machines")
        os.makedirs(self.machines)
        self.addCleanup(self._tmp.cleanup)

    def registry(self, **env):
        base = {"HOME": os.path.join(self.tmp, "home"), "XDG_STATE_HOME": os.path.join(self.tmp, "state"),
                "WK_MACHINES_DIR": self.machines, "WK_STORE": os.path.join(self.tmp, "store"),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        base.update(env)
        return targets.Registry(REPO, env=base)


class TestWsTargetDerivesFromTheStore(RegistryTest):
    def test_ws_target_resolves_an_unknown_name_to_container(self):
        self.assertEqual(self.registry().ws_target(f"nowhere-{rand_suffix()}"), "container")

    def _creating(self, name, pid):
        store = os.path.join(self.tmp, "fakebox-store")
        with open(os.path.join(self.machines, "fakebox.conf"), "w") as fh:
            fh.write("kind=build\ndriver=remote\nlocal=1\n"
                     "root=%s\nstore=%s\n" % (store, store))
        Records(store, env={"WK_STORE": store}).begin("new", "here", name, "wk new %s --kill" % name, "/nolog",
                                                      ["checking"], pid=pid)

    def test_ws_target_resolves_a_live_creating_record_to_its_target(self):
        name = f"creating-{rand_suffix()}"
        self._creating(name, os.getpid())
        self.assertEqual(self.registry().ws_target(name), "fakebox")

    def test_ws_target_ignores_a_dead_creating_record(self):
        name = f"dead-{rand_suffix()}"
        self._creating(name, 4194304)
        self.assertEqual(self.registry().ws_target(name), "container")


class TestTargetAllReadsTheMachineRegistry(RegistryTest):
    """`Registry.all`: the built-ins plus one entry per conf in WK_MACHINES_DIR."""

    def builtins(self, reg):
        return ["container"] + (["vm"] if reg.vm_listed() else [])

    def test_a_registry_of_one_machine_is_the_whole_fleet(self):
        name = f"fakebox-{rand_suffix()}"
        with open(os.path.join(self.machines, f"{name}.conf"), "w") as fh:
            fh.write("kind=build\ndriver=remote\nhost=nonexistent.invalid\n")
        reg = self.registry()
        self.assertEqual(reg.all(), self.builtins(reg) + [name])

    def test_an_empty_registry_is_a_machine_that_knows_no_fleet(self):
        reg = self.registry()
        self.assertEqual(reg.all(), self.builtins(reg))


class TestCompletionListsTheStore(unittest.TestCase):
    def test_completion_list_workspaces_lists_fake_store_workspace(self):
        with tempfile.TemporaryDirectory(prefix="wk-registry-free-") as tmp:
            name = f"demo-{rand_suffix()}"
            os.makedirs(os.path.join(tmp, "ws", name))
            self.assertIn(name, completion.local_workspaces(str(REPO), {"WK_STORE": tmp}))


if __name__ == "__main__":
    unittest.main()
