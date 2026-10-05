"""A preflight whose measurement cannot be taken reports unknown and lets the command go on; a measured shortfall refuses."""
import contextlib
import io
import sys
import types
import unittest

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import guest, resources  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.bench import board, mac_ab, pipeline, record, report  # noqa: E402
from wk.machine import Fake  # noqa: E402


def failing_df():
    m = Fake()
    m.answer(["df", "-Pk"], rc=1, err="df: no such file")
    return m


def disk_admit():
    b = resources.Budget(failing_df(), {})
    b.disk_admit("this build", 60, b.free_gb("/s"), "/s")


def disk_admit_on_garbage():
    resources.Budget(Fake(), {}).disk_admit("this build", 60, resources.parse_df("garbage"), "the guest")


def host_disk():
    guest.host_disk(types.SimpleNamespace(machine=failing_df(), env={}))


def load_row():
    run = types.SimpleNamespace(recs=types.SimpleNamespace(list=lambda: []), here=Fake(), reg=types.SimpleNamespace(env={}),
                                system=types.SimpleNamespace(host_os="linux"), busy_builds=lambda: 0)
    rows = pipeline.Run.idle_rows(run)
    if record.failed(rows):
        raise Refused(1)


class TestAnUnmeasuredPreflightIsUnknownNotFailure(unittest.TestCase):
    CASES = {"disk_admit on a df that fails": disk_admit,
             "disk_admit on a df nobody can parse": disk_admit_on_garbage,
             "host_disk on a df that fails": host_disk,
             "the bench machine-idle row on an unreadable load average": load_row}

    def test_each_goes_ahead(self):
        for name, check in self.CASES.items():
            with self.subTest(name), contextlib.redirect_stderr(io.StringIO()):
                check()

    def test_a_measured_shortfall_still_refuses(self):
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()):
            resources.Budget(Fake(), {}).disk_admit("this build", 60, 1, "/s")

    def test_an_unreadable_word_size_does_not_refuse_the_profiler_stage(self):
        run = types.SimpleNamespace(board="b", doc={}, bench=lambda: Fake(), facts_={})
        leg = types.SimpleNamespace(o={}, slot="s")
        with contextlib.redirect_stderr(io.StringIO()):
            board.BoardSystem.profiler_stage(run, leg)

    def test_an_unread_clock_is_not_a_failed_clock_check(self):
        system = types.SimpleNamespace(clk={}, display="hdmi", throttled=lambda: "")
        rows, _ = board.BoardSystem.checks(system, types.SimpleNamespace())
        self.assertEqual([], record.failed(rows))
        self.assertEqual(["clock pinned"], [what for _, what, _ in record.unmeasured(rows)])

    def test_a_measured_unpinned_clock_still_fails(self):
        system = types.SimpleNamespace(clk={"min": "600000", "max": "1400000"}, display="hdmi", throttled=lambda: "")
        rows, _ = board.BoardSystem.checks(system, types.SimpleNamespace())
        self.assertEqual(["clock pinned"], [what for _, what, _ in record.failed(rows)])

    def test_an_absent_staging_root_or_bench_home_refuses_the_preflight(self):
        d = types.SimpleNamespace(mode="host", bench_root=lambda: "")
        run = types.SimpleNamespace(d=d, name="m", guest=False)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, mac_ab.MacAB.preflight(run))
        m = types.SimpleNamespace(test=lambda *a: True)
        d = types.SimpleNamespace(mode="host", bench_root=lambda: "/r", bench_home=lambda: "", c=lambda k: "")
        run = types.SimpleNamespace(d=d, name="m", guest=False, mac=m, provisioned=lambda root: True)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, mac_ab.MacAB.preflight(run))

    def test_a_reboot_stops_on_a_display_or_firmware_default_it_could_not_read(self):
        for guest, display, firmware in ((True, (record.UNKNOWN, "no JSON"), None),
                                         (False, (True, ""), record.UNKNOWN),
                                         (False, (True, ""), False)):
            run = types.SimpleNamespace(name="m", guest=guest, display_check=lambda display=display: display, fw_detail="x",
                                        firmware_is_bench=lambda firmware=firmware: firmware,
                                        d=types.SimpleNamespace(boot_id=lambda: self.fail("rebooted")))
            with self.subTest(guest=guest, display=display, firmware=firmware), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(Refused):
                    mac_ab.MacAB.restart(run)

    def test_gates_and_preflight_count_the_rows_not_measured(self):
        self.assertEqual("", record.not_measured(0))
        self.assertIn("2", record.not_measured(2))

    def test_a_reading_that_could_not_be_taken_is_named_in_the_result_and_its_report(self):
        rows = [(True, "a", "fine"), (record.UNKNOWN, "b", "not read"), (False, "c", "measured bad")]
        notes = record.preflight_notes(rows, ["n"])
        self.assertIn("c: measured bad", notes)
        self.assertIn(record.UNMEASURED + "b: not read", notes)
        self.assertNotIn(record.UNMEASURED + "c", notes)
        run = {"env": {"preflight_notes": notes}, "state": "ok", "dir": "/nowhere"}
        verdicts = {(v, c) for v, c, _ in report.checks("/nowhere", {}, [run], "workspace")}
        self.assertIn(("unknown", "preflight"), verdicts)

    def test_an_unknown_row_renders_apart_from_ok_and_fail(self):
        for ok, word in ((True, "ok"), (False, "FAIL"), (record.UNKNOWN, "unk")):
            with self.subTest(word), contextlib.redirect_stderr(io.StringIO()) as e:
                pipeline.Run.check(ok, "x", "d")
            self.assertTrue(e.getvalue().lstrip().startswith(word))


if __name__ == "__main__":
    unittest.main()
