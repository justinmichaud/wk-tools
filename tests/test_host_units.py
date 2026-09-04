"""The systemd --user units a machine that runs workspaces carries, and the one
installer that puts them there and starts them (host/units.sh,
host/units/*.service).

Two machines install them and they differ in two strings: where the checkout
is (a virtiofs mount at /opt/wk-tools inside the podman machine on macOS, this
checkout itself on a Linux workstation) and where the store is. So there is one
body per service, with those two spelled as placeholders, and one installer
taking the caller's own way of running a command on the machine the unit is for
-- `sh -c` locally, the ssh wrapper for the podman machine.

The verdict that installer answers with is systemd's: every body is Type=notify
or Type=forking, so a start job finishes when the service can be *used*, and a
service that execs and dies a fraction of a second later fails the start job
instead of answering `is-active` yes at t=0.

Run: python3 -m unittest tests.test_host_units -v
"""
import os
import unittest
from pathlib import Path

from tests.support import REPO, WkTest, bash, func_body

UNIT_DIR = REPO / "host" / "units"
UNITS = ("wk-proxy.service", "wk-ssh-agent.service", "wk-github-inject.service",
         "wk-broker.service")

# Each stage that installs units, and the ones it installs. The podman machine
# runs no broker: on macOS the broker is a LaunchAgent on the host itself
# (host/macos/broker.sh), because the boards it drives are reachable from there.
STAGES = {
    "host/macos/vmtools.sh": ("wk-proxy.service", "wk-ssh-agent.service",
                              "wk-github-inject.service"),
    "host/linux/sdk.sh": ("wk-proxy.service", "wk-ssh-agent.service",
                          "wk-github-inject.service"),
    "host/linux/broker.sh": ("wk-broker.service",),
}

# The services whose program is in this tree and can therefore say READY=1
# itself; ssh-agent is the system's own binary and cannot.
NOTIFYING = ("wk-proxy.service", "wk-github-inject.service", "wk-broker.service")


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

    def test_no_stage_writes_a_unit_body_of_its_own(self):
        for rel in STAGES:
            text = (REPO / rel).read_text()
            with self.subTest(stage=rel):
                self.assertNotIn("[Install]", text,
                                 f"{rel} still writes a unit body inline")
                self.assertNotIn("_install_unit", text,
                                 f"{rel} still has an installer of its own")


class TestEveryStageGoesThroughTheOneInstaller(unittest.TestCase):
    def test_each_stage_starts_its_units_through_the_shared_installer(self):
        for rel, units in STAGES.items():
            text = (REPO / rel).read_text()
            with self.subTest(stage=rel):
                self.assertIn('. "$WK_ROOT/host/units.sh"', text)
                for name in units:
                    self.assertIn(f"unit_start {name} ", text,
                                  f"{rel} does not start {name}")

    def test_the_stages_differ_only_in_the_root_the_store_and_the_transport(self):
        """The whole point of the split: everything else about a unit -- when
        it is restarted, what its failure says, what "ready" means -- is the
        same on both machines and lives in host/units.sh."""
        calls = {}
        for rel in STAGES:
            calls[rel] = [l.split() for l in (REPO / rel).read_text().splitlines()
                          if l.startswith("unit_start ")]
        mac, linux = calls["host/macos/vmtools.sh"], calls["host/linux/sdk.sh"]
        self.assertEqual([c[1] for c in mac], [c[1] for c in linux])
        self.assertEqual(['"$_unit_root"'] * 3, [c[2] for c in mac])
        self.assertEqual(['"$WK_ROOT"'] * 3, [c[2] for c in linux])
        self.assertEqual(['"$_unit_store"'] * 3, [c[3] for c in mac])
        self.assertEqual(['"$WK_STORE"'] * 3, [c[3] for c in linux])

    def test_no_stage_runs_systemctl_of_its_own(self):
        """The defect this replaced: three stages each asking `is-active`
        straight after `enable --now`, which a service that is about to die
        answers yes to."""
        for rel in STAGES:
            with self.subTest(stage=rel):
                self.assertNotIn("systemctl", (REPO / rel).read_text(),
                                 f"{rel} drives systemd itself instead of unit_start")


class TestReadinessIsWhatSystemdIsAskedFor(unittest.TestCase):
    """Structural facts that make the fix real rather than a settle-and-look."""

    def test_no_unit_is_type_simple(self):
        for name in UNITS:
            body = (UNIT_DIR / name).read_text()
            types = [l for l in body.splitlines() if l.startswith("Type=")]
            with self.subTest(unit=name):
                self.assertEqual(1, len(types), f"{name}: {types}")
                self.assertIn(types[0], ("Type=notify", "Type=forking"),
                              f"{name} is {types[0]}, which is active at t=0")

    def test_the_services_this_tree_writes_are_type_notify(self):
        for name in NOTIFYING:
            body = (UNIT_DIR / name).read_text()
            with self.subTest(unit=name):
                self.assertIn("Type=notify", body)
                self.assertIn("NotifyAccess=all", body)

    def test_ssh_agent_forks_so_the_start_job_waits_for_the_bound_socket(self):
        """ssh-agent binds and listens before it forks, and says nothing over
        sd_notify -- so the parent exiting is the readiness signal, and -D
        (stay in the foreground) would put readiness back at the exec."""
        body = (UNIT_DIR / "wk-ssh-agent.service").read_text()
        self.assertIn("Type=forking", body)
        exec_start = [l for l in body.splitlines() if l.startswith("ExecStart=")][0]
        self.assertNotIn(" -D ", exec_start)
        self.assertIn("/usr/bin/ssh-agent -a %t/wk/ssh-agent.sock", exec_start)

    def test_the_installer_asks_is_active_before_it_starts_anything(self):
        """Only to choose a word between "started" and "already ready". The
        verdict is the exit status of the start itself, so nothing may look
        again afterwards."""
        body = func_body((REPO / "host" / "units.sh").read_text(), "unit_start")
        ask, start = "systemctl --user is-active", "systemctl --user enable --now"
        self.assertEqual(1, body.count(ask))
        self.assertEqual(1, body.count(start))
        self.assertLess(body.index(ask), body.index(start))

    def test_one_sd_notify_in_the_tree(self):
        defs = [p for p in REPO.rglob("*.py")
                if "__pycache__" not in p.parts
                and p.name != Path(__file__).name
                and "def sd_notify(" in p.read_text(errors="replace")]
        self.assertEqual([REPO / "lib" / "wknotify.py"], defs)

    def test_every_notifying_service_imports_it_and_says_ready(self):
        for name in NOTIFYING:
            rel = unit_program(name)
            text = (REPO / rel).read_text()
            with self.subTest(program=rel):
                # assertTrue, not assertIn: the haystack is a whole program.
                self.assertTrue("from wknotify import sd_notify" in text,
                                f"{rel} does not import the one sd_notify")
                self.assertTrue('sd_notify("READY=1")' in text,
                                f"{rel} never tells systemd it is ready")


def unit_program(name):
    """The same derivation host/units.sh makes, asked of the shell that owns
    it rather than repeated here."""
    cp = bash(f'. "$WK_ROOT/host/units.sh"; unit_program {name}')
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return cp.stdout.strip()


class TestTheProgramAUnitRuns(unittest.TestCase):
    def test_a_service_that_runs_this_tree_names_its_file(self):
        self.assertEqual("container/proxy/wk-proxy.py", unit_program("wk-proxy.service"))
        self.assertEqual("container/proxy/github-inject.py",
                         unit_program("wk-github-inject.service"))
        self.assertEqual("container/broker/wk-broker.py", unit_program("wk-broker.service"))

    def test_a_service_that_runs_a_system_binary_names_nothing(self):
        self.assertEqual("", unit_program("wk-ssh-agent.service"))

    def test_every_named_file_exists(self):
        for name in UNITS:
            rel = unit_program(name)
            if rel:
                with self.subTest(unit=name):
                    self.assertTrue((REPO / rel).is_file(), rel)


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
        """By both readers of a body, and by name -- sed and awk would fail on
        the path instead, naming neither the unit nor where a body belongs."""
        for call in ("unit_render wk-nonesuch.service /r /s",
                     "unit_program wk-nonesuch.service"):
            with self.subTest(call=call):
                cp = bash(f'. "$WK_ROOT/host/units.sh"; {call}')
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


# A systemctl that answers as told. Every branch of unit_start turns on what
# systemd says, so the fake is what systemd says and nothing else.
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
    """`unit_start` against a scripted systemctl and a scratch store: one test
    per state a machine can be in when ./setup reaches it."""

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

    def _start(self, active=False, start_ok=True, restart_ok=True):
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.binp}:{os.environ['PATH']}",
            "WK_FAKE_LOG": str(self.log),
            "WK_FAKE_ACTIVE": "0" if active else "1",
            "WK_FAKE_START": "0" if start_ok else "1",
            "WK_FAKE_RESTART": "0" if restart_ok else "1",
            "WK_DEBUG": "1",          # so `unchanged` is visible too
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
        """The defect: `enable --now` returning non-zero is systemd saying the
        start job failed, and that is the whole verdict -- nothing looks
        again."""
        cp, out = self._start(active=False, start_ok=False)
        self.assertNotIn("started", out)
        self.assertIn("did not reach readiness", out)
        self.assertIn(self.UNIT, out)
        self.assertIn("workspaces will have no egress", out)
        self.assertIn(f"OVER-THERE journalctl --user -u {self.UNIT} -e", out)
        # Nothing is recorded about a program that never ran.
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
        """A unit that has hit its start limit refuses to start at all until
        the counter is cleared, so a ./setup after a bad one could not fix the
        thing the bad one broke."""
        self._start(active=False)
        log = self.log.read_text().splitlines()
        reset = [i for i, l in enumerate(log) if "reset-failed" in l]
        start = [i for i, l in enumerate(log) if "enable --now" in l]
        self.assertTrue(reset and start, log)
        self.assertLess(reset[0], start[0])

    def test_a_service_with_no_program_of_ours_stamps_nothing(self):
        """ssh-agent is the system's own binary: there is no file in this tree
        an edit could make stale, so there is nothing to record."""
        self.UNIT, self.STAMP = "wk-ssh-agent.service", ".wk-ssh-agent.program"
        cp, out = self._start(active=False)
        self.assertIn("started wk-ssh-agent.service", out)
        self.assertEqual([], list(self.store.iterdir()))


if __name__ == "__main__":
    unittest.main()
