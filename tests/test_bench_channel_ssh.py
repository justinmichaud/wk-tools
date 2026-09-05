"""The bench system is reached as root whatever the machine is in host mode.

Two channels, two questions. `m_ssh` talks to the machine in *host* mode,
where NODE_ROLE is the right question: a bench-device's host mode is its
rescue, driven as root; a workstation's is a person. `i_ssh` talks to the
*bench system*, which is a wk image either way -- the driving key is in
root's authorized_keys (disk_install_fleet) and it boots with a fresh host
key every time it is written.

Asking the role on the bench channel left every board whose host mode is a
workstation unreachable once it was in bench mode: `jmichaud@rpi5-bench`
answers "Permission denied (publickey,password)" where `root@rpi5-bench`
gives a shell, so `wk boot --keep`, `wk pi deploy` and `wk pi bench` could
not reach the system they exist to drive (rpi5, 2026-09-04).

Run: python3 -m unittest tests.test_bench_channel_ssh -v
"""
import re
import subprocess
import unittest

from tests.support import REPO, bash

MACHINES = REPO / "boot" / "machines.sh"
PI = REPO / "cmd" / "pi"


def lift(*funcs):
    out = []
    for f in funcs:
        body = subprocess.run(["sed", "-n", f"/^{f}()/,/^}}/p", str(MACHINES)],
                              capture_output=True, text=True).stdout
        assert body.strip(), f"could not lift {f}"
        out.append(body)
    return "\n".join(out)


STUB = '_unpinned_host_key_opts() { printf "%s" "-o StrictHostKeyChecking=no"; }\n'


class TestTheBenchChannelIsAlwaysRoot(unittest.TestCase):
    def _opts(self, role):
        return bash(lift("i_ssh_opts") + STUB + f'NODE_ROLE={role}\ni_ssh_opts')

    def test_a_bench_device_is_root(self):
        cp = self._opts("bench-device")
        self.assertIn("-l root", cp.stdout)

    def test_a_workstation_is_root_too(self):
        """rpi5's host mode is a workstation; the system it boots for a
        measurement is not."""
        cp = self._opts("workstation")
        self.assertIn("-l root", cp.stdout, "the bench system would be reached as the driving user")

    def test_an_unset_role_is_root_too(self):
        cp = self._opts("")
        self.assertIn("-l root", cp.stdout)

    def test_it_never_pins_the_host_key(self):
        """A written system boots with a fresh host key, which reads as a
        man-in-the-middle against a shared known_hosts."""
        for role in ("bench-device", "workstation"):
            with self.subTest(role=role):
                self.assertIn("StrictHostKeyChecking", self._opts(role).stdout)


class TestTheHostChannelStillAsksTheRole(unittest.TestCase):
    def _opts(self, role):
        return bash(lift("m_ssh_opts") + STUB + f'NODE_ROLE={role}\nm_ssh_opts')

    def test_a_bench_device_in_host_mode_is_root(self):
        self.assertIn("-l root", self._opts("bench-device").stdout)

    def test_a_workstation_in_host_mode_is_the_driving_user(self):
        """wk takes no passwordless root on a workstation beyond its named
        helpers, so this must not ask for one."""
        self.assertEqual("", self._opts("workstation").stdout.strip())


class TestBothCallersUseTheBenchChannel(unittest.TestCase):
    """One implementation per behaviour: the shell wrapper and the python
    driver's spelled-out command line must agree."""

    def test_i_ssh_uses_it(self):
        body = re.search(r"(?ms)^i_ssh\(\) \{.*?^\}", MACHINES.read_text()).group(0)
        self.assertIn("i_ssh_opts", body)
        self.assertNotIn("m_ssh_opts", body, "the bench channel asks the host channel's question")

    def test_the_python_driver_uses_it(self):
        body = re.search(r"(?ms)^pi_ssh_cmd\(\) \{.*?^\}", PI.read_text()).group(0)
        self.assertIn("i_ssh_opts", body)
        self.assertNotIn("m_ssh_opts", body)

    def test_i_ssh_does_not_add_the_host_key_options_twice(self):
        body = re.search(r"(?ms)^i_ssh\(\) \{.*?^\}", MACHINES.read_text()).group(0)
        self.assertNotIn("_unpinned_host_key_opts", body,
                         "i_ssh_opts already carries them")


class TestPrivilegeFollowsTheChannel(unittest.TestCase):
    """r_sudo and boot_priv ask which channel answered, not what the machine
    is in host mode. A bench system is a wk image driven as root and carries
    only the card helper; asking NODE_ROLE sent `wk boot --back` at it looking
    for wk-boot-priv, which answered "command not found" with the board stuck
    in bench mode (rpi5, 2026-09-04)."""

    def _root(self, channel, role):
        cp = bash(lift("r_is_root")
                  + "MODE_CHANNEL=%s\nNODE_ROLE=%s\n" % (channel, role)
                  + "if r_is_root; then echo ROOT; else echo USER; fi")
        return cp.stdout.strip()

    def test_a_bench_system_is_root_whatever_the_host_role(self):
        for role in ("bench-device", "workstation", ""):
            with self.subTest(role=role):
                self.assertEqual("ROOT", self._root("bench", role))

    def test_a_bench_device_in_host_mode_is_root(self):
        """Its host mode is the rescue, which is also driven as root."""
        self.assertEqual("ROOT", self._root("host", "bench-device"))

    def test_a_workstation_in_host_mode_is_a_person(self):
        """wk takes no passwordless sudo there beyond its named helpers."""
        self.assertEqual("USER", self._root("host", "workstation"))

    def test_the_helpers_ask_the_predicate(self):
        text = MACHINES.read_text()
        for fn in ("r_sudo", "boot_priv", "boot_priv_require"):
            with self.subTest(fn=fn):
                body = re.search(r"(?ms)^" + fn + r"\(\).*?^\}", text).group(0)
                self.assertIn("r_is_root", body,
                              fn + " decides privilege from something other than the channel")
                self.assertNotIn("NODE_ROLE", body,
                                 fn + " still reads the host role directly")


if __name__ == "__main__":
    unittest.main()
