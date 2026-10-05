"""Un-managed clobbering is detected (CLAUDE.md rule 5): a `podman rm` or `tart delete` by hand on a finished
workspace reads `broken`, through the real drivers and a stub podman/tart."""
import os
import sys
import unittest
from unittest import mock

from tests.support import REPO, WkTest, rand_suffix, stub_path, temp_store

sys.path.insert(0, str(REPO / "lib"))
from wk import targets  # noqa: E402
from wk.store import Store  # noqa: E402


class Clobbered(WkTest):
    def state(self, kind, env, binp, name):
        with mock.patch.dict(os.environ, {"PATH": "%s:%s" % (binp, os.environ["PATH"])}):
            env = dict(env, HOME=str(self.tmp), PATH=os.environ["PATH"])
            return targets.Registry(REPO, env=env).load(kind).state(name)


class TestPodmanRmByHand(Clobbered):
    def test_a_container_removed_by_hand_reads_broken(self):
        fake_podman = "case \"$*\" in *inspect*) exit 1 ;; *) exit 0 ;; esac\n"
        with temp_store() as store, stub_path({"podman": fake_podman}) as binp:
            name = f"demo-{rand_suffix()}"
            ws = store["path"] / "ws" / name
            (ws / "home").mkdir(parents=True)
            (ws / "home" / targets.READY_MARKER).write_text("")
            (ws / "base-id").write_text("deadbeef\n")
            st = self.state("container", {"WK_STORE": store["WK_STORE"], "WK_IN_VM": "1"}, binp, name)
            self.assertEqual(st, "broken")


class TestTartDeleteByHand(Clobbered):
    def test_a_guest_deleted_by_hand_reads_broken(self):
        fake_tart = 'case "$1" in list) echo "[]" ;; *) exit 1 ;; esac\n'
        with temp_store() as store, stub_path({"tart": fake_tart}) as binp, \
                mock.patch.object(Store, "macos_host", new_callable=mock.PropertyMock, return_value=True):
            name = f"demo-{rand_suffix()}"
            ws = store["path"] / "ws" / name
            ws.mkdir(parents=True)
            (ws / targets.READY_MARKER).write_text("")
            st = self.state("vm", {"WK_VM_STORE": store["WK_STORE"]}, binp, name)
            self.assertEqual(st, "broken")


if __name__ == "__main__":
    unittest.main()
