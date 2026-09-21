"""`wk ai claude` typed *inside* a workspace.

A session in a workspace starts the same way whether it is driven from the host
or from in there, so `claude` in a workspace shell is a function that calls this
command (shell/bashrc) and the command no longer refuses the `local` target.
What differs is that the host's push switch cannot be thrown from inside, so it
is measured beside the sandbox probes rather than thrown -- and every one of
them runs at once, because a person waiting to start a session waits for all of
it.

checks_here is driven against a scratch $WK_ROOT whose cmd/verify is a stub:
every path in the tree is the real one except that file, so the assembly under
test is cmd/ai's and the probes it names are whatever the stub makes them. A
real container is what the probes themselves need, and tests/support cannot
conjure one (tests/test_verify_credentials.py drives those).

Run: python3 -m unittest tests.test_ai_inside -v
"""
import platform
import re
import time
import unittest

from tests.support import REPO, WkTest, bash

AI = (REPO / "cmd" / "ai").read_text()
BASHRC = (REPO / "shell" / "bashrc").read_text()

# Stands in for cmd/verify's library half: the reporting helpers cmd/ai's
# probes and its own totalling use, lib/par.sh, and one stub per probe
# checks_here names. Each sleeps, so a serial assembly is visible on the clock,
# fails as many times as $WK_TEST_FAIL_<name> says, and exits reporting nothing
# under $WK_TEST_DIE_<name>.
FAKE_VERIFY = '''#!/usr/bin/env bash
exec 3>&2
fails=0
pass() { printf '  ok    %s\\n' "$*" >&3; }
fail() { printf '  FAIL  %s\\n' "$*" >&3; fails=$((fails + 1)); }
note() { printf '        %s\\n' "$*" >&3; }
inside() { :; }
. "$WK_ROOT/lib/par.sh"

_stub() {
    local n="$1" want die
    eval "want=\\${WK_TEST_FAIL_$1:-0}"
    eval "die=\\${WK_TEST_DIE_$1:-}"
    sleep "${WK_TEST_PROBE_SECS:-0}"
    [ -z "$die" ] || exit 9
    local i=0
    while [ "$i" -lt "$want" ]; do fail "$n went wrong"; i=$((i + 1)); done
    [ "$want" -gt 0 ] || pass "$n is fine"
    return "$want"
}
for _p in push_here github_api bugzilla_api github allowlist off_allowlist \\
          isolation no_credentials_inside gitwebkit_setup commit_wall; do
    eval "probe_$_p() { _stub $_p; }"
done
[ "${WK_VERIFY_LIB:-}" != 1 ] || return 0
echo "the real wk verify would have run" >&2
'''


class _Inside(WkTest):
    def _root(self):
        root = self.tmp / "root"
        root.mkdir(exist_ok=True)
        for p in REPO.iterdir():
            if p.name != "cmd" and not (root / p.name).exists():
                (root / p.name).symlink_to(p)
        cmd = root / "cmd"
        cmd.mkdir(exist_ok=True)
        for p in (REPO / "cmd").iterdir():
            if p.name != "verify" and not (cmd / p.name).exists():
                (cmd / p.name).symlink_to(p)
        v = cmd / "verify"
        v.write_text(FAKE_VERIFY)
        v.chmod(0o755)
        return root

    def _checks(self, env=None, secs="0"):
        root = self._root()
        marker = self.tmp / "wk-marker"
        src = self.tmp / "src"
        src.mkdir(exist_ok=True)
        marker.write_text(f"name=demo\nsrc={src}\n")
        e = {
            "WK_ROOT": str(root),
            "WK_MARKER": str(marker),
            "WK_TARGET": "local",
            "WK_NAME": "demo",
            "WK_LOCAL_STORE": str(self.tmp / "state"),
            "WK_STORE": str(self.tmp / "store"),
            "WK_HOST_SECRETS": str(self.tmp / "secrets"),
            "WK_TEST_PROBE_SECS": secs,
        }
        e.update(env or {})
        return bash(f'''
set -euo pipefail
export WK_CLAUDE_LIB=1
. "{root}/cmd/ai"
NAME=demo
load_target local
checks_here
''', env=e)


class TestTheCommandRunsInAWorkspace(unittest.TestCase):
    def test_the_local_target_is_no_longer_refused(self):
        self.assertNotIn("already inside workspace", AI)

    def test_the_local_target_runs_the_in_workspace_checks(self):
        case = AI[AI.index('case "$WK_TARGET_KIND" in\ncontainer|vm)'):]
        case = case[:case.index("\nesac\n")]
        self.assertIn("local)", case)
        self.assertIn("checks_here", case)

    def test_the_commit_wall_covers_a_session_started_from_inside(self):
        """The wall is a property of the container, not of which side the
        command was typed on, and bwrap is what applies it."""
        body = AI[AI.index("wall_applies() {"):]
        body = body[:body.index("\n}\n")]
        self.assertIn("container)", body)
        self.assertIn("local)", body)


class TestEveryCheckRunsAtOnce(_Inside):
    def test_the_probes_are_all_started_through_par_run(self):
        body = AI[AI.index("checks_here() {"):]
        body = body[:body.index("\n}\n")]
        named = re.findall(r"par_run \S+\s+(probe_\w+)", body)
        self.assertEqual(len(named), len(set(named)), named)
        self.assertIn("probe_push_here", named)
        for direct in named:
            self.assertNotRegex(body, rf"^\s*{direct}\s*$")

    def test_the_wall_clock_is_the_slowest_probe_and_not_the_sum(self):
        started = time.monotonic()
        cp = self._checks(secs="0.6")
        elapsed = time.monotonic() - started
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertLess(elapsed, 3.0, f"{elapsed:.1f}s for 10 probes of 0.6s each")

    def test_every_probes_verdict_is_reported(self):
        cp = self._checks()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for name in ("push_here", "github_api", "bugzilla_api", "isolation",
                     "github"):
            self.assertIn(f"{name} is fine", cp.stderr)
        self.assertIn("nothing in here can publish", cp.stderr)

    def test_the_commit_wall_is_probed_exactly_where_it_applies(self):
        """The wall is bwrap's read-only .git, and bwrap is Linux's:
        `wall_applies` (cmd/ai) says a `local` workspace on macOS has no wall,
        and checks_here probes what applies rather than the same ten
        everywhere. This runs against the `local` target, so the platform
        under the test is this machine's."""
        cp = self._checks()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        if platform.system() == "Darwin":
            self.assertNotIn("commit_wall", cp.stderr,
                             "a wall that cannot be applied was reported on")
        else:
            self.assertIn("commit_wall is fine", cp.stderr)


class TestWhatEachKindOfFailureDoes(_Inside):
    def test_a_sandbox_failure_is_a_barrier(self):
        """The same verdict `wk ai claude <ws>` reaches from the host: it
        refuses, and an explicit --force crosses it."""
        cp = self._checks(env={"WK_TEST_FAIL_isolation": "1"})
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("the sandbox around 'demo' is not intact", cp.stderr)
        self.assertIn("--force", cp.stderr)

        forced = self._checks(env={"WK_TEST_FAIL_isolation": "1", "WK_FORCE": "1"})
        self.assertEqual(forced.returncode, 0, forced.stdout + forced.stderr)

    def test_a_way_to_publish_is_not_forceable(self):
        """A session that starts with a working push is the failure the whole
        arrangement exists to prevent, and nothing in here could fix it. All
        three ways count: a key that signs, a GitHub write, a Bugzilla write."""
        for env in ({"WK_TEST_FAIL_push_here": "1"},
                    {"WK_TEST_FAIL_github_api": "1"},
                    {"WK_TEST_FAIL_bugzilla_api": "1"},
                    {"WK_TEST_FAIL_push_here": "1", "WK_FORCE": "1"}):
            with self.subTest(env=env):
                cp = self._checks(env=env)
                self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                self.assertIn("could publish", cp.stderr)
                self.assertIn("wk push off", cp.stderr)

    def test_a_probe_that_dies_without_reporting_is_named(self):
        """Unmeasured is not the same as passed: a probe killed by its own
        `set -e` leaves an exit status and no record, and a count that only
        added the status would show a number with no FAIL line under it."""
        cp = self._checks(env={"WK_TEST_DIE_isolation": "1"})
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("the 'isolation' probe died before it reported anything",
                      cp.stderr)
        self.assertIn("the sandbox around 'demo' is not intact", cp.stderr)

    def test_a_publishing_probe_that_dies_is_not_forceable_either(self):
        cp = self._checks(env={"WK_TEST_DIE_push_here": "1", "WK_FORCE": "1"})
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("could publish", cp.stderr)


class TestTypingClaudeInAWorkspaceGoesThroughIt(unittest.TestCase):
    def _fn(self):
        m = re.search(r"claude\(\) \{ wk ai claude \"\$@\"; \}", BASHRC)
        self.assertIsNotNone(m, "shell/bashrc defines no claude function")
        start = BASHRC.rindex("case $- in", 0, m.start())
        return BASHRC[start:BASHRC.index("esac", m.end())]

    def test_it_is_defined_only_in_a_workspace(self):
        self.assertIn('[ -f "$HOME/.wk-workspace" ]', self._fn())

    def test_it_is_defined_only_for_an_interactive_shell(self):
        """The start itself runs the CLI from `bash -lc`, where this must not
        be defined or the command would call itself."""
        self.assertTrue(self._fn().startswith("case $- in"), self._fn())
        self.assertIn("*i*)", self._fn())

    def test_a_shell_with_no_wk_still_gets_a_claude(self):
        self.assertIn("command -v wk", self._fn())


class TestTheFunctionDoesNotShadowTheRealCli(WkTest):
    """Driven for real: the function answers an interactive shell and nothing
    else, so cmd/ai's own `bash -lc ... exec claude` finds the CLI rather than
    calling this command again."""

    def _what_claude_is(self, interactive, workspace=True):
        home = self.tmp / "home"
        (home / ".local" / "bin").mkdir(parents=True, exist_ok=True)
        if workspace:
            (home / ".wk-workspace").write_text("name=demo\nsrc=/src\n")
        real = home / ".local" / "bin" / "claude"
        real.write_text("#!/bin/sh\necho REAL-CLI\n")
        real.chmod(0o755)
        flags = "-ic" if interactive else "-c"
        cp = bash(
            f'bash {flags} \'. "{REPO}/shell/bashrc" >/dev/null 2>&1; '
            f'type -t claude; type claude\' 2>/dev/null',
            env={"HOME": str(home), "NO_ZSH": "1", "TERM": "dumb",
                 "PATH": f"{home}/.local/bin:/usr/bin:/bin"})
        return cp.stdout

    def test_an_interactive_shell_in_a_workspace_gets_the_measured_start(self):
        out = self._what_claude_is(interactive=True)
        self.assertIn("function", out)
        self.assertIn("wk ai claude", out)

    def test_a_non_interactive_shell_gets_the_cli(self):
        out = self._what_claude_is(interactive=False)
        self.assertIn("file", out)
        self.assertNotIn("wk ai claude", out)

    def test_an_interactive_shell_that_is_not_a_workspace_gets_the_cli(self):
        """This workstation sources the same rc, and `claude` here is a
        session on this machine."""
        out = self._what_claude_is(interactive=True, workspace=False)
        self.assertIn("file", out)
        self.assertNotIn("wk ai claude", out)


if __name__ == "__main__":
    unittest.main()
