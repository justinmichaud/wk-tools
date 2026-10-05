"""`unit rm.final_state[<place>]`: `wk rm` over each real driver on the Fake machine leaves nothing of the
workspace -- no environment, no directory, no record, no alias, no creation log, no guest file -- and its record
is the last thing to go, so an rm killed after any effect and re-run converges on that.

Run: python3 tests/run.py -k tests.test_rm_final_state
"""
import contextlib
import io
import os
import sys
from unittest import mock

from tests.killpoints import converges
from tests.support import REPO
from tests.test_wk_workspace import World, WorkspaceTest

sys.path.insert(0, str(REPO / "lib"))
from wk import workspace  # noqa: E402
from wk.store import Store  # noqa: E402

KINDS = ("container", "vm", "remote")


class RmFinalStateTest(WorkspaceTest):
    def setUp(self):
        super().setUp()
        os.environ["WK_YES"] = "1"
        # This host is a Mac, which a guest is driven from; the store rule that says so is held by test_wk_store.
        mac = mock.patch.object(Store, "macos_host", property(lambda store: not store.env.get("WK_IN_VM")))
        mac.start()
        self.addCleanup(mac.stop)

    def world(self, kind):
        w = World(self.tmp, kinds={"fakebox": kind})
        w.make()
        w.alias()
        w.files[w.driver.create_log("ws")] = "log\n"
        w.begin().end(0)
        w.begin("build", pid=99).end(0)
        w.effects = []
        return w

    def rm(self, w):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = workspace.rm_names(w.reg, w.records, ["ws"])
        return rc, err.getvalue()


class TestRmFinalState(RmFinalStateTest):
    def test_rm_final_state(self):
        """Inside a workspace (local) there is nothing to remove it from: the refusal names the host and removes nothing."""
        for kind in KINDS + ("local",):
            with self.subTest(driver=kind):
                w = self.world(kind)
                before = w.left()
                self.assertTrue(before, "the world made nothing to remove")
                rc, err = self.rm(w)
                if kind == "local":
                    self.assertEqual((rc, w.left()), (1, before), err)
                    continue
                self.assertEqual((rc, w.left()), (0, {}), err)
                self.assertIn("Host other", w.files[workspace.sshalias.alias_path(w.env)])
                self.assertEqual(w.record_goes_last(), [])

    def test_rm_final_state_killed_after_any_effect_and_rerun(self):
        for kind in KINDS:
            with self.subTest(driver=kind):
                converges(self, lambda: self.world(kind), self.rm, lambda w: w.left(), max_effects=80)
