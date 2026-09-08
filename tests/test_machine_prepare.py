"""Getting a machine ready is a command, not a paste (boot/machines.sh,
cmd/boot --prepare).

A machine wk drives needs two things on it before `wk boot` can arm it or the
Mac lane can restart it: this tree, and the privileged helpers admin/install.sh
puts behind a NOPASSWD rule. Installing those is the one sudo this lane ever
asks for, so it needs the operator's terminal -- and everything else about the
step is derived, not typed: one declared tools path rather than a search of
candidate directories, and a refusal that names the command instead of the
incantation.

Nothing here reaches a real machine: `ssh`, `rsync` and `sudo` are stubs on
PATH that record their argv.

Run: python3 -m unittest tests.test_machine_prepare -v
"""
import os
import subprocess
import unittest

from tests.support import REPO, WkTest, bash, func_body, scratch_dir, stub_path

MACHINES = REPO / "boot" / "machines.sh"
BOOT = REPO / "cmd" / "boot"
MACAB = REPO / "bench" / "mac-ab.sh"

LIB = '. "$WK_ROOT/lib/common.sh"; . "$WK_ROOT/boot/machines.sh"\n'


class TheToolsPathIsDeclared(WkTest):
    """tolken carries two clones of this repo -- `Development/wk-tools` at one
    commit and `Development/wk-tools-wip` at an older one -- so a search of
    candidate directories makes whichever it reaches first the tree that drives
    a measurement. Measured 2026-09-08."""

    def test_one_path_and_it_is_the_remote_tools_convention(self):
        cp = bash(LIB + 'machine_tools_dir')
        self.assertEqual("Development/wk-tools", cp.stdout.strip())

    def test_the_lane_reads_that_path_and_searches_for_nothing(self):
        body = func_body(MACAB.read_text(), "host_tools")
        self.assertIn("machine_tools_dir", body)
        self.assertNotIn("~/wk-tools", body)
        self.assertNotIn("for d in", body)

    def test_an_absent_tree_names_the_command_that_puts_one_there(self):
        body = func_body(MACAB.read_text(), "host_tools")
        self.assertIn("--prepare", body)


class PreparingNeedsATerminalOnlyForTheSudo(WkTest):
    """The sync needs nothing privileged, so it happens either way. Installing a
    NOPASSWD rule authenticates once, and nothing can bootstrap that from a
    session with no terminal -- so that is where it stops, having left the tree
    in place, and it says which command finishes the job."""

    def _prepare(self):
        """Stubbed ssh and rsync: no test reaches a real machine (support.py's
        fleet-blind rule), and both are recorded so the order is checkable."""
        with scratch_dir() as tmp:
            with stub_path({
                "ssh":   '#!/bin/sh\necho "$@" >> %s/ssh.argv\n' % tmp,
                "rsync": '#!/bin/sh\necho "$@" >> %s/rsync.argv\n' % tmp,
            }) as path:
                cp = subprocess.run(
                    ["bash", "-c", LIB + 'NODE_NAME=mbp\n'
                     'machine_prepare tolken || echo "rc=$?"'],
                    env=dict(os.environ,
                             PATH="%s:%s" % (path, os.environ["PATH"]),
                             WK_ROOT=str(REPO)),
                    capture_output=True, text=True, stdin=subprocess.DEVNULL)
            argv = {n: (tmp / f"{n}.argv").read_text() if (tmp / f"{n}.argv").exists()
                    else "" for n in ("ssh", "rsync")}
            return cp, cp.stdout + cp.stderr, argv

    def test_it_stops_at_the_sudo_and_names_what_finishes_it(self):
        cp, out, _ = self._prepare()
        self.assertIn("rc=1", out, out)
        self.assertIn("wk boot mbp --prepare", out, out)
        self.assertIn("sudoers.d", out, out)

    def test_the_tree_is_synced_anyway(self):
        """Everything that needs no password is done, so the session ends with
        the machine closer to ready than it started."""
        _, out, argv = self._prepare()
        self.assertIn("Development/wk-tools", argv["rsync"], argv["rsync"])
        self.assertIn("tolken:", argv["rsync"], argv["rsync"])
        self.assertIn("the tree is in place", out, out)

    def test_it_installs_nothing_with_no_terminal_to_authenticate_on(self):
        _, _, argv = self._prepare()
        self.assertNotIn("setup --stage quiesce", argv["ssh"], argv["ssh"])


class TheVerbIsWiredIn(WkTest):
    def test_boot_has_a_prepare_action(self):
        text = BOOT.read_text()
        self.assertIn("--prepare) ACTION=prepare", text)
        self.assertIn("prepare) cmd_prepare", text)

    def test_its_help_block_names_it(self):
        """The leading block is what `wk boot -h` prints (explain_cmd)."""
        cp = bash('exec "$WK_ROOT/wk" boot -h')
        self.assertIn("--prepare", cp.stdout + cp.stderr)

    def test_a_machine_with_no_ssh_destination_has_nothing_to_prepare(self):
        body = func_body(BOOT.read_text(), "cmd_prepare")
        self.assertIn("NODE_SSH", body)
        self.assertIn("nothing to prepare", body)

    def test_the_dry_run_reaches_no_machine(self):
        with scratch_dir() as tmp:
            with stub_path({
                "ssh":   '#!/bin/sh\necho "$@" >> %s/ssh.argv\n' % tmp,
                "rsync": '#!/bin/sh\necho "$@" >> %s/rsync.argv\n' % tmp,
            }) as path:
                cp = bash('exec "$WK_ROOT/cmd/boot" mbp --prepare --dry-run',
                          env={"PATH": "%s:%s" % (path, os.environ["PATH"])})
            out = cp.stdout + cp.stderr
            self.assertIn("would sync", out, out)
            self.assertFalse((tmp / "rsync.argv").exists(), "a dry run synced")


class TheHostOsGateAsksWhetherTheDriverCanReachIt(WkTest):
    """`mbp.conf` and `benchvm.conf` both say os=macos, but for different
    reasons: the mac-volume driver reads and acts over ssh, while mac-guest
    needs `tart` on the machine itself. So the gate is `b_probeable` -- what a
    driver declares about its own reach -- and not the OS. Held to the OS alone,
    it said 'run this over there' about a machine it had just probed."""

    def _boot(self, machine, *args):
        return bash('exec "$WK_ROOT/cmd/boot" %s %s' % (machine, " ".join(args)))

    def test_a_driver_that_reaches_its_machine_answers_from_a_linux_host(self):
        for args in (("--status",), ("--dry-run",)):
            with self.subTest(args=args):
                cp = self._boot("mbp", *args)
                out = cp.stdout + cp.stderr
                self.assertNotIn("macOS host only", out, out)

    def test_a_driver_that_cannot_reach_its_machine_still_refuses(self):
        cp = self._boot("benchvm", "--dry-run")
        out = cp.stdout + cp.stderr
        self.assertIn("macOS host only", out, out)
        self.assertIn("cannot reach it from here", out, out)

    def test_the_gate_rests_on_the_drivers_own_declaration(self):
        text = BOOT.read_text()
        self.assertIn("if ! b_probeable; then", text)
        self.assertNotIn("_os_bound", text)


if __name__ == "__main__":
    unittest.main()
