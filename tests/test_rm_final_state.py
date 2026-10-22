"""`wk rm` as the dispatcher and lib/wk/workspace.py run it, in process over a fake world. `unit rm.final_state[<place>]`:
over each real driver it leaves nothing of the workspace -- no environment, no directory, no record, no alias, no
creation log, no guest file -- and its record is the last thing to go. A workspace holding a bench task no export holds
as it is now is refused, naming `wk bench export <task>`; `--force` crosses it. "Exported" is read off the zips."""
import contextlib
import io
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from tests.fakes import Fake, FlowTest
from tests.support import REPO, fake_workspace
from tests.test_bench_results import complete_task
from tests.test_bench_task import TASK
from tests.test_dispatch import dispatched
from tests.test_wk_workspace import World, WorkspaceTest

sys.path.insert(0, str(REPO / "lib"))
from wk import machine, places, workspace  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.bench import cli, record  # noqa: E402
from wk.machine import Local  # noqa: E402
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


class TestTheDispatchersRefusals(unittest.TestCase):
    def test_a_name_and_all_together_are_refused_with_the_usage(self):
        how, status, out = dispatched(["rm", "alpha", "--all"])
        self.assertEqual((how, status), ("exit", 2))
        self.assertIn("unexpected argument: alpha", out)

    def test_inside_a_workspace_all_is_refused_naming_the_host_command(self):
        with fake_workspace() as ws:
            how, status, out = dispatched(["rm", "--all"], env=ws.env())
        self.assertEqual(how, "exit")
        self.assertIn("From the host:  wk rm --all", out)


class TestATaskNoExportHoldsIsNotDestroyed(RmFinalStateTest):
    def setUp(self):
        super().setUp()
        self.task = complete_task(self.tmp / "bench")
        p = mock.patch.object(places.Container, "results", lambda driver, ws: (Local(), str(self.tmp / "bench")))
        p.start()
        self.addCleanup(p.stop)
        self.w = self.world("container")
        self.downloads = Path(self.w.env["HOME"]) / "Downloads" / (TASK + ".zip")

    def exported_to(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(cli.archive(str(self.task), False))

    def refused_rm(self):
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()) as err:
            workspace.rm_names(self.w.reg, self.w.records, ["ws"])
        self.assertTrue(self.w.left(), "it was destroyed")
        return err.getvalue()

    def test_an_unexported_task_refuses_the_removal_naming_it_and_the_export(self):
        err = self.refused_rm()
        self.assertIn("wk bench export %s" % TASK, err)
        self.assertIn("--force", err)

    def test_the_default_export_lets_it_go(self):
        self.exported_to(self.downloads)
        self.assertEqual((self.rm(self.w)[0], self.w.left()), (0, {}))

    def test_the_destination_the_task_records_counts_too(self):
        dest = self.tmp / "shared" / (TASK + ".zip")
        self.exported_to(dest)
        (self.task / record.EXPORT_RECORD).write_text(str(dest) + "\n")
        self.assertEqual((self.rm(self.w)[0], self.w.left()), (0, {}))

    def test_an_export_of_an_earlier_state_holds_nothing(self):
        self.exported_to(self.downloads)
        run_dir = sorted((self.task / "runs").iterdir())[0]
        record.write_env(str(run_dir / "env.json"), {"wall_time_s": "999"}, update=True)
        self.refused_rm()

    def test_a_warmup_file_the_export_lacks_holds_nothing(self):
        self.exported_to(self.downloads)
        (self.task / "warmup" / "rpi3-b.profile.json").write_text("{}")
        self.refused_rm()

    def test_force_crosses_it_and_says_so(self):
        os.environ["WK_FORCE"] = "1"
        rc, err = self.rm(self.w)
        self.assertEqual((rc, self.w.left()), (0, {}), err)
        self.assertIn("FORCED", err)


class TestTheEvidenceIsReadOrRefused(unittest.TestCase):
    def test_a_bench_directory_that_cannot_be_read_is_not_exported(self):
        class Unreadable(Fake):
            def isdir(self, path):
                return True

            def listdir(self, path):
                raise OSError("permission denied")
        (why,) = record.unexported(Unreadable(), "/ws/w/bench", "/home/Downloads")
        self.assertEqual(why[0], "/ws/w/bench")
        self.assertIn("permission denied", why[1])

    def test_a_task_whose_record_cannot_be_read_is_not_exported(self):
        class Flaky(Fake):
            def read(self, path):
                raise OSError("gone")
        m = Flaky()
        m._set_file("/b/t/task.json", "{}")
        m._set_file("/b/t/" + record.EXPORT_RECORD, "/x.zip")
        (why,) = record.unexported(m, "/b", "/d")
        self.assertEqual(why[0], "t")
        self.assertIn("gone", why[1])

    def test_no_bench_directory_is_nothing_to_lose(self):
        self.assertEqual(record.unexported(Fake(), "/ws/w/bench", "/d"), [])

    def test_a_build_boxs_tasks_are_in_its_workspace_directory_there(self):
        box = types.SimpleNamespace(peer=False, machine="ssh", ws_dir_there=lambda ws: "/srv/wk/ws/" + ws)
        self.assertEqual(places.Remote.results(box, "w"), ("ssh", "/srv/wk/ws/w/bench"))

    def test_a_peers_tasks_are_left_to_the_peers_own_rm(self):
        peer = types.SimpleNamespace(peer=True, results=lambda ws: self.fail("a peer's tasks were read from here"))
        reg = types.SimpleNamespace(env={}, store=types.SimpleNamespace(home=lambda: "/h"), in_remote_host=lambda: False)
        self.assertEqual(workspace.unsaved_results(reg, [("w", peer, "workspace")]), [])


class _Holder:
    """A place whose workspace's bench tasks are read through another machine, the way a Mac reads the podman machine's."""

    name = "container"
    peer = False

    def __init__(self, machine, bench):
        self.machine, self.bench = machine, bench

    def results(self, ws):
        return self.machine, self.bench


class TestAMacReadsItsZipsBeforeTheRemovalIsForwarded(FlowTest):
    """A Mac's container `wk rm` runs in the podman machine, which cannot read a zip on this host, so the host reads them."""

    def setUp(self):
        super().setUp()
        self.task = complete_task(self.tmp / "vm" / "ws" / "w" / "bench")
        self.home = self.tmp / "home"

    def reg(self, **env):
        from tests.test_bench_task import registry
        reg = registry(self.tmp / "store", env=dict(env, HOME=str(self.home)))
        reg.load = lambda name: self.holder()
        return reg

    def test_the_host_refuses_an_unexported_task_before_forwarding(self):
        with self.assertRaises(Refused):
            workspace.refuse_unsaved_before_forward(self.reg(), ["w"])

    def test_the_host_lets_an_exported_one_go(self):
        dest = self.home / "Downloads" / (TASK + ".zip")
        dest.parent.mkdir(parents=True)
        dest.write_bytes(cli.archive(str(self.task), False))
        workspace.refuse_unsaved_before_forward(self.reg(), ["w"])

    def holder(self):
        return _Holder(Local(), str(self.tmp / "vm" / "ws" / "w" / "bench"))

    def test_the_podman_machine_leaves_it_to_the_host_that_forwarded_it(self):
        marker = self.tmp / "podman-machine"
        marker.write_text("applehv\n")
        with mock.patch.object(machine, "PODMAN_MACHINE", str(marker)):
            self.assertEqual([], workspace.unsaved_results(self.reg(WK_IN_VM="1", WK_HOST_SELF="1"), [("w", self.holder(), "workspace")]))
            self.assertTrue(workspace.unsaved_results(self.reg(WK_IN_VM="1"), [("w", self.holder(), "workspace")]))

    def test_the_variables_alone_skip_nothing_off_a_podman_machine(self):
        with mock.patch.object(machine, "PODMAN_MACHINE", str(self.tmp / "absent")):
            self.assertTrue(workspace.unsaved_results(self.reg(WK_IN_VM="1", WK_HOST_SELF="1"), [("w", self.holder(), "workspace")]))

    def test_the_dispatcher_asks_before_it_forwards_a_removal(self):
        def refuse(registry, names):
            raise Refused("wk bench export task")

        env = {"WK_YES": "1", "WK_DESTRUCTIVE": "1", "WK_CONFIRMED": "1"}
        how, status, _ = dispatched(["rm", "ws1"], macos=True, env=env, refuse=refuse, run=True)
        self.assertEqual(how, "exit")
        self.assertNotEqual(status, 0)
        self.assertEqual(dispatched(["rm", "ws1"], macos=True, env=env, run=True)[0], "forward")
