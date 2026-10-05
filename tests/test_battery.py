"""Battery charge cap: bridge/bin/wk-bridge-battery writes it on a bridge phone, `wk doctor --all` reads it back."""
import subprocess
import unittest

from tests.support import REPO, scratch_dir
from tests.test_doctor import MISS, OK, doctor

BATTERY_BIN = REPO / "bridge" / "bin" / "wk-bridge-battery"


class TestWkBridgeBatteryScript(unittest.TestCase):

    def _run(self, conf_path):
        return subprocess.run(
            ["sh", str(BATTERY_BIN)],
            env={"WK_BRIDGE_BATTERY_CONF": str(conf_path), "PATH": "/usr/bin:/bin"},
            capture_output=True, text=True, timeout=10,
        )

    def test_writes_the_threshold_and_it_reads_back(self):
        with scratch_dir(prefix="wk-test-battery-") as d:
            node = d / "sysfs" / "axp20x-battery"
            node.mkdir(parents=True)
            attr = node / "charge_control_end_threshold"
            attr.write_text("100")
            conf = d / "wk-bridge-battery.conf"
            conf.write_text(f"node={node}\nlimit=80\n")

            cp = self._run(conf)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertEqual(attr.read_text().strip(), "80")

    def test_idempotent_when_already_at_the_target(self):
        with scratch_dir(prefix="wk-test-battery-") as d:
            node = d / "sysfs" / "max170xx_battery"
            node.mkdir(parents=True)
            attr = node / "charge_control_end_threshold"
            attr.write_text("80")
            conf = d / "wk-bridge-battery.conf"
            conf.write_text(f"node={node}\nlimit=80\n")

            cp = self._run(conf)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertEqual(attr.read_text().strip(), "80")

    def test_a_node_that_is_not_writable_fails_and_is_left_alone(self):
        with scratch_dir(prefix="wk-test-battery-") as d:
            node = d / "sysfs" / "bq25890-battery"
            node.mkdir(parents=True)
            attr = node / "charge_control_end_threshold"
            attr.write_text("100")
            attr.chmod(0o444)
            conf = d / "wk-bridge-battery.conf"
            conf.write_text(f"node={node}\nlimit=80\n")

            try:
                cp = self._run(conf)
                self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                self.assertEqual(attr.read_text().strip(), "100")
            finally:
                attr.chmod(0o644)

    def test_no_conf_yet_is_a_no_op_not_a_crash(self):
        with scratch_dir(prefix="wk-test-battery-") as d:
            cp = self._run(d / "no-such-conf.conf")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_a_clamped_readback_is_reported_as_did_not_take(self):
        from tests.support import stub_path

        with scratch_dir(prefix="wk-test-battery-") as d:
            node = d / "sysfs" / "clamping-battery"
            node.mkdir(parents=True)
            attr = node / "charge_control_end_threshold"
            attr.write_text("100")
            conf = d / "wk-bridge-battery.conf"
            conf.write_text(f"node={node}\nlimit=80\n")

            fake_cat = (
                f'if [ "$1" = "{attr}" ]; then\n'
                f'    echo 90\n'
                f'else\n'
                f'    exec /bin/cat "$@"\n'
                f'fi\n'
            )
            with stub_path({"cat": fake_cat}) as binp:
                cp = subprocess.run(
                    ["sh", str(BATTERY_BIN)],
                    env={"WK_BRIDGE_BATTERY_CONF": str(conf),
                         "PATH": f"{binp}:/usr/bin:/bin"},
                    capture_output=True, text=True, timeout=10,
                )
            self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
            # The real write went through -- the failure is the clamped
            # readback, not a write that never landed.
            self.assertEqual(attr.read_text().strip(), "80")


_ANSWERING_SSH = '''#!/bin/sh
# Stand in for a live phone: run the remote command locally, the way a real
# ssh to a reachable bridge would (tests/test_fleet_walk.py's own stub).
for last; do :; done
exec bash -c "$last"
'''


class TestBatteryAppliedThroughFakeSsh(unittest.TestCase):

    def test_ssh_driven_write_and_read_back(self):
        with scratch_dir(prefix="wk-test-battery-ssh-") as d:
            from tests.support import stub_path

            node = d / "sysfs" / "axp20x-battery"
            node.mkdir(parents=True)
            attr = node / "charge_control_end_threshold"
            attr.write_text("100")
            conf = d / "wk-bridge-battery.conf"
            conf.write_text(f"node={node}\nlimit=80\n")

            with stub_path({"ssh": _ANSWERING_SSH}) as binp:
                cmd = (
                    f"WK_BRIDGE_BATTERY_CONF={conf} sh {BATTERY_BIN}"
                )
                cp = subprocess.run(
                    ["ssh", "-o", "BatchMode=yes", "root@fake-bridge-phone", cmd],
                    env={"PATH": f"{binp}:/usr/bin:/bin"},
                    capture_output=True, text=True, timeout=10,
                )
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertEqual(attr.read_text().strip(), "80")


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

    PLUGGED_IN = (
        "Now drawing from 'AC Power'\n"
        " -InternalBattery-0 (id=4325193)\t87%; charging; 0:45 remaining present: true\n"
    )
    ON_BATTERY = (
        "Now drawing from 'Battery Power'\n"
        " -InternalBattery-0 (id=4325193)\t62%; discharging; 3:12 remaining present: true\n"
    )
    NO_BATTERY = "Now drawing from 'AC Power'\n"

    def test_plugged_in(self):
        self.assertEqual("plugged in, 87% -- no OS limit exists", doctor.mac_battery_line(self.PLUGGED_IN))

    def test_on_battery(self):
        self.assertEqual("on battery, 62% -- no OS limit exists", doctor.mac_battery_line(self.ON_BATTERY))

    def test_no_battery_is_no_line(self):
        self.assertIsNone(doctor.mac_battery_line(self.NO_BATTERY))


if __name__ == "__main__":
    unittest.main()
