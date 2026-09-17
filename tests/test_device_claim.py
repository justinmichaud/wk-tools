"""One board, one driver -- across the whole fleet.

A benchmark board is a fleet resource: exactly one thing may drive it at a
time, and the two workstations are peers with their own `wk`. The claim is a
task record that declares what it holds (`task_begin --holds device:rpi5`,
lib/task.sh), so there is no second store to keep in step: a holder is live by
construction, and liveness is asked of the process table at read time.

`task_holders` is this machine's half of the answer, `wk status --holds` is
that same read as a read-only CLI surface, and `fleet_holders` asks every peer
workstation through its own wk. `device_hold` is the barrier the commands that
touch a board take first (`wk pi bench`, `wk pi deploy`, `wk boot`): it names
the machine, the task and the command that stops it, and `--force` crosses it
and records that it did.

No board, no ssh, no hardware: the fleet is stubbed and every record is
written into a scratch $WK_STORE by lib/task.sh itself.

Run: python3 -m unittest tests.test_device_claim -v
"""
import os
import shlex
import subprocess
import time
import unittest
from pathlib import Path

from tests.support import REPO, WkTest, bash

TAB = "\t"
PRELUDE = '. "%s/lib/common.sh"\n. "%s/lib/task.sh"\n' % (REPO, REPO)

# This machine's store is its own, so the podman machine is not a second store
# to ask: that arm is TestThePodmanMachineIsAskedToo's.
OWN_STORE = 'store_is_local() { return 0; }\n'

# What fleet_holders needs of lib/target.sh, with no machine behind it: one
# peer, reachable, answering about its own store through its own wk.
PEER = OWN_STORE + '''
peer_workstations() { echo moose; }
load_target() { :; }
machine_answers() { return 0; }
t_wk() { printf '%s\\n' "$PEER_ROWS"; }
'''

# The same, with nothing to ask: this machine is the whole fleet.
NO_PEERS = OWN_STORE + 'peer_workstations() { :; }\n'

DEAF_PEER = OWN_STORE + '''
peer_workstations() { echo moose; }
load_target() { :; }
machine_answers() { printf '%s  unreachable over ssh\\n' "$1"; return 1; }
t_wk() { echo "the peer was asked anyway"; }
'''

# A peer that answers, with a wk too old to know the flag.
OLD_PEER = OWN_STORE + '''
peer_workstations() { echo moose; }
load_target() { :; }
machine_answers() { return 0; }
t_wk() { return 1; }
'''


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
        self.env = {"WK_STORE": str(self.store)}

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

    def holder(self, machine="rpi5", kind="bench", name="rpi5/speedometer3",
               pid=None, ended=None):
        """A live task record holding <machine>, written by lib/task.sh."""
        if pid is None:
            pid = self.spawn()
        body = ['d=$(task_begin --holds device:%s %s here %s "kill %d" "" "one step")'
                % (machine, kind, shlex.quote(name), pid),
                'task_pid "$d" %d' % pid]
        if ended is not None:
            body.append('task_end "$d" %s' % ended)
        body.append('printf "%s" "$d"')
        return Path(self.sh("\n".join(body)).stdout.strip()), pid

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

    def test_the_claim_is_written_before_the_plan_that_publishes_the_record(self):
        """task_list only counts a directory with a plan in it, so a claim
        written after the plan would be a board held by nobody for as long as
        the two writes are apart."""
        text = (REPO / "lib" / "task.sh").read_text()
        self.assertLess(text.index('_task_put "$dir/holds"'),
                        text.index('mv "$dir/plan.tmp.$$" "$dir/plan"'))

    def test_a_live_holder_is_one_row_naming_the_machine_and_its_kill(self):
        _, pid = self.holder()
        rows = self.sh('task_holders device:rpi5').stdout.splitlines()
        self.assertEqual(1, len(rows), rows)
        task_id, machine, what, kill = rows[0].split(TAB)
        self.assertTrue(task_id.startswith("bench-rpi5-speedometer3-"), task_id)
        self.assertEqual("bench rpi5/speedometer3", what)
        self.assertEqual("kill %d" % pid, kill)
        self.assertTrue(machine, "the row does not say which machine holds it")

    def test_another_resource_is_not_this_one(self):
        self.holder(machine="rpi4")
        self.assertEqual("", self.sh('task_holders device:rpi5').stdout)

    def test_a_task_that_ended_holds_nothing(self):
        self.holder(ended=0)
        self.assertEqual("", self.sh('task_holders device:rpi5').stdout)

    def test_a_holder_whose_pid_is_gone_holds_nothing(self):
        """The claim cannot outlive its holder: liveness is the process table
        at read time, so a killed driver leaves no board held."""
        _, pid = self.holder()
        os.kill(pid, 9)
        for _ in range(50):
            if not alive(pid):
                break
            time.sleep(0.1)
        self.assertEqual("", self.sh('task_holders device:rpi5').stdout)

    def test_the_record_is_released_when_its_command_ends(self):
        d, _ = self.holder()
        self.sh('WK_DEVICE_TASK=%s device_release' % shlex.quote(str(d)))
        self.assertTrue((d / "exit").exists())
        self.assertEqual("", self.sh('task_holders device:rpi5').stdout)


class TestTheFleetIsAsked(ClaimTest):
    ROW = "bench-rpi5-x-20260915T000000Z-9\tmoose\tbench rpi5/speedometer3\tkill 4242"

    def test_a_peer_is_asked_through_its_own_wk(self):
        cp = self.sh(PEER + 'fleet_holders device:rpi5',
                     env={"PEER_ROWS": self.ROW})
        self.assertEqual([self.ROW], cp.stdout.splitlines())

    def test_this_machine_and_the_peers_are_both_the_answer(self):
        self.holder()
        cp = self.sh(PEER + 'fleet_holders device:rpi5', env={"PEER_ROWS": self.ROW})
        rows = cp.stdout.splitlines()
        self.assertEqual(2, len(rows), rows)
        self.assertEqual(self.ROW, rows[1])

    def test_a_peer_that_cannot_be_reached_is_a_row_of_its_own(self):
        """Never silence: an unread machine is not a free board."""
        cp = self.sh(DEAF_PEER + 'fleet_holders device:rpi5')
        rows = cp.stdout.splitlines()
        self.assertEqual(1, len(rows), rows)
        _, who, what, why = rows[0].split(TAB)
        self.assertEqual(("moose", "unknown"), (who, what))
        self.assertIn("unreachable over ssh", why)

    def test_a_peer_whose_wk_does_not_read_the_flag_is_the_same_row(self):
        cp = self.sh(OLD_PEER + 'fleet_holders device:rpi5')
        rows = cp.stdout.splitlines()
        self.assertEqual(1, len(rows), rows)
        _, who, what, why = rows[0].split(TAB)
        self.assertEqual(("moose", "unknown"), (who, what))
        self.assertIn("wk sync --tools moose", why)

    def test_the_read_needs_the_library_that_knows_the_peers(self):
        cp = bash(PRELUDE + 'fleet_holders device:rpi5', env=self.env)
        self.assertNotEqual(0, cp.returncode)
        self.assertIn("lib/target.sh", cp.stdout + cp.stderr)


class TestTheBarrier(ClaimTest):
    HOLD = 'device_hold rpi5 bench rpi5/jetstream3 "kill $$" "" "jetstream3 on rpi5"'

    def test_a_free_board_is_taken_and_the_claim_is_the_record(self):
        cp = self.sh(NO_PEERS + self.HOLD + '\nprintf "%s|%s" "$WK_DEVICE_TASK" "$WK_DEVICE_HELD"')
        task, held = cp.stdout.split("|")
        self.assertEqual("device:rpi5", held)
        self.assertEqual("device:rpi5", (Path(task) / "holds").read_text().strip())
        self.assertTrue(task.startswith(str(self.store)), task)

    def test_a_held_board_refuses_naming_the_machine_the_task_and_the_remedy(self):
        _, pid = self.holder()
        cp = self.sh(NO_PEERS + self.HOLD, check=False)
        out = cp.stdout + cp.stderr
        self.assertNotEqual(0, cp.returncode, out)
        self.assertIn("rpi5", out)
        self.assertIn("bench rpi5/speedometer3", out)
        self.assertIn("kill %d" % pid, out)
        self.assertIn("--force", out)
        self.assertEqual(1, len(self.tasks()), "a refused claim wrote a record anyway")

    def test_a_peer_holding_it_refuses_here(self):
        cp = self.sh(PEER + self.HOLD, check=False,
                     env={"PEER_ROWS": TestTheFleetIsAsked.ROW})
        out = cp.stdout + cp.stderr
        self.assertNotEqual(0, cp.returncode, out)
        self.assertIn("moose", out)
        self.assertIn("kill 4242", out)
        self.assertEqual([], self.tasks())

    def test_force_crosses_it_records_the_forcing_and_takes_the_claim(self):
        self.holder()
        cp = self.sh(NO_PEERS + self.HOLD + '\nprintf "%s" "$WK_DEVICE_TASK"',
                     env={"WK_FORCE": "1"})
        out = cp.stdout + cp.stderr
        self.assertIn("FORCED past a barrier", out)
        self.assertEqual(2, len(self.tasks()), out)

    def test_a_peer_that_could_not_be_asked_is_reported_and_does_not_refuse(self):
        """A workstation that is off is the normal state, so this is a warning
        and not a refusal -- but it is never silent."""
        cp = self.sh(DEAF_PEER + self.HOLD)
        self.assertIn("moose", cp.stderr)
        self.assertIn("could not be asked", cp.stderr)
        self.assertEqual(1, len(self.tasks()))

    def test_one_drivers_own_claim_passes_down_to_what_it_runs(self):
        """`wk pi bench --ab-systems` runs `wk boot` for each leg: a claim
        that refused its own holder would deadlock the board's own driver."""
        self.holder()
        cp = self.sh(NO_PEERS + self.HOLD + '\nprintf "%s" "${WK_DEVICE_TASK:-none}"',
                     env={"WK_DEVICE_HELD": "device:rpi5"})
        self.assertEqual("none", cp.stdout)
        self.assertEqual(1, len(self.tasks()), "the inherited claim wrote a second record")

    def test_an_inherited_claim_on_another_board_still_refuses(self):
        self.holder()
        cp = self.sh(NO_PEERS + self.HOLD, check=False, env={"WK_DEVICE_HELD": "device:rpi4"})
        self.assertNotEqual(0, cp.returncode, cp.stdout + cp.stderr)

    def test_a_dry_run_reads_the_claim_and_takes_none(self):
        cp = self.sh(NO_PEERS + self.HOLD + '\nprintf "%s" "${WK_DEVICE_TASK:-none}"',
                     env={"WK_DRY_RUN": "1"})
        self.assertEqual("none", cp.stdout)
        self.assertEqual([], self.tasks())

    def test_a_dry_run_still_refuses_a_held_board(self):
        self.holder()
        cp = self.sh(NO_PEERS + self.HOLD, check=False, env={"WK_DRY_RUN": "1"})
        self.assertNotEqual(0, cp.returncode, cp.stdout + cp.stderr)


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


MACHINE_CONF = '''NODE_SSH=fakeboard
NODE_DRIVER=no-such-driver
NODE_DEVICE=/dev/null
NODE_PROFILE=webkit-2.52-yocto-rpi5-64
NODE_ROLE=bench-device
NODE_NOTE="a board that is not there, for a refusal that needs no hardware"
'''


class TestThePodmanMachineIsAskedToo(WkTest):
    """A deploy is routed to the machine holding the lane, and on a macOS
    workstation that is the podman machine -- so a claim can be taken in a
    store this side does not read. It is asked exactly when the store is not
    this machine's: on Linux the container target's store is the directory
    task_holders has already walked, and a board would read as held by
    itself."""

    ASK = PRELUDE + '''
peer_workstations() { :; }
wk_machine_name() { echo tolken; }
load_target() { :; }
t_wk() { printf '%s\\n' "$VM_ROWS"; }
'''

    def _rows(self, local, vm_rows="", store_is_local=True):
        return bash(self.ASK
                    + ("store_is_local() { return %d; }\n" % (0 if store_is_local else 1))
                    + ("task_holders() { %s; }\n" % (("printf '%s\\n' " + repr(local))
                                                     if local else ":"))
                    + "fleet_holders device:rpi5\n",
                    env={"VM_ROWS": vm_rows})

    ROW = "id\ttolken\tbench rpi5/speedometer3\twk pi bench rpi5 --kill"

    def test_a_store_of_this_machines_own_is_asked_once(self):
        cp = self._rows(self.ROW, vm_rows=self.ROW)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip().count("bench rpi5/speedometer3"), 1,
                         "a board read as held by itself")

    def test_a_store_elsewhere_is_asked_as_well(self):
        cp = self._rows("", vm_rows=self.ROW, store_is_local=False)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("bench rpi5/speedometer3", cp.stdout,
                      "a claim taken in the podman machine was invisible")

    def test_a_machine_that_did_not_answer_is_a_row_of_its_own(self):
        """Never silence: an unread store is not a free board."""
        cp = bash(PRELUDE + '''
peer_workstations() { :; }
wk_machine_name() { echo tolken; }
load_target() { :; }
store_is_local() { return 1; }
task_holders() { :; }
t_wk() { return 1; }
fleet_holders device:rpi5
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("unknown", cp.stdout)
        self.assertIn("podman machine", cp.stdout)


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
            env=dict(os.environ, WK_STORE=str(self.store)),
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
