"""The bench system is reached as root whatever the machine is in host mode.

Two channels, two questions (lib/wk/boot/driver.py's Channel). `m_ssh` talks to the machine in *host* mode, where
role is the right question: a bench-device's host mode is its rescue, driven as root; a workstation's is a
person. The bench channel talks to the *bench system*, which is a wk image either way: the driving key is in root's
authorized_keys and it boots with a fresh host key every time it is written.

Run: python3 tests/run.py -k test_bench_channel_ssh
"""
import shlex
import sys
import unittest

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk.boot.driver import Channel  # noqa: E402
from wk.machine import Fake  # noqa: E402


def channel(role, name="bench", env=None, via=None):
    return Channel(REPO, {"name": "b", "ssh": "b", "role": role}, name, env=env or {}, via=via or Fake())


class TestTheBenchChannelIsAlwaysRoot(unittest.TestCase):
    """lib/wk/boot/driver.py's Channel.machine("i_ssh"): the bench system as run-benchmark's board driver and every
    leg reach it."""

    def _machine(self, role):
        return channel(role, env={"WK_IMAGE_HOST": "192.0.2.9"}).machine("i_ssh")

    def test_it_is_root_whatever_the_host_role(self):
        """rpi5's host mode is a workstation; the system it boots for a measurement is not."""
        for role in ("bench-device", "workstation", ""):
            with self.subTest(role=role):
                m = self._machine(role)
                self.assertEqual((m.opts[:2], m.dest), (["-l", "root"], "192.0.2.9"))

    def test_it_never_pins_the_host_key(self):
        """A written system boots with a fresh host key, which reads as a man-in-the-middle against a shared known_hosts."""
        for role in ("bench-device", "workstation"):
            with self.subTest(role=role):
                self.assertIn("StrictHostKeyChecking=no", " ".join(self._machine(role).opts))


class TestTheHostChannelStillAsksTheRole(unittest.TestCase):
    def test_a_bench_device_in_host_mode_is_root(self):
        self.assertEqual(channel("bench-device", "host").opts("m_ssh")[:2], ["-l", "root"])

    def test_a_workstation_in_host_mode_is_the_driving_user(self):
        """wk takes no passwordless root on a workstation beyond its named helpers, so this must not ask for one."""
        self.assertEqual(channel("workstation", "host").opts("m_ssh"), [])


class TestPrivilegeFollowsTheChannel(unittest.TestCase):
    """lib/wk/boot/driver.py's Channel asks which channel answered, not what the machine is in host mode: a bench
    system is a wk image driven as root and carries only the card helper."""

    def _argv(self, name, role, fn="card_priv", *args):
        via = Fake()
        via.answer(("ssh",))
        channel(role, name, via=via).call(fn, *args)
        return [shlex.split(e[1][-1])[-1] for e in via.effects if e[1][0] == "ssh"]

    def _root(self, name, role):
        return "USER" if self._argv(name, role, "card_priv", "status")[-1].startswith("sudo -n ") else "ROOT"

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

    def test_the_boot_helper_follows_the_channel_too(self):
        """Root runs the firmware call itself; a person asks the helper, and no helper is looked for on a bench system."""
        self.assertIn("vcmailbox", self._argv("bench", "workstation", "boot_priv", "order", "0xf64")[-1])
        self.assertIn("sudo -n /usr/local/libexec/wk-boot-priv order 0xf64",
                      self._argv("host", "workstation", "boot_priv", "order", "0xf64"))
        self.assertEqual([], self._argv("bench", "workstation", "boot_priv_require"))


if __name__ == "__main__":
    unittest.main()
