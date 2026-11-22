import os
import sys
import tempfile
import unittest

from wk.agent.wrapper import HOLDS, LEAKS, UNKNOWN, minimal, variables, verdict, wrap
from wk.core.machine import Local, Result

ENV = {"PATH": "/s:/bin", "HOME": "/home/u", "WK_AGENT": "1"}
SET = ["HOME=/home/u", "PATH=/s:/bin", "WK_AGENT=1"]
DUMP = Result(0, "HOME=/home/u\0PATH=/s:/bin\0WK_AGENT=1\0")
EMPTY = Result(0, "")


class TestWrap(unittest.TestCase):
    def test_linux_mounts_an_empty_directory_over_each_hidden_path_and_sets_exactly_the_environment(self):
        self.assertEqual(wrap("linux", ["claude", "-r"], ["/w/secrets", "/other"], ENV),
                         ["bwrap", "--dev-bind", "/", "/", "--tmpfs", "/w/secrets", "--tmpfs", "/other", "--clearenv",
                          "--setenv", "HOME", "/home/u", "--setenv", "PATH", "/s:/bin", "--setenv", "WK_AGENT", "1", "--", "claude", "-r"])

    def test_mac_denies_reads_under_each_hidden_path_and_starts_from_an_empty_environment(self):
        self.assertEqual(wrap("mac", ["claude", "-r"], ["/w/secrets"], ENV),
                         ["sandbox-exec", "-p", '(version 1)(allow default)(deny file-read* (subpath "/w/secrets"))',
                          "env", "-i"] + SET + ["claude", "-r"])

    def test_a_mac_path_is_quoted_as_a_seatbelt_string(self):
        profile = wrap("mac", ["true"], ['/w/a "b"\\c d', "/e"], {})[2]
        self.assertEqual(profile, '(version 1)(allow default)(deny file-read* (subpath "/w/a \\"b\\"\\\\c d"))(deny file-read* (subpath "/e"))')

    def test_a_borrowed_box_holds_no_credential_so_its_wrapper_is_the_minimal_environment_alone(self):
        self.assertEqual(wrap("borrowed", ["pi"], ["/w/secrets"], ENV), ["env", "-i"] + SET + ["pi"])


class TestEnvironment(unittest.TestCase):
    def test_an_env_dump_is_read_whole_with_values_that_hold_equals_signs_and_newlines(self):
        self.assertEqual(variables("A=1\0B=x=y\nC=3\0\0"), {"A": "1", "B": "x=y\nC=3"})

    def test_only_the_allowlist_and_the_locale_survive(self):
        kept = {"HOME": "/h", "USER": "u", "LOGNAME": "u", "SHELL": "/bin/zsh", "PATH": "/bin", "TERM": "xterm", "LANG": "C",
                "TZ": "UTC", "LC_ALL": "C", "LC_CTYPE": "C"}
        dropped = {"SSH_AUTH_SOCK": "/tmp/a", "GITHUB_TOKEN": "t", "BUGS_WEBKIT_ORG_PASSWORD": "p", "HTTPS_PROXY": "h",
                   "WK_BUILD": "1", "ANTHROPIC_API_KEY": "k", "XLC_": "1"}
        self.assertEqual(minimal(dict(kept, **dropped)), kept)


class TestVerdict(unittest.TestCase):
    def check(self, want, listing, dump=DUMP):
        self.assertEqual(verdict([("/w/secrets", listing)], dump, ENV), want)

    def test_an_empty_listing_and_only_the_environment_given_holds(self):
        self.check((HOLDS, ""), EMPTY)

    def test_a_listing_ls_itself_was_refused_holds(self):
        for said in ("ls: /w/secrets: Operation not permitted\n", "ls: cannot open directory '/w/secrets': Permission denied\n",
                     "ls: cannot access '/w/secrets': No such file or directory\n"):
            self.check((HOLDS, ""), Result(1, "", said))

    def test_the_working_directory_bwrap_keeps_is_not_a_leak(self):
        self.check((HOLDS, ""), EMPTY, Result(0, DUMP.out + "PWD=/w/ws/x/src\0"))

    def test_a_listing_with_a_name_in_it_leaks(self):
        self.check((LEAKS, "/w/secrets is readable under the wrapper: it lists env.sh, github-token"), Result(0, "env.sh\ngithub-token\n"))

    def test_a_variable_that_was_not_given_leaks_and_the_ssh_agent_is_one(self):
        self.check((LEAKS, "the environment under the wrapper carries GITHUB_TOKEN, SSH_AUTH_SOCK"),
                   EMPTY, Result(0, DUMP.out + "SSH_AUTH_SOCK=/tmp/a\0GITHUB_TOKEN=t\0"))

    def test_a_wrapper_that_died_is_unknown_even_when_it_says_permission_denied(self):
        self.check((UNKNOWN, "the stand-in could not list /w/secrets (exit 1): bwrap: setting up uid map: Permission denied"),
                   Result(1, "", "bwrap: setting up uid map: Permission denied\n"))

    def test_a_listing_that_failed_for_another_reason_is_unknown(self):
        self.check((UNKNOWN, "the stand-in could not list /w/secrets (exit 124): timed out after 30s"), Result(124, "", "timed out after 30s"))

    def test_an_environment_that_could_not_be_printed_is_unknown(self):
        self.check((UNKNOWN, "the stand-in could not print its environment (exit 127): no env"), EMPTY, Result(127, "", "no env\n"))

    def test_every_hidden_path_is_judged(self):
        self.assertEqual(verdict([("/a", EMPTY), ("/b", Result(0, "key\n"))], DUMP, ENV)[0], LEAKS)


@unittest.skipUnless(sys.platform == "darwin", "Seatbelt is macOS's")
class TestSeatbeltOnThisMac(unittest.TestCase):
    def setUp(self):
        if not Local().run(["sandbox-exec", "-p", "(version 1)(allow default)", "true"]).ok:
            self.skipTest("sandbox-exec does not start here: this process is already inside a sandbox")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.secrets = os.path.join(os.path.realpath(tmp.name), 'se "crets')
        os.mkdir(self.secrets)
        with open(os.path.join(self.secrets, "token"), "w") as f:
            f.write("t")
        self.env = {"PATH": "/usr/bin:/bin", "HOME": tmp.name, "LC_ALL": "C"}

    def probe(self, kind):
        def run(argv):
            return Local().run(wrap(kind, argv, [self.secrets], self.env), env={"SSH_AUTH_SOCK": "/tmp/a", "GITHUB_TOKEN": "t"})
        return verdict([(self.secrets, run(["ls", "-A", self.secrets]))], run(["env", "-0"]), self.env), run(["cat", self.secrets + "/token"])

    def test_the_profile_hides_a_directory_and_the_environment_is_exactly_the_one_given(self):
        found, cat = self.probe("mac")
        self.assertEqual((found, cat.rc, cat.out), ((HOLDS, ""), 1, ""))

    def test_the_same_probe_without_the_profile_reads_the_directory(self):
        found, cat = self.probe("borrowed")
        self.assertEqual((found, cat.out), ((LEAKS, "%s is readable under the wrapper: it lists token" % self.secrets), "t"))
