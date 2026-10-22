"""The build wall: an agent cannot drive a build or a test run by hand."""
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

    def test_the_wrapper_takes_off_every_wall_and_nothing_else(self):
        import sys
        sys.path.insert(0, str(REPO / "lib"))
        from wk.sysimage import task
        self.assertEqual(task.off_wall("/home/me/wk-tools/container/bin:/usr/local/bin:/opt/wk-tools/container/bin:"
                                       "/opt/wk-tools/container/bin/ws:/usr/bin:/bin:/opt/container/binaries"),
                         "/usr/local/bin:/usr/bin:/bin:/opt/container/binaries")


class TestOneFileUnderEveryName(WallTest):
    def test_every_name_is_a_symlink_to_the_wall_and_nothing_else_is(self):
        self.assertEqual(sorted(NAMES), sorted(p.name for p in BIN.iterdir() if p.is_symlink()))
        for name in NAMES:
            self.assertEqual("wk-build-wall", os.readlink(BIN / name), name)

    def test_it_refuses_its_own_name(self):
        cp = self.call(str(WALL))
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("wk-build-wall", cp.stderr)


BRIDGE = "/opt/wk-tools/container/proxy/ensure-bridge.sh"


class TestSudoGivesRootABridge(WallTest):
    """`sudo` in a workspace (container/bin/ws/sudo) is the wall, which puts ensure-bridge.sh between sudo's options
    and the command; a form that runs no command reaches sudo as it was."""

    def setUp(self):
        super().setUp()
        self.stub = self.fake / "sudo"
        self.stub.write_text("#!/bin/sh\nfor a; do printf '%s\\n' \"$a\"; done\n")
        self.stub.chmod(0o755)

    def sudo(self, *args, env=None):
        e = {"HOME": str(self.tmp), "PATH": f"{BIN}/ws:{BIN}:{self.fake}:/usr/bin:/bin", "TERM": "dumb"}
        e.update(env or {})
        cp = subprocess.run(["sudo", *args], cwd=str(self.tmp), env=e, capture_output=True, text=True, timeout=60)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return cp.stdout.splitlines()

    def test_the_command_runs_under_the_bridge_after_sudo_s_options(self):
        for argv, want in ((["apt-get", "install", "x"], [BRIDGE, "apt-get", "install", "x"]),
                           (["-n", "-u", "root", "apt-get", "update"], ["-n", "-u", "root", BRIDGE, "apt-get", "update"]),
                           (["-nu", "root", "cmd"], ["-nu", "root", BRIDGE, "cmd"]),
                           (["-uroot", "-C3", "cmd"], ["-uroot", "-C3", BRIDGE, "cmd"]),
                           (["-h", "box", "cmd"], ["-h", "box", BRIDGE, "cmd"]),
                           (["--user=root", "cmd"], ["--user=root", BRIDGE, "cmd"]),
                           (["--user", "root", "--", "cmd", "a", "b"], ["--user", "root", "--", BRIDGE, "cmd", "a", "b"]),
                           (["VAR=1", "cmd"], ["VAR=1", BRIDGE, "cmd"]),
                           (["-E", "-H", "cmd"], ["-E", "-H", BRIDGE, "cmd"]),
                           (["-k", "cmd"], ["-k", BRIDGE, "cmd"]),
                           (["-s", "cmd"], ["-s", BRIDGE, "cmd"])):
            with self.subTest(argv=argv):
                self.assertEqual(want, self.sudo(*argv))

    def test_a_form_that_runs_no_command_passes_through_unchanged(self):
        for argv in ([], ["-l"], ["-v"], ["-k"], ["-K"], ["-V"], ["-h"], ["--help"], ["-e", "f"], ["--"], ["-u"], ["--bogus", "cmd"]):
            with self.subTest(argv=argv):
                self.assertEqual(argv, self.sudo(*argv))

    def test_a_bare_shell_is_the_shell_under_the_bridge_with_the_flag_dropped(self):
        self.assertEqual([BRIDGE, "/bin/zsh"], self.sudo("-s", env={"SHELL": "/bin/zsh"}))
        self.assertEqual(["-nu", "root", BRIDGE, "/bin/bash"], self.sudo("-nsu", "root", env={"SHELL": "/bin/bash"}))
        self.assertEqual([BRIDGE, "bash", "-l"], self.sudo("-i"))
        self.assertEqual(["-u", "root", BRIDGE, "bash", "-l"], self.sudo("-u", "root", "--login"))

    def test_the_exit_status_is_sudo_s(self):
        self.stub.write_text("#!/bin/sh\nexit 7\n")
        cp = subprocess.run(["sudo", "cmd"], cwd=str(self.tmp), capture_output=True, text=True, timeout=60,
                            env={"HOME": str(self.tmp), "PATH": f"{BIN}/ws:{BIN}:{self.fake}:/usr/bin:/bin"})
        self.assertEqual(7, cp.returncode)


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
