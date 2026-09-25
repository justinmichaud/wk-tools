"""The bench system is reached as root whatever the machine is in host mode.

Two channels, two questions. `m_ssh` talks to the machine in *host* mode,
where NODE_ROLE is the right question: a bench-device's host mode is its
rescue, driven as root; a workstation's is a person. The bench channel
(lib/wk/boot/driver.py's Channel) talks to the *bench system*, which is a wk image either way -- the driving key is in
root's authorized_keys (disk_install_fleet) and it boots with a fresh host
key every time it is written.

Asking the role on the bench channel left every board whose host mode is a
workstation unreachable once it was in bench mode: `jmichaud@rpi5-bench`
answers "Permission denied (publickey,password)" where `root@rpi5-bench`
gives a shell, so `wk boot --keep`, a deploy and a board run could
not reach the system they exist to drive (rpi5, 2026-09-04).

Run: python3 -m unittest tests.test_bench_channel_ssh -v
"""
import subprocess
import sys
import unittest

from tests.support import REPO, bash

MACHINES = REPO / "boot" / "machines.sh"


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
    """lib/wk/boot/driver.py's Channel.machine("i_ssh"): the bench system as run-benchmark's board driver and every
    leg reach it."""

    def _machine(self, role):
        sys.path.insert(0, str(REPO / "lib"))
        from wk.boot.driver import Channel
        from wk.machine import Fake
        return Channel(REPO, {"NODE_NAME": "b", "NODE_SSH": "b", "NODE_ROLE": role}, "bench", env={"WK_IMAGE_HOST": "192.0.2.9"},
                       via=Fake()).machine("i_ssh")

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
    def _opts(self, role):
        return bash(lift("m_ssh_opts") + STUB + f'NODE_ROLE={role}\nm_ssh_opts')

    def test_a_bench_device_in_host_mode_is_root(self):
        self.assertIn("-l root", self._opts("bench-device").stdout)

    def test_a_workstation_in_host_mode_is_the_driving_user(self):
        """wk takes no passwordless root on a workstation beyond its named
        helpers, so this must not ask for one."""
        self.assertEqual("", self._opts("workstation").stdout.strip())


class TestPrivilegeFollowsTheChannel(unittest.TestCase):
    """lib/wk/boot/driver.py's Channel asks which channel answered, not what the machine is in host mode: a bench
    system is a wk image driven as root and carries only the card helper."""

    def _argv(self, channel, role, fn="card_priv", *args):
        sys.path.insert(0, str(REPO / "lib"))
        from wk.boot.driver import Channel
        from wk.machine import Fake
        via = Fake()
        via.answer(("ssh",))
        Channel(REPO, {"NODE_NAME": "b", "NODE_SSH": "b", "NODE_ROLE": role}, channel, env={}, via=via).call(fn, *args)
        return [e[1][-1] for e in via.effects if e[1][0] == "ssh"]

    def _root(self, channel, role):
        return "USER" if self._argv(channel, role, "card_priv", "status")[-1].startswith("sudo -n ") else "ROOT"

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
