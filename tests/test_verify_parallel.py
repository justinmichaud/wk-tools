"""`wk verify`: the probes run at once, and one of them is whether the agent in
the workspace can authenticate at all.

Two things are under test. The shape -- every probe that only reads is started
through lib/par.sh, and the two that write or that the others read are not --
and the probes themselves, driven one at a time against a fake `inside` the way
tests/test_claude_rc.py drives cmd/ai's rc_* functions (`WK_VERIFY_LIB=1`, the
guard cmd/verify defines for exactly this). A real container is what the whole
command needs, and tests/support cannot conjure one.

Run: python3 -m unittest tests.test_verify_parallel -v
"""
import re
import unittest

from tests.support import REPO, WkTest, bash

VERIFY = (REPO / "cmd" / "verify").read_text()

# The probes, and a fake driver contract for them: `inside` answers from a
# table the test writes, `t_agent_secret_remedy` says what a real target would.
PROBE = '''
set -euo pipefail
export WK_ROOT="__REPO__"
export WK_VERIFY_LIB=1
. "__REPO__/cmd/verify"

NAME=demo
TARGET=faketarget
inside() { printf '%s' "${WK_TEST_ANSWER:-}"; }
t_agent_secret_remedy() { printf "a remedy naming %s" "$2"; }
t_tools() { printf '/opt/wk-tools'; }
'''


# What `claude auth status` looks like coming back through a TTY, byte for
# byte from a running container workspace.
TTY_WRAPPED_LOGIN = (b'\x1b7\x1b[r\x1b8\x1b[?25h{\r\r\n\x1b[3G"loggedIn":'
                     b'\x1b[15Gtrue,\r\r\n\x1b[3G"authMethod":\x1b[17G"claude.ai"\r\r\n}\r\r\n'
                     b'\x1b[?25h\x1b[?1006l\x1b(B\x0f\x1b[>4m\x1b[<u\x1b[?1004l\x1b7\x1b[r\x1b8\x1b[?25h\n')


def probe(body, kind="container", env=None):
    """Run one probe with `inside` answering a fixed string. Its own fail count
    is the exit status, which is how cmd/verify totals them."""
    e = {"WK_TARGET_KIND": kind}
    e.update(env or {})
    script = PROBE.replace("__REPO__", str(REPO)) + body
    return bash(script, env=e)


class TestEveryReadingProbeRunsAtOnce(WkTest):
    """Run one at a time, the wall clock is the sum of seventeen execs, eight
    of them network probes whose timeouts sum to about two minutes -- and `wk
    ai claude`, `wk new` and `wk start` all wait for it."""

    def _par_run_names(self):
        return re.findall(r"^[ \t]*(?:\[.*\] \|\| )?par_run \S+ (probe_\w+)",
                          VERIFY, re.M)

    def test_every_probe_defined_is_started_through_par_run(self):
        defined = set(re.findall(r"^(probe_\w+)\(\)", VERIFY, re.M))
        self.assertTrue(defined)
        self.assertEqual(defined, set(self._par_run_names()),
                         "a probe is defined and never run, or run twice")

    def test_no_probe_is_called_in_this_shell(self):
        """A call outside par_run would write its verdicts to stderr in the
        middle of the parallel ones, and its fails would not be counted."""
        for name in set(re.findall(r"^(probe_\w+)\(\)", VERIFY, re.M)):
            calls = re.findall(r"^[ \t]*%s\b(?!\(\))" % name, VERIFY, re.M)
            self.assertEqual([], calls, "%s is called directly" % name)

    def test_the_write_probe_and_the_push_switch_stay_sequential(self):
        """One writes into the workspace and must be able to clean up after
        itself; the other is what two of the probes read."""
        self.assertIn(".wk-write-probe", VERIFY)
        self.assertNotIn("par_run", VERIFY.split(".wk-write-probe")[1])
        head, _, tail = VERIFY.partition("par_begin")
        self.assertIn('"$WK_ROOT/wk" push status', head,
                      "the push switch is read after the probes that need it")
        self.assertNotIn('"$WK_ROOT/wk" push status', tail)

    def test_the_total_is_the_sum_of_what_the_probes_counted(self):
        """Each job's exit status is its own fail count, so a probe that fails
        twice counts twice -- and a probe that dies counts at all."""
        self.assertIn('fails=$((fails + $2))', VERIFY)
        self.assertIn("par_wait", VERIFY)

    def test_nothing_is_left_behind_when_a_run_is_killed(self):
        self.assertIn("wk_atexit par_cleanup", VERIFY)


class TestAProbeThatDiesIsStillReported(WkTest):
    """The join reads each job's records and its exit status. A probe that dies
    under `set -e` has an exit status and no records, and counting the status
    alone puts a number in the total with no line saying what went unmeasured."""

    JOIN = VERIFY[VERIFY.index("par_wait\nset -- $_par_status"):
                  VERIFY.index("par_end\n\nif [")]

    def _join(self, status, records):
        arms = "\n".join("        %s) printf %s ;;" % (n, repr(r))
                         for n, r in records.items())
        harness = (
            "set -u\n"
            "NAME=demo\n"
            "fails=0\n"
            'fail() { printf "FAIL  %s\\n" "$*"; fails=$((fails + 1)); }\n'
            "par_record() {\n"
            '    case "$1" in\n'
            + arms + "\n"
            '        *) printf "" ;;\n'
            "    esac\n"
            "}\n"
            "par_end() { :; }\n"
            "_par_status=" + repr(status) + "\n")
        return bash(harness + self.JOIN
                    + '\nprintf "fails=%s\\n" "$fails"\n')

    def test_a_probe_that_wrote_nothing_and_failed_is_named(self):
        cp = self._join("gpu 1", {})
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("the 'gpu' probe died", cp.stdout)
        self.assertIn("fails=1", cp.stdout)

    def test_a_probe_that_reported_is_counted_from_its_own_records(self):
        cp = self._join("gpu 2", {"gpu": "FAIL  one\nFAIL  two\n"})
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertNotIn("died", cp.stdout)
        self.assertIn("fails=2", cp.stdout)

    def test_a_probe_that_passed_is_neither(self):
        cp = self._join("gpu 0", {"gpu": "ok    a gpu\n"})
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("fails=0", cp.stdout)
        self.assertNotIn("died", cp.stdout)


class TestAProbeCountsItsOwnFails(WkTest):
    def test_a_pass_is_zero_and_the_record_says_ok(self):
        """push is off and no identity reaches the workspace."""
        cp = probe('PUSH_ON=0; t_agent_sock() { printf /tmp/sock; }\n'
                   'probe_agent_identities 3>&1',
                   env={"WK_TEST_ANSWER": "0"})
        self.assertEqual(0, cp.returncode, cp.stdout)
        self.assertIn("a push is refused", cp.stdout)

    def test_a_fail_is_one_and_the_record_says_so(self):
        """push is off (the default here) and an identity reaches the
        workspace anyway."""
        cp = probe('PUSH_ON=0; t_agent_sock() { printf /tmp/sock; }\n'
                   'probe_agent_identities 3>&1',
                   env={"WK_TEST_ANSWER": "2"})
        self.assertEqual(1, cp.returncode, cp.stdout)
        self.assertIn("FAIL", cp.stdout)
        self.assertIn("wk push off", cp.stdout)


class TestWhetherTheAgentCanAuthenticateIsMeasured(WkTest):
    """The defect: a workspace was handed two Claude credentials, the token
    won, and every session asked to authenticate -- while `wk verify` said the
    sandbox was intact, because nothing asked the agent anything.

    `claude auth status` is the CLI's own report of the credential it found.
    Measured against 2.1.268: it is local -- it says loggedIn for a token
    Anthropic has never seen -- so it is evidence of delivery and method, and
    `wk key check` is what asks Anthropic."""

    LOGGED_IN = '{"loggedIn": true, "authMethod": "claude.ai"}'
    TOKEN = '{"loggedIn": true, "authMethod": "oauth_token"}'
    OUT = '{"loggedIn": false}'

    def _with_token(self, answer, token, kind="container"):
        """`inside` is asked two things: whether the token is in the
        environment, and what the CLI says."""
        body = ('inside() { case "$*" in *CLAUDE_CODE_OAUTH_TOKEN*) '
                'printf %s "$WK_TEST_TOKEN" ;; *) printf %s "$WK_TEST_ANSWER" ;; '
                'esac; }\nprobe_agent_credential 3>&1')
        return probe(body, kind=kind,
                     env={"WK_TEST_ANSWER": answer, "WK_TEST_TOKEN": token})

    def test_a_container_with_the_login_and_nothing_else_passes(self):
        cp = self._with_token(self.LOGGED_IN, "")
        self.assertEqual(0, cp.returncode, cp.stdout)
        self.assertIn("authenticated", cp.stdout)
        self.assertIn("claude-login", cp.stdout)

    def test_a_container_that_also_has_the_token_fails_and_says_which_wins(self):
        """Both arrive, the token takes precedence, and remote control refuses
        the token: the state the delivery column exists to prevent."""
        cp = self._with_token(self.TOKEN, "set")
        self.assertEqual(2, cp.returncode, cp.stdout)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", cp.stdout)
        self.assertIn("the token wins", cp.stdout)

    def test_a_guest_is_the_other_way_round(self):
        """A vm is given the token and no login, so the token is what must be
        there and claude.ai is what would be wrong."""
        cp = self._with_token(self.TOKEN, "set", kind="vm")
        self.assertEqual(0, cp.returncode, cp.stdout)
        cp = self._with_token(self.TOKEN, "", kind="vm")
        self.assertEqual(1, cp.returncode, cp.stdout)
        self.assertIn("no $CLAUDE_CODE_OAUTH_TOKEN", cp.stdout)

    def test_a_workspace_that_would_stop_at_login_fails_with_the_remedy(self):
        cp = self._with_token(self.OUT, "")
        self.assertEqual(1, cp.returncode, cp.stdout)
        self.assertIn("not logged in", cp.stdout)
        self.assertIn("a remedy naming claude-login", cp.stdout)

    def test_a_cli_that_answers_nothing_is_unmeasured_and_said_to_be(self):
        cp = self._with_token("", "")
        self.assertEqual(1, cp.returncode, cp.stdout)
        self.assertIn("claude --version", cp.stdout)
        self.assertIn("It answered (first 80 bytes): b''", cp.stdout,
                      "an unreadable answer is quoted, not guessed at")

    def test_a_cli_that_is_not_there_is_said_to_be_missing_not_unreadable(self):
        """Two faults, two messages: nothing to run is not the same as
        something that answered in a shape this cannot read."""
        cp = self._with_token("wk-no-claude-cli", "")
        self.assertEqual(1, cp.returncode, cp.stdout)
        self.assertIn("no 'claude' on $PATH", cp.stdout)
        self.assertNotIn("It answered", cp.stdout)

    def test_the_terminal_control_sequences_a_tty_wraps_the_json_in_are_read_through(self):
        """`inside` reaches a container through `wkdev-enter --exec`, which
        allocates a TTY, so the CLI pretty-prints: cursor save/restore, a
        scroll-region reset, a column move per line and CRLF line endings. The
        exact bytes measured from a running workspace."""
        cp = self._with_token(
            TTY_WRAPPED_LOGIN.decode("utf-8", "surrogateescape"), "")
        self.assertEqual(0, cp.returncode, cp.stdout)
        self.assertIn("authenticated", cp.stdout)
        self.assertIn("claude-login", cp.stdout)


class TestGitWebkitSetupIsMeasured(WkTest):
    """`git-webkit setup` runs once, at a container's first start or in a
    guest's golden base, and a GitHub request that fails there leaves the
    checkout with no hooks and no fork remote for `git-webkit pr` -- measured
    once, "Is your API token out of date?" 43 s after the egress bridge came
    up, while the same command later in the same container succeeded. Nothing
    re-runs it, so the marker it writes is what says whether it finished."""

    def test_the_marker_being_true_is_the_pass(self):
        cp = probe("probe_gitwebkit_setup 3>&1", env={"WK_TEST_ANSWER": "true"})
        self.assertEqual(0, cp.returncode, cp.stdout)
        self.assertIn("git-webkit is set up in 'demo'", cp.stdout)

    def test_no_marker_fails_and_names_the_one_command_that_converges_it(self):
        cp = probe("probe_gitwebkit_setup 3>&1", env={"WK_TEST_ANSWER": ""})
        self.assertEqual(1, cp.returncode, cp.stdout)
        self.assertIn("FAIL", cp.stdout)
        self.assertIn("has not completed", cp.stdout)
        self.assertIn("wk remotes demo --fix", cp.stdout)

    def test_a_half_written_marker_is_not_true_either(self):
        cp = probe("probe_gitwebkit_setup 3>&1", env={"WK_TEST_ANSWER": "false"})
        self.assertEqual(1, cp.returncode, cp.stdout)

    def test_it_asks_the_checkout_the_other_probes_use(self):
        cp = probe('inside() { echo "$*" >&2; printf true; }\n'
                   "probe_gitwebkit_setup 3>&1")
        self.assertEqual(0, cp.returncode, cp.stdout)
        self.assertIn("git -C /src/WebKit config --get webkitscmpy.setup", cp.stderr)


if __name__ == "__main__":
    unittest.main()
