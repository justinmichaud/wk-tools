"""The build wall: an agent cannot drive a build or a test run by hand.

`wk build` derives the job count from available memory and runs the build at a
nice level that keeps the machine usable; a hand-rolled `ninja -j$(nproc)` on a
shared build box takes it down. Inside a workspace the agent's own permissions
are relaxed (`wk ai claude` passes --dangerously-skip-permissions, because the
workspace is the blast radius), so a Claude Code deny rule is the advisory half
only. The wall itself is where the privilege is: one script,
`container/bin/wk-build-wall`, on PATH ahead of the real tools in every shell
every target sources (shell/path.sh), invoked under each wrapped name through a
symlink beside it.

    an agent    CLAUDECODE=1 (Claude Code exports it into every shell it runs)
                or WK_AGENT=<name> (`wk ai <agent>`, cmd/ai)   -> refused
    wk's build  WK_BUILD=1 (build/build-in-target.sh)          -> the real tool
    a person     neither                                       -> the real tool

No real build tool runs here: each test puts fakes of its own further along
PATH and checks which one was reached.

Run: python3 -m unittest tests.test_build_wall -v
"""
import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from tests.support import REPO, WkTest

BIN = REPO / "container" / "bin"
WALL = BIN / "wk-build-wall"
RC = REPO / "shell" / "bashrc"
SETTINGS = REPO / "claude" / "settings.json"
# The host variant a build-box agent gets (claude/install.sh) carries the same rules.
SETTINGS_ALL = [SETTINGS, REPO / "claude" / "settings-host.json"]

# Files this change touched, for the parse check below.
EDITED = ("container/bin/wk-build-wall", "cmd/ai", "shell/path.sh", "shell/bashrc")


def wall_names():
    """The wrapped names, read from the one place that lists them (WALL_NAMES
    in the wall itself) rather than copied into this test -- the same way
    tests/support.py reads WK_DISPATCH_VARS out of lib/common.sh."""
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
        """A PATH directory holding what the wall itself runs and no build
        tool at all.

        `/usr/bin` cannot stand in for this: a host with ninja or cmake
        installed has one *on* PATH, so a test asserting "no real tool
        anywhere" found the host's and read exit 1 instead of 127. Only the
        four the wall needs are linked in -- `env` resolves bash through
        PATH, and the wall calls dirname, grep and readlink.
        """
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
    """Both pieces of evidence, for every wrapped name: exit 1, the reason and
    the remedy on stderr, and the real tool not run."""

    def test_claudecode_is_refused(self):
        for name in NAMES:
            with self.subTest(tool=name):
                cp = self.call(name, "-j64", env={"CLAUDECODE": "1"})
                self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
                self.assertIn("refused", cp.stderr)
                self.assertIn(name, cp.stderr)
                self.assertIn("wk build", cp.stderr)
                self.assertEqual("", cp.stdout)
        self.assertEqual("", self.ran(), "a refusal ran the real tool")

    def test_wk_agent_is_refused(self):
        """The variable `wk ai <agent>` sets, so the next agent is walled by
        the line that names it and needs nothing else."""
        for name in NAMES:
            with self.subTest(tool=name):
                cp = self.call(name, "--build", ".", env={"WK_AGENT": "claude"})
                self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
                self.assertIn("wk build", cp.stderr)
        self.assertEqual("", self.ran())

    def test_the_remedy_names_every_command_that_replaces_it(self):
        cp = self.call("ninja", env={"CLAUDECODE": "1"})
        for remedy in ("wk build", "wk test", "wk bench", "wk run"):
            self.assertIn(remedy, cp.stderr)


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
        """With no real tool anywhere on PATH the wall says so rather than
        re-execing the symlink beside it forever."""
        cp = self.call("ninja", fake=False)
        self.assertEqual(127, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("not on PATH", cp.stderr)


class TestWkOwnBuildPassesThrough(WallTest):
    """`wk build` reaches the real cmake and ninja through
    build/build-in-target.sh, and an agent typing `wk build` hands CLAUDECODE
    down the whole chain -- so the wall has to let wk's own build through on
    evidence wk sets."""

    def test_wk_build_is_not_walled_even_under_claudecode(self):
        for name in NAMES:
            with self.subTest(tool=name):
                cp = self.call(name, env={"CLAUDECODE": "1", "WK_BUILD": "1"})
                self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                self.assertIn(f"REAL {name}", cp.stdout)

    def test_wk_build_is_not_walled_under_wk_agent_either(self):
        cp = self.call("cmake", env={"WK_AGENT": "claude", "WK_BUILD": "1"})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("REAL cmake", cp.stdout)


class TestOneFileUnderEveryName(WallTest):
    def test_the_names_and_the_symlinks_are_the_same_set(self):
        """WALL_NAMES is the one list; a name added there without its symlink
        (or the other way round) is a wrapped tool the wall never sees."""
        links = sorted(p.name for p in BIN.iterdir() if p.is_symlink())
        self.assertEqual(sorted(NAMES), links)

    def test_every_symlink_is_the_wall(self):
        for name in NAMES:
            with self.subTest(tool=name):
                link = BIN / name
                self.assertEqual("wk-build-wall", os.readlink(link))
                self.assertEqual(WALL.resolve(), link.resolve())

    def test_the_wall_is_executable(self):
        self.assertTrue(os.access(WALL, os.X_OK), "container/bin/wk-build-wall is not +x")

    def test_it_refuses_its_own_name(self):
        """Nothing invokes it as `wk-build-wall`; running it that way has no
        tool to stand in front of."""
        cp = self.call(str(WALL))
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("wk-build-wall", cp.stderr)


class TestTwoWallsDoNotExecEachOther(WallTest):
    """Two wk-tools trees on PATH is the ordinary case -- a person's clone and
    the tree `wk` pushed -- and `-ef` recognises only the copy that is running.
    Measured: the two walls exec each other until the process table gives out.
    Any wall is skipped, whichever tree it came from."""

    def _second_tree(self):
        """A second container/bin: a copy of the wall (not a link into this
        tree, so `-ef` cannot see it) plus the symlinks beside it."""
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

    def test_the_second_wall_is_skipped_and_the_real_tool_is_reached(self):
        other = self._second_tree()
        cp = self._call("ninja", f"{BIN}:{other}:{self.fake}:/usr/bin:/bin")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("REAL ninja", cp.stdout)

    def test_the_other_tree_first_reaches_the_real_tool_too(self):
        other = self._second_tree()
        cp = self._call("ninja", f"{other}:{BIN}:{self.fake}:/usr/bin:/bin")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("REAL ninja", cp.stdout)

    def test_two_walls_and_no_real_tool_say_so_rather_than_looping(self):
        other = self._second_tree()
        cp = self._call("ninja", f"{BIN}:{other}:{self.bare_bin()}")
        self.assertEqual(127, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("not on PATH", cp.stderr)

    def test_a_copy_that_is_not_a_symlink_is_recognised_too(self):
        """The far tree's wall may be reached as a plain file under a wrapped
        name -- a copy rather than a symlink. The marker line in its header is
        what says it is a wall."""
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
        """An empty PATH entry means `.` to a shell. This decides which binary
        runs, so whatever happens to sit in $PWD is not a candidate."""
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

    def test_a_wall_with_no_wall_beside_it_refuses(self):
        """WALL is the wall's own path, and every skip below rests on it. A
        `cd` that fails leaves it naming a file that does not exist, which
        matches nothing -- so the wall would exec itself for ever. It refuses
        instead."""
        lone = self.tmp / "lone"
        lone.mkdir()
        c = lone / "ninja"
        c.write_bytes(WALL.read_bytes())     # a wall with no sibling of its own
        c.chmod(0o755)
        cp = subprocess.run(
            [str(c)], cwd=str(self.tmp),
            env={"HOME": str(self.tmp), "TERM": "dumb",
                 "PATH": f"{self.fake}:/usr/bin:/bin"},
            capture_output=True, text=True, timeout=60)
        self.assertEqual(1, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("cannot locate the wall", cp.stderr)
        self.assertEqual("", self.ran(), "it ran the real tool anyway")


class TestItIsFirstOnPathInEveryShell(WkTest):
    """One PATH decision (shell/path.sh) reached by every shell kind on every
    target: `bash -lc` is every `t_exec`, an editor's terminal pane is an
    interactive non-login zsh, and a container exec reads no rc at all (it
    sources shell/path.sh directly, tests/test_shell_path.py's subject)."""

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
        """The container-exec path, which reads no rc file."""
        cp = subprocess.run(
            ["bash", "-c", f'WK_TOOLS_DIR="{REPO}" . "{REPO}/shell/path.sh"; command -v ninja'],
            cwd="/", env={"HOME": str(self.tmp), "PATH": "/usr/bin:/bin"},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60,
        )
        self.assertEqual(str(BIN / "ninja"), cp.stdout.strip(), cp.stdout)


class TestTheAgentIsToldUpFront(unittest.TestCase):
    """The advisory half: a deny rule per wrapped name, so the agent is told
    before it tries, and one sentence in the workspace briefing."""

    def test_settings_json_parses(self):
        for f in SETTINGS_ALL:
            json.loads(f.read_text())

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

    def test_the_wall_and_the_readme_say_what_it_covers(self):
        """The wall covers a bare name on PATH, in every shell that sources
        shell/path.sh -- the workstation's own shells included. Both places
        that describe it say so, and name the file rather than re-listing the
        wrapped names in prose."""
        for text, where in ((WALL.read_text(), "the wall's header"),
                            ((REPO / "README.md").read_text(), "README.md")):
            with self.subTest(where=where):
                self.assertIn("shell/path.sh", text)
                self.assertIn("bare name", text)
                self.assertIn("Bash(*/<name> *)", text)

    def test_no_prose_re_lists_the_wrapped_names(self):
        """WALL_NAMES is the one list, and a prose copy of it drifts. Each of
        these names the file instead; an example or two is not a list."""
        header = WALL.read_text().split('WALL_NAMES="')[0]
        readme = (REPO / "README.md").read_text()
        para = readme[readme.index("Nothing an agent runs drives a build"):][:1400]
        briefing = (REPO / "claude" / "CLAUDE.md").read_text()
        for text, where in ((header, "the wall's header"),
                            (para, "README.md"),
                            (briefing, "claude/CLAUDE.md")):
            with self.subTest(where=where):
                self.assertIn("wk-build-wall", text)
                self.assertFalse(all(n in text for n in NAMES),
                                 "the nine wrapped names are re-listed in prose")

    def test_the_workspace_briefing_states_the_rule_and_the_remedy(self):
        """The agent is told before it tries: the file that wraps the tools,
        that they refuse, and what to reach for instead."""
        text = (REPO / "claude" / "CLAUDE.md").read_text()
        self.assertIn("container/bin/wk-build-wall", text)
        self.assertIn("refuse", text)
        for remedy in ("wk build", "wk test", "wk bench", "wk run"):
            self.assertIn(remedy, text)

    def test_the_readme_states_it(self):
        self.assertIn("wk-build-wall", (REPO / "README.md").read_text())


class TestCmdAiSetsTheVariable(unittest.TestCase):
    """cmd/ai is the one thing that knows a session is an agent's, and every
    way it hands over control has to carry that: a foreground session, a
    headless one, and the detached remote-control server."""

    def test_all_three_hand_overs_carry_wk_agent(self):
        text = (REPO / "cmd" / "ai").read_text()
        self.assertEqual(3, text.count('env WK_AGENT="$AGENT"'), text.count("t_exec"))

    def test_the_checkout_path_is_quoted_into_the_login_shell(self):
        """The hand-over is a `bash -lc` *string*; an unquoted `cd $(t_src ...)`
        breaks on a workspace path with a space in it."""
        text = (REPO / "cmd" / "ai").read_text()
        self.assertNotIn('cd $(t_src "$NAME")', text)
        self.assertEqual(2, text.count('cd $(sh_quote "$(t_src "$NAME")")'))

    def test_the_agent_name_is_not_hard_coded(self):
        """`$AGENT` rather than the word `claude`, so the pi agent is walled by
        the line that names it and nothing has to be added here."""
        text = (REPO / "cmd" / "ai").read_text()
        self.assertNotIn("WK_AGENT=claude", text)


class TestScriptsParse(unittest.TestCase):
    """bash -n under both interpreters this host has: the wall runs in a Fedora
    container, in a macOS guest (bash 3.2) and on a build box."""

    def test_bash_n(self):
        interps = ["bash"] + (["/bin/bash"] if Path("/bin/bash").exists() else [])
        for f in EDITED:
            for interp in interps:
                with self.subTest(script=f, interp=interp):
                    cp = subprocess.run([interp, "-n", str(REPO / f)],
                                        capture_output=True, text=True, timeout=60)
                    self.assertEqual(cp.returncode, 0, cp.stderr)


if __name__ == "__main__":
    unittest.main()
