"""`wk run --until-crash`: loop the jsc invocation in one round trip into the
workspace until it exits non-zero, cap the iterations, and keep the crashing
iteration's output; `--lldb` combined with it drops into the debugger at the
crash instead of exiting with it.

Nothing here builds or runs real WebKit: a FakeWorkspace stands in for a
checkout, and a planted shell script at the path `config_jsc_path` resolves
for jsc-release stands in for the jsc binary (targets/local.sh makes
WK_TARGET=local inside a fake workspace, so `t_exec` runs it right here).

Run: python3 -m unittest tests.test_run_until_crash -v
"""
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from tests.support import REPO, WkTest, fake_workspace


def _plant_fake_jsc(ws, script_body):
    """The path config_jsc_path/config_run_dir resolve for jsc-release,
    relative to the FakeWorkspace's checkout (build/configs.sh:
    config_build_dir's cmake:--jsc-only case)."""
    src = ws.ws_dir / "WebKit"
    bindir = src / "WebKitBuild" / "JSCOnly" / "Release" / "bin"
    libdir = src / "WebKitBuild" / "JSCOnly" / "Release" / "lib"
    bindir.mkdir(parents=True, exist_ok=True)
    libdir.mkdir(parents=True, exist_ok=True)
    jsc = bindir / "jsc"
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
        """3 iterations, the 3rd exits 139: the loop stops there, exits 139,
        and the log it names holds that iteration's output"""
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
        """--max 2 against a jsc that never crashes: 2 iterations, exit 0,
        and the cap is named as the reason"""
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
        """--max abc is refused, naming the remedy, before anything runs"""
        with fake_workspace() as ws:
            cp = ws.run("run", "--until-crash", "--max", "abc", "--", "x.js")
            self.assertNotEqual(cp.returncode, 0, cp.stdout)
            self.assertIn("--max needs a positive integer", cp.stdout)


@unittest.skipUnless(shutil.which("lldb"), "no lldb on this host")
class TestUntilCrashLldbCommandFile(unittest.TestCase):
    """The generated --until-crash --lldb command sequence is exercised
    through container/lldb/until-crash-run-file (cmd/run:until-crash+lldb
    branch), not by driving a real crash through lldb here: launching and
    controlling *any* process under lldb hangs indefinitely on this host
    (measured -- `DevToolsSecurity -status` reports Developer Mode disabled,
    and `lldb -o run -o quit -- /bin/echo hi` never returns). Both checks
    below give `run` no target, so lldb fails fast ("invalid target")
    instead of trying to launch anything, and cannot hit that hang."""

    PATH = REPO / "container" / "lldb" / "until-crash-run-file"

    def test_file_exists_and_does_not_stop_at_entry(self):
        """unlike run-file (interactive breakpoint-setting), this one runs
        straight through so an unattended loop can tell pass from crash"""
        self.assertTrue(self.PATH.is_file())
        text = self.PATH.read_text()
        self.assertNotIn("--stop-at-entry", text)
        self.assertIn("\nrun\n", "\n" + text)

    def test_commands_are_accepted_by_lldb(self):
        """every non-script command lldb recognises: `run` is reached and
        fails only for lack of a target, never as an unknown command"""
        cp = subprocess.run(["lldb", "-b", "-s", str(self.PATH)],
                             capture_output=True, text=True, timeout=15)
        self.assertIn("invalid target", cp.stderr, cp.stdout + cp.stderr)
        self.assertNotIn("is not a valid command", cp.stdout + cp.stderr)

    def test_script_lines_are_valid_python(self):
        """the conditional-quit lines, chained in one session as the real
        file runs them, with no process to inspect -- the SB API returns
        empty/zero values rather than raising, so this only fails on an
        actual syntax or name error"""
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
