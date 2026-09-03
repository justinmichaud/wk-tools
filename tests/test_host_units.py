"""The three systemd --user units a machine that runs workspaces carries, and
the one installer that puts them there (host/units.sh, host/units/*.service).

Two machines install them and they differ in two strings: where the checkout
is (a virtiofs mount at /opt/wk-tools inside the podman machine on macOS, this
checkout itself on a Linux workstation) and where the store is. So there is one
body per service, with those two spelled as placeholders, and one installer
taking the caller's own way of running a command on the machine the unit is for
-- `sh -c` locally, the ssh wrapper for the podman machine.

Run: python3 -m unittest tests.test_host_units -v
"""
import os
import unittest
from pathlib import Path

from tests.support import REPO, WkTest, bash

UNIT_DIR = REPO / "host" / "units"
UNITS = ("wk-proxy.service", "wk-ssh-agent.service", "wk-github-inject.service")
HOSTS = ("host/macos/vmtools.sh", "host/linux/sdk.sh")


class TestOneBodyPerService(unittest.TestCase):
    def test_every_unit_has_a_body_and_there_are_no_others(self):
        self.assertEqual(sorted(UNITS), sorted(p.name for p in UNIT_DIR.glob("*")))

    def test_each_body_is_defined_exactly_once_in_the_tree(self):
        """A second copy of a unit is a second thing to keep true, and the two
        would drift silently: the machine that runs the stale one just behaves
        differently."""
        for name in UNITS:
            exec_start = [l for l in (UNIT_DIR / name).read_text().splitlines()
                          if l.startswith("ExecStart=")]
            self.assertEqual(1, len(exec_start), f"{name} has {len(exec_start)} ExecStart lines")
            # The ExecStart *line* itself, placeholders and all, is what must
            # be unique -- the program path alone is named by prose too.
            hits = [p for p in REPO.rglob("*")
                    if p.is_file() and ".git" not in p.parts
                    and "__pycache__" not in p.parts
                    and p.name != Path(__file__).name
                    and exec_start[0] in p.read_text(errors="replace")]
            self.assertEqual([UNIT_DIR / name], hits,
                             f"{name}'s ExecStart is written in more than one place: {hits}")

    def test_no_host_writes_a_unit_body_of_its_own(self):
        for rel in HOSTS:
            text = (REPO / rel).read_text()
            with self.subTest(host=rel):
                self.assertNotIn("[Install]", text,
                                 f"{rel} still writes a unit body inline")
                self.assertNotIn("_install_unit", text,
                                 f"{rel} still has an installer of its own")


class TestBothHostsInstallAllThree(unittest.TestCase):
    def test_each_host_installs_each_unit_through_the_shared_installer(self):
        for rel in HOSTS:
            text = (REPO / rel).read_text()
            with self.subTest(host=rel):
                self.assertIn('. "$WK_ROOT/host/units.sh"', text)
                for name in UNITS:
                    self.assertIn(f"unit_install {name} ", text,
                                  f"{rel} does not install {name}")

    def test_the_two_hosts_differ_only_in_the_root_the_store_and_the_transport(self):
        """The whole point of the split: everything else about a unit is the
        same on both machines."""
        calls = {}
        for rel in HOSTS:
            calls[rel] = sorted(l.split() for l in (REPO / rel).read_text().splitlines()
                                if l.startswith("unit_install "))
        mac, linux = calls["host/macos/vmtools.sh"], calls["host/linux/sdk.sh"]
        self.assertEqual([c[1] for c in mac], [c[1] for c in linux])
        self.assertEqual(['"$_unit_root"'] * 3, [c[2] for c in mac])
        self.assertEqual(['"$WK_ROOT"'] * 3, [c[2] for c in linux])
        self.assertEqual([["_rsh"]] * 3, [c[4:] for c in mac])
        self.assertEqual([["sh", "-c"]] * 3, [c[4:] for c in linux])


class TestRenderingSubstitutesBothEnds(unittest.TestCase):
    def _render(self, name, root, store):
        cp = bash(f'. "$WK_ROOT/host/units.sh"; unit_render {name} {root} {store}')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout

    def test_the_podman_machines_spelling(self):
        out = self._render("wk-proxy.service", "/opt/wk-tools", "/var/lib/wk")
        self.assertIn("ExecStart=/usr/bin/python3 /opt/wk-tools/container/proxy/wk-proxy.py", out)
        self.assertIn("Environment=WK_STORE=/var/lib/wk", out)
        self.assertIn("RequiresMountsFor=/opt/wk-tools", out)

    def test_a_workstations_spelling(self):
        out = self._render("wk-github-inject.service", "/home/x/wk-tools", "/home/x/.local/share/wk")
        self.assertIn("ExecStart=/usr/bin/python3 /home/x/wk-tools/container/proxy/github-inject.py", out)
        self.assertIn("ReadWritePaths=/home/x/.local/share/wk", out)

    def test_no_placeholder_survives_any_render(self):
        for name in UNITS:
            with self.subTest(unit=name):
                self.assertNotIn("@WK_", self._render(name, "/r", "/s"))

    def test_percent_t_is_left_for_systemd(self):
        """%t is the *machine's* runtime directory, resolved by systemd there;
        rendering must not touch it."""
        out = self._render("wk-ssh-agent.service", "/r", "/s")
        self.assertIn("%t/wk/ssh-agent.sock", out)

    def test_a_unit_with_no_body_is_refused_by_name(self):
        cp = bash('. "$WK_ROOT/host/units.sh"; unit_render wk-nonesuch.service /r /s')
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("wk-nonesuch.service", cp.stderr)


class TestTheInstallerConverges(WkTest):
    """Driven with `sh -c` against a scratch HOME, so the writes, the compare
    and the daemon-reload are the real ones."""

    def _install(self, name="wk-proxy.service"):
        home = self.tmp / "home"
        home.mkdir(exist_ok=True)
        log = self.tmp / "systemctl.log"
        binp = self.tmp / "bin"
        binp.mkdir(exist_ok=True)
        (binp / "systemctl").write_text(f'#!/bin/sh\necho "$*" >> {log}\n')
        (binp / "systemctl").chmod(0o755)
        cp = bash(f'. "$WK_ROOT/host/units.sh"; unit_install {name} /opt/wk-tools /var/lib/wk sh -c',
                  env={"HOME": str(home), "PATH": f"{binp}:{os.environ['PATH']}"})
        return cp, home / ".config" / "systemd" / "user" / name, log

    def test_a_first_run_writes_it_and_reloads(self):
        cp, unit, log = self._install()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("installed wk-proxy.service", cp.stdout + cp.stderr)
        self.assertIn("ExecStart=/usr/bin/python3 /opt/wk-tools/container/proxy/wk-proxy.py",
                      unit.read_text())
        self.assertEqual(0o644, unit.stat().st_mode & 0o777)
        self.assertIn("--user daemon-reload", log.read_text())

    def test_a_second_run_changes_nothing_and_does_not_reload(self):
        """A re-run of ./setup must not daemon-reload under a service a build
        is depending on."""
        self._install()
        cp, unit, log = self._install()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("installed", cp.stdout + cp.stderr)
        self.assertEqual(1, log.read_text().count("daemon-reload"))

    def test_nothing_is_left_beside_the_live_unit(self):
        cp, unit, _ = self._install()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual([unit.name], [p.name for p in unit.parent.iterdir()])

    def test_an_edited_unit_is_replaced_and_reloaded(self):
        """The machine wins over the record everywhere else; here the tree
        wins, because the unit is generated from it."""
        _cp, unit, _log = self._install()
        unit.write_text("[Service]\nExecStart=/bin/false\n")
        cp, unit, log = self._install()
        self.assertIn("installed wk-proxy.service", cp.stdout + cp.stderr)
        self.assertNotIn("/bin/false", unit.read_text())
        self.assertEqual(2, log.read_text().count("daemon-reload"))


if __name__ == "__main__":
    unittest.main()
