"""Registry-free target resolution (Registry.ws_target, lib/wk/targets.py)
derives which target a workspace lives on from evidence -- a stat per
configured target against its own store, or, before that store exists, the
task record `wk new` writes as its first act into that store, trusted only
while the process writing it is alive -- never from a workspace->target
registry, because there is no longer one to consult. The completion script's
workspace list derives the same way, walking the stores this machine can see.
Each docstring is the phrase of the behaviour it checks.

There is no hit/miss pair to test any more: a registry is a cache that can
answer right or wrong about a fact recomputed elsewhere, so testing it means
testing both cases. A derivation has exactly one answer, so there is nothing
to compare it against -- only whether that one answer is right.

Run: python3 -m unittest tests.test_registry_free -v
The podman-gated assertion (no registry file left behind by a real `wk new`)
lives in tests.test_lifecycle's existing container-lifecycle test, not here.
"""
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
    def test_ws_target_resolves_a_workspace_in_a_fake_local_store(self):
        """`ws_target` resolves a workspace that exists only in a fake local store"""
        name = f"demo-{rand_suffix()}"
        os.makedirs(os.path.join(self.tmp, "store", "ws", name))
        self.assertEqual(
            self.registry().ws_target(name), "container",
            "a workspace directory under $WK_STORE/ws is a container workspace: "
            "container is the one built-in kind whose store is plain $WK_STORE",
        )

    def test_ws_target_resolves_an_unknown_name_to_container(self):
        """a name in no store resolves to `container`"""
        self.assertEqual(self.registry().ws_target(f"nowhere-{rand_suffix()}"), "container")

    def _creating(self, name, pid):
        """A machine of this one's own whose store holds only `wk new`'s record of `name`, taken by `pid`."""
        store = os.path.join(self.tmp, "fakebox-store")
        with open(os.path.join(self.machines, "fakebox.conf"), "w") as fh:
            fh.write("kind=build\ndriver=remote\nlocal=1\n"
                     "root=%s\nstore=%s\n" % (store, store))
        Records(store, env={"WK_STORE": store}).begin("new", "here", name, "wk new %s --kill" % name, "/nolog",
                                                      ["checking"], pid=pid)

    def test_ws_target_resolves_a_live_creating_record_to_its_target(self):
        """a live creation record in a target's store resolves to that target before any store dir exists"""
        name = f"creating-{rand_suffix()}"
        # This process's pid: alive for exactly as long as the call that has to trust it.
        self._creating(name, os.getpid())
        self.assertEqual(
            self.registry().ws_target(name), "fakebox",
            "a live creation record in fakebox's store, with no workspace directory "
            "yet, should resolve to fakebox",
        )

    def test_ws_target_ignores_a_dead_creating_record(self):
        """a dead process's creation record is not trusted"""
        name = f"dead-{rand_suffix()}"
        self._creating(name, 4194304)
        self.assertEqual(
            self.registry().ws_target(name), "container",
            "a record left by a process that is no longer running is an "
            "unverifiable claim, not evidence -- it must not be believed",
        )


class TestTargetAllReadsTheMachineRegistry(RegistryTest):
    """`Registry.all` is container (and vm, where this host keeps a store for
    guests) plus one entry per machine conf, read from WK_MACHINES_DIR at the
    moment it is asked -- the seam that gives a test, or a second checkout, a
    fleet of its own instead of this machine's."""

    def builtins(self, reg):
        return ["container"] + (["vm"] if reg.vm_listed() else [])

    def test_a_registry_of_one_machine_is_the_whole_fleet(self):
        """all() lists the built-ins and exactly the confs in WK_MACHINES_DIR"""
        name = f"fakebox-{rand_suffix()}"
        with open(os.path.join(self.machines, f"{name}.conf"), "w") as fh:
            fh.write("kind=build\ndriver=remote\nhost=nonexistent.invalid\n")
        reg = self.registry()
        self.assertEqual(reg.all(), self.builtins(reg) + [name])

    def test_an_empty_registry_is_a_machine_that_knows_no_fleet(self):
        """an empty WK_MACHINES_DIR leaves only the built-in kinds"""
        reg = self.registry()
        self.assertEqual(reg.all(), self.builtins(reg))


class TestCompletionListsTheStore(unittest.TestCase):
    def test_completion_list_workspaces_lists_fake_store_workspace(self):
        """the completion script's workspace list holds the fake store's workspace"""
        with tempfile.TemporaryDirectory(prefix="wk-registry-free-") as tmp:
            name = f"demo-{rand_suffix()}"
            os.makedirs(os.path.join(tmp, "ws", name))
            self.assertIn(name, completion.local_workspaces(str(REPO), {"WK_STORE": tmp}))


if __name__ == "__main__":
    unittest.main()
