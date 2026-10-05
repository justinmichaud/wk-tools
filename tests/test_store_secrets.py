"""The keyring is this device's own: `wk key` and `wk key push` read and write it with a `podman` on PATH that
leaves a witness and fails, and every reader refuses a link a workspace could plant in agent-rw.

Run: python3 -m unittest tests.test_store_secrets -v
"""
import contextlib
import io
import os
import sys
import unittest

from tests.support import REPO, WkTest, bash, stub_path
from tests.test_wk_secrets import KEY_SH

sys.path.insert(0, str(REPO / "lib"))
from wk.machine import Local  # noqa: E402
from wk.secrets import Secrets  # noqa: E402

# Not a token, and deliberately nothing like one.
PLACEHOLDER = "placeholder-value-for-this-test"

FAKE_PODMAN = '''#!/bin/sh
printf '%s\\n' "$*" >> "$WK_TEST_PODMAN_WITNESS"
echo "podman was called: $*" >&2
exit 1
'''


class _Here(WkTest):

    def setUp(self):
        super().setUp()
        self.store = self.tmp / "store"
        self.secrets = self.store / "secrets"
        self.witness = self.tmp / "podman-was-called"
        (self.store / "ws").mkdir(parents=True)

    def env(self, extra=None):
        e = {
            "WK_HOST_SECRETS": str(self.secrets),
            "WK_STORE": str(self.store),
            "WK_TEST_PODMAN_WITNESS": str(self.witness),
            "XDG_STATE_HOME": str(self.tmp / "state"),
        }
        if extra:
            e.update(extra)
        return e

    def called(self):
        return self.witness.read_text() if self.witness.exists() else ""

    def assert_no_podman(self, cp):
        self.assertEqual("", self.called(),
                         f"podman was called:\n{self.called()}\n{cp.stdout}")

    def wk(self, *args, env=None):
        with stub_path({"podman": FAKE_PODMAN}) as binp:
            cp = self.run_wk(*args, env=self.env({**(env or {}),
                                                  "PATH": f"{binp}:{os.environ['PATH']}"}))
        return cp

    def sh(self, script, env=None):
        with stub_path({"podman": FAKE_PODMAN}) as binp:
            cp = bash(KEY_SH + script, env=self.env({**(env or {}), "PATH": f"{binp}:{os.environ['PATH']}"}))
        return cp

    def sec(self):
        return Secrets(REPO, self.env(), Local())

    def call(self, method, name):
        """(answer, stderr) of one Secrets reader; lib/secretfile.py's refusal arrives on stderr."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            got = getattr(self.sec(), method)(name)
        return got, err.getvalue()


class TestTheStoreFunctionsReadAndWriteHere(_Here):
    def test_store_read_and_clear_round_trip(self):
        cp = self.sh(f'printf "%s\\n" {PLACEHOLDER} | key_store claude')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(PLACEHOLDER + "\n", self.sec().cred_read("claude"))
        cp = self.sh('key_clear claude')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual("", self.sec().cred_read("claude"))
        self.assertFalse(self.witness.exists(), cp.stderr)

    def test_the_keys_and_the_token_are_read_from_the_held_directory(self):
        held = self.secrets.parent / "push-keys"
        held.mkdir(parents=True)
        (held / "build_key_fork").write_text(f"{PLACEHOLDER}-fork\n")
        (held / "github-pat").write_text(f"{PLACEHOLDER}-pat\n")
        sec = self.sec()
        self.assertEqual(f"{self.secrets}/claude-token", sec.cred_path("claude"))
        self.assertEqual(f"{PLACEHOLDER}-fork\n", sec.read(sec.push_key_path("fork")))
        self.assertEqual(f"{held}/github-pat", sec.github_pat_path())
        self.assertEqual(f"{PLACEHOLDER}-pat\n", sec.cred_read("github-pat"))


class TestThePushSwitchRunsHere(_Here):
    """The agent is pointed at a socket of its own (WK_PUSH_AGENT_SOCK), so the podman witness must stay empty."""

    def setUp(self):
        super().setUp()
        self.held = self.secrets.parent / "push-keys"
        self.sock = self.tmp / "agent.sock"
        self.pat = self.tmp / "pat"

    def env(self, extra=None):
        return super().env({"WK_PUSH_AGENT_SOCK": str(self.sock),
                            "WK_PUSH_PAT_FILE": str(self.pat),
                            **(extra or {})})

    def _keys(self):
        self.held.mkdir(parents=True, exist_ok=True)
        self.secrets.mkdir(parents=True, exist_ok=True)
        for fork in ("fork", "forkwpe"):
            p = self.held / f"build_key_{fork}"
            p.write_text(f"{PLACEHOLDER}-{fork}\n")
            p.chmod(0o600)

    def test_status_and_off_read_the_keys_here_and_start_nothing(self):
        self.assertEqual(4, self.wk("key", "push", "status").returncode)
        self._keys()
        for action, rc in (("status", 1), ("off", 0)):
            with self.subTest(action=action):
                cp = self.wk("key", "push", action)
                self.assertEqual(rc, cp.returncode, cp.stdout)
                self.assert_no_podman(cp)


class TestNothingButAFileIsReadOrWrittenThroughAgentRw(_Here):
    """agent-rw is mounted read-write into every container beside the unmounted push-keys: a link planted there
    must not turn a host-side read or write of the login into one of the token."""

    NAME = "claude-login"
    REAL = "not-a-real-credential-just-this-tests-bytes"

    def setUp(self):
        super().setUp()
        self.agent_rw = self.secrets.parent / "agent-rw"
        self.agent_rw.mkdir(parents=True)
        self.held = self.secrets.parent / "push-keys"
        self.held.mkdir(parents=True)
        self.token = self.held / "github-pat"
        self.token.write_text(self.REAL + "\n")
        self.cred = self.agent_rw / ".credentials.json"

    def entry_points(self):
        """Each reader and writer of one of these paths, as (succeeded, stdout, stderr), each run on its own so
        a refusal in one cannot be mistaken for another's. A refused read answers None."""
        def reader(method):
            got, err = self.call(method, self.NAME)
            return got is not None, "" if got is None else str(got), err

        def key_store():
            cp = self.sh(f'printf "%s\\n" replacement | key_store {self.NAME}')
            return cp.returncode == 0, cp.stdout, cp.stderr
        return {"cred_read": lambda: reader("cred_read"), "cred_stored": lambda: reader("cred_stored"),
                "key_store": key_store}

    def test_a_symlink_out_of_it_is_refused_by_every_entry_point(self):
        self.assertEqual(str(self.cred), self.sec().cred_path(self.NAME))
        self.cred.symlink_to(self.token)
        for name, run in self.entry_points().items():
            with self.subTest(entry=name):
                ok, out, err = run()
                self.assertFalse(ok, out + err)
                self.assertNotIn(self.REAL, out + err)
                self.assertIn("not a file", err)
                self.assertIn(str(self.cred), err)
        self.assertEqual(self.REAL + "\n", self.token.read_text(),
                         "the write went through the link to the token")

    def test_a_hard_link_to_the_token_is_refused_by_every_entry_point(self):
        """O_NOFOLLOW cannot see this one; only st_nlink says so."""
        os.link(self.token, self.cred)
        for name, run in self.entry_points().items():
            with self.subTest(entry=name):
                ok, out, err = run()
                self.assertFalse(ok, out + err)
                self.assertNotIn(self.REAL, out + err)
                self.assertIn("hard links", err)
        self.assertEqual(self.REAL + "\n", self.token.read_text())

    def test_a_directory_in_its_place_is_refused(self):
        self.cred.mkdir()
        got, err = self.call("cred_stored", self.NAME)
        self.assertIsNone(got, err)
        self.assertIn("not a regular file", err)

    def test_a_plain_file_reads_writes_and_reports_present(self):
        doc = "{-a-: 1}"
        cp = self.sh(f'printf "%s" "{doc}" | key_store {self.NAME}')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIs(True, self.call("cred_stored", self.NAME)[0])
        self.assertEqual(doc + "\n", self.call("cred_read", self.NAME)[0])
        self.assertEqual(0o600, self.cred.stat().st_mode & 0o777)

    def test_a_missing_one_is_absent_and_not_a_refusal(self):
        self.assertIs(False, self.call("cred_stored", self.NAME)[0])
        self.assertEqual("", self.call("cred_read", self.NAME)[0])


if __name__ == "__main__":
    unittest.main()
