"""`wk pi`: the bare/`-h`/unknown-subcommand usage page (cmd/pi's
pi_usage_page, read out of this file's own header comment), the on-board
bench run's elapsed-seconds report (pi_bench_once's start/waited arithmetic),
and `wk pi bench --cores` (the same cpu-list flag `wk bench` has, through the
same parser -- lib/wkdata.py cores-valid/cores-wrap -- applied as a
taskset -c prefix on the board's run-benchmark command line).

Unit tests only -- no board, no ssh, no hardware. Bare `wk pi` and `wk pi
bogus` are run for real: nothing before the subcommand dispatch in cmd/pi
touches a board, so both are as safe as any other refusal. pi_bench_once is
lifted verbatim out of cmd/pi with `sed -n '/^fn()/,/^}/p'`, the idiom
tests/test_quick.py uses to lift cmd/status's `bump` and
tests/test_bench_cores.py uses to lift cmd/bench's bench_cores_refusal, with
every ssh-reaching dependency (m_ssh, detach_remote, detach_wait_remote,
pi_build_dir, pi_bench_env, pi_bench_record, pi_slot_sha, pi_slot_ws) stubbed
out so only the real timing and command-building code under test runs
against real wall-clock time and real string interpolation.

Run: python3 -m unittest tests.test_pi -v
"""
import unittest

from tests.support import REPO, WkTest, bash

CMD_PI = REPO / "cmd" / "pi"


def lift(fn_name):
    """The `sed -n '/^fn()/,/^}/p'` idiom, as a reusable fragment: prints the
    named function's body out of cmd/pi, ready to `eval`."""
    return f'''
body="$(sed -n '/^{fn_name}()/,/^}}/p' "{CMD_PI}")"
[ -n "$body" ] || {{ echo "lift {fn_name} failed"; exit 1; }}
eval "$body"
'''


# Every dependency pi_bench_once reaches for besides lib/common.sh's own
# die/warn/info/log/sh_quote, stubbed so a lifted call touches no board.
# detach_remote's stub records the command line it was asked to run, which
# is what the --cores test inspects.
STUBS = '''
m_ssh() { return 0; }
pi_build_dir() { printf '/tmp/build'; }
pi_bench_env() { printf ''; }
pi_bench_record() { :; }
pi_slot_sha() { :; }
pi_slot_ws() { :; }
'''


class TestPiUsagePage(WkTest):
    """Bare `wk pi` and an unknown subcommand both print the whole
    board-lifecycle sequence -- the header comment `wk pi -h` reads through
    the dispatcher's explain_cmd -- rather than a bare usage line. Bare exits
    0 (asking for help is not an error); the unknown-subcommand case is the
    same text as a refusal, on stderr, exit 1."""

    STEPS = (
        "wk sysimage build",
        "wk sysimage write --from",
        "wk pi boot-order",
        "wk boot",
        "wk pi setup",
        "wk pi deploy",
        "wk pi bench",
    )

    def test_bare_exits_0_and_names_every_step(self):
        cp = self.run_wk("pi")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        for step in self.STEPS:
            self.assertIn(step, cp.stdout, f"{step!r} missing from bare 'wk pi'")

    def test_unknown_subcommand_exits_1_with_the_same_text(self):
        bare = self.run_wk("pi")
        bogus = self.run_wk("pi", "bogus")
        self.assertEqual(bogus.returncode, 1, bogus.stdout)
        self.assertEqual(bare.stdout, bogus.stdout)

    def test_no_board_is_contacted_before_a_subcommand_is_chosen(self):
        """Safe to run for real: ssh only starts after cmd/pi's dispatch
        case, so a bogus host name never gets reached for either verb."""
        cp = self.run_wk("pi", "bogus", "not-a-real-host", timeout=15)
        self.assertEqual(cp.returncode, 1)


class TestPiBenchElapsed(WkTest):
    """pi_bench_once's `waited` is computed from wall-clock time captured
    before the blocking wait, not after it (or reset by a subshell) --
    lifted and run against a stubbed detach_wait_remote that sleeps for a
    known span, so a wrong computation reads back as ~0 instead."""

    def _run(self, sleep_s, extra_stubs=""):
        script = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
{lift("pi_bench_once")}
{STUBS}
{extra_stubs}
detach_remote() {{ return 0; }}
detach_wait_remote() {{ sleep {sleep_s}; printf '0'; return 0; }}
m_ssh() {{ return 0; }}

machine=fake-machine plan=fake-plan sysid=x kernel=x gov=x throttled=x
root_dev=x renderer=gl session=drm count="" timeout_s="" cores=""

pi_bench_once a
'''
        return bash(script, timeout=30)

    def test_reports_the_real_span_not_zero(self):
        cp = self._run(2)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        m = None
        for line in cp.stderr.splitlines():
            if "result" in line and "after" in line:
                m = line
        self.assertIsNotNone(m, cp.stderr)
        waited = int(m.split("after")[1].strip().rstrip("s"))
        self.assertGreaterEqual(waited, 1, f"a ~2s run reported {waited}s: {m!r}")


class TestPiBenchCores(WkTest):
    """`wk pi bench --cores`: refused for a bad cpu list before any board is
    touched (same parser and same message shape as `wk bench --cores`), and
    a valid one lands as a literal `taskset -c <set>` prefix on the on-board
    run-benchmark command line."""

    def test_invalid_cores_is_refused_before_the_board_is_touched(self):
        cp = self.run_wk(
            "pi", "bench", "not-a-real-machine", "jetstream3", "--cores", "abc",
            timeout=15,
        )
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("not a valid Linux cpu list", cp.stdout)

    def test_valid_cores_becomes_a_taskset_prefix_on_the_board(self):
        capture = self.tmp / "captured-cmd"
        script = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
{lift("pi_bench_once")}
{STUBS}
detach_remote() {{ printf '%s' "$*" > {capture}; return 0; }}
detach_wait_remote() {{ printf '0'; return 0; }}

machine=fake-machine plan=fake-plan sysid=x kernel=x gov=x throttled=x
root_dev=x renderer=gl session=drm count="" timeout_s="" cores="0-3"

pi_bench_once a
'''
        cp = bash(script, timeout=30)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertTrue(capture.exists(), "detach_remote was never called")
        self.assertIn("taskset -c 0-3", capture.read_text())

    def test_no_cores_means_no_taskset_prefix(self):
        capture = self.tmp / "captured-cmd"
        script = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
{lift("pi_bench_once")}
{STUBS}
detach_remote() {{ printf '%s' "$*" > {capture}; return 0; }}
detach_wait_remote() {{ printf '0'; return 0; }}

machine=fake-machine plan=fake-plan sysid=x kernel=x gov=x throttled=x
root_dev=x renderer=gl session=drm count="" timeout_s="" cores=""

pi_bench_once a
'''
        cp = bash(script, timeout=30)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("taskset", capture.read_text())


if __name__ == "__main__":
    unittest.main()
