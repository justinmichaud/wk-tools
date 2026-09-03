"""Battery charge cap (docs/Urgent/HUMAN-battery.md): `wk bridge setup` writes
charge_control_end_threshold on the two bridge phones (bridge/provision.sh,
bridge/bin/wk-bridge-battery, bridge/init.d/wk-bridge-battery) and
`wk doctor --all` reads it back, over ssh, through `wk bridge battery
<name>`. On this Mac there is no equivalent to set -- macOS's optimized
charging has no CLI knob -- so `wk doctor --all` prints the honest
`pmset -g batt` line instead.

Every test here lifts the exact code that runs in production (the phone-side
apply script, and the two pure verdict functions in cmd/doctor) rather than
re-typing a second copy of the logic; see tests/test_wifi_seed.py and
tests/test_quick.py's _ls_classify for the same technique. No test needs a
phone or a Mac's real /sys or /etc: the apply script's CONF path is
overridden via WK_BRIDGE_BATTERY_CONF (bridge/bin/wk-bridge-battery), and the
verdict functions take their input as plain strings.

Run: python3 -m unittest tests.test_battery -v
"""
import re
import subprocess
import unittest
from pathlib import Path

from tests.support import REPO, scratch_dir

BATTERY_BIN = REPO / "bridge" / "bin" / "wk-bridge-battery"
BATTERY_INIT = REPO / "bridge" / "init.d" / "wk-bridge-battery"
CMD_BRIDGE = REPO / "cmd" / "bridge"
CMD_DOCTOR = REPO / "cmd" / "doctor"
LIB_COMMON = REPO / "lib" / "common.sh"
PROVISION = REPO / "bridge" / "provision.sh"


def _lift_func(path, name):
    """A function's body, sed'd out of a shell file -- the same technique
    tests/test_wifi_seed.py's _lift and tests/test_quick.py's _ls_classify
    lift use, so the exact code that runs in production is what is called."""
    text = subprocess.run(
        ["sed", "-n", f"/^{name}()/,/^}}/p", str(path)],
        capture_output=True, text=True,
    ).stdout
    assert text.strip(), f"{name}() not found in {path}"
    return text


class TestWkBridgeBatteryScript(unittest.TestCase):
    """The apply script phone-side: reads /etc/wk-bridge-battery.conf (here,
    a scratch file via WK_BRIDGE_BATTERY_CONF), writes
    charge_control_end_threshold, and is idempotent. It runs locally on the
    phone (the openrc service invokes it directly, no ssh involved there);
    `wk bridge setup` reaches it *through* ssh, which is what
    TestBatteryAppliedThroughFakeSsh below drives."""

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
        """A sysfs write can return success against a value the driver
        clamps to something else -- the write's own exit status is not
        proof the cap took. A fake `cat` stands in for the node: it always
        answers a value other than the one requested, the way a driver's
        show() callback would if it clamped the store() it just accepted."""
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
    """The same script, this time reached the way `wk bridge setup` reaches
    it: over ssh. A fake `ssh` (tests/support.py's stub_path) runs the
    command locally instead of on a phone, so this is the real write/read-back
    path with a scratch sysfs standing in for the phone's."""

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
    """battery_verdict (cmd/doctor): the ok/mismatch line `wk doctor --all`
    prints for one bridge phone, from `wk bridge battery <name>`'s
    key=value blob. Lifted so the exact function is what is tested; kv_get
    (lib/common.sh) is lifted alongside it, since battery_verdict reads its
    blob through the one parser rather than its own sed."""

    def _verdict(self, name, blob):
        script = (_lift_func(LIB_COMMON, "kv_get")
                  + _lift_func(CMD_DOCTOR, "battery_verdict")
                  + f'\nbattery_verdict {name!r} "$1"\n')
        cp = subprocess.run(["bash", "-c", script, "_", blob],
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout

    def test_ok_when_current_equals_the_configured_limit(self):
        out = self._verdict("tailnet-bridge-generic",
                             "percent=87\nstatus=Charging\nlimit=80\ncurrent=80\n")
        verdict, line = out.rstrip("\n").split("\t", 1)
        self.assertEqual(verdict, "ok")
        self.assertIn("87%", line)
        self.assertIn("capped at 80%", line)

    def test_miss_when_current_does_not_match_the_limit(self):
        out = self._verdict("tailnet-bridge-generic",
                             "percent=100\nstatus=Full\nlimit=80\ncurrent=100\n")
        verdict, line, remedy = out.rstrip("\n").split("\t", 2)
        self.assertEqual(verdict, "miss")
        self.assertIn("cap reads 100", line)
        self.assertIn("want 80", line)
        self.assertIn("wk bridge setup tailnet-bridge-generic", remedy)

    def test_miss_when_the_node_never_answered_a_current_value(self):
        out = self._verdict("tailnet-bridge-moose-bmc",
                             "percent=42\nstatus=Discharging\nlimit=80\ncurrent=?\n")
        verdict = out.split("\t", 1)[0]
        self.assertEqual(verdict, "miss")


class TestMacBatteryLine(unittest.TestCase):
    """mac_battery_line (cmd/doctor): the honest line for this Mac itself,
    parsed from a captured `pmset -g batt`. No settable limit exists on
    macOS, so every case ends in the same disclaimer."""

    PLUGGED_IN = (
        "Now drawing from 'AC Power'\n"
        " -InternalBattery-0 (id=4325193)\t87%; charging; 0:45 remaining present: true\n"
    )
    ON_BATTERY = (
        "Now drawing from 'Battery Power'\n"
        " -InternalBattery-0 (id=4325193)\t62%; discharging; 3:12 remaining present: true\n"
    )
    NO_BATTERY = "Now drawing from 'AC Power'\n"

    def _line(self, sample):
        script = _lift_func(CMD_DOCTOR, "mac_battery_line") + '\nmac_battery_line "$1"\n'
        return subprocess.run(["bash", "-c", script, "_", sample],
                               capture_output=True, text=True, timeout=10)

    def test_plugged_in(self):
        cp = self._line(self.PLUGGED_IN)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout, "plugged in, 87% -- no OS limit exists")

    def test_on_battery(self):
        cp = self._line(self.ON_BATTERY)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout, "on battery, 62% -- no OS limit exists")

    def test_no_battery_prints_nothing_and_fails(self):
        cp = self._line(self.NO_BATTERY)
        self.assertNotEqual(cp.returncode, 0)
        self.assertEqual(cp.stdout, "")


class TestSyntax(unittest.TestCase):
    """bash -n on every touched file: a name check `wk selftest` can run on
    every platform, phones included, with nothing to reach."""

    def test_touched_files_parse(self):
        for path, shell in (
            (CMD_BRIDGE, "bash"),
            (CMD_DOCTOR, "bash"),
            (PROVISION, "sh"),
            (BATTERY_BIN, "sh"),
            (BATTERY_INIT, "sh"),
        ):
            cp = subprocess.run([shell, "-n", str(path)], capture_output=True, text=True)
            self.assertEqual(cp.returncode, 0, f"{path}: {cp.stderr}")


class TestNoCaseNamesAPhone(unittest.TestCase):
    """CLAUDE.md: 'a case statement naming a machine is a bug'. The battery
    feature is one behaviour for both phones -- the sysfs node is
    autodetected (or pinned per-host in bridge/hosts/<name>.conf), never
    picked by branching on which phone this is."""

    def _device_names(self):
        text = (REPO / "bridge" / "devices.sh").read_text()
        m = re.search(r"cat <<'LIST'\n(.*?)\nLIST", text, re.S)
        assert m, "bridge_device_list's heredoc moved"
        return [line.split()[0] for line in m.group(1).splitlines() if line.strip()]

    def test_no_case_on_device_name_or_br_name(self):
        provision_text = PROVISION.read_text()
        step = re.search(
            r"(?ms)^# --- 17\. battery charge limit.*?(?=^# --- 18\.)", provision_text
        )
        self.assertIsNotNone(step, "battery step markers moved in bridge/provision.sh")

        bridge_text = CMD_BRIDGE.read_text()
        cmd_battery = re.search(r"(?ms)^cmd_battery\(\) \{.*?^\}", bridge_text)
        self.assertIsNotNone(cmd_battery, "cmd_battery not found in cmd/bridge")

        combined = "\n".join([
            step.group(0), cmd_battery.group(0),
            BATTERY_BIN.read_text(), BATTERY_INIT.read_text(),
        ])
        self.assertNotRegex(
            combined, r'case\s+"\$BR_(DEVICE|NAME|HOSTNAME)"',
            "battery code branches on which phone this is -- autodetect the sysfs "
            "node instead (BR_BATTERY in the host conf), the way BR_LAN_MAC does",
        )
        for name in self._device_names():
            self.assertNotIn(
                f'"{name}")', combined,
                f"battery code has a case arm for device '{name}'",
            )


if __name__ == "__main__":
    unittest.main()
