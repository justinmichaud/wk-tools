"""The bench system is reached as root whatever the machine is in host mode."""
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

    def _machine(self, role):
        return channel(role, env={"WK_IMAGE_HOST": "192.0.2.9"}).machine("i_ssh")

    def test_it_is_root_whatever_the_host_role(self):
        for role in ("bench-device", "workstation", ""):
            with self.subTest(role=role):
                m = self._machine(role)
                self.assertEqual((m.opts[:2], m.dest), (["-l", "root"], "192.0.2.9"))

    def test_it_never_pins_the_host_key(self):
        for role in ("bench-device", "workstation"):
            with self.subTest(role=role):
                self.assertIn("StrictHostKeyChecking=no", " ".join(self._machine(role).opts))


class TestPrivilegeFollowsTheChannel(unittest.TestCase):

    def _argv(self, name, role, fn="card_priv", *args):
        via = Fake()
        via.answer(("ssh",))
        channel(role, name, via=via).call(fn, *args)
        return [shlex.split(e[1][-1])[-1] for e in via.effects if e[1][0] == "ssh"]

    def _root(self, name, role):
        return "USER" if self._argv(name, role, "card_priv", "status")[-1].startswith("sudo -n ") else "ROOT"

    def test_a_bench_system_is_root_and_host_mode_asks_the_role(self):
        for name, role, who, login in (("bench", "bench-device", "ROOT", None), ("bench", "workstation", "ROOT", None),
                                       ("bench", "", "ROOT", None), ("host", "bench-device", "ROOT", ["-l", "root"]),
                                       ("host", "workstation", "USER", [])):
            with self.subTest(name=name, role=role):
                self.assertEqual(who, self._root(name, role))
                if login is not None:
                    self.assertEqual(channel(role, name).opts("m_ssh")[:2], login)

    def test_the_boot_helper_follows_the_channel_too(self):
        self.assertIn("vcmailbox", self._argv("bench", "workstation", "boot_priv", "order", "0xf64")[-1])
        self.assertIn("sudo -n /usr/local/libexec/wk-boot-priv order 0xf64",
                      self._argv("host", "workstation", "boot_priv", "order", "0xf64"))
        self.assertEqual([], self._argv("bench", "workstation", "boot_priv_require"))


if __name__ == "__main__":
    unittest.main()
