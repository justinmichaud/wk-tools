"""Getting a machine ready is a command, not a paste (boot/machines.sh,
cmd/boot --prepare), and it runs the command on the machine the conf names.

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
import os
import pty
import subprocess
import sys
import unittest

from tests.support import REPO, WkTest, bash, func_body, scratch_dir, stub_path


def _lift_between(text, first, last):
    a = text.index(first)
    return text[a:text.index(last, a)]


def macab_func(name):
    return func_body(MACAB.read_text(), name)


def pty_bash(script, timeout=60):
    """bash with a terminal on stdin, for the one branch whose condition is
    having one. Output comes back merged, as it does from a terminal."""
    primary, secondary = pty.openpty()
    try:
        cp = subprocess.run(
            ["bash", "-c", script], cwd=str(REPO),
            env=dict(os.environ, WK_ROOT=str(REPO)), stdin=secondary,
            capture_output=True, text=True, timeout=timeout)
    finally:
        os.close(primary)
        os.close(secondary)
    return cp

MACHINES = REPO / "boot" / "machines.sh"
BOOT = REPO / "cmd" / "boot"
MACAB = REPO / "bench" / "mac-ab.sh"
DRIVER = REPO / "boot" / "mac-volume.sh"

LIB = '. "$WK_ROOT/lib/common.sh"; . "$WK_ROOT/boot/machines.sh"\n'


class TheToolsPathIsDeclared(WkTest):
    """tolken carries two clones of this repo -- `Development/wk-tools` at one
    commit and `Development/wk-tools-wip` at an older one -- so a search of
    candidate directories makes whichever it reaches first the tree that drives
    a measurement. Measured 2026-09-08."""

    def test_one_path_and_it_is_the_remote_tools_convention(self):
        cp = bash(LIB + 'machine_tools_dir')
        self.assertEqual("Development/wk-tools", cp.stdout.strip())

    def test_the_driver_reads_that_path_and_searches_for_nothing(self):
        """It is the driver that answers where the tree is, because the machine
        that holds it is the one *managing* the target and not always the one
        being measured -- a guest's manager is the Mac running it."""
        body = func_body(DRIVER.read_text(), "b_manage_tools")
        self.assertIn("machine_tools_dir", body)
        self.assertNotIn("~/wk-tools", body)
        self.assertNotIn("for d in", body)

    def test_the_lane_asks_the_driver_and_reads_no_path_of_its_own(self):
        body = func_body(MACAB.read_text(), "mgr_tools")
        self.assertIn("b_manage_tools", body)
        self.assertNotIn("machine_tools_dir", body)

    def test_an_absent_tree_names_the_command_that_puts_one_there(self):
        body = func_body(MACAB.read_text(), "mgr_tools")
        self.assertIn("--prepare", body)


def git(cwd, *args):
    """git in a scratch tree, with an identity of its own: the machine running
    the suite need not have one configured."""
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True,
        timeout=60, check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


# The far side: `bash -c` on the one command string, with stdin (the bundle)
# flowing through, which is the shape tools_push hands its caller's wrapper.
FAR_SSH = """#!/bin/sh
for a in "$@"; do last="$a"; done
printf '%%s\\n' "$last" >> %s
exec bash -c "$last"
"""


class PreparingPushesACommitAndStopsAtTheSudo(WkTest):
    """A machine is given a *commit*, never a file copy (`tools_push`,
    lib/tools.sh -- the fleet's one deploy). What lands there is a checkout at
    this tree's HEAD, so nothing over there is content that exists only over
    there and a later `git pull` has nothing of anyone's to replace. An rsync
    of the working tree had that backwards: it pushed uncommitted work into a
    checkout the operator also pulls into, and the pull won silently (tolken,
    2026-09-08).

    The deploy needs nothing privileged, so it happens either way. Installing a
    NOPASSWD rule authenticates once, and nothing can bootstrap that from a
    session with no terminal -- so that is where it stops, having left the
    checkout in place, and it says which command finishes the job."""

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
        with stub_path({"ssh": FAR_SSH % log}) as path:
            cp = subprocess.run(
                ["bash", "-c", LIB + f"WK_ROOT={src}\nNODE_NAME=mbp\n"
                 'machine_prepare tolken || echo "rc=$?"'],
                env=dict(os.environ,
                         PATH="%s:%s" % (path, os.environ["PATH"]),
                         WK_ROOT=str(REPO), HOME=str(home)),
                capture_output=True, text=True, stdin=subprocess.DEVNULL)
        asked = log.read_text() if log.exists() else ""
        return cp, cp.stdout + cp.stderr, asked

    def test_it_stops_at_the_sudo_and_names_what_finishes_it(self):
        cp, out, _ = self._prepare()
        self.assertIn("rc=1", out, out)
        self.assertIn("wk boot mbp --prepare", out, out)
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
        working tree here is committed first. `tools_committed` says so and
        names the two commands, the same refusal `wk sync --tools` makes."""
        cp, out, _ = self._prepare(dirty=True)
        self.assertIn("rc=1", out, out)
        self.assertIn("uncommitted changes", out, out)
        self.assertIn("git -C", out, out)
        self.assertFalse(self.far.exists(), f"{self.far} was written anyway")

    def test_it_installs_nothing_with_no_terminal_to_authenticate_on(self):
        _, _, asked = self._prepare()
        self.assertNotIn("setup --stage quiesce", asked, asked)

    def test_the_deploy_is_the_fleets_one_deploy_and_not_a_second_one(self):
        body = func_body(MACHINES.read_text(), "machine_prepare")
        self.assertIn("tools_push", body)
        self.assertNotIn("rsync", body)


class PreflightPreparesAMacItCannotRestart(WkTest):
    """`wk bench mac-ab`'s preflight runs the prepare itself when it finds a Mac
    it cannot restart and has a terminal to answer the one sudo on. Since the
    deploy is a commit, an uncommitted tree here surfaces `tools_committed`'s
    refusal *inside* the preflight -- which then reports the machine as still
    not restartable, rather than reporting it prepared."""

    BLOCK = _lift_between(MACAB.read_text(),
                          '    if ! b_restart_ready && [ -t 0 ]',
                          '\n    log "" >&2')

    def _preflight(self, prepare_rc, ready_after):
        # On a pty, because "has a terminal to answer the one sudo on" is the
        # condition under test and `[ -t 0 ]` is what asks it.
        return pty_bash(
            '. "$WK_ROOT/lib/common.sh"\n'
            'PF_FAIL=0; DRY=""; MACHINE=mbp\n'
            'ck() {%s}\n'
            '_n=0\n'
            'b_restart_ready() { _n=$((_n + 1)); [ "$_n" -gt 1 ] && return %d; return 1; }\n'
            'b_restart_detail() { printf "no boot helper on tolken, and plain sudo there wants a password"; }\n'
            'b_manage_prepare() { warn "wk-tools here has uncommitted changes, so there is no'
            ' commit to put on a machine."; return %d; }\n'
            '%s\n'
            'echo "PF_FAIL=$PF_FAIL"'
            % (macab_func("ck"), 0 if ready_after else 1, prepare_rc, self.BLOCK))

    def test_a_dirty_tree_surfaces_the_commit_first_refusal_inside_preflight(self):
        cp = self._preflight(prepare_rc=1, ready_after=False)
        out = cp.stdout + cp.stderr
        self.assertIn("cannot be restarted unattended yet -- preparing it now", out)
        self.assertIn("uncommitted changes", out)
        self.assertIn("restartable", out)
        self.assertIn("PF_FAIL=1", out, out)

    def test_a_prepare_that_worked_leaves_the_check_passing(self):
        """The discriminating half: the prepare is run for its effect, so a
        preflight that reported `restartable no` either way would be reporting
        a record and not the machine."""
        cp = self._preflight(prepare_rc=0, ready_after=True)
        out = cp.stdout + cp.stderr
        self.assertIn("PF_FAIL=0", out, out)
        self.assertIn("this lane restarts mbp itself", out)

    def test_a_failed_prepare_does_not_end_the_preflight(self):
        """The rest of preflight is what the operator needs to see, and a
        machine that cannot be restarted is a barrier and not a stop."""
        self.assertIn("b_manage_prepare || true", self.BLOCK)


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
                cp = bash('WK_DRY_RUN=1 exec "$WK_ROOT/cmd/boot" mbp --prepare',
                          env={"PATH": "%s:%s" % (path, os.environ["PATH"])})
            out = cp.stdout + cp.stderr
            # What it says it would do has to be what it does: it pushes a
            # commit now, and "sync" was the rsync this no longer runs.
            self.assertIn("would push this tree, as a commit", out, out)
            self.assertNotIn("would sync", out, out)
            self.assertFalse((tmp / "rsync.argv").exists(), "a dry run synced")


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
                if "tart" not in p and ".local/bin" not in p)
            cp = self._boot("benchvm", "--dry-run", env={"HOME": str(tmp), "PATH": no_tart})
        out = cp.stdout + cp.stderr
        self.assertIn("cannot reach benchvm from here", out, out)
        self.assertIn("what is missing is on this host", out, out)
        self.assertNotIn("run this over there", out,
                         "this is over there: a macOS host and a macOS-only machine")

    def test_the_gate_rests_on_the_drivers_own_declaration(self):
        text = BOOT.read_text()
        self.assertIn("if ! b_probeable; then", text)
        self.assertNotIn("_os_bound", text)


class TheCommandRunsOnTheMachineTheConfNames(WkTest):
    """One way to run a command on a machine (`m_ssh`, boot/machines.sh), and
    the test for running it here rather than over ssh is standing on that
    machine: `hostname -s` against the destination the conf names, the same
    comparison bench/mac-ab.sh makes before refusing to reboot the machine it
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
        for verb in ("reboot", "reboot-tryboot"):
            with self.subTest(verb=verb):
                _, asked = self._m_ssh(
                    'NODE_SSH=othermach\nNODE_ROLE=bench-device\n'
                    'MODE_CHANNEL=host\nboot_priv %s' % verb)
                self.assertIn("nohup ", asked, asked)
                self.assertNotIn("setsid", asked, asked)


if __name__ == "__main__":
    unittest.main()
