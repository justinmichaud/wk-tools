"""Un-managed clobbering is detected, not silently trusted (CLAUDE.md rule
5: when the record and the machine disagree, the machine wins and the
command says so). Both tests drive the real drivers in lib/wk/targets.py on
the real local machine against a stub `podman`/`tart` on PATH, so the
driver's own translation of "the environment is gone" into `broken` is
exercised for the one case a person can produce by hand: `podman rm` /
`tart delete` on a workspace whose creation had already finished.

Run: python3 -m unittest tests.test_clobbering -v
"""
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
        # `inspect` failing is exactly what `podman rm <container>` leaves
        # behind: the workspace directory and its finished-creation marker
        # survive (they are host-side files), only the container is gone.
        fake_podman = "case \"$*\" in *inspect*) exit 1 ;; *) exit 0 ;; esac\n"
        with temp_store() as store, stub_path({"podman": fake_podman}) as binp:
            name = f"demo-{rand_suffix()}"
            ws = store["path"] / "ws" / name
            (ws / "home").mkdir(parents=True)
            (ws / "home" / targets.READY_MARKER).write_text("")
            (ws / "base-id").write_text("deadbeef\n")
            st = self.state("container", {"WK_STORE": store["WK_STORE"], "WK_IN_VM": "1"}, binp, name)
            self.assertEqual(st, "broken", "a hand-removed container should read 'broken' (rule 5)")


class TestTartDeleteByHand(Clobbered):
    def test_a_guest_deleted_by_hand_reads_broken(self):
        # `tart list --format json` returning nothing for this VM is exactly
        # what `tart delete wk-<name>` leaves behind: the host-side
        # workspace directory and its ready marker survive, the guest does not.
        fake_tart = 'case "$1" in list) echo "[]" ;; *) exit 1 ;; esac\n'
        with temp_store() as store, stub_path({"tart": fake_tart}) as binp, \
                mock.patch.object(Store, "macos_host", new_callable=mock.PropertyMock, return_value=True):
            name = f"demo-{rand_suffix()}"
            ws = store["path"] / "ws" / name
            ws.mkdir(parents=True)
            (ws / targets.READY_MARKER).write_text("")
            st = self.state("vm", {"WK_VM_STORE": store["WK_STORE"]}, binp, name)
            self.assertEqual(st, "broken", "a hand-deleted guest should read 'broken' (rule 5)")


if __name__ == "__main__":
    unittest.main()
