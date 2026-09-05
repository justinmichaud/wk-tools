"""`wk verify` proves that nothing in a workspace can publish.

A read is authenticated whatever position the switch is in and a write only
while push is on, so what the checks measure is that split. The two things that
could publish are an ssh key that pushes and a GitHub API token that posts, and
neither is a file inside the boundary: the keys are in an ssh-agent outside it
and the tokens are in the credential injector. So the workspace is asked
whether a read is authenticated and whether a write is authenticated only while
the switch is on.

The block is run for real rather than read: it is lifted out of cmd/verify and
given a stub `inside` that answers each probe from a table, a stub `wk` that
answers `push status`, stubs for the two store functions it asks about this
device, and the same pass/fail/note the command uses. Every check therefore has
a passing case and a failing case, which is the point -- a check that cannot
fail is not a check.

Run: python3 -m unittest tests.test_verify_credentials -v
"""
import unittest

from tests.support import REPO, WkTest, bash, func_body

VERIFY = (REPO / "cmd" / "verify").read_text()

# The block under test is cmd/verify's own credential_checks(), lifted whole
# rather than copied, so a check added there is a check this file runs and one
# whose behaviour drifts is a failure here. A function boundary, not a pair of
# statements to slice between: those have to keep being spelled that way, and
# adding a branch elsewhere in the file that happened to contain one of them
# emptied this block into a syntax error.

# Answers for every probe the block makes, keyed by a substring of the command
# it runs inside the workspace. The defaults are a healthy workspace with the
# switch off on a device that holds a token; a test overrides one and asserts
# the check fails.
DEFAULTS = {
    "PRIVATE KEY": "",
    "ssh-add -l": "0",
    "https://api.github.com/ ": "200",
    "api.github.com/user": "200",
    "/pulls": "401",
    "GITHUB_COM_TOKEN": "wk-injects-this",
    "GH_TOKEN": "wk-injects-this",
    "hosts.yml": "",
}

ORDER = ("PRIVATE KEY", "ssh-add -l", "api.github.com/user",
         "https://api.github.com/ ", "/pulls",
         "GITHUB_COM_TOKEN", "GH_TOKEN", "hosts.yml")

# The one fork the stubbed wk_push_forks names, which is what the write probe
# addresses.
FORK = "wkuser/WebKit"


class _Block(WkTest):
    def block(self):
        return func_body(VERIFY, "credential_checks")

    def run_block(self, push_on=False, answers=None, stored_pat="ghp-stored-here",
                  agent_sock="/run/wk/ssh-agent.sock"):
        table = dict(DEFAULTS)
        table.update(answers or {})

        # A `wk` that answers only `push status`: 0 is on, 1 is off, which is
        # the command's own contract (cmd/push).
        root = self.tmp / "root"
        root.mkdir(exist_ok=True)
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
wk_github_pat() {{ printf %s "{stored_pat}"; }}
wk_push_forks() {{ printf "%s\\n" "fork {FORK} github-webkit"; }}
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
        self.assertIn("a read is authenticated (HTTP 200)", out)
        self.assertIn("a write is unauthenticated (HTTP 401)", out)
        self.assertIn("GITHUB_COM_TOKEN in the workspace is the placeholder", out)
        self.assertIn("GH_TOKEN in the workspace is the placeholder", out)

    def test_push_on_is_the_other_correct_state(self):
        out = self.run_block(push_on=True,
                             answers={"ssh-add -l": "2", "/pulls": "422"})
        self.assertEqual(0, self.fails(out), out)
        self.assertIn("the host says push is ON", out)
        self.assertIn("2 deploy key(s) reach this workspace", out)
        self.assertIn("a write is authenticated (HTTP 422", out)

    def test_a_device_with_no_token_stored_is_a_correct_state_too(self):
        """No `wk key set github-pat` anywhere: reads answer 401, which is a
        workspace on public GitHub at 60 requests an hour, not a fault."""
        out = self.run_block(stored_pat="", answers={"api.github.com/user": "401"})
        self.assertEqual(0, self.fails(out), out)
        self.assertIn("reads are unauthenticated (HTTP 401)", out)


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


class TestTheKeyScanRunsForReal(WkTest):
    """The command the block hands `inside`, run against a real directory tree
    shaped like a workspace -- because what it does *not* walk is the point,
    and a table of canned answers cannot show that.

    A guest's checkout is inside its home (t_src is /Users/<user>/WebKit), and
    WebKit ships PEM fixtures, so a recursive grep of home fails every guest
    and walks gigabytes doing it."""

    PEM = "-----BEGIN OPENSSH PRIVATE KEY-----\nnot a real key\n"

    def scan(self):
        """The one command cmd/verify runs, lifted rather than retyped."""
        i = VERIFY.index("material=$(inside \"")
        j = VERIFY.index('")\n', i)
        cmd = VERIFY[i + len('material=$(inside "'):j]
        return cmd.replace('\\$', '$')

    def home(self):
        h = self.tmp / "home"
        (h / "WebKit" / "Source" / "WebKit" / "test").mkdir(parents=True)
        (h / "WebKit" / "Source" / "WebKit" / "test" / "cert.pem").write_text(self.PEM)
        (h / ".ssh").mkdir()
        return h

    def run_scan(self, home):
        cp = self.bash("HOME=%s\n%s" % (home, self.scan()))
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout.strip()

    def test_a_pem_in_the_checkout_is_not_reported(self):
        home = self.home()
        self.assertEqual("", self.run_scan(home),
                         "the scan walked the checkout, so every guest fails it")

    def test_a_key_in_dot_ssh_is_reported(self):
        home = self.home()
        (home / ".ssh" / "id_fork").write_text(self.PEM)
        self.assertIn("id_fork", self.run_scan(home))

    def test_a_key_dropped_at_the_top_of_home_is_reported(self):
        home = self.home()
        (home / "leaked_key").write_text(self.PEM)
        self.assertIn("leaked_key", self.run_scan(home))

    def test_a_credential_file_under_dot_claude_is_reported(self):
        home = self.home()
        (home / ".claude" / "deep").mkdir(parents=True)
        (home / ".claude" / "deep" / "k").write_text(self.PEM)
        self.assertIn("k", self.run_scan(home))

    def test_it_never_names_the_checkout_and_so_needs_no_exclusion(self):
        """Excluding t_src by name would be a second place the checkout's path
        is decided; not recursing into home is the same guarantee with nothing
        to keep in step."""
        block = _Block.block(self)
        self.assertNotIn("$(t_src", block)
        self.assertNotIn("t_mirror_dir", block)
        self.assertNotIn(r"grep -rl 'PRIVATE KEY' \$HOME ", block)


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
        out = self.run_block(push_on=True, answers={"/pulls": "422"})
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


class TestAReadIsAuthenticatedFromTheStandingToken(_Block):
    """Reading is open whatever position the switch is in, and the probe reads
    the answer rather than a store: the token lives where the injector reads
    one -- this machine for a container, the macOS host for a guest -- and
    `wk verify` runs on neither. Measured 2026-09-04: asking `wk_github_pat`
    from inside the podman machine always reads empty, because push-keys is
    the one store directory nothing mounts."""

    def test_an_unauthenticated_read_is_reported_but_is_not_a_failure(self):
        """A device with no token at all is a legitimate state: reads still
        work, rate-limited and public-only, and the note says so."""
        out = self.run_block(answers={"api.github.com/user": "401"})
        self.assertEqual(0, self.fails(out), out)
        self.assertIn("reads are unauthenticated (HTTP 401)", out)
        self.assertIn("wk key set github-pat", out)
        self.assertIn("./setup", out)

    def test_neither_arm_asks_this_device_for_a_token(self):
        self.assertNotIn("wk_github_pat", self.block())

    def test_any_other_answer_is_the_injector_not_answering(self):
        out = self.run_block(answers={"api.github.com/user": "000"})
        self.assertEqual(1, self.fails(out), out)
        self.assertIn("rather than 200 or 401", out)

    def test_the_switch_does_not_govern_it(self):
        """The same 200 with push on and with push off: a check that answered
        differently would be measuring the write token."""
        for push_on, pulls in ((True, "422"), (False, "401")):
            with self.subTest(push_on=push_on):
                out = self.run_block(push_on=push_on, answers={"/pulls": pulls,
                                                               "ssh-add -l": "2" if push_on else "0"})
                self.assertEqual(0, self.fails(out), out)
                self.assertIn("a read is authenticated (HTTP 200)", out)


class TestTheSwitch(_Block):
    def test_a_write_that_succeeds_while_push_is_off_fails(self):
        out = self.run_block(push_on=False, answers={"/pulls": "422"})
        self.assertEqual(1, self.fails(out), out)
        self.assertIn("a write token is still on the machine", out)
        self.assertIn("wk push off", out)

    def test_a_write_that_is_refused_while_push_is_on_fails(self):
        out = self.run_block(push_on=True, answers={"ssh-add -l": "1",
                                                    "/pulls": "401"})
        self.assertEqual(1, self.fails(out), out)
        self.assertIn("the injector has no write token", out)

    def test_the_probe_cannot_create_a_pull_request(self):
        """An empty body names no head or base branch, which is why a 422 is
        the authenticated answer and nothing is created by measuring."""
        block = self.block()
        self.assertIn("-X POST -d '{}' https://api.github.com/repos/$FORK/pulls", block)


class TestThePlaceholders(_Block):
    """Both variables, because `git-webkit` sends one and `gh` the other, and
    a request with no Authorization header has nothing for the injector to
    replace."""

    def test_a_real_looking_token_fails_and_is_never_printed(self):
        """Printing what the workspace holds would print a token on the one
        run where this check matters."""
        for var in ("GITHUB_COM_TOKEN", "GH_TOKEN"):
            with self.subTest(var=var):
                out = self.run_block(answers={var: "ghp-a-real-one"})
                self.assertEqual(1, self.fails(out), out)
                self.assertIn("%s in the workspace is not the placeholder" % var, out)
                self.assertNotIn("ghp-a-real-one", out)

    def test_an_unset_token_fails_and_names_what_exports_it(self):
        for var in ("GITHUB_COM_TOKEN", "GH_TOKEN"):
            with self.subTest(var=var):
                out = self.run_block(answers={var: ""})
                self.assertEqual(1, self.fails(out), out)
                self.assertIn("%s is unset in here" % var, out)
                self.assertIn("container/proxy/ensure-bridge.sh", out)


class TestAStoredGhCredential(_Block):
    def test_a_gh_credential_in_the_home_fails(self):
        out = self.run_block(answers={"hosts.yml": "/home/u/.config/gh/hosts.yml"})
        self.assertEqual(1, self.fails(out), out)
        self.assertIn("GitHub credential inside the workspace", out)

    def test_the_placeholder_gh_reads_is_not_itself_a_finding(self):
        """`gh` holding GH_TOKEN is the arrangement working: the injector
        replaces it. What is scanned for is a credential nothing put there."""
        block = self.block()
        self.assertIn("GITHUB_TOKEN|GH_ENTERPRISE_TOKEN", block)
        self.assertNotIn("gh auth status", block)


class TestWhatAnAgentInHereCanSpend(WkTest):
    """The blast-radius note, lifted and run: whether this workspace has the
    claude.ai login is a question for the target driver, not for this
    machine's store -- a guest holds one of its own and is never handed the
    host's (t_agent_secret_present, lib/target.sh)."""

    def run_note(self, present):
        block = func_body(VERIFY, "blast_radius_note")
        harness = f'''
set -u
NAME=demo
note() {{ printf "      %s\\n" "$*"; }}
t_agent_secret_present() {{ printf "asked %s about %s\\n" "$1" "$2" >&2; [ {present} = yes ]; }}
t_agent_secret_remedy()  {{ printf "the remedy for %s" "$2"; }}
'''
        cp = bash(harness + block)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout + cp.stderr

    def test_it_asks_the_target_about_this_workspace(self):
        out = self.run_note("yes")
        self.assertIn("asked demo about claude-login", out, out)

    def test_a_workspace_that_has_one_is_reported_as_account_scope(self):
        out = self.run_note("yes")
        self.assertIn("account scope", out, out)

    def test_a_workspace_without_one_gets_the_targets_remedy(self):
        """Not one baked sentence: a container's remedy is `wk key set` here
        and a guest's is a login in there."""
        out = self.run_note("no")
        self.assertIn("the remedy for claude-login", out, out)
        self.assertIn("remote control refuses", out, out)


class TestBothTargetsAreMeasured(unittest.TestCase):
    def test_the_block_is_outside_the_container_only_guard(self):
        """A guest reaches the agent through an `ssh -R` and a container
        through a bind-mounted socket, but every probe is the same command run
        by t_exec -- so the credential checks are not inside the
        $WK_SANDBOX guard, which is about container properties."""
        guard = VERIFY.index('if [ "${WK_SANDBOX:-}" = rootless-proxy ]')
        block = VERIFY.index("credential_checks() {")
        self.assertGreater(block, guard)
        between = VERIFY[guard:block]
        # The guard's own `fi` closes before the block starts.
        self.assertIn("\nfi\n", between)

    def test_a_guest_is_not_excluded_from_the_network_checks(self):
        """A guest used to be told, in a warning, that its egress was somebody
        else's business -- so `wk verify` printed "sandbox intact" for a guest
        whose network nothing had measured. Softnet runs on this host, but what
        it produces is measured from inside the guest like every other
        target's."""
        self.assertNotIn("vm workspaces have no in-guest checks yet", VERIFY)
        self.assertNotIn("not the network ones", VERIFY)
        gate = VERIFY.index("github reachable through the proxy")
        self.assertNotIn('WK_TARGET_KIND" = vm', VERIFY[:gate])


if __name__ == "__main__":
    unittest.main()
