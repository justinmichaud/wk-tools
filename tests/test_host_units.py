"""The systemd --user units a machine that runs workspaces carries, and the one installer that puts them there
and starts them (host/units.sh, host/units/*.service)."""
import os
import shlex
import shutil
import subprocess
import time
import unittest

from tests.support import REPO, WkTest, bash

# Each unit and the file of this tree it runs; a system binary names none.
UNITS = {"wk-proxy.service": "container/proxy/wk-proxy.py", "wk-ssh-agent.service": "",
         "wk-github-inject.service": "container/proxy/github-inject.py", "wk-broker.service": "container/broker/wk-broker.py"}


class TestTheProgramAUnitRuns(unittest.TestCase):
    def test_a_service_names_the_file_of_this_tree_it_runs(self):
        for name, rel in UNITS.items():
            with self.subTest(unit=name):
                cp = bash(f'. "$WK_ROOT/host/units.sh"; unit_program {name}')
                self.assertEqual((0, rel), (cp.returncode, cp.stdout.strip()), cp.stderr)
                self.assertTrue(not rel or (REPO / rel).is_file(), rel)


class TestRenderingSubstitutesBothEnds(unittest.TestCase):
    def _render(self, name, root, store):
        cp = bash(f'. "$WK_ROOT/host/units.sh"; unit_render {name} {root} {store}')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout

    def test_the_tree_and_the_store_reach_the_unit(self):
        for name, root, store, lines in (
                ("wk-proxy.service", "/opt/wk-tools", "/var/lib/wk",
                 ("ExecStart=/usr/bin/python3 /opt/wk-tools/container/proxy/wk-proxy.py",
                  "Environment=WK_STORE=/var/lib/wk", "RequiresMountsFor=/opt/wk-tools")),
                ("wk-github-inject.service", "/home/x/wk-tools", "/home/x/.local/share/wk",
                 ("ExecStart=/usr/bin/python3 /home/x/wk-tools/container/proxy/github-inject.py",
                  "ReadWritePaths=/home/x/.local/share/wk"))):
            out = self._render(name, root, store)
            for line in lines:
                with self.subTest(unit=name, line=line):
                    self.assertIn(line, out)

    def test_no_placeholder_survives_any_render(self):
        for name in UNITS:
            with self.subTest(unit=name):
                self.assertNotIn("@WK_", self._render(name, "/r", "/s"))

    def test_percent_t_is_left_for_systemd(self):
        out = self._render("wk-ssh-agent.service", "/r", "/s")
        self.assertIn("%t/wk/ssh-agent.sock", out)

    def test_a_unit_with_no_body_is_refused_by_name(self):
        for call in ("unit_render wk-nonesuch.service /r /s",
                     "unit_program wk-nonesuch.service"):
            with self.subTest(call=call):
                cp = bash(f'. "$WK_ROOT/host/units.sh"; {call}')
                self.assertNotEqual(cp.returncode, 0, cp.stdout)
                self.assertIn("wk-nonesuch.service", cp.stderr)


class TestTheInstallerConverges(WkTest):

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

    def test_a_first_run_writes_it_alone_and_reloads(self):
        cp, unit, log = self._install()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("installed wk-proxy.service", cp.stdout + cp.stderr)
        self.assertIn("ExecStart=/usr/bin/python3 /opt/wk-tools/container/proxy/wk-proxy.py",
                      unit.read_text())
        self.assertEqual(0o644, unit.stat().st_mode & 0o777)
        self.assertIn("--user daemon-reload", log.read_text())
        self.assertEqual([unit.name], [p.name for p in unit.parent.iterdir()])

    def test_a_second_run_changes_nothing_and_does_not_reload(self):
        self._install()
        cp, unit, log = self._install()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("installed", cp.stdout + cp.stderr)
        self.assertEqual(1, log.read_text().count("daemon-reload"))

    def test_an_edited_unit_is_replaced_and_reloaded(self):
        _cp, unit, _log = self._install()
        unit.write_text("[Service]\nExecStart=/bin/false\n")
        cp, unit, log = self._install()
        self.assertIn("installed wk-proxy.service", cp.stdout + cp.stderr)
        self.assertNotIn("/bin/false", unit.read_text())
        self.assertEqual(2, log.read_text().count("daemon-reload"))


FAKE_SYSTEMCTL = """#!/bin/sh
echo "$*" >> "$WK_FAKE_LOG"
case "$*" in
    *"is-active"*)    exit "${WK_FAKE_ACTIVE:-1}" ;;
    *"enable --now"*) exit "${WK_FAKE_START:-0}" ;;
    *" restart "*)    exit "${WK_FAKE_RESTART:-0}" ;;
esac
exit 0
"""


class TestTheStartVerdictComesFromSystemd(WkTest):

    UNIT = "wk-proxy.service"
    STAMP = ".wk-proxy.program"

    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.store = self.tmp / "store"
        self.store.mkdir()
        self.log = self.tmp / "systemctl.log"
        self.binp = self.tmp / "bin"
        self.binp.mkdir()
        (self.binp / "systemctl").write_text(FAKE_SYSTEMCTL)
        (self.binp / "systemctl").chmod(0o755)

    def _start(self, active=False, start_ok=True, restart_ok=True, dry=False):
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.binp}:{os.environ['PATH']}",
            "WK_FAKE_LOG": str(self.log),
            "WK_FAKE_ACTIVE": "0" if active else "1",
            "WK_FAKE_START": "0" if start_ok else "1",
            "WK_FAKE_RESTART": "0" if restart_ok else "1",
            "WK_DEBUG": "1",          # so `unchanged` is visible too
            "WK_DRY_RUN": "1" if dry else "",
        }
        cp = bash(
            f'. "$WK_ROOT/host/units.sh"; unit_start {self.UNIT} /opt/wk-tools '
            f'{self.store} "workspaces will have no egress" "OVER-THERE " sh -c',
            env=env)
        return cp, cp.stdout + cp.stderr

    def _real_hash(self):
        cp = bash('cksum < "$WK_ROOT/container/proxy/wk-proxy.py" | awk "{print \\$1}"')
        return cp.stdout.strip()

    def _stamp(self):
        p = self.store / self.STAMP
        return p.read_text().strip() if p.exists() else None

    def test_a_service_that_reaches_readiness_is_reported_started(self):
        cp, out = self._start(active=False, start_ok=True)
        self.assertEqual(0, cp.returncode, out)
        self.assertIn(f"started {self.UNIT}", out)
        self.assertIn("--user enable --now " + self.UNIT, self.log.read_text())
        self.assertEqual(self._real_hash(), self._stamp())

    def test_a_service_that_does_not_reach_readiness_is_not_reported_started(self):
        cp, out = self._start(active=False, start_ok=False)
        self.assertNotIn("started", out)
        self.assertIn("did not reach readiness", out)
        self.assertIn(self.UNIT, out)
        self.assertIn("workspaces will have no egress", out)
        self.assertIn(f"OVER-THERE journalctl --user -u {self.UNIT} -e", out)
        self.assertIsNone(self._stamp())

    def test_a_running_service_on_an_unchanged_program_is_left_alone(self):
        (self.store / self.STAMP).write_text(self._real_hash() + "\n")
        cp, out = self._start(active=True)
        self.assertEqual(0, cp.returncode, out)
        self.assertIn(f"{self.UNIT} ready", out)
        self.assertNotIn("restarted", out)
        self.assertNotIn("--user restart", self.log.read_text())

    def test_a_running_service_on_a_changed_program_is_restarted(self):
        (self.store / self.STAMP).write_text("0 not-the-current-program\n")
        cp, out = self._start(active=True)
        self.assertEqual(0, cp.returncode, out)
        self.assertIn(f"restarted {self.UNIT} (program changed)", out)
        self.assertIn("--user restart " + self.UNIT, self.log.read_text())
        self.assertEqual(self._real_hash(), self._stamp())

    def test_a_restart_that_does_not_reach_readiness_is_reported_and_not_stamped(self):
        (self.store / self.STAMP).write_text("0 not-the-current-program\n")
        cp, out = self._start(active=True, restart_ok=False)
        self.assertIn("did not reach readiness", out)
        self.assertNotIn("restarted", out)
        self.assertEqual("0 not-the-current-program", self._stamp())

    def test_a_start_limited_unit_is_cleared_before_it_is_started(self):
        self._start(active=False)
        log = self.log.read_text().splitlines()
        reset = [i for i, l in enumerate(log) if "reset-failed" in l]
        start = [i for i, l in enumerate(log) if "enable --now" in l]
        self.assertTrue(reset and start, log)
        self.assertLess(reset[0], start[0])

    def test_a_dry_run_says_what_it_would_do_and_drives_nothing(self):
        cp, out = self._start(active=False, dry=True)
        self.assertEqual(0, cp.returncode, out)
        self.assertIn(f"would install {self.UNIT}", out)
        self.assertIn(f"would start {self.UNIT}", out)
        log = self.log.read_text()
        for word in ("daemon-reload", "enable --now", "reset-failed", "restart"):
            self.assertNotIn(word, log, log)
        self.assertFalse((self.home / ".config" / "systemd").exists(), out)
        self.assertIsNone(self._stamp())

    def test_a_dry_run_over_a_running_service_reports_only_what_changed(self):
        (self.store / self.STAMP).write_text(self._real_hash() + "\n")
        cp, out = self._start(active=True, dry=True)
        self.assertIn(f"{self.UNIT} ready", out)
        self.assertNotIn("would start", out)
        (self.store / self.STAMP).write_text("0 not-the-current-program\n")
        cp, out = self._start(active=True, dry=True)
        self.assertIn(f"would restart {self.UNIT}", out)
        self.assertEqual("0 not-the-current-program", self._stamp())

    def test_a_service_with_no_program_of_ours_stamps_nothing(self):
        self.UNIT, self.STAMP = "wk-ssh-agent.service", ".wk-ssh-agent.program"
        cp, out = self._start(active=False)
        self.assertIn("started wk-ssh-agent.service", out)
        self.assertEqual([], list(self.store.iterdir()))


def _has_user_systemd():
    if not shutil.which("systemd-run"):
        return False
    return subprocess.run(["systemctl", "--user", "is-system-running"],
                          capture_output=True).returncode in (0, 1)


@unittest.skipUnless(_has_user_systemd(), "no systemd --user bus here")
class TestAServiceRunningOlderCodeThanTheTree(WkTest):
    """systemd goes on running what it exec'd after a tools sync replaces the program under it."""

    UNIT = "wk-test-unit-stale.service"

    def setUp(self):
        super().setUp()
        self.root = self.tmp / "tree"
        (self.root / "host" / "units").mkdir(parents=True)
        self.prog = self.root / "sleeper.sh"
        self.prog.write_text("exec sleep 300\n")
        # unit_program picks the field after the interpreter; another shape would read `current` throughout.
        (self.root / "host" / "units" / self.UNIT).write_text(
            "[Service]\nExecStart=/bin/bash @WK_ROOT@/sleeper.sh\n")
        self.assertEqual("sleeper.sh", self.program())
        subprocess.run(["systemctl", "--user", "reset-failed", self.UNIT],
                       capture_output=True)
        cp = subprocess.run(["systemd-run", "--user", "--unit", self.UNIT,
                             "/bin/bash", str(self.prog)],
                            capture_output=True, text=True)
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.addCleanup(subprocess.run, ["systemctl", "--user", "stop", self.UNIT],
                        capture_output=True)

    def program(self):
        cp = bash('. "$WK_ROOT/host/units.sh"\nWK_ROOT=%s unit_program %s'
                  % (shlex.quote(str(self.root)), self.UNIT))
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        return cp.stdout.strip()

    def stale(self):
        cp = bash('. "$WK_ROOT/host/units.sh"\n'
                  'WK_ROOT=%s unit_stale %s && echo stale || echo current'
                  % (shlex.quote(str(self.root)), self.UNIT))
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        return cp.stdout.strip().splitlines()[-1]

    def test_the_program_it_started_with_reads_current(self):
        self.assertEqual("current", self.stale())

    def test_a_program_written_since_it_started_reads_stale(self):
        time.sleep(1.1)   # one second of mtime resolution on the coarsest filesystem
        self.prog.write_text("#!/bin/bash\nexec sleep 301\n")
        self.assertEqual("stale", self.stale())

    def test_a_service_that_is_not_running_is_not_called_stale(self):
        subprocess.run(["systemctl", "--user", "stop", self.UNIT], capture_output=True)
        self.prog.write_text("#!/bin/bash\nexec sleep 301\n")
        self.assertEqual("current", self.stale())


if __name__ == "__main__":
    unittest.main()
