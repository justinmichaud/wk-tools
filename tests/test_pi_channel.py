"""The bench-system ssh channel: `i_ssh` (boot/machines.sh) is the channel
`wk pi deploy` and `wk pi bench` talk to the board over -- the system under
test, not the rescue/host install `m_ssh` reaches on a bench-device -- and
`wk pi boot-order`'s EEPROM order names, including the default this command
picks for a bench-device from its bench medium's own transport
(disk_tran_of_name, boot/disk.sh).

Unit tests only -- no board, no ssh, no hardware: a stub `ssh` on PATH
stands in for the real thing, the way tests/test_fleet_walk.py drives
boot/machines.sh's m_ssh/i_ssh against a fake fleet. boot_order_first is
lifted verbatim out of cmd/pi with the `sed -n '/^fn()/,/^}/p'` idiom
tests/test_pi.py already uses for pi_slot_dir/pi_launch_cmd.

Run: python3 -m unittest tests.test_pi_channel -v
"""
import re
import unittest

from tests.support import REPO, WkTest, bash, rand_suffix, run, scratch_dir, stub_path

CMD_PI = REPO / "cmd" / "pi"


def lift(fn_name):
    """See tests/test_pi.py's lift(): the same sed idiom, printing one
    function's body out of cmd/pi ready to `eval`."""
    return f'''
body="$(sed -n '/^{fn_name}()/,/^}}/p' "{CMD_PI}")"
[ -n "$body" ] || {{ echo "lift {fn_name} failed"; exit 1; }}
eval "$body"
'''


class TestBootOrderFirst(WkTest):
    """boot_order_first <order> <nibble>: that nibble moved to the front,
    the network nibble (2) stripped, everything else -- including a nibble
    this fleet does not use, like 6 (NVMe) -- left alone."""

    def _first(self, order, nibble):
        script = f'''
set -euo pipefail
{lift("boot_order_first")}
boot_order_first "{order}" "{nibble}"
'''
        cp = bash(script, timeout=15)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout.strip()

    def test_usb_first_from_the_sd_first_default(self):
        self.assertEqual(self._first("0xf412", "4"), "0xf14")

    def test_sd_first_from_usb_first(self):
        self.assertEqual(self._first("0xf14", "1"), "0xf41")

    def test_usb_first_from_sd_first(self):
        self.assertEqual(self._first("0xf41", "4"), "0xf14")

    def test_an_nvme_nibble_is_kept(self):
        out = self._first("0xf641", "4")
        self.assertIn("6", out.lstrip("0x"))


_FAKE_NODE_CONF = '''NODE_SSH={ssh}
NODE_DRIVER=pi-mbr
NODE_DEVICE={device}
NODE_ROOT=/dev/sda2
NODE_PROFILE=webkit-2.52-buildroot-rpi4
NODE_MAC=02:00:00:00:00:09
NODE_BRIDGE=""
NODE_ROLE={role}
NODE_OS=any
NODE_LOCAL=""
NODE_VOLUME=""
NODE_DTB=""
NODE_BENCH_SSH="{ssh}-bench"
NODE_NET=ethernet
NODE_NOTE="fake board for test_pi_channel"
'''

# Stands in for a live board: `wk pi boot-order`'s rsh/ssh calls all run
# their remote command locally via bash -- including `sudo`, `rpi-eeprom-config`
# and `command -v rpi-eeprom-config`, which this stub set makes answer the
# way a Pi 4 running Raspberry Pi OS would (rpi-eeprom-config present,
# onboard method) with one starting BOOT_ORDER: 0xf421, chosen so every one
# of the three orders below computes a different value, and none of them is
# a no-op -- the "boot order: <name>" log line this test greps for is
# printed only when applying the order actually changes BOOT_ORDER.
_ANSWERING_SSH = '''#!/bin/sh
for last; do :; done
exec bash -c "$last"
'''
_PASSTHROUGH_SUDO = '''#!/bin/sh
exec "$@"
'''
_FAKE_EEPROM_CONFIG = '''#!/bin/sh
printf 'BOOT_ORDER=0xf421\\n'
'''


class TestBootOrderDefault(WkTest):
    """The default order `wk pi boot-order` picks when none is named on the
    command line: a bench-device's default follows its bench medium's own
    transport (NODE_DEVICE), a workstation's is always `local`."""

    def _default_order_name(self, role, device):
        with scratch_dir(prefix="wk-test-pi-machines-") as machdir, \
             stub_path({
                 "ssh": _ANSWERING_SSH,
                 "sudo": _PASSTHROUGH_SUDO,
                 "rpi-eeprom-config": _FAKE_EEPROM_CONFIG,
             }) as binp:
            host = f"fakepi{rand_suffix(4)}"
            (machdir / f"{host}.conf").write_text(
                _FAKE_NODE_CONF.format(ssh=host, device=device, role=role)
            )
            env = {
                "WK_MACHINES_DIR": str(machdir),
                "PATH": f"{binp}:{self._real_path()}",
            }
            cp = run("pi", "boot-order", host, "--dry-run", env=env, timeout=30)
            self.assertEqual(cp.returncode, 0, cp.stdout)
            return cp.stdout

    @staticmethod
    def _real_path():
        import os
        return os.environ.get("PATH", "/usr/bin:/bin")

    def test_bench_device_on_mmcblk_defaults_sd_first(self):
        out = self._default_order_name("bench-device", "/dev/mmcblk0")
        self.assertIn("boot order: sd-first", out, out)

    def test_bench_device_on_sda_defaults_usb_first(self):
        out = self._default_order_name("bench-device", "/dev/sda")
        self.assertIn("boot order: usb-first", out, out)

    def test_workstation_defaults_local(self):
        out = self._default_order_name("workstation", "/dev/sda")
        self.assertIn("boot order: local", out, out)

    def test_unknown_order_name_is_still_refused(self):
        with scratch_dir(prefix="wk-test-pi-machines-") as machdir, \
             stub_path({"ssh": _ANSWERING_SSH}) as binp:
            host = f"fakepi{rand_suffix(4)}"
            (machdir / f"{host}.conf").write_text(
                _FAKE_NODE_CONF.format(ssh=host, device="/dev/sda", role="bench-device")
            )
            env = {
                "WK_MACHINES_DIR": str(machdir),
                "PATH": f"{binp}:{self._real_path()}",
            }
            cp = run("pi", "boot-order", host, "bogus-order", env=env, timeout=30)
            self.assertNotEqual(cp.returncode, 0, cp.stdout)
            self.assertIn("no such boot order", cp.stdout)
            self.assertIn("sd-first", cp.stdout)
            self.assertIn("usb-first", cp.stdout)
            self.assertIn("local", cp.stdout)


_ECHO_SSH = '''#!/bin/sh
echo "ARGS:$*"
'''


class TestISshLoginRule(WkTest):
    """i_ssh (boot/machines.sh) logs in as root whatever NODE_ROLE says: this
    channel reaches the *bench system*, which is a wk image either way -- the
    driving key lives in root's authorized_keys (disk_install_fleet), and its
    whole install regenerates every reflash.

    NODE_ROLE is about host mode, and asking it here left a board whose host
    mode is a workstation unreachable in bench mode: jmichaud@rpi5-bench
    answers "Permission denied (publickey,password)" where root@rpi5-bench
    gives a shell (rpi5, 2026-09-04). Every i_ssh caller is cmd/pi; the macOS
    drivers override m_ssh with their own user and never come here.

    WK_IMAGE_HOST short-circuits image_addr's tailnet lookup (see
    tests/test_wk_overrides_lib.py's TestBootMachines)."""

    def _i_ssh_args(self, role):
        with stub_path({"ssh": _ECHO_SSH}) as binp:
            cp = self.bash(
                f'''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/reach.sh"
. "$WK_ROOT/boot/machines.sh"
WK_IMAGE_HOST=192.0.2.9
NODE_NAME=testmach
NODE_SSH=""
NODE_BENCH_SSH=""
NODE_MAC=""
NODE_ROLE={role}
i_ssh true
''',
                env={"PATH": f"{binp}:{self._real_path()}"},
            )
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            return cp.stdout

    @staticmethod
    def _real_path():
        import os
        return os.environ.get("PATH", "/usr/bin:/bin")

    def test_bench_device_passes_root(self):
        out = self._i_ssh_args("bench-device")
        self.assertIn("-l root", out, out)
        self.assertIn("192.0.2.9", out, out)

    def test_a_workstations_bench_system_is_root_too(self):
        """The machine is a workstation in host mode; the system it boots for
        a measurement is not, and only root accepts the driving key."""
        out = self._i_ssh_args("workstation")
        self.assertIn("-l root", out, out)
        self.assertIn("192.0.2.9", out, out)

    def test_i_ssh_no_longer_logs_in_as_the_caller(self):
        """The bug this replaces: i_ssh always logged in as `$(id -un)`,
        which is wrong for a bench-device (root only)."""
        import getpass
        out = self._i_ssh_args("bench-device")
        self.assertNotIn(f"{getpass.getuser()}@192.0.2.9", out, out)


BENCH_CHANNEL_FUNCTIONS = (
    "pi_deploy_slot", "cmd_bench", "pi_bench_once", "pi_session_rdk",
    "pi_session_weston", "pi_browsers_dead", "pi_display", "pi_pin_governor", "pi_ssh_cmd",
)


class TestBenchFunctionsUseTheBenchChannel(unittest.TestCase):
    """`wk pi deploy` and `wk pi bench` act on the bench system, not the
    rescue/host install `m_ssh`/`$NODE_SSH` name on a bench-device -- so
    none of the functions that carry either verb's work may spell either
    one out. A regression here is a board silently benched on its rescue
    system again."""

    @classmethod
    def setUpClass(cls):
        cls.text = CMD_PI.read_text()

    def _body(self, fn_name):
        m = re.search(r"^%s\(\) \{[^\n]*\n(.*?)^\}\n" % re.escape(fn_name),
                       self.text, re.M | re.S)
        self.assertIsNotNone(m, f"could not find function {fn_name!r} in {CMD_PI}")
        return m.group(1)

    def test_no_m_ssh_or_mach_ssh_in_bench_channel_functions(self):
        for fn in BENCH_CHANNEL_FUNCTIONS:
            body = self._body(fn)
            self.assertIsNone(
                re.search(r"(?<![\w])m_ssh(?![\w])", body),
                f"{fn} still calls m_ssh (the rescue/host channel):\n{body}",
            )
            self.assertNotIn(
                "$NODE_SSH", body,
                f"{fn} still names $NODE_SSH (the rescue/host ssh alias):\n{body}",
            )


if __name__ == "__main__":
    unittest.main()
