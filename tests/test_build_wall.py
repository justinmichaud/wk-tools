"""The build wall: an agent cannot drive a build or a test run by hand."""
import json
import os
import re
import shutil
import subprocess
import unittest
import unittest.mock

from tests.support import REPO, WkTest

BIN = REPO / "container" / "bin"
WALL = BIN / "wk-build-wall"
RC = REPO / "shell" / "bashrc"
SETTINGS = REPO / "claude" / "settings.json"
# The host variant a build-box agent gets (claude/install.sh) carries the same rules.
SETTINGS_ALL = [SETTINGS, REPO / "claude" / "settings-host.json"]



def wall_names():
    m = re.search(r'^WALL_NAMES="([^"]+)"', WALL.read_text(), re.M)
    assert m, "container/bin/wk-build-wall no longer defines WALL_NAMES"
    return tuple(m.group(1).split())


NAMES = wall_names()


class WallTest(WkTest):
    """A scratch directory of fake build tools, and one way to call the wall."""

    def setUp(self):
        super().setUp()
        self.fake = self.tmp / "fake-bin"
        self.fake.mkdir()
        self.marker = self.tmp / "ran"
        for name in NAMES:
            p = self.fake / name
            p.write_text(
                "#!/bin/sh\n"
                f'printf "%s\\n" "{name} $*" >> "{self.marker}"\n'
                f'echo "REAL {name}"\n'
            )
            p.chmod(0o755)

    def bare_bin(self):
        d = self.tmp / "bare-bin"
        if not d.exists():
            d.mkdir()
            for tool in ("bash", "sh", "dirname", "grep", "readlink"):
                src = shutil.which(tool)
                if src:
                    (d / tool).symlink_to(src)
        return d

    def call(self, name, *args, env=None, fake=True):
        path = f"{BIN}:{self.fake}:/usr/bin:/bin" if fake else f"{BIN}:{self.bare_bin()}"
        e = {"HOME": str(self.tmp), "PATH": path, "TERM": "dumb"}
        if env:
            e.update(env)
        return subprocess.run(
            [name, *args], cwd=str(self.tmp), env=e,
            capture_output=True, text=True, timeout=60,
        )

    def ran(self):
        return self.marker.read_text() if self.marker.exists() else ""


class TestItRefusesAnAgent(WallTest):
    def test_claudecode_and_wk_agent_are_refused_naming_the_remedies(self):
        for env in ({"CLAUDECODE": "1"}, {"WK_AGENT": "claude"}):
            for name in NAMES:
                with self.subTest(env=env, tool=name):
                    cp = self.call(name, "-j64", env=env)
                    self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
                    self.assertIn(name, cp.stderr)
                    for remedy in ("wk build", "wk test", "wk bench", "wk run"):
                        self.assertIn(remedy, cp.stderr)
                    self.assertEqual("", cp.stdout)
        self.assertEqual("", self.ran(), "a refusal ran the real tool")


class TestAPersonGetsTheRealTool(WallTest):
    def test_every_name_execs_the_real_tool(self):
        for name in NAMES:
            with self.subTest(tool=name):
                cp = self.call(name, "--release")
                self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                self.assertIn(f"REAL {name}", cp.stdout)
        self.assertEqual(len(NAMES), len(self.ran().splitlines()))

    def test_the_arguments_arrive_unchanged(self):
        self.call("ninja", "-C", "out/Release", "jsc")
        self.assertEqual("ninja -C out/Release jsc\n", self.ran())

    def test_the_real_tools_exit_status_is_the_wall_s(self):
        p = self.fake / "ninja"
        p.write_text("#!/bin/sh\nexit 7\n")
        p.chmod(0o755)
        self.assertEqual(7, self.call("ninja").returncode)

    def test_it_never_finds_itself(self):
        cp = self.call("ninja", fake=False)
        self.assertEqual(127, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("not on PATH", cp.stderr)


class TestWkOwnBuildPassesThrough(WallTest):

    def test_wk_build_is_not_walled_even_under_an_agent(self):
        for name in NAMES:
            with self.subTest(tool=name):
                cp = self.call(name, env={"CLAUDECODE": "1", "WK_AGENT": "claude", "WK_BUILD": "1"})
                self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                self.assertIn(f"REAL {name}", cp.stdout)


    def test_every_builder_declares_itself(self):
        import sys
        sys.path.insert(0, str(REPO / "lib"))
        from wk.sysimage import task
        seen = {}
        with unittest.mock.patch("os.execvpe", lambda f, argv, env: seen.update(env)), \
                unittest.mock.patch("sys.stderr"):
            task.stage_main("build", ["true"], environ={"PATH": "/usr/bin", "CLAUDECODE": "1"})
        self.assertEqual(seen.get("WK_BUILD"), "1")


class TestBitbakeGetsTheRealTools(WkTest):


    def test_the_gate_is_a_wall_so_the_strip_has_to_name_it(self):
        ws = BIN / "ws"
        names = sorted(p.name for p in ws.iterdir())
        self.assertTrue(names, "container/bin/ws is empty")
        for name in names:
            with self.subTest(tool=name):
                text = (ws / name).read_text()
                self.assertIn("# wk-build-wall:", "\n".join(text.splitlines()[:5]))
                self.assertRegex(text, r'(?m)^\. ".*/wk-build-wall"$')


class TestNoBuilderRecordsTheWall(unittest.TestCase):

    wk_tier = "lint"
    TASK = REPO / "lib" / "wk" / "sysimage" / "task.py"

    def _fn(self, path, name):
        m = re.search(r"(?ms)^    def %s\(.*?(?=^    def |\Z)" % name, path.read_text())
        self.assertIsNotNone(m, f"{name} is not in {path}")
        return m.group(0)


    def test_the_wrapper_takes_off_every_wall_and_nothing_else(self):
        import sys
        sys.path.insert(0, str(REPO / "lib"))
        from wk.sysimage import task
        self.assertEqual(task.off_wall("/home/me/wk-tools/container/bin:/usr/local/bin:/opt/wk-tools/container/bin:"
                                       "/opt/wk-tools/container/bin/ws:/usr/bin:/bin:/opt/container/binaries"),
                         "/usr/local/bin:/usr/bin:/bin:/opt/container/binaries")


class TestOneFileUnderEveryName(WallTest):
    def test_the_names_and_the_symlinks_are_the_same_set(self):
        links = sorted(p.name for p in BIN.iterdir() if p.is_symlink())
        self.assertEqual(sorted(NAMES), links)

    def test_every_symlink_is_the_wall(self):
        for name in NAMES:
            with self.subTest(tool=name):
                link = BIN / name
                self.assertEqual("wk-build-wall", os.readlink(link))
                self.assertEqual(WALL.resolve(), link.resolve())


    def test_it_refuses_its_own_name(self):
        cp = self.call(str(WALL))
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("wk-build-wall", cp.stderr)


class TestTwoWallsDoNotExecEachOther(WallTest):

    def _second_tree(self):
        other = self.tmp / "other-tools" / "container" / "bin"
        other.mkdir(parents=True)
        copy = other / "wk-build-wall"
        copy.write_text(WALL.read_text())
        copy.chmod(0o755)
        for name in NAMES:
            (other / name).symlink_to("wk-build-wall")
        return other

    def _call(self, name, path):
        return subprocess.run(
            [name], cwd=str(self.tmp),
            env={"HOME": str(self.tmp), "PATH": path, "TERM": "dumb"},
            capture_output=True, text=True, timeout=60)


    def test_either_tree_first_skips_the_other_wall_and_reaches_the_real_tool(self):
        other = self._second_tree()
        for path in (f"{BIN}:{other}", f"{other}:{BIN}"):
            cp = self._call("ninja", f"{path}:{self.fake}:/usr/bin:/bin")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("REAL ninja", cp.stdout)

    def test_two_walls_and_no_real_tool_say_so_rather_than_looping(self):
        other = self._second_tree()
        cp = self._call("ninja", f"{BIN}:{other}:{self.bare_bin()}")
        self.assertEqual(127, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("not on PATH", cp.stderr)

    def test_a_copy_that_is_not_a_symlink_is_recognised_too(self):
        other = self.tmp / "copied-bin"
        other.mkdir()
        c = other / "ninja"
        c.write_text(WALL.read_text())
        c.chmod(0o755)
        cp = self._call("ninja", f"{BIN}:{other}:{self.fake}:/usr/bin:/bin")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("REAL ninja", cp.stdout)


class TestTheCandidateSearchIsStrict(WallTest):
    def test_an_empty_path_entry_is_not_the_current_directory(self):
        cwd_tool = self.tmp / "ninja"
        cwd_tool.write_text("#!/bin/sh\necho \"CWD ninja\"\n")
        cwd_tool.chmod(0o755)
        cp = subprocess.run(
            ["ninja"], cwd=str(self.tmp),
            env={"HOME": str(self.tmp), "TERM": "dumb",
                 "PATH": f"{BIN}::{self.fake}:/usr/bin:/bin"},
            capture_output=True, text=True, timeout=60)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("REAL ninja", cp.stdout)
        self.assertNotIn("CWD ninja", cp.stdout)

    def _linked_away(self):
        away = self.tmp / "hosttools"
        away.mkdir()
        (away / "ninja").symlink_to(BIN / "ninja")
        return away / "ninja"

    def _run_away(self, link, *args, env=None):
        e = {"HOME": str(self.tmp), "TERM": "dumb",
             "PATH": f"{self.fake}:/usr/bin:/bin"}
        if env:
            e.update(env)
        return subprocess.run(
            [str(link), *args], cwd=str(self.tmp), env=e,
            capture_output=True, text=True, timeout=60)

    def test_a_wall_reached_through_a_symlink_elsewhere_finds_itself(self):
        cp = self._run_away(self._linked_away(), "-j4")
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertNotIn("cannot locate the wall", cp.stderr)
        self.assertIn("ninja -j4", self.ran(), "it never reached the real tool")

    def test_finding_itself_is_not_a_way_around_it(self):
        cp = self._run_away(self._linked_away(), "-j64", env={"CLAUDECODE": "1"})
        self.assertEqual(1, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("refused", cp.stderr)
        self.assertEqual("", self.ran(), "it ran the real tool anyway")


class TestItIsFirstOnPathInEveryShell(WkTest):

    SHELLS = {
        "editor terminal pane": ("zsh", ["-i", "-c"]),
        "login zsh": ("zsh", ["-l", "-c"]),
        "bash -lc (every t_exec)": ("bash", ["-lc"]),
        "non-interactive bash": ("bash", ["-c"]),
    }

    def _resolved(self, shell, args, tool):
        cp = subprocess.run(
            [shell, *args, f'. "{RC}"; command -v {tool}'],
            cwd=str(REPO),
            env={"HOME": str(self.tmp), "TERM": "dumb",
                 "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=120,
        )
        return cp.stdout.strip().splitlines()[-1] if cp.stdout.strip() else ""

    def test_the_wall_answers_first_in_every_shell(self):
        for what, (shell, args) in self.SHELLS.items():
            if not shutil.which(shell):
                continue
            for tool in NAMES:
                with self.subTest(shell=what, tool=tool):
                    self.assertEqual(str(BIN / tool), self._resolved(shell, args, tool))

    def test_path_sh_alone_is_enough(self):
        cp = subprocess.run(
            ["bash", "-c", f'WK_TOOLS_DIR="{REPO}" . "{REPO}/shell/path.sh"; command -v ninja'],
            cwd="/", env={"HOME": str(self.tmp), "PATH": "/usr/bin:/bin"},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60,
        )
        self.assertEqual(str(BIN / "ninja"), cp.stdout.strip(), cp.stdout)


class TestTheAgentIsToldUpFront(unittest.TestCase):
    """The advisory half: a deny rule per wrapped name, so the agent is told
    before it tries, and one sentence in the workspace briefing."""


    def test_every_wrapped_name_is_denied(self):
        for f in SETTINGS_ALL:
            deny = json.loads(f.read_text())["permissions"]["deny"]
            for name in NAMES:
                with self.subTest(file=f.name, tool=name):
                    self.assertIn(f"Bash({name} *)", deny)

    def test_both_settings_variants_deny_the_same_set(self):
        rules = [json.loads(f.read_text())["permissions"]["deny"] for f in SETTINGS_ALL]
        self.assertEqual(sorted(rules[0]), sorted(rules[1]))

    def test_the_path_spelling_is_denied_in_both_files(self):
        """The shim cannot intercept `Tools/Scripts/build-webkit --gtk`: a
        path names a file and never consults PATH. The deny rule is the only
        cover it has, so it is anchored at a path separator."""
        for f in SETTINGS_ALL:
            deny = json.loads(f.read_text())["permissions"]["deny"]
            for name in NAMES:
                with self.subTest(file=f.name, tool=name):
                    self.assertIn(f"Bash(*/{name} *)", deny)

    def test_no_rule_matches_a_bare_word_anywhere_in_a_command(self):
        """`Bash(*make *)` matches `echo make it so`, and a deny rule that
        fires on prose trains the reader to ignore it."""
        for f in SETTINGS_ALL:
            deny = json.loads(f.read_text())["permissions"]["deny"]
            for name in NAMES:
                with self.subTest(file=f.name, tool=name):
                    self.assertNotIn(f"Bash(*{name} *)", deny)

    def test_the_two_spellings_are_the_whole_of_the_deny_list(self):
        """Two rules per wrapped name and nothing else: a rule nobody can
        derive from the name list is a rule nobody maintains."""
        want = sorted([f"Bash({n} *)" for n in NAMES]
                      + [f"Bash(*/{n} *)" for n in NAMES])
        for f in SETTINGS_ALL:
            deny = sorted(json.loads(f.read_text())["permissions"]["deny"])
            self.assertEqual(want, deny, f.name)

    def test_the_wall_names_what_covers_each_spelling(self):
        """The wall's header points at the two things outside it that its
        cover depends on: the file that puts it first on PATH (a bare name)
        and the deny-rule form that covers the path spelling it cannot see.
        Each is checked against what exists, not against prose."""
        header = WALL.read_text().split('WALL_NAMES="')[0]
        self.assertIn("shell/path.sh", header)
        self.assertTrue((REPO / "shell" / "path.sh").is_file())
        self.assertIn("Bash(*/<name> *)", header)
        for f in SETTINGS_ALL:
            deny = json.loads(f.read_text())["permissions"]["deny"]
            self.assertTrue(any(r.startswith("Bash(*/") for r in deny), f.name)


if __name__ == "__main__":
    unittest.main()
