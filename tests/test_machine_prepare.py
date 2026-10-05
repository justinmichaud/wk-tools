"""`wk machine setup <mac>` pushes a commit and stops at the one sudo; a machine's command runs over ssh unless
this is that machine. ssh and scp are stubs on PATH, and the far side of a deploy is a scratch directory."""
import contextlib
import io
import os
import shlex
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from tests.support import REPO, WkTest, bash, scratch_dir, stub_path

sys.path.insert(0, str(REPO / "lib"))
from wk import act, machine_cmd  # noqa: E402
from wk.boot.cli import load_conf  # noqa: E402
from wk.boot.driver import Channel  # noqa: E402
from wk.machine import Fake, Local  # noqa: E402


class TheToolsPathIsDeclared(WkTest):
    def test_the_driver_answers_where_the_tree_is_and_searches_for_nothing(self):
        from wk.boot.mac import MacVolume
        m = Fake("tolken")
        m.answer(["test", "-x", "Development/wk-tools/wk"], rc=0)
        d = MacVolume(str(REPO), {"name": "mbp", "ssh": "tolken"}, None)
        self.assertEqual("Development/wk-tools", d.manager_tools(m))
        self.assertEqual([e[1] for e in m.effects], [("test", "-x", "Development/wk-tools/wk")])

def git(cwd, *args):
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
    """Real `Ssh`/`Local` machines over stubbed ssh/scp: the push lands a checkout at HEAD, then the sudo stops it."""

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
                        .setup_mac("mbp", {"kind": "mac", "ssh": "tolken"})
                except act.Refused as e:
                    rc = e.status
        asked = log.read_text() if log.exists() else ""
        return rc, err.getvalue(), asked

    def test_the_commit_lands_as_a_checkout_at_this_head_and_it_stops_at_the_sudo(self):
        rc, out, asked = self._prepare()
        self.assertEqual(rc, 1, out)
        self.assertIn("./setup --stage quiesce", out, out)
        self.assertIn("sudoers.d", out, out)
        self.assertNotIn("setup --stage quiesce", asked, asked)
        self.assertTrue((self.far / ".git").is_dir(), f"{self.far} is no checkout")
        self.assertEqual(self.sha, git(self.far, "rev-parse", "HEAD").stdout.strip())
        self.assertEqual("#!/bin/sh\necho committed\n", (self.far / "wk").read_text())

    def test_an_uncommitted_tree_is_refused_by_name_and_nothing_is_pushed(self):
        rc, out, _ = self._prepare(dirty=True)
        self.assertEqual(rc, 1, out)
        self.assertIn("uncommitted changes", out, out)
        self.assertIn("git -C", out, out)
        self.assertFalse(self.far.exists(), f"{self.far} was written anyway")



class TheHostOsGateAsksWhetherTheDriverCanReachIt(WkTest):
    """The gate is the driver's own reach (`probeable`), not the host OS: mac-volume works over ssh, mac-guest needs tart here."""

    def _boot(self, machine, *args, env=None):
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
    """Channel's `m_ssh` runs here only when `hostname -s` is the conf's destination (case-insensitively)."""

    def _m_ssh(self, conf, hostname="moose"):
        """(what ran here, the ssh argvs issued) for `m_ssh "echo RAN-HERE"` under `conf`."""
        via = Fake("here")
        via.answer(["hostname", "-s"], out=hostname + "\n")
        via.answer(["tailscale"], rc=1)
        env = {"WK_ROOT": str(REPO), "WK_MACHINES_DIR": str(REPO / "machines")}
        Channel(REPO, conf, env=env, via=via).call("m_ssh", "echo RAN-HERE")
        runs = [list(e[1]) for e in via.effects if e[0] == "run"]
        return [r for r in runs if r[:2] == ["sh", "-c"]], [r for r in runs if r[0] == "ssh"]

    def mbp(self):
        return load_conf(REPO, "mbp", {"WK_MACHINES_DIR": str(REPO / "machines")})

    def test_a_command_for_another_machine_goes_over_ssh(self):
        here, sshed = self._m_ssh({"ssh": "othermach"})
        self.assertEqual(here, [])
        self.assertEqual([r[-2:-1] + [shlex.split(r[-1])[-1]] for r in sshed], [["othermach", "sh -c 'echo RAN-HERE'"]], sshed)

    def test_a_command_for_this_machine_runs_here(self):
        here, sshed = self._m_ssh({"ssh": "moose"}, hostname="MOOSE")
        self.assertEqual(here, [["sh", "-c", "echo RAN-HERE"]])
        self.assertEqual(sshed, [])

    def test_the_mac_is_reached_over_ssh_from_a_machine_that_is_not_it(self):
        here, sshed = self._m_ssh(self.mbp())
        self.assertEqual(here, [])
        self.assertEqual([r[-2] for r in sshed], ["tolken"], sshed)

    def test_the_mac_runs_it_here_when_this_is_the_mac(self):
        here, sshed = self._m_ssh(self.mbp(), hostname="tolken")
        self.assertEqual(here, [["sh", "-c", "echo RAN-HERE"]])
        self.assertEqual(sshed, [])

    def test_a_machine_with_no_ssh_destination_is_never_this_one(self):
        here, sshed = self._m_ssh({"ssh": ""}, hostname="")
        self.assertEqual((here, sshed), ([], []))

    def test_the_reboot_it_issues_detaches_with_what_both_platforms_ship(self):
        """macOS ships no `setsid` (measured on tolken, macOS 26.6.2), so the reboot detaches with nohup."""
        sys.path.insert(0, str(REPO / "lib"))
        from wk.boot.driver import root_priv
        for verb in ("reboot", "reboot-tryboot"):
            with self.subTest(verb=verb):
                asked = " ".join(root_priv(verb))
                self.assertIn("nohup ", asked, asked)
                self.assertNotIn("setsid", asked, asked)


if __name__ == "__main__":
    unittest.main()
