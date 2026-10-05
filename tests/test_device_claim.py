"""One board, one driver, across the fleet: the claim is a `holds: device:<machine>` field on the holder's own task
record (lib/wk/record.py), asked of this store, the podman machine's and every peer's; `record.hold` is the barrier."""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.fakes import FakeTarget
from tests.support import REPO, WkTest

sys.path.insert(0, str(REPO / "lib"))
from wk import act, record  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake  # noqa: E402

ROW = ("bench-rpi5-x-20260915T000000Z-9", "moose", "bench rpi5/speedometer3", "kill 4242")


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class ClaimTest(WkTest):

    def setUp(self):
        super().setUp()
        self.store = self.tmp / "store"
        self.store.mkdir()
        (self.tmp / "no-machines").mkdir()
        self.env = {"WK_STORE": str(self.store), "WK_MACHINES_DIR": str(self.tmp / "no-machines")}

    def spawn(self):
        """A pid this test is not the parent of, so `kill -0` never reads an unreaped zombie."""
        cp = subprocess.run(["bash", "-c", "sleep 300 >/dev/null 2>&1 & echo $!"],
                            capture_output=True, text=True, timeout=30)
        pid = int(cp.stdout.strip())
        self.addCleanup(lambda: alive(pid) and os.kill(pid, 9))
        return pid

    def records(self):
        return record.Records(env={"WK_STORE": str(self.store)})

    def holder(self, machine="rpi5", kind="bench", name="rpi5/speedometer3", pid=None, ended=None):
        if pid is None:
            pid = self.spawn()
        t = self.records().begin(kind, "here", name, "kill %d" % pid, "", ["one step"],
                                 holds="device:%s" % machine, pid=pid)
        if ended is not None:
            t.end(ended)
        return t.path, pid

    def rows(self, resource="device:rpi5"):
        return self.records().holders(resource)

    def tasks(self):
        d = self.store / "task"
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


class TestTheRecordDeclaresWhatItHolds(ClaimTest):
    def test_holds_is_a_field_of_the_record(self):
        d, _ = self.holder()
        self.assertEqual("device:rpi5", (d / "holds").read_text().strip())

    def test_a_record_that_claims_nothing_has_no_holds_field(self):
        t = self.records().begin("build", "here", "ws1", "wk build ws1 --kill", "/l", ["compile"])
        self.assertFalse((t.path / "holds").exists())

    def test_a_live_holder_is_one_row_naming_the_machine_and_its_kill(self):
        _, pid = self.holder()
        rows = self.rows()
        self.assertEqual(1, len(rows), rows)
        task_id, machine, what, kill = rows[0]
        self.assertTrue(task_id.startswith("bench-rpi5-speedometer3-"), task_id)
        self.assertEqual("bench rpi5/speedometer3", what)
        self.assertEqual("kill %d" % pid, kill)
        self.assertTrue(machine, "the row does not say which machine holds it")

    def test_another_resource_is_not_this_one(self):
        self.holder(machine="rpi4")
        self.assertEqual([], self.rows())

    def test_a_task_that_ended_holds_nothing(self):
        self.holder(ended=0)
        self.assertEqual([], self.rows())

    def test_a_holder_whose_pid_is_gone_holds_nothing(self):
        _, pid = self.holder()
        os.kill(pid, 9)
        for _ in range(50):
            if not alive(pid):
                break
            time.sleep(0.1)
        self.assertEqual([], self.rows())


class TestTheFleetIsAsked(unittest.TestCase):
    def setUp(self):
        self.records = mock.Mock()
        self.records.holders.return_value = []

    def test_a_peer_is_asked_and_its_rows_are_the_answer(self):
        rows = record.fleet_holders("device:rpi5", self.records, [("moose", lambda r: ("\t".join(ROW) + "\r\n", ""))])
        self.assertEqual([ROW], rows)

    def test_this_machine_and_the_peers_are_both_the_answer(self):
        here = ("id", "tolken", "bench rpi5/jetstream3", "kill 1")
        self.records.holders.return_value = [here]
        rows = record.fleet_holders("device:rpi5", self.records, [("moose", lambda r: ("\t".join(ROW), ""))])
        self.assertEqual([here, ROW], rows)

    def test_a_store_that_could_not_be_asked_is_a_row_of_its_own(self):
        rows = record.fleet_holders("device:rpi5", self.records, [("moose", lambda r: (None, "unreachable\tover ssh"))])
        self.assertEqual([("?", "moose", "unknown", "unreachable over ssh")], rows)

    def stores(self, local, targets, peers=("moose",)):
        with mock.patch("wk.store.Store.is_local", return_value=local), \
                mock.patch("wk.targets.Registry") as reg:
            reg.return_value.load.side_effect = lambda name: targets[name]
            reg.return_value.peer_workstations.return_value = list(peers)
            return [(name, ask("device:rpi5")) for name, ask in record.fleet_stores(str(REPO), {}, Fake())]

    def test_a_peer_is_asked_through_its_own_wk(self):
        moose = FakeTarget("moose", out="\t".join(ROW))
        self.assertEqual([("moose", ("\t".join(ROW), ""))], self.stores(True, {"moose": moose}))
        self.assertEqual([("status", "--holds", "device:rpi5")], [args for args, _, _ in moose.asked])

    def test_a_peer_that_cannot_be_reached_is_not_asked_and_says_why(self):
        moose = FakeTarget("moose", side="unreachable", why="timed out")
        [(_, (rows, why))] = self.stores(True, {"moose": moose})
        self.assertIsNone(rows)
        self.assertIn("unreachable over ssh", why)
        self.assertEqual([], moose.asked)

    def test_a_peer_whose_wk_does_not_read_the_flag_names_the_sync(self):
        [(_, (rows, why))] = self.stores(True, {"moose": FakeTarget("moose", rc=1)})
        self.assertIsNone(rows)
        self.assertIn("wk sync --tools moose", why)

    def test_a_store_of_this_machines_own_is_not_asked_twice(self):
        self.assertEqual([], self.stores(True, {}, peers=()))

    def test_a_store_elsewhere_is_asked_as_well(self):
        vm = FakeTarget("container", out="\t".join(ROW))
        [(name, (rows, _))] = self.stores(False, {"container": vm}, peers=())
        self.assertIn("podman machine", name)
        self.assertEqual("\t".join(ROW), rows)

    def test_a_podman_machine_that_did_not_answer_says_so(self):
        [(name, (rows, why))] = self.stores(False, {"container": FakeTarget("container", rc=1)}, peers=())
        self.assertIn("podman machine", name)
        self.assertIsNone(rows)
        self.assertIn("wk start", why)


class TestTheBarrier(unittest.TestCase):
    def setUp(self):
        self.store = Path(__import__("tempfile").mkdtemp(prefix="wk-test-claim-"))
        self.addCleanup(shutil.rmtree, self.store, True)
        self.machine = Fake()
        self.records = record.Records(self.store, clock=FakeClock(), machine=self.machine,
                                      env={"WK_STORE": str(self.store)})
        self.fleet_asked = []

    def hold(self, rows=(), env=None, **os_env):
        def fleet(resource):
            self.fleet_asked.append(resource)
            return list(rows)
        err = io.StringIO()
        with mock.patch.dict(os.environ, os_env), contextlib.redirect_stderr(err):
            try:
                t = record.hold(self.records, fleet, "rpi5", "bench", "rpi5/jetstream3", "kill 1", "",
                                ["jetstream3 on rpi5"], 4321, env or {})
            except act.Refused as e:
                return e, err.getvalue()
        return t, err.getvalue()

    def tasks(self):
        return [t.id for t in self.records.list()]

    def test_a_free_board_is_taken_and_the_claim_is_the_holders_own_record(self):
        t, _ = self.hold()
        self.assertEqual(("device:rpi5", "4321"), (t.field("holds"), t.field("pid")))
        self.assertEqual(["device:rpi5"], self.fleet_asked)

    def test_a_held_board_refuses_naming_the_machine_the_task_and_the_remedy(self):
        e, out = self.hold(rows=[ROW])
        self.assertIsInstance(e, act.Refused)
        for want in ("rpi5", "bench rpi5/speedometer3", "moose", "kill 4242", "--force"):
            self.assertIn(want, out)
        self.assertEqual([], self.tasks(), "a refused claim wrote a record anyway")

    def test_force_crosses_it_records_the_forcing_and_takes_the_claim(self):
        with mock.patch.object(act, "_forced", []):
            t, out = self.hold(rows=[ROW], WK_FORCE="1")
        self.assertIn("FORCED past a barrier", out)
        self.assertEqual([t.id], self.tasks())

    def test_a_machine_that_could_not_be_asked_is_reported_and_does_not_refuse(self):
        t, out = self.hold(rows=[("?", "moose", "unknown", "unreachable over ssh")])
        self.assertIn("moose -- unreachable over ssh", out)
        self.assertIn("could not be asked", out)
        self.assertEqual([t.id], self.tasks())

    def test_one_drivers_own_claim_passes_down_to_what_it_runs(self):
        t, _ = self.hold(rows=[ROW], env={"WK_DEVICE_HELD": "device:rpi5"})
        self.assertIsNone(t)
        self.assertEqual([], self.fleet_asked)
        self.assertEqual([], self.tasks())

    def test_an_inherited_claim_on_another_board_still_refuses(self):
        e, _ = self.hold(rows=[ROW], env={"WK_DEVICE_HELD": "device:rpi4"})
        self.assertIsInstance(e, act.Refused)

    def test_a_dry_run_reads_the_claim_and_takes_none(self):
        t, _ = self.hold(WK_DRY_RUN="1")
        self.assertIsNone(t)
        self.assertEqual(["device:rpi5"], self.fleet_asked)
        self.assertEqual([], self.tasks())

    def test_a_dry_run_still_refuses_a_held_board(self):
        e, _ = self.hold(rows=[ROW], WK_DRY_RUN="1")
        self.assertIsInstance(e, act.Refused)


class TestStatusHolds(ClaimTest):

    def test_it_prints_the_rows_and_changes_nothing(self):
        _, pid = self.holder()
        before = self.tasks()
        cp = self.run_wk("status", "--holds", "device:rpi5", env=self.env)
        self.assertEqual(0, cp.returncode, cp.stdout)
        rows = [l for l in cp.stdout.splitlines() if l.strip()]
        self.assertEqual(1, len(rows), cp.stdout)
        self.assertIn("bench rpi5/speedometer3", rows[0])
        self.assertIn("kill %d" % pid, rows[0])
        self.assertEqual(before, self.tasks())

    def test_a_free_board_is_no_rows_and_still_exit_0(self):
        cp = self.run_wk("status", "--holds", "device:rpi5", env=self.env)
        self.assertEqual(0, cp.returncode, cp.stdout)
        self.assertEqual("", cp.stdout.strip())

MACHINE_CONF = '''kind=board
ssh=fakeboard
driver=no-such-driver
device=/dev/null
profile=webkit-2.52-yocto-rpi5-64
role=bench-device
note="a board that is not there"
'''


class TestTheCommandsTakeIt(ClaimTest):
    """The commands that touch a board take the claim before the first ssh."""

    def setUp(self):
        super().setUp()
        self.machines = self.tmp / "machines"
        self.machines.mkdir()
        (self.machines / "fakeboard.conf").write_text(MACHINE_CONF)
        self.env["WK_MACHINES_DIR"] = str(self.machines)

    def held(self):
        return self.holder(machine="fakeboard", name="fakeboard/speedometer3")[1]

    def assert_refused(self, cp, pid):
        out = cp.stdout + cp.stderr
        self.assertNotEqual(0, cp.returncode, out)
        self.assertIn("fakeboard", out)
        self.assertIn("bench fakeboard/speedometer3", out)
        self.assertIn("kill %d" % pid, out)

    def test_bench_deploy_takes_it_where_the_image_workspace_is(self):
        from wk import targets
        from wk.bench import cli
        from wk.clock import Clock
        from wk.machine import Local
        pid = self.held()
        err = io.StringIO()
        with mock.patch.dict(os.environ, self.env), contextlib.redirect_stderr(err), self.assertRaises(act.Refused):
            cli.Bench(str(REPO), targets.Registry(REPO, env=dict(os.environ), machine=Local()), Clock()).deploy(
                "webkit-2.52-yocto-rpi5-64", "fakeboard", "a")
        out = err.getvalue()
        self.assertIn("bench fakeboard/speedometer3", out)
        self.assertIn("kill %d" % pid, out)

    def test_boot_refuses(self):
        pid = self.held()
        for args in ((), ("--back",)):
            with self.subTest(args=args):
                self.assert_refused(self.run_wk("boot", "fakeboard", *args, env=self.env), pid)

    def test_reading_the_boards_state_takes_no_claim(self):
        self.held()
        cp = self.run_wk("boot", "fakeboard", "--status", env=self.env)
        out = cp.stdout + cp.stderr
        self.assertNotIn("another live task holds it", out)
        self.assertIn("no boot driver", out)
        self.assertEqual(1, len(self.tasks()), out)

    def test_a_dry_run_holds_nothing(self):
        cp = self.run_wk("boot", "fakeboard", "--dry-run", env=self.env)
        self.assertIn("no boot driver", cp.stdout + cp.stderr)
        self.assertEqual([], self.tasks())


if __name__ == "__main__":
    unittest.main()
