"""`wk run --until-crash`: loop jsc in the workspace until it exits non-zero, capped, keeping the crash's output."""
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from tests.support import REPO, WkTest, fake_workspace

sys.path.insert(0, str(REPO / "lib"))
from wk import buildconf  # noqa: E402


def _jsc_layout(src):
    """Where jsc-release puts its binary and its libraries under `src`, from
    lib/wk/buildconf.py itself -- the same resolve `wk run` calls."""
    cfg = buildconf.resolve("jsc-release", "macos" if sys.platform == "darwin" else "linux", "local", {})
    return Path(cfg.jsc_path(str(src))), Path(cfg.run_dir(str(src)))


def _plant_fake_jsc(ws, script_body):
    """A fake jsc at the path Config.jsc_path resolves for jsc-release,
    relative to the FakeWorkspace's checkout."""
    src = ws.ws_dir / "WebKit"
    jsc, run_dir = _jsc_layout(src)
    jsc.parent.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    jsc.write_text(script_body)
    jsc.chmod(0o755)
    return jsc


def _counting_script(counter_file, crash_at=None, crash_status=139):
    """A fake jsc: counts its own invocations in `counter_file`, prints a
    distinctive line naming the call number, and -- if `crash_at` is given --
    exits non-zero on that call and 0 on every other one."""
    crash_clause = ""
    if crash_at is not None:
        crash_clause = f'if [ "$n" -eq {crash_at} ]; then exit {crash_status}; fi\n'
    return f"""#!/bin/sh
n=$(cat {counter_file} 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > {counter_file}
echo "DISTINCTIVE-JSC-OUTPUT call=$n"
{crash_clause}exit 0
"""


class TestUntilCrashLoopsToTheCrash(WkTest):
    def test_runs_until_nonzero_exit_and_reports_it(self):
        with fake_workspace() as ws:
            counter = ws.tmp / "calls"
            home = ws.tmp / "home"
            home.mkdir()
            _plant_fake_jsc(ws, _counting_script(counter, crash_at=3, crash_status=139))

            cp = ws.run("run", "--until-crash", "--", "x.js", env={"HOME": str(home)})

            self.assertEqual(cp.returncode, 139, cp.stdout)
            self.assertIn("until-crash: crashed on iteration 3 (exit 139)", cp.stdout)
            self.assertIn("until-crash: args:", cp.stdout)
            # exactly 3 calls happened -- the loop did not run a 4th
            self.assertEqual(counter.read_text().strip(), "3")

            m = re.search(r"until-crash: log: (\S+)", cp.stdout)
            self.assertIsNotNone(m, cp.stdout)
            log_path = Path(m.group(1))
            self.assertTrue(log_path.is_file(), f"{log_path} was not written")
            self.assertIn("DISTINCTIVE-JSC-OUTPUT call=3", log_path.read_text())
            # under the workspace's home (t_home), not /tmp
            self.assertEqual(log_path.parent, home / "until-crash")

    def test_max_caps_iterations_and_exits_zero(self):
        with fake_workspace() as ws:
            counter = ws.tmp / "calls"
            home = ws.tmp / "home"
            home.mkdir()
            _plant_fake_jsc(ws, _counting_script(counter))

            cp = ws.run("run", "--until-crash", "--max", "2", "--", "x.js",
                        env={"HOME": str(home)})

            self.assertEqual(cp.returncode, 0, cp.stdout)
            self.assertIn("until-crash: reached the cap of 2 iterations without a crash", cp.stdout)
            self.assertEqual(counter.read_text().strip(), "2")

    def test_max_refuses_a_non_numeric_value(self):
        with fake_workspace() as ws:
            cp = ws.run("run", "--until-crash", "--max", "abc", "--", "x.js")
            self.assertNotEqual(cp.returncode, 0, cp.stdout)
            self.assertIn("--max needs a positive integer", cp.stdout)


@unittest.skipUnless(shutil.which("lldb"), "no lldb on this host")
class TestUntilCrashLldbCommandFile(unittest.TestCase):

    PATH = REPO / "container" / "lldb" / "until-crash-run-file"

    def test_commands_are_accepted_by_lldb(self):
        cp = subprocess.run(["lldb", "-b", "-s", str(self.PATH)],
                             capture_output=True, text=True, timeout=15)
        self.assertIn("invalid target", cp.stderr, cp.stdout + cp.stderr)
        self.assertNotIn("is not a valid command", cp.stdout + cp.stderr)

    def test_script_lines_are_valid_python(self):
        lines = [l for l in self.PATH.read_text().splitlines() if l.startswith("script ")]
        self.assertTrue(lines)
        args = ["lldb"]
        for l in lines:
            args += ["-o", l]
        args += ["-o", "quit"]
        env = dict(os.environ)
        env["WK_UNTIL_CRASH_STATUS"] = "/dev/null"
        cp = subprocess.run(args, capture_output=True, text=True, timeout=15, env=env)
        self.assertNotIn("Traceback", cp.stdout, cp.stdout)
