"""One board, one driver -- across the whole fleet.

A benchmark board is a fleet resource: exactly one thing may drive it at a
time, and the two workstations are peers with their own `wk`. The claim is a
hold on the holder's own task record (`holds: device:<machine>`,
lib/wk/record.py), so there is no second store to keep in step: a holder is
live by construction, and liveness is asked of the process table at read time.

`Records.holders` is this machine's half of the answer, `wk status --holds` is
that same read as a read-only CLI surface, and `record.fleet_holders` asks the
podman machine's store and every peer workstation through its own wk.
`record.hold` is the barrier the commands that touch a board take first (`wk
pi bench`, `wk pi deploy`, `wk boot`, through lib/task.sh's device_hold): it
names the machine, the task and the command that stops it, and `--force`
crosses it and records that it did.

No board, no ssh, no hardware: the fleet is fakes, and the bash half writes
into a scratch $WK_STORE.

Run: python3 tests/run.py -k tests.test_device_claim
"""
import contextlib
import io
import os
import shlex
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.support import REPO, WkTest, bash, clean_env

sys.path.insert(0, str(REPO / "lib"))
from wk import act, record  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake  # noqa: E402

PRELUDE = '. "%s/lib/common.sh"\n. "%s/lib/task.sh"\n' % (REPO, REPO)
ROW = ("bench-rpi5-x-20260915T000000Z-9", "moose", "bench rpi5/speedometer3", "kill 4242")


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class ClaimTest(WkTest):
    """A scratch store that is this machine's own, and a registry of no other machine."""

    def setUp(self):
        super().setUp()
        self.store = self.tmp / "store"
        self.store.mkdir()
        (self.tmp / "no-machines").mkdir()
        self.env = {"WK_STORE": str(self.store), "WK_MACHINES_DIR": str(self.tmp / "no-machines")}

    def sh(self, body, env=None, check=True):
        cp = bash(PRELUDE + body, env=dict(self.env, **(env or {})))
        if check:
            self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        return cp

    def spawn(self):
        """A pid this test is not the parent of, so `kill -0` reads it as
        running rather than as an unreaped zombie (tests/test_stop_tasks.py)."""
        cp = subprocess.run(["bash", "-c", "sleep 300 >/dev/null 2>&1 & echo $!"],
                            capture_output=True, text=True, timeout=30)
        pid = int(cp.stdout.strip())
        self.addCleanup(lambda: alive(pid) and os.kill(pid, 9))
        return pid

    def holder(self, machine="rpi5", kind="bench", name="rpi5/speedometer3", pid=None, ended=None):
        """A live task record holding <machine>, written through lib/task.sh."""
        if pid is None:
            pid = self.spawn()
        body = ['d=$(task_begin --holds device:%s %s here %s "kill %d" "" "one step")'
                % (machine, kind, shlex.quote(name), pid),
                'task_pid "$d" %d' % pid]
        if ended is not None:
            body.append('task_end "$d" %s' % ended)
        body.append('printf "%s" "$d"')
        return Path(self.sh("\n".join(body)).stdout.strip()), pid

    def rows(self, resource="device:rpi5"):
        return record.Records(env={"WK_STORE": str(self.store)}).holders(resource)

    def tasks(self):
        d = self.store / "task"
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


class TestTheRecordDeclaresWhatItHolds(ClaimTest):
    def test_holds_is_a_field_of_the_record(self):
        d, _ = self.holder()
        self.assertEqual("device:rpi5", (d / "holds").read_text().strip())

    def test_a_record_that_claims_nothing_has_no_holds_field(self):
        d = Path(self.sh('task_begin build here ws1 "wk build ws1 --kill" /l compile').stdout.strip())
        self.assertFalse((d / "holds").exists())

    def test_a_hold_taken_from_bash_names_the_shell_that_took_it(self):
        """The one path: lib/task.sh's task_begin is lib/wk/record.py's begin,
        and the pid it records is the caller's, not the Python it ran."""
        out = self.sh('d=$(task_begin --holds device:rpi5 bench here x "k" "" one)\n'
                      'printf "%s %s" "$$" "$(task_field "$d" pid)"').stdout.split()
        self.assertEqual(out[0], out[1])

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
        """The claim cannot outlive its holder: liveness is the process table
        at read time, so a killed driver leaves no board held."""
        _, pid = self.holder()
        os.kill(pid, 9)
        for _ in range(50):
            if not alive(pid):
                break
            time.sleep(0.1)
        self.assertEqual([], self.rows())

    def test_the_record_is_released_when_its_command_ends(self):
        d, _ = self.holder()
        self.sh('WK_DEVICE_TASK=%s device_release' % shlex.quote(str(d)))
        self.assertTrue((d / "exit").exists())
        self.assertEqual([], self.rows())


class Target:
    """A machine that answers a probe with `side` and its own wk with (rc, out)."""

    def __init__(self, name, side="answering", why="", rc=0, out=""):
        self.name, self.env = name, {}
        self.side, self.why, self.rc, self.out = side, why, rc, out
        self.asked = []

    def probe(self):
        return self.side, self.why

    def wk(self, *args, env=None, quiet=False):
        self.asked.append(args)
        return self.rc, self.out


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
        """Never silence: an unread machine is not a free board."""
        rows = record.fleet_holders("device:rpi5", self.records, [("moose", lambda r: (None, "unreachable\tover ssh"))])
        self.assertEqual([("?", "moose", "unknown", "unreachable over ssh")], rows)

    def stores(self, local, targets, peers=("moose",)):
        with mock.patch("wk.shell.store_is_local", return_value=local), \
                mock.patch("wk.shell.peer_workstations", return_value=list(peers)), \
                mock.patch("wk.targets.Registry") as reg:
            reg.return_value.load.side_effect = lambda name: targets[name]
            return [(name, ask("device:rpi5")) for name, ask in record.fleet_stores(str(REPO), {}, Fake())]

    def test_a_peer_is_asked_through_its_own_wk(self):
        moose = Target("moose", out="\t".join(ROW))
        self.assertEqual([("moose", ("\t".join(ROW), ""))], self.stores(True, {"moose": moose}))
        self.assertEqual([("status", "--holds", "device:rpi5")], moose.asked)

    def test_a_peer_that_cannot_be_reached_is_not_asked_and_says_why(self):
        moose = Target("moose", side="unreachable", why="timed out")
        [(_, (rows, why))] = self.stores(True, {"moose": moose})
        self.assertIsNone(rows)
        self.assertIn("unreachable over ssh", why)
        self.assertEqual([], moose.asked)

    def test_a_peer_whose_wk_does_not_read_the_flag_names_the_sync(self):
        [(_, (rows, why))] = self.stores(True, {"moose": Target("moose", rc=1)})
        self.assertIsNone(rows)
        self.assertIn("wk sync --tools moose", why)

    def test_a_store_of_this_machines_own_is_not_asked_twice(self):
        """On Linux the container target's store is the directory this machine
        has already walked, and a board would read as held by itself."""
        self.assertEqual([], self.stores(True, {}, peers=()))

    def test_a_store_elsewhere_is_asked_as_well(self):
        vm = Target("container", out="\t".join(ROW))
        [(name, (rows, _))] = self.stores(False, {"container": vm}, peers=())
        self.assertIn("podman machine", name)
        self.assertEqual("\t".join(ROW), rows)

    def test_a_podman_machine_that_did_not_answer_says_so(self):
        [(name, (rows, why))] = self.stores(False, {"container": Target("container", rc=1)}, peers=())
        self.assertIn("podman machine", name)
        self.assertIsNone(rows)
        self.assertIn("wk start", why)


class TestTheBarrier(unittest.TestCase):
    def setUp(self):
        self.store = Path(__import__("tempfile").mkdtemp(prefix="wk-test-claim-"))
        self.addCleanup(record._rmtree, self.store)
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
        """A workstation that is off is the normal state, so this is a warning
        and not a refusal -- but it is never silent."""
        t, out = self.hold(rows=[("?", "moose", "unknown", "unreachable over ssh")])
        self.assertIn("moose -- unreachable over ssh", out)
        self.assertIn("could not be asked", out)
        self.assertEqual([t.id], self.tasks())

    def test_one_drivers_own_claim_passes_down_to_what_it_runs(self):
        """`wk pi bench --ab-systems` runs `wk boot` for each leg: a claim
        that refused its own holder would deadlock the board's own driver."""
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


class TestTheShimTakesIt(ClaimTest):
    """lib/task.sh's device_hold over the Python: the claim, exported to what
    the driver runs, released when the command ends."""

    HOLD = 'device_hold rpi5 bench rpi5/jetstream3 "kill $$" "" "jetstream3 on rpi5"'

    def test_a_free_board_is_taken_and_exported(self):
        cp = self.sh(self.HOLD + '\nprintf "%s|%s|%s" "$WK_DEVICE_TASK" "$WK_DEVICE_HELD" "$$"')
        task, held, shell = cp.stdout.split("|")
        self.assertEqual("device:rpi5", held)
        self.assertEqual("device:rpi5", (Path(task) / "holds").read_text().strip())
        self.assertEqual(shell, (Path(task) / "pid").read_text().strip())
        self.assertTrue((Path(task) / "exit").exists(), "the claim outlived the command that took it")

    def test_a_held_board_ends_the_caller(self):
        _, pid = self.holder()
        cp = self.sh(self.HOLD + '\necho SURVIVED', check=False)
        out = cp.stdout + cp.stderr
        self.assertNotEqual(0, cp.returncode, out)
        self.assertNotIn("SURVIVED", out)
        self.assertIn("kill %d" % pid, out)
        self.assertEqual(1, len(self.tasks()))


class TestStatusHolds(ClaimTest):
    """`wk status --holds <resource>`: the read-only CLI surface the asking
    machine calls over its own wk. It answers about this store alone -- the
    caller walks the peers -- and changes nothing."""

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

    def test_the_flag_is_declared_readonly(self):
        decl = [l for l in (REPO / "cmd" / "status").read_text().splitlines()
                if l.startswith("# wk:")]
        self.assertTrue(any("--holds=" in l for l in decl), decl)
        self.assertTrue(any("readonly" in l for l in decl), decl)


MACHINE_CONF = '''KIND=board
NODE_SSH=fakeboard
NODE_DRIVER=no-such-driver
NODE_DEVICE=/dev/null
NODE_PROFILE=webkit-2.52-yocto-rpi5-64
NODE_ROLE=bench-device
NODE_NOTE="a board that is not there, for a refusal that needs no hardware"
'''


class TestTheCommandsTakeIt(ClaimTest):
    """The three commands that touch a board take the claim before anything
    reaches the board. Driven against a machine conf of this test's own
    (WK_MACHINES_DIR), so nothing here needs a board: the refusal comes
    before the first ssh."""

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

    def test_pi_bench_refuses(self):
        pid = self.held()
        self.assert_refused(
            self.run_wk("pi", "bench", "fakeboard", "speedometer3", env=self.env), pid)

    def test_pi_deploy_takes_it_where_the_lane_is(self):
        """A deploy is routed to the machine holding the lane (`name=derived`,
        the dispatcher), so the claim is taken there and not here -- which is
        why that machine's store is one fleet_holders asks. Driven with a lane
        this machine holds, so the routing leaves it here and the refusal is
        the one this test can see."""
        pid = self.held()
        self.assert_refused(
            self.run_wk("pi", "deploy", "webkit-2.52-yocto-rpi5-64", "fakeboard",
                        "--slot", "a", env={**self.env, "WK_IN_VM": "1"}), pid)

    def test_boot_refuses(self):
        pid = self.held()
        self.assert_refused(self.run_wk("boot", "fakeboard", env=self.env), pid)

    def test_boot_back_refuses(self):
        pid = self.held()
        self.assert_refused(self.run_wk("boot", "fakeboard", "--back", env=self.env), pid)

    def test_reading_the_boards_state_takes_no_claim(self):
        """Read-only is read-only: `--status` reports on a board somebody
        else is benching on rather than refusing, and holds nothing itself."""
        self.held()
        cp = self.run_wk("boot", "fakeboard", "--status", env=self.env)
        out = cp.stdout + cp.stderr
        self.assertNotIn("another live task holds it", out)
        self.assertIn("no boot driver", out)   # as far as it gets with no driver
        self.assertEqual(1, len(self.tasks()), out)

    def test_a_dry_run_holds_nothing(self):
        cp = self.run_wk("boot", "fakeboard", "--dry-run", env=self.env)
        self.assertIn("no boot driver", cp.stdout + cp.stderr)
        self.assertEqual([], self.tasks())

    def test_the_two_refusals_say_different_true_things(self):
        """The lock is this machine's own serialization and refuses by naming
        the pid holding it here; the claim is the board's, and names the
        machine and the task holding it anywhere."""
        ready = self.tmp / "locked"
        holder = subprocess.Popen(
            ["bash", "-c",
             '. "%s/lib/common.sh"; hold_lock pi-bench-fakeboard -w 5 || exit 1; '
             'touch %s; sleep 60' % (REPO, shlex.quote(str(ready)))],
            env=clean_env({"WK_STORE": str(self.store)}),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        for _ in range(100):
            if ready.exists():
                break
            time.sleep(0.1)
        self.assertTrue(ready.exists(), "the test's own lock holder never started")

        cp = self.run_wk("pi", "bench", "fakeboard", "speedometer3", env=self.env)
        out = cp.stdout + cp.stderr
        self.assertNotEqual(0, cp.returncode, out)
        self.assertIn("pi-bench-fakeboard lock", out)
        self.assertIn("pid %d" % holder.pid, out)
        self.assertNotIn("fleet resource", out)


if __name__ == "__main__":
    unittest.main()
