"""A build box's workspace has one state, read the same from the workstation and on the box: the box's own wk makes and
destroys it and keeps its record, and a clone from the box's mirror is present once it is marked ready."""
import contextlib
import io
import os
import shlex
import sys
from unittest import mock

from tests.support import REPO
from tests.test_wk_places import LINUX_PROBE, RemoteTest

sys.path.insert(0, str(REPO / "lib"))
from wk import places, pr, record, repos, workspace  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.clock import FakeClock  # noqa: E402


class BoxTest(RemoteTest):
    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ)
        p.start()
        self.addCleanup(p.stop)
        for v in ("WK_DRY_RUN", "WK_DESTRUCTIVE", "WK_FORCE", "WK_YES"):
            os.environ.pop(v, None)

    def on_the_box(self):
        """This machine as the box itself, holding a clone marked ready and no base-id."""
        root = self.tmp / "rr"
        self.conf("me", "local=1\nroot=%s\n" % root)
        me = self.reg.load("me")
        self.fake.answer(["sh", "-c", places.PROBE_SCRIPT], out=LINUX_PROBE)
        self.fake.answer(["sh", "-c"], out="present\n")
        self.fake.dirs.add(me.store.ws_dir("integ"))
        return me


class TestTheBoxReadsItsWorkspace(BoxTest):
    def test_a_clone_marked_ready_is_present_on_the_box_as_from_the_workstation(self):
        self.assertEqual(self.on_the_box().state("integ"), "present")
        self.fake.answer_remote("ws/integ ]", out="present\n")
        self.assertEqual(self.t.state("integ"), "present")

    def test_creating_on_the_box_needs_no_snapshot(self):
        me = self.on_the_box()
        records = record.Records(root=str(self.tmp / "records"), clock=FakeClock(), machine=self.fake)
        with mock.patch.object(me, "create") as create, mock.patch.object(me, "ready", lambda ws, clock: True), \
                mock.patch.object(me, "install_agents"), mock.patch.object(workspace, "freshen"):
            workspace._create(me, records, None, FakeClock(), "fresh", None, "native", repos.default(), "absent")
        create.assert_called_once()
        self.assertEqual(("fresh", None, "native", repos.default().name), create.call_args.args[:3] + (create.call_args.args[3].name,))


class TestTheWorkstationHandsTheLifecycleOver(BoxTest):
    def front(self, **opts):
        records = record.Records(root=str(self.tmp / "records"), clock=FakeClock(), machine=self.fake)
        self.fake.answer(["ssh"])
        with mock.patch("os.isatty", lambda fd: False), contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                return workspace.new_front(self.reg, records, "integ", dict(opts, place="box"), pr), err.getvalue()
            except Refused as e:
                return e.status, err.getvalue()

    def test_new_on_a_box_is_its_own_wk_new_and_leaves_nothing_here(self):
        rc, err = self.front(no_wait=True)
        self.assertEqual(rc, 0, err)
        handed = [e[1] for e in self.fake.effects if e[0] == "run_tty"]
        self.assertEqual(len(handed), 1, self.fake.effects)
        self.assertIn("wk new integ --no-wait", handed[0][-1])
        self.assertEqual([e for e in self.fake.effects if e[0] == "spawn" or e[1:] and str(e[1]).startswith(self.t.store.store_dir())], [])
        self.assertEqual(self.t.records().list(), [])

    def test_a_detached_new_on_a_box_opens_no_zed_and_carries_its_timeouts(self):
        os.environ.update(WK_NEW_TIMEOUT="7", WK_READY_TIMEOUT="9")
        rc, err = self.front(no_wait=True, zed=True)
        self.assertEqual(rc, 0, err)
        handed = [shlex.split(shlex.split(e[1][-1])[-1]) for e in self.fake.effects if e[0] == "run_tty"]
        self.assertLessEqual({"WK_NEW_TIMEOUT=7", "WK_READY_TIMEOUT=9"}, set(handed[0]))
        self.assertEqual([e for e in self.fake.effects if "cmd/zed" in str(e)], [])

    def test_a_box_at_another_wk_tools_commit_is_refused_the_new(self):
        self.tools_at("0000stale000")
        rc, err = self.front()
        self.assertEqual(rc, 1)
        self.assertIn("wk sync --tools box", err)
        self.assertEqual([e for e in self.fake.effects if e[0] == "run_tty"], [])

    def test_rm_on_a_box_is_its_own_wk_rm(self):
        self.fake.dirs.add(self.t.store.ws_dir("a"))
        self.fake.answer_remote("ws/a ]", out="present\n")
        self.fake.answer_remote("wk rm a", out="==> workspace 'a' destroyed\n")
        _, err = self.stderr_of(lambda: self.t.destroy("a"))
        line = shlex.split(self.fake.ssh_calls("wk rm a")[0][-1])
        self.assertEqual(line[-2:], ["rm", "a"])
        self.assertLessEqual({"WK_YES=1", "WK_EXPORTS_READ=1"}, set(line))
        self.assertEqual(self.fake.effects[-1], ("remove", self.t.store.ws_dir("a")))
        self.assertEqual(self.fake.ssh_calls("rm -rf"), [])
        self.fake.answer_remote("wk rm a", rc=1, out="error: 'a' has work running in it\n")
        self.fake.effects = []
        err = self.refused(lambda: self.t.destroy("a"))
        self.assertIn("box.example did not destroy 'a'", err)
        self.assertEqual([e for e in self.fake.effects if e[0] == "remove"], [])

    def test_rm_of_a_checkout_already_gone_from_the_box_drops_the_record_here(self):
        self.fake.dirs.add(self.t.store.ws_dir("a"))
        self.fake.answer_remote("ws/a ]", out="absent\n")
        self.fake.answer_remote("wk rm a", rc=1, out="error: no such workspace: a\n")
        self.stderr_of(lambda: self.t.destroy("a"))
        self.assertEqual(self.fake.ssh_calls("wk rm a"), [])
        self.assertEqual(self.fake.effects[-1], ("remove", self.t.store.ws_dir("a")))

    def test_rm_on_the_box_itself_removes_the_checkout(self):
        me = self.on_the_box()
        _, err = self.stderr_of(lambda: me.destroy("integ"))
        self.assertIn(("run", ("sh", "-c", "rm -rf %s" % me.ws_dir_there("integ"))), self.fake.effects)
        self.assertEqual(self.fake.effects[-1], ("remove", me.store.ws_dir("integ")))

    def test_the_box_leaves_the_export_check_to_the_workstation_that_handed_rm_over(self):
        found = [("integ", self.t, "workspace")]
        with mock.patch("wk.bench.record.unexported", lambda m, path, downloads: [("t1", "no export")]):
            self.assertEqual(len(workspace.unsaved_results(self.reg, found)), 1)
            marker = self.tmp / "wk-remote"
            marker.write_text("")
            self.env["WK_REMOTE_MARKER"] = str(marker)
            self.assertEqual(len(workspace.unsaved_results(places.Registry(REPO, env=self.env, machine=self.fake), found)), 1)
            self.env["WK_EXPORTS_READ"] = "1"
            self.assertEqual(workspace.unsaved_results(places.Registry(REPO, env=self.env, machine=self.fake), found), [])
