"""`wk verify` proves that nothing in a workspace can publish.

The two things that could are an ssh key that pushes and a GitHub API token
that posts, and neither is a file inside the boundary any more: the keys are in
an ssh-agent outside it and the token is in the credential injector. So the
checks changed shape -- "there is no key in the mount" became "no key material
anywhere, the agent holds nothing, and an authenticated API call is refused" --
and the old claim that api.github.com is unreachable is retired, because it is
now reachable and unauthenticated.

The block is run for real rather than read: it is lifted out of cmd/verify and
given a stub `inside` that answers each probe from a table, a stub `wk` that
answers `push status`, and the same pass/fail/note the command uses. Every
check therefore has a passing case and a failing case, which is the point --
a check that cannot fail is not a check.

Run: python3 -m unittest tests.test_verify_credentials -v
"""
import unittest

from tests.support import REPO, WkTest, bash

VERIFY = (REPO / "cmd" / "verify").read_text()

# The block under test, by the two comment banners that bound it. Sliced
# rather than copied so that a check added to cmd/verify is a check this file
# runs -- and one whose behaviour drifts is a failure here.
START = "# --- the two credentials, and that neither one is in here"
END = "# --- GPU ---"

# Answers for every probe the block makes, keyed by a substring of the command
# it runs inside the workspace. A default per key is the healthy workspace; a
# test overrides one and asserts the check fails.
DEFAULTS = {
    "PRIVATE KEY": "",
    "ssh-add -l": "0",
    "https://api.github.com/ ": "200",
    "api.github.com/user": "401",
    "GITHUB_COM_TOKEN": "wk-injects-this",
    "hosts.yml": "",
    "command -v gh": "ABSENT",
}

ORDER = ("PRIVATE KEY", "ssh-add -l", "api.github.com/user",
         "https://api.github.com/ ", "GITHUB_COM_TOKEN", "hosts.yml",
         "command -v gh")


class _Block(WkTest):
    def block(self):
        i = VERIFY.index(START)
        j = VERIFY.index(END)
        return VERIFY[i:j]

    def run_block(self, push_on=False, answers=None, agent_sock="/run/wk/ssh-agent.sock"):
        table = dict(DEFAULTS)
        table.update(answers or {})

        # A `wk` that answers only `push status`: 0 is on, 1 is off, which is
        # the command's own contract (cmd/push).
        root = self.tmp / "root"
        root.mkdir()
        (root / "wk").write_text("#!/bin/sh\nexit %d\n" % (0 if push_on else 1))
        (root / "wk").chmod(0o755)

        cases = "\n".join(
            '        *"%s"*) printf "%%s" "%s" ;;' % (key, table[key])
            for key in ORDER)

        harness = f'''
set -u
WK_ROOT={root}
TARGET=container
NAME=demo
fails=0
pass() {{ printf "ok    %s\\n" "$*"; }}
fail() {{ printf "FAIL  %s\\n" "$*"; fails=$((fails + 1)); }}
note() {{ printf "      %s\\n" "$*"; }}
t_agent_sock() {{ {"echo " + agent_sock if agent_sock else "return 1"}; }}
inside() {{
    case "$*" in
{cases}
        *) printf "" ;;
    esac
}}
'''
        cp = bash(harness + self.block() + '\nprintf "fails=%s\\n" "$fails"\n')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout + cp.stderr

    def fails(self, out):
        for line in out.splitlines():
            if line.startswith("fails="):
                return int(line.split("=", 1)[1])
        raise AssertionError(out)


class TestAHealthyWorkspacePasses(_Block):
    def test_push_off_is_a_correct_state_and_every_check_passes(self):
        out = self.run_block(push_on=False)
        self.assertEqual(0, self.fails(out), out)
        self.assertIn("the host says push is OFF", out)
        self.assertIn("no private key material", out)
        self.assertIn("no identity reaches this workspace", out)
        self.assertIn("reachable through the injector", out)
        self.assertIn("refused (HTTP 401)", out)
        self.assertIn("is the placeholder", out)

    def test_push_on_is_the_other_correct_state(self):
        out = self.run_block(push_on=True,
                             answers={"ssh-add -l": "2",
                                      "api.github.com/user": "200"})
        self.assertEqual(0, self.fails(out), out)
        self.assertIn("the host says push is ON", out)
        self.assertIn("2 deploy key(s) reach this workspace", out)
        self.assertIn("an authenticated API call succeeds", out)


class TestKeyMaterial(_Block):
    def test_a_private_key_anywhere_readable_fails(self):
        out = self.run_block(answers={"PRIVATE KEY": "/home/u/.ssh/id_fork"})
        self.assertEqual(1, self.fails(out), out)
        self.assertIn("private key material inside the workspace", out)
        self.assertIn("id_fork", out)

    def test_the_checkout_is_deliberately_not_scanned(self):
        """WebKit ships PEM test fixtures of its own, so a recursive grep there
        reports the upstream tree rather than this boundary."""
        block = self.block()
        self.assertIn("/secrets /run/wk", block)
        self.assertNotIn("$(t_src", block)


class TestTheAgent(_Block):
    def test_an_identity_reaching_the_workspace_while_push_is_off_fails(self):
        out = self.run_block(push_on=False, answers={"ssh-add -l": "1"})
        self.assertEqual(1, self.fails(out), out)
        self.assertIn("push is off, yet 1 identity", out)
        self.assertIn("wk push off", out)

    def test_no_identity_while_push_is_on_fails(self):
        """The other direction, and it matters: a workspace whose socket is
        empty while the host says on is a `git push` that will fail at the
        door, reported as the sandbox holding when it is the plumbing."""
        out = self.run_block(push_on=True, answers={"api.github.com/user": "200"})
        self.assertEqual(1, self.fails(out), out)
        self.assertIn("no identity reaches", out)

    def test_a_workspace_with_no_ssh_add_fails_rather_than_reading_as_empty(self):
        """A missing tool would answer "no identities" and pass -- the same
        false pass the interface probe in cmd/verify guards against."""
        out = self.run_block(answers={"ssh-add -l": "MISSING"})
        self.assertEqual(1, self.fails(out), out)
        self.assertIn("no ssh-add in the workspace", out)

    def test_a_target_that_names_no_socket_fails(self):
        out = self.run_block(agent_sock="")
        self.assertGreaterEqual(self.fails(out), 1, out)
        self.assertIn("names no ssh-agent socket", out)


class TestTheApi(_Block):
    def test_an_unreachable_api_fails_because_the_injector_is_not_in_the_path(self):
        out = self.run_block(answers={"https://api.github.com/ ": "000"})
        self.assertEqual(1, self.fails(out), out)
        self.assertIn("the injector is not in the path", out)

    def test_an_authenticated_call_that_succeeds_while_push_is_off_fails(self):
        out = self.run_block(push_on=False, answers={"api.github.com/user": "200"})
        self.assertEqual(1, self.fails(out), out)
        self.assertIn("expected 401", out)

    def test_an_authenticated_call_that_is_refused_while_push_is_on_fails(self):
        out = self.run_block(push_on=True, answers={"ssh-add -l": "1",
                                                    "api.github.com/user": "401"})
        self.assertEqual(1, self.fails(out), out)
        self.assertIn("wk key set github-pat", out)


class TestThePlaceholder(_Block):
    def test_a_real_looking_token_fails_and_is_never_printed(self):
        """Printing what the workspace holds would print a token on the one
        run where this check matters."""
        out = self.run_block(answers={"GITHUB_COM_TOKEN": "ghp-a-real-one"})
        self.assertEqual(1, self.fails(out), out)
        self.assertIn("is not the placeholder", out)
        self.assertNotIn("ghp-a-real-one", out)

    def test_an_unset_token_fails_and_names_what_writes_it(self):
        out = self.run_block(answers={"GITHUB_COM_TOKEN": ""})
        self.assertEqual(1, self.fails(out), out)
        self.assertIn("/secrets/github-user", out)


class TestGh(_Block):
    def test_a_gh_credential_in_the_home_fails(self):
        out = self.run_block(answers={"hosts.yml": "/home/u/.config/gh/hosts.yml"})
        self.assertEqual(1, self.fails(out), out)
        self.assertIn("GitHub credential inside the workspace", out)

    def test_an_authenticated_gh_fails_because_it_bypasses_the_injector(self):
        out = self.run_block(answers={"command -v gh": "AUTHED"})
        self.assertEqual(1, self.fails(out), out)
        self.assertIn("bypasses the injector", out)

    def test_gh_present_but_logged_out_passes(self):
        out = self.run_block(answers={"command -v gh": "REFUSED"})
        self.assertEqual(0, self.fails(out), out)
        self.assertIn("not authenticated", out)


class TestBothTargetsAreMeasured(unittest.TestCase):
    def test_the_block_is_outside_the_container_only_guard(self):
        """A guest reaches the agent through an `ssh -R` and a container
        through a bind-mounted socket, but every probe is the same command run
        by t_exec -- so the credential checks are not inside the
        $WK_SANDBOX guard, which is about container properties."""
        guard = VERIFY.index('if [ "${WK_SANDBOX:-}" = rootless-proxy ]')
        block = VERIFY.index(START)
        self.assertGreater(block, guard)
        between = VERIFY[guard:block]
        # The guard's own `fi` closes before the block starts.
        self.assertIn("\nfi\n", between)

    def test_the_vm_arm_no_longer_says_it_measures_nothing(self):
        self.assertNotIn("vm workspaces have no in-guest checks yet", VERIFY)
        self.assertIn("the credential and GPU checks", VERIFY)


if __name__ == "__main__":
    unittest.main()
