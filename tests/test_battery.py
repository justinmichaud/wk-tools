"""Battery charge cap: bridge/bin/wk-bridge-battery writes it on a bridge phone, `wk doctor --all` reads it back."""
import subprocess
import unittest

from tests.support import REPO, scratch_dir, stub_path
from tests.test_doctor import MISS, OK, doctor

BATTERY_BIN = REPO / "bridge" / "bin" / "wk-bridge-battery"


class TestWkBridgeBatteryScript(unittest.TestCase):
    def setUp(self):
        self.d = self.enterContext(scratch_dir(prefix="wk-test-battery-"))
        node = self.d / "sysfs" / "battery"
        node.mkdir(parents=True)
        self.attr = node / "charge_control_end_threshold"
        self.attr.write_text("100")
        self.conf = self.d / "wk-bridge-battery.conf"
        self.conf.write_text("node=%s\nlimit=80\n" % node)

    def run_script(self, path="/usr/bin:/bin", conf=None):
        return subprocess.run(["sh", str(BATTERY_BIN)], env={"WK_BRIDGE_BATTERY_CONF": str(conf or self.conf), "PATH": path},
                              capture_output=True, text=True, timeout=10)

    def test_writes_the_threshold_and_a_rerun_keeps_it(self):
        for _ in range(2):
            cp = self.run_script()
            self.assertEqual((cp.returncode, self.attr.read_text().strip()), (0, "80"), cp.stdout + cp.stderr)

    def test_a_node_that_is_not_writable_fails_and_is_left_alone(self):
        self.attr.chmod(0o444)
        self.addCleanup(self.attr.chmod, 0o644)
        self.assertNotEqual(self.run_script().returncode, 0)
        self.assertEqual(self.attr.read_text().strip(), "100")

    def test_no_conf_yet_is_a_no_op_not_a_crash(self):
        self.assertEqual(self.run_script(conf=self.d / "no-such-conf.conf").returncode, 0)

    def test_a_clamped_readback_is_reported_as_did_not_take(self):
        fake_cat = 'if [ "$1" = "%s" ]; then echo 90; else exec /bin/cat "$@"; fi\n' % self.attr
        with stub_path({"cat": fake_cat}) as binp:
            cp = self.run_script(path="%s:/usr/bin:/bin" % binp)
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertEqual(self.attr.read_text().strip(), "80")


class TestBatteryVerdict(unittest.TestCase):

    def test_ok_when_current_equals_the_configured_limit(self):
        state, line, _ = doctor.battery_verdict("tailnet-bridge-generic",
                                                "percent=87\nstatus=Charging\nlimit=80\ncurrent=80\n")
        self.assertEqual(state, OK)
        self.assertIn("87%", line)
        self.assertIn("capped at 80%", line)

    def test_miss_when_current_does_not_match_the_limit(self):
        state, line, remedy = doctor.battery_verdict("tailnet-bridge-generic",
                                                     "percent=100\nstatus=Full\nlimit=80\ncurrent=100\n")
        self.assertEqual(state, MISS)
        self.assertIn("cap reads 100", line)
        self.assertIn("want 80", line)
        self.assertEqual("wk machine setup tailnet-bridge-generic", remedy)

    def test_miss_when_the_node_never_answered_a_current_value(self):
        state = doctor.battery_verdict("tailnet-bridge-moose-bmc",
                                       "percent=42\nstatus=Discharging\nlimit=80\ncurrent=?\n")[0]
        self.assertEqual(state, MISS)


class TestMacBatteryLine(unittest.TestCase):

    def test_the_power_source_and_charge_or_no_line_without_a_battery(self):
        for pmset, line in (("Now drawing from 'AC Power'\n -InternalBattery-0 (id=4325193)\t87%; charging; 0:45 remaining present: true\n",
                             "plugged in, 87% -- no OS limit exists"),
                            ("Now drawing from 'Battery Power'\n -InternalBattery-0 (id=4325193)\t62%; discharging; 3:12 remaining present: true\n",
                             "on battery, 62% -- no OS limit exists"),
                            ("Now drawing from 'AC Power'\n", None)):
            with self.subTest(line=line):
                self.assertEqual(line, doctor.mac_battery_line(pmset))


if __name__ == "__main__":
    unittest.main()
