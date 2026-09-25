"""Getting a machine ready is a command, not a paste (`wk machine setup <mac>`,
lib/wk/machine_cmd/mac.py's setup_mac, which the Mac A/B's preflight names when it
finds a Mac it cannot restart), and it runs the command on the machine the conf names.

A machine wk drives needs two things on it before `wk boot` can arm it or the
Mac lane can restart it: this tree, and the privileged helpers admin/install.sh
puts behind a NOPASSWD rule. Installing those is the one sudo this lane ever
asks for, so it needs the operator's terminal -- and everything else about the
step is derived, not typed: one declared tools path rather than a search of
candidate directories, one deploy (a commit, never a file copy), one way to
run a command on a machine, and a refusal that names the command instead of
the incantation.

Nothing here reaches a real machine: `ssh`, `rsync`, `hostname` and `sudo` are
stubs on PATH that record their argv, and the far side of a deploy is a
directory in a scratch tree.

Run: python3 -m unittest tests.test_machine_prepare -v
"""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from tests.support import REPO, WkTest, bash, scratch_dir, stub_path

sys.path.insert(0, str(REPO / "lib"))
from wk import act, machine_cmd  # noqa: E402
from wk.machine import Fake, Local  # noqa: E402


MACHINES = REPO / "boot" / "machines.sh"

LIB = '. "$WK_ROOT/lib/common.sh"; . "$WK_ROOT/boot/machines.sh"\n'


class TheToolsPathIsDeclared(WkTest):
    """tolken carries two clones of this repo -- `Development/wk-tools` at one
    commit and `Development/wk-tools-wip` at an older one -- so a search of
    candidate directories makes whichever it reaches first the tree that drives
    a measurement. Measured 2026-09-08."""

    def test_one_path_and_it_is_the_remote_tools_convention(self):
        from wk.boot import mac
        self.assertEqual("Development/wk-tools", mac.TOOLS)

    def test_the_driver_answers_where_the_tree_is_and_searches_for_nothing(self):
        """The machine that holds it is the one *managing* the target and not
        always the one being measured -- a guest's manager is the Mac running it."""
        from wk.boot.mac import MacVolume
        m = Fake("tolken")
        m.answer(["test", "-x", "Development/wk-tools/wk"], rc=0)
        d = MacVolume(str(REPO), {"NODE_NAME": "mbp", "NODE_SSH": "tolken"}, None)
        self.assertEqual("Development/wk-tools", d.manager_tools(m))
        self.assertEqual([e[1] for e in m.effects], [("test", "-x", "Development/wk-tools/wk")])

    def test_an_absent_tree_names_the_command_that_puts_one_there(self):
        import inspect
        from wk.bench import mac
        self.assertIn("wk machine setup", inspect.getsource(mac.MacAB.manager))


def git(cwd, *args):
    """git in a scratch tree, with an identity of its own: the machine running
    the suite need not have one configured."""
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True,
        timeout=60, check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


# The far side: `bash -c` on the one command string, and a copy (the bundle) landing at the path after the colon.
FAR_SSH = """#!/bin/sh
for a in "$@"; do last="$a"; done
printf '%%s\\n' "$last" >> %s
exec bash -c "$last"
"""
FAR_SCP = """#!/bin/sh
for a in "$@"; do src="$last"; last="$a"; done
cp "$src" "${last#*:}"
"""


class PreparingPushesACommitAndStopsAtTheSudo(WkTest):
    """A machine is given a *commit*, never a file copy (`tools.push`,
    lib/wk/tools.py -- the fleet's one deploy). What lands there is a checkout at
    this tree's HEAD, so nothing over there is content that exists only over
    there and a later `git pull` has nothing of anyone's to replace. An rsync
    of the working tree had that backwards: it pushed uncommitted work into a
    checkout the operator also pulls into, and the pull won silently (tolken,
    2026-09-08).

    The deploy needs nothing privileged, so it happens either way. Installing a
    NOPASSWD rule authenticates once, and nothing can bootstrap that from a
    session with no terminal -- so that is where it stops, having left the
    checkout in place, and it says which command finishes the job.

    Real `Ssh`/`Local` machines, over stubbed ssh/scp on PATH: setup_mac's own
    behaviour is under test, not a Fake standing in for the transport."""

    def _prepare(self, dirty=False):
        home = self.tmp / "home"
        src = self.tmp / "src"
        home.mkdir()
        src.mkdir()
        git(src, "init", "-q", ".")
        (src / "wk").write_text("#!/bin/sh\necho committed\n")
        git(src, "add", "-A")
        git(src, "commit", "-qm", "one")
        self.sha = git(src, "rev-parse", "HEAD").stdout.strip()
        if dirty:
            (src / "wk").write_text("#!/bin/sh\necho uncommitted\n")
        self.far = home / "Development" / "wk-tools"
        log = self.tmp / "ssh.argv"
        env = {"PATH": "", "HOME": str(home)}
        with stub_path({"ssh": FAR_SSH % log, "scp": FAR_SCP, "tailscale": "#!/bin/sh\necho '{}'\n"}) as path:
            env["PATH"] = "%s:%s" % (path, os.environ["PATH"])
            err = io.StringIO()
            with mock.patch.dict(os.environ, env), \
                 contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                try:
                    rc = machine_cmd.Machines(str(src), env=dict(os.environ), here=Local()) \
                        .setup_mac("mbp", {"KIND": "mac", "NODE_SSH": "tolken"})
                except act.Refused as e:
                    rc = e.status
        asked = log.read_text() if log.exists() else ""
        return rc, err.getvalue(), asked

    def test_it_stops_at_the_sudo_and_names_what_finishes_it(self):
        rc, out, _ = self._prepare()
        self.assertEqual(rc, 1, out)
        self.assertIn("./setup --stage quiesce", out, out)
        self.assertIn("sudoers.d", out, out)

    def test_the_commit_lands_anyway_as_a_checkout_at_this_head(self):
        """Everything that needs no password is done, so the session ends with
        the machine closer to ready than it started."""
        _, out, _ = self._prepare()
        self.assertIn("the tree is in place", out, out)
        self.assertTrue((self.far / ".git").is_dir(), f"{self.far} is no checkout")
        self.assertEqual(self.sha, git(self.far, "rev-parse", "HEAD").stdout.strip())
        self.assertEqual("#!/bin/sh\necho committed\n", (self.far / "wk").read_text())

    def test_an_uncommitted_tree_is_refused_by_name_and_nothing_is_pushed(self):
        """The decision this end makes: a machine takes a commit, so the
        working tree here is committed first. `wk.tools.committed` says so and
        names the two commands, the same refusal `wk sync --tools` makes."""
        rc, out, _ = self._prepare(dirty=True)
        self.assertEqual(rc, 1, out)
        self.assertIn("uncommitted changes", out, out)
        self.assertIn("git -C", out, out)
        self.assertFalse(self.far.exists(), f"{self.far} was written anyway")

    def test_it_installs_nothing_with_no_terminal_to_authenticate_on(self):
        _, _, asked = self._prepare()
        self.assertNotIn("setup --stage quiesce", asked, asked)

    def test_the_deploy_is_the_fleets_one_deploy_and_not_a_second_one(self):
        import inspect
        body = inspect.getsource(machine_cmd.Machines.setup_mac)
        self.assertIn("tools.push", body)
        self.assertNotIn("rsync", body)


class TheVerbMoved(WkTest):
    def test_boot_prepare_names_machine_setup(self):
        cp = bash('exec "$WK_ROOT/cmd/boot" mbp --prepare')
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("wk machine setup mbp", cp.stderr)

    def test_machine_setup_prepares_a_mac(self):
        """5.13: `wk machine setup mbp` pushes this tree and installs the privileged helpers."""
        cp = bash('WK_DRY_RUN=1 exec "$WK_ROOT/cmd/machine" setup mbp')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)


class TheHostOsGateAsksWhetherTheDriverCanReachIt(WkTest):
    """`mbp.conf` and `benchvm.conf` both say os=macos, but for different
    reasons: the mac-volume driver reads and acts over ssh, while mac-guest
    needs `tart` on the machine itself. So the gate is `b_probeable` -- what a
    driver declares about its own reach -- and not the OS, and the refusal says
    which of the two it is: a host of the wrong kind is told to run it over
    there, and the right kind of host missing what the driver needs is told
    that, since there is nowhere else to run it."""

    def _boot(self, machine, *args, env=None):
        # cmd/boot is exec'd past the dispatcher, which is what would turn --dry-run into WK_DRY_RUN.
        dry = "WK_DRY_RUN=1 " if "--dry-run" in args else ""
        args = [a for a in args if a != "--dry-run"]
        return bash('%sexec "$WK_ROOT/cmd/boot" %s %s' % (dry, machine, " ".join(args)), env=env)

    def test_a_driver_that_reaches_its_machine_answers_from_a_linux_host(self):
        for args in (("--status",), ("--dry-run",)):
            with self.subTest(args=args):
                cp = self._boot("mbp", *args)
                out = cp.stdout + cp.stderr
                self.assertNotIn("macOS host only", out, out)

    @unittest.skipUnless(sys.platform == "darwin",
                         "the host-kind-matches case needs a macOS host")
    def test_a_driver_that_cannot_reach_its_machine_still_refuses(self):
        """mac-guest's b_probeable needs tart on this machine (the class
        docstring), so on a machine that happens to have tart installed for
        its own wk-tools development, the refusal must not depend on that --
        tart is hidden here the way it is genuinely absent on a Linux host."""
        with scratch_dir() as tmp:
            no_tart = os.pathsep.join(
                p for p in os.environ.get("PATH", "").split(os.pathsep)
                if p and not (Path(p) / "tart").exists())
            cp = self._boot("benchvm", "--dry-run", env={"HOME": str(tmp), "PATH": no_tart})
        out = cp.stdout + cp.stderr
        self.assertIn("cannot reach benchvm from here", out, out)
        self.assertIn("what is missing is on this host", out, out)
        self.assertNotIn("run this over there", out,
                         "this is over there: a macOS host and a macOS-only machine")


class TheCommandRunsOnTheMachineTheConfNames(WkTest):
    """One way to run a command on a machine (`m_ssh`, boot/machines.sh), and
    the test for running it here rather than over ssh is standing on that
    machine: `hostname -s` against the destination the conf names, the same
    comparison lib/wk/bench/mac.py's MacAB makes before refusing to reboot the machine it
    is driven from.

    Nothing a conf declares can answer it -- whether a machine "drives itself"
    is a property of the caller -- and a driver that asked the *platform*
    instead said the same wrong thing: driven from another Mac, every read
    about tolken described the Mac it was typed on.

    `ssh` and `hostname` are stubs, so both branches run on any machine and
    what is checked is the command string each machine is issued -- which
    catches a tool that one of the two platforms does not ship without that
    platform in hand."""

    def _m_ssh(self, script, hostname="moose"):
        with scratch_dir() as tmp:
            with stub_path({
                "ssh":      '#!/bin/sh\necho "$@" >> %s/ssh.argv\n' % tmp,
                "hostname": '#!/bin/sh\necho %s\n' % hostname,
            }) as path:
                cp = bash(LIB + script,
                          env={"PATH": "%s:%s" % (path, os.environ["PATH"])})
            log = tmp / "ssh.argv"
            return cp.stdout + cp.stderr, (log.read_text() if log.exists() else "")

    def test_a_command_for_another_machine_goes_over_ssh(self):
        out, asked = self._m_ssh('NODE_SSH=othermach\nm_ssh "echo RAN-HERE"')
        self.assertNotIn("RAN-HERE", out, out)
        self.assertIn("othermach echo RAN-HERE", asked, asked)

    def test_a_command_for_this_machine_runs_here(self):
        """Case-insensitively: a machine's own spelling of its name need not
        be the one the conf reaches it by."""
        out, asked = self._m_ssh('NODE_SSH=moose\nm_ssh "echo RAN-HERE"',
                                 hostname="MOOSE")
        self.assertIn("RAN-HERE", out, out)
        self.assertEqual("", asked, asked)

    def test_the_mac_is_reached_over_ssh_from_a_machine_that_is_not_it(self):
        """mbp's conf is the case a declared flag got wrong: it said "this
        machine drives itself", so from anywhere else the command ran on the
        driving machine and answered about the wrong computer."""
        out, asked = self._m_ssh('machine_load mbp\nm_ssh "echo RAN-HERE"')
        self.assertNotIn("RAN-HERE", out, out)
        self.assertIn("tolken echo RAN-HERE", asked, asked)

    def test_the_mac_runs_it_here_when_this_is_the_mac(self):
        out, asked = self._m_ssh('machine_load mbp\nm_ssh "echo RAN-HERE"',
                                 hostname="tolken")
        self.assertIn("RAN-HERE", out, out)
        self.assertEqual("", asked, asked)

    def test_a_machine_with_no_ssh_destination_is_never_this_one(self):
        """A machine reached only through something else (benchvm, whose
        driver overrides m_ssh) names no destination, and a hostname that
        cannot be read is not a match for it."""
        out, asked = self._m_ssh('NODE_SSH=""\nm_ssh "echo RAN-HERE"',
                                 hostname="")
        self.assertNotIn("RAN-HERE", out, out)

    def test_the_reboot_it_issues_detaches_with_what_both_platforms_ship(self):
        """`setsid` is util-linux and macOS ships none -- measured on tolken
        (macOS 26.6.2): `command -v setsid` answers nothing, `command -v
        nohup` answers /usr/bin/nohup. A reboot that quietly does nothing
        exits 0, so the string is read here rather than trusted over there."""
        sys.path.insert(0, str(REPO / "lib"))
        from wk.boot.driver import root_priv
        for verb in ("reboot", "reboot-tryboot"):
            with self.subTest(verb=verb):
                asked = " ".join(root_priv(verb))
                self.assertIn("nohup ", asked, asked)
                self.assertNotIn("setsid", asked, asked)


if __name__ == "__main__":
    unittest.main()
