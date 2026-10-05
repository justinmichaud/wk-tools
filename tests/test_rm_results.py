"""`wk rm` refuses to take a bench task no export holds as it is now, naming `wk bench export <task>`; `--force`
crosses it. "Exported" is read off the zips themselves. Driven through ./wk against a remote place that is this machine."""
import sys
import types
from unittest import mock

from tests.support import REPO, WkTest, run
from tests.test_bench_results import complete_task
from tests.test_bench_task import TASK
from tests.test_rm_remote import _LOCAL_CONF

sys.path.insert(0, str(REPO / "lib"))
from wk import machine, places, workspace  # noqa: E402
from wk.bench import cli, record  # noqa: E402
from wk.machine import Fake, Local  # noqa: E402


class RmResultsTest(WkTest):
    def setUp(self):
        super().setUp()
        registry, self.root, store = self.tmp / "hosts", self.tmp / "root", self.tmp / "store"
        registry.mkdir()
        (self.root / "ws" / "w").mkdir(parents=True)
        (self.root / "ws" / "w" / ".wk-ready").write_text("")
        (store / "ws" / "w").mkdir(parents=True)
        (registry / "fakebox.conf").write_text(_LOCAL_CONF.format(root=self.root, store=store))
        self.home = self.tmp / "home"
        self.env = {"WK_MACHINES_DIR": str(registry), "XDG_STATE_HOME": str(self.tmp / "state"), "WK_PLACE": "fakebox",
                    "HOME": str(self.home), "WK_YES": "1"}
        self.task = complete_task(self.root / "ws" / "w" / "bench")

    def rm(self, *extra):
        return run("rm", "w", *extra, env=self.env, input="", timeout=120)

    def exported_to(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(cli.archive(str(self.task), False))

    def gone(self):
        return not (self.root / "ws" / "w").exists()


class TestATaskNoExportHoldsIsNotDestroyed(RmResultsTest):
    def test_an_unexported_task_refuses_the_removal_naming_it_and_the_export(self):
        cp = self.rm()
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("wk bench export %s" % TASK, cp.stdout)
        self.assertIn("--force", cp.stdout)
        self.assertFalse(self.gone(), cp.stdout)

    def test_the_default_export_lets_it_go(self):
        self.exported_to(self.home / "Downloads" / (TASK + ".zip"))
        cp = self.rm()
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertTrue(self.gone(), cp.stdout)

    def test_the_destination_the_task_records_counts_too(self):
        dest = self.tmp / "shared" / (TASK + ".zip")
        self.exported_to(dest)
        (self.task / record.EXPORT_RECORD).write_text(str(dest) + "\n")
        self.assertEqual(self.rm().returncode, 0)
        self.assertTrue(self.gone())

    def test_an_export_of_an_earlier_state_holds_nothing(self):
        self.exported_to(self.home / "Downloads" / (TASK + ".zip"))
        run_dir = sorted((self.task / "runs").iterdir())[0]
        record.write_env(str(run_dir / "env.json"), ["wall_time_s=999"], update=True)
        self.assertNotEqual(self.rm().returncode, 0)
        self.assertFalse(self.gone())

    def test_a_warmup_file_the_export_lacks_holds_nothing(self):
        self.exported_to(self.home / "Downloads" / (TASK + ".zip"))
        (self.task / "warmup" / "rpi3-b.profile.json").write_text("{}")
        self.assertNotEqual(self.rm().returncode, 0)

    def test_force_crosses_it_and_says_so(self):
        cp = self.rm("--force")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertTrue(self.gone(), cp.stdout)
        self.assertIn("FORCED", cp.stdout)


class TestTheEvidenceIsReadOrRefused(WkTest):
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

    def __init__(self, machine, bench):
        self.machine, self.bench = machine, bench

    def results(self, ws):
        return self.machine, self.bench


class TestAMacReadsItsZipsBeforeTheRemovalIsForwarded(WkTest):
    """A Mac's container `wk rm` runs in the podman machine, which cannot read a zip on this host, so the host reads them."""

    def setUp(self):
        super().setUp()
        from wk import workspace
        self.workspace = workspace
        self.task = complete_task(self.tmp / "vm" / "ws" / "w" / "bench")
        self.home = self.tmp / "home"

    def reg(self, **env):
        from tests.test_bench_task import registry
        from wk.machine import Local
        reg = registry(self.tmp / "store", env=dict(env, HOME=str(self.home)))
        reg.load = lambda name: _Holder(Local(), str(self.tmp / "vm" / "ws" / "w" / "bench"))
        return reg

    def test_the_host_refuses_an_unexported_task_before_forwarding(self):
        from wk.act import Refused
        with self.assertRaises(Refused):
            self.workspace.refuse_unsaved_before_forward(self.reg(), ["w"])

    def test_the_host_lets_an_exported_one_go(self):
        dest = self.home / "Downloads" / (TASK + ".zip")
        dest.parent.mkdir(parents=True)
        dest.write_bytes(cli.archive(str(self.task), False))
        self.workspace.refuse_unsaved_before_forward(self.reg(), ["w"])

    def holder(self):
        return _Holder(Local(), str(self.tmp / "vm" / "ws" / "w" / "bench"))

    def test_the_podman_machine_leaves_it_to_the_host_that_forwarded_it(self):
        marker = self.tmp / "podman-machine"
        marker.write_text("applehv\n")
        with mock.patch.object(machine, "PODMAN_MACHINE", str(marker)):
            self.assertEqual([], self.workspace.unsaved_results(self.reg(WK_IN_VM="1", WK_HOST_SELF="1"), [("w", self.holder(), "workspace")]))
            self.assertTrue(self.workspace.unsaved_results(self.reg(WK_IN_VM="1"), [("w", self.holder(), "workspace")]))

    def test_the_variables_alone_skip_nothing_off_a_podman_machine(self):
        with mock.patch.object(machine, "PODMAN_MACHINE", str(self.tmp / "absent")):
            self.assertTrue(self.workspace.unsaved_results(self.reg(WK_IN_VM="1", WK_HOST_SELF="1"), [("w", self.holder(), "workspace")]))

    def test_the_dispatcher_asks_before_it_forwards_a_removal(self):
        text = (REPO / "lib" / "wk" / "dispatch.py").read_text()
        branch = text[text.index('if d.post == "ssh-alias-remove":'):]
        self.assertLess(branch.index("refuse_unsaved_before_forward"), branch.index("forward_status"))
