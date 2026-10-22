"""mac-volume (lib/wk/boot/mac.py) against FakeMac (tests/fake_boot.py): its own behaviour beyond machine.conformance."""
import contextlib
import io
import sys
import unittest

from tests.support import REPO, requires_machine

sys.path.insert(0, str(REPO / "lib"))

from wk import act  # noqa: E402
from wk.boot import open_driver  # noqa: E402
from wk.boot.mac import HELPER  # noqa: E402
from tests.fake_boot import board, conf_for  # noqa: E402

def quiet(fn, *args):
    with contextlib.redirect_stderr(io.StringIO()) as err:
        try:
            return fn(*args), err.getvalue()
        except act.Refused:
            return act.Refused, err.getvalue()


def volume(**kw):
    return board("mac-volume", **kw)


class TestProbe(unittest.TestCase):
    """Host mode first, and asked for the marker too; a bench node answering without one is some other computer."""

    def test_a_marker_on_the_host_side_is_bench_mode_there(self):
        fake, d = volume()
        fake.roots["host"]["id"] = "perf-macos-tolken-2026-08"
        self.assertEqual((d.probe(), d.ch.side), ("bench perf-macos-tolken-2026-08", "host"))
        self.assertFalse([a for a in fake.asked if a[0] == "bench"])

    def test_a_bench_node_without_the_marker_or_a_machine_declaring_none_is_unreachable(self):
        fake, d = volume()
        fake.enter_bench()
        fake.roots["bench"]["id"] = ""
        self.assertEqual((d.probe(), d.ch.side), ("unreachable", "none"))
        fake.conf["bench_ssh"] = ""
        fake.asked.clear()
        self.assertEqual(d.probe(), "unreachable")
        self.assertFalse([a for a in fake.asked if a[0] == "bench"])

    def test_the_boot_id_is_the_boottime_seconds_and_not_usec(self):
        fake, d = volume()
        d.probe()
        self.assertEqual((d.boot_id(), d.booted_at()), (str(fake.booted), fake.clock.iso()))


class TestArm(unittest.TestCase):
    """--setBoot is sticky and Apple Silicon has no one-shot form, so the return is proven before the trip out."""

    def arm(self, **fake_state):
        fake, d = volume()
        for k, v in fake_state.items():
            setattr(fake, k, v)
        d.probe()
        steps = d.arm_sequence(*d.choose(""))
        got, err = quiet(lambda: [step() for _, step in steps[:-1]] and 0)
        return fake, got, err, fake.priv_asked()

    def test_it_arms_and_asserts_the_firmware_afterwards(self):
        fake, got, err, verbs = self.arm()
        self.assertEqual(got, 0, err)
        self.assertIn("the firmware will boot 'WK Bench' next", err)
        self.assertLess(verbs.index("boot-host"), verbs.index("boot-volume"))
        self.assertEqual(fake.firmware, "bench")
        self.assertEqual({a[0] for a in fake.asked if a[1:4] == ("sudo", "-n", HELPER)}, {"host"})

    def test_each_refusal_reports_what_the_mac_said_and_arms_nothing_past_it(self):
        for state, words, blessed in (
                ({"said": {"boot-host": (1, "wk-boot-priv: bless said: Error -60005: cannot sign\n")}}, "Error -60005: cannot sign", False),
                ({"said": {"boot-volume": (1, "wk-boot-priv: bless exited 1, so the firmware was not told\n")}}, "nothing was changed", True),
                ({"firmware": "bench", "said": {"boot-host": (0, "wk-boot-priv: blessed /\n")}}, "Nothing was armed", False),
                ({"stuck": True}, "mbp's firmware still names", True)):
            with self.subTest(words=words):
                fake, got, err, verbs = self.arm(**state)
                self.assertIs(got, act.Refused)
                self.assertIn(words, err)
                self.assertEqual("boot-volume" in verbs, blessed)

    def test_an_absent_volume_or_helper_is_refused_before_anything_is_asked(self):
        for state, words in (({"attached": False}, "is not attached to mbp"), ({"helper_is": None}, "boot helper is not installed")):
            with self.subTest(state=state):
                fake, got, err, verbs = self.arm(**state)
                self.assertIs(got, act.Refused)
                self.assertIn(words, err)
                self.assertEqual(verbs, [])


class TestReturn(unittest.TestCase):
    def test_a_reboot_from_the_bench_side_is_refused_with_the_reason(self):
        fake, d = volume()
        fake.enter_bench()
        d.probe()
        got, err = quiet(d.reboot)
        self.assertIs(got, act.Refused)
        self.assertIn("carries no boot helper", err)
        self.assertEqual(fake.priv_asked(), [])

    def test_a_disarm_that_cannot_bless_back_is_refused(self):
        fake, d = volume()
        fake.said = {"boot-host": (1, "wk-boot-priv: bless exited 1\n")}
        d.probe()
        got, err = quiet(d.disarm)
        self.assertIs(got, act.Refused)
        for remedy in ("wk machine setup mbp", "wk boot mbp --disarm"):
            self.assertIn(remedy, err)

class TestReports(unittest.TestCase):
    def test_a_bench_answer_reports_the_medium_as_that_installs_own_root(self):
        fake, d = volume()
        fake.enter_bench()
        d.probe()
        self.assertIn("tolken-bench is running from it", d.media())

    def test_silence_on_both_nodes_names_both_of_them(self):
        fake, d = volume()
        fake.up = False
        d.probe()
        self.assertIn("neither tolken nor tolken-bench answers", d.media())
        for want in ("booted_volume=unknown", "firmware_default=unknown", "bench_display=builtin 1280x832"):
            self.assertIn(want, d.evidence())

    def test_evidence_reads_the_facts_with_the_trees_own_reader(self):
        fake, d = volume()
        fake.firmware = "bench"
        d.probe()
        ev = d.evidence()
        for want in ("booted_volume=Macintosh HD", "attached at /Volumes/WK Bench", "a plain reboot is expected to enter bench mode"):
            self.assertIn(want, ev)
        self.assertTrue(fake.fact_input.startswith("#!/usr/bin/env python3"), "lib/wk/mac.py travels on stdin")

    def test_the_bench_system_is_the_volumes_marker(self):
        fake, d = volume()
        d.probe()
        self.assertEqual(d.systems(), [("/Volumes/WK Bench", "sys-a")])
        fake.attached = False
        self.assertEqual(d.systems(), [])
        fake.up = False
        self.assertIsNone(d.systems())


class TestOnTheMac(unittest.TestCase):
    @requires_machine("tolken")
    def test_what_an_arming_proves_first_reads_true_on_mbp(self):
        """`live boot.arm[mbp]`, read-only as `requires_machine` is: host mode, the volume attached, and a firmware
        default naming one of its installs. The bless round trip itself is the owed half."""
        d = open_driver(REPO, conf_for("mac-volume"))
        if d.probe() != "host":
            self.skipTest("mbp answers as %s, and an arming starts from host mode" % d.mode)
        self.assertTrue(d.volume_present(), d.media())
        fw = d.firmware_default()
        self.assertTrue("the host install" in fw or "'WK Bench'" in fw, fw)


if __name__ == "__main__":
    unittest.main()
