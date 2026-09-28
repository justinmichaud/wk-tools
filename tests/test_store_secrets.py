"""The secrets directory is this device's own, and nothing crosses into the
podman machine to reach it.

The directory is this host's (Store.secrets_dir, lib/wk/store.py) and the machine
mounts it read-only, so `wk key set`, `wk key ensure` and `wk push` are all
answered here: storing a token or throwing the push switch needs no virtual
machine running.

That is testable as an absence, which is what this file is: `podman` on PATH
leaves a witness file behind and then fails. Every command below has to
succeed, and the witness must never appear.

Run: python3 -m unittest tests.test_store_secrets -v
"""
import contextlib
import inspect
import io
import os
import subprocess
import sys
import unittest

from tests.support import REPO, WkTest, bash, stub_path
from tests.test_wk_secrets import KEY_SH

sys.path.insert(0, str(REPO / "lib"))
from wk import guest  # noqa: E402
from wk.machine import Local  # noqa: E402
from wk.secrets import Secrets  # noqa: E402

# Not a token, and deliberately nothing like one.
PLACEHOLDER = "placeholder-value-for-this-test"

# A `podman` that records the fact it was called and then fails, so a command
# that reaches for it neither succeeds nor does it quietly.
FAKE_PODMAN = '''#!/bin/sh
printf '%s\\n' "$*" >> "$WK_TEST_PODMAN_WITNESS"
echo "podman was called: $*" >&2
exit 1
'''


class _Here(WkTest):
    """A scratch secrets directory, a scratch store, and the witness.

    Store.secrets_dir reads WK_HOST_SECRETS on a macOS host and
    $WK_STORE/secrets everywhere else, so the two names below are one
    directory and every assertion holds on either platform."""

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

    def assert_no_machine_hop(self, cp):
        """Asking the machine about its own containers is allowed and needs it
        already up (`podman -c wk ps`); reaching into it for the keys, or
        starting it to do so, is what is gone."""
        for verb in ("machine ssh", "machine start", "machine init", "machine rm"):
            with self.subTest(verb=verb):
                self.assertNotIn(verb, self.called(), f"{self.called()}\n{cp.stdout}")

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

    def test_the_path_is_the_host_directory_and_not_the_store(self):
        sec = self.sec()
        self.assertEqual(f"{self.secrets}/claude-token", sec.cred_path("claude"))
        self.assertEqual(str(self.secrets), sec.secrets_dir())
        self.assertEqual(f"{self.secrets.parent}/push-keys", sec.held_dir())

    def test_a_deploy_key_is_read_from_here_too(self):
        """From the directory nothing mounts, which is where every private half
        lives now -- `wk push` loads it into an agent rather than moving it."""
        held = self.secrets.parent / "push-keys"
        held.mkdir(parents=True)
        (held / "build_key_fork").write_text(f"{PLACEHOLDER}-fork\n")
        sec = self.sec()
        self.assertEqual(f"{PLACEHOLDER}-fork\n", sec.read(sec.push_key_path("fork")))

    def test_the_github_token_is_beside_the_private_halves(self):
        """It publishes, so it is in the directory nothing mounts and not in
        the one every workspace reads."""
        held = self.secrets.parent / "push-keys"
        held.mkdir(parents=True)
        (held / "github-pat").write_text(f"{PLACEHOLDER}-pat\n")
        sec = self.sec()
        self.assertEqual(f"{held}/github-pat", sec.github_pat_path())
        self.assertEqual(f"{PLACEHOLDER}-pat\n", sec.cred_read("github-pat"))


class TestKeyEnsureRunsHere(_Here):
    def test_it_makes_the_keys_with_no_machine_in_the_path(self):
        cp = self.wk("key", "ensure")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assert_no_podman(cp)
        self.assertTrue((self.secrets.parent / "push-keys" / "build_key_fork").exists(),
                        cp.stdout)

    def test_the_public_half_reads_back_the_same_way(self):
        self.wk("key", "ensure")
        cp = self.wk("key", "pub", "fork")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assert_no_podman(cp)
        self.assertIn("ssh-ed25519", cp.stdout)


class TestThePushSwitchRunsHere(_Here):
    """The credentials are read from this host with nothing started. The one
    thing that is *not* here is the ssh-agent -- it is on the machine that runs
    the workspaces, because that is the only machine whose containers can see a
    socket -- so `wk push` reaches it in one `podman machine ssh` and reaches
    for nothing else. Every test below points that at an agent of its own
    (WK_PUSH_AGENT_SOCK), so the podman witness must stay empty."""

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

    def test_status_reads_the_keys_here(self):
        self._keys()
        cp = self.wk("push", "status")
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assert_no_podman(cp)
        self.assertIn("push is OFF", cp.stdout)
        self.assertIn("held back", cp.stdout)

    def test_off_asks_the_agent_here_and_starts_nothing(self):
        self._keys()
        cp = self.wk("push", "off")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assert_no_podman(cp)
        self.assertIn("push is OFF", cp.stdout)

    def test_the_private_halves_stay_where_they_are(self):
        """The switch is the agent's contents; no file moves in either
        direction, so there is no half-thrown position to crash into."""
        self._keys()
        self.wk("push", "off")
        self.assertTrue((self.held / "build_key_fork").exists())
        self.assertFalse((self.secrets / "build_key_fork").exists())

    def test_status_with_none_says_so_and_still_touches_nothing(self):
        cp = self.wk("push", "status")
        self.assertEqual(cp.returncode, 4, cp.stdout)
        self.assert_no_podman(cp)
        self.assertIn("no deploy keys", cp.stdout)


class TestNothingButAFileIsReadOrWrittenThroughAgentRw(_Here):
    """`~/.config/wk/agent-rw` is mounted read-write into every container --
    the Claude CLI rotates the claude.ai login in place and every holder has to
    be looking at one set of bytes -- and it is a sibling of `push-keys`, which
    holds the private deploy keys and the GitHub token and is mounted nowhere.

    So a workspace can plant a link in the one directory it can write:

        ln -sfn ../push-keys/github-pat /agent-rw/.credentials.json

    and every host-side read of that name becomes a read of the token (`wk
    start` copies what it reads into a guest) and every host-side write becomes
    a write through the link, so `wk key set claude-login` would overwrite it.
    A hard link does the same without being a symlink.

    Every reader and writer goes through lib/secretfile.py, and this is that
    rule measured at each of them."""

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

    def test_the_row_really_does_live_in_the_writable_directory(self):
        """Otherwise everything below is testing the wrong path."""
        self.assertEqual(str(self.cred), self.sec().cred_path(self.NAME))

    def test_a_symlink_out_of_it_is_refused_by_every_entry_point(self):
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

    def test_an_absolute_symlink_is_refused_the_same_way(self):
        self.cred.symlink_to(str(self.token))
        ok, out, err = self.entry_points()["cred_read"]()
        self.assertFalse(ok, out + err)
        self.assertNotIn(self.REAL, out)

    def test_a_hard_link_to_the_token_is_refused_by_every_entry_point(self):
        """O_NOFOLLOW cannot see this one: it is the same inode under a second
        name, and only st_nlink says so."""
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
        """The refusal is about what a workspace could put there, and the
        ordinary path is untouched: the credential still round-trips."""
        doc = "{-a-: 1}"
        cp = self.sh(f'printf "%s" "{doc}" | key_store {self.NAME}')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIs(True, self.call("cred_stored", self.NAME)[0])
        self.assertEqual(doc + "\n", self.call("cred_read", self.NAME)[0])
        self.assertEqual(0o600, self.cred.stat().st_mode & 0o777)

    def test_a_missing_one_is_absent_and_not_a_refusal(self):
        self.assertIs(False, self.call("cred_stored", self.NAME)[0])
        self.assertEqual("", self.call("cred_read", self.NAME)[0])

    def test_a_refused_path_stops_a_guests_start_rather_than_arriving_empty(self):
        """lib/wk/guest.py copies a value row into a guest, so a path its reader
        refuses must stop the start rather than land in there as an empty file.
        A file row is never copied into a guest at all -- a copy is a second
        holder of a credential its tool rotates -- so the document is not read
        out of the store on that path either."""
        body = inspect.getsource(guest.Guest.write_agent_secrets)
        self.assertIn("self.secrets.cred_stored(name)", body)
        self.assertIn("if here is None:\n                return False", body)
        file_row = body[body.index('if kind == "file":'):body.index("here = ")]
        self.assertIn("continue", file_row)
        self.assertNotIn("cred_read", file_row)


class TestNoForwardingIsLeftInTheSource(unittest.TestCase):
    """The hop is gone from the files that had it, not merely unused."""

    def test_the_two_commands_never_reach_into_the_machine(self):
        for f in ("cmd/key", "cmd/push"):
            with self.subTest(script=f):
                self.assertNotIn("podman machine ssh", (REPO / f).read_text())

    def test_the_library_hops_for_the_agent_and_for_nothing_else(self):
        """The credentials are read here. The ssh-agent and the injector are on
        the machine that runs the workspaces -- there is nowhere else they
        could be -- so Secrets.agent_argv is the one place that reaches it, and
        the secrets reader is not."""
        text = (REPO / "lib" / "wk" / "secrets.py").read_text()
        hop = '"podman", "machine", "ssh"'
        self.assertEqual(1, text.count(hop))
        self.assertIn(hop, inspect.getsource(Secrets.agent_argv))
        for reader in (Secrets.read, Secrets.cred_read, Secrets.cred_stored):
            with self.subTest(reader=reader.__name__):
                self.assertNotIn("podman", inspect.getsource(reader))

    def test_the_dispatcher_no_longer_forwards_push(self):
        decls = subprocess.run([str(REPO / "wk"), "--declarations"],
                               cwd=str(REPO), stdout=subprocess.PIPE, text=True,
                               timeout=60).stdout
        for line in decls.splitlines():
            f = line.split("\t")
            if f and f[0] == "push":
                self.assertEqual("local", f[1], line)
                return
        self.fail("push is not declared")

    def test_the_secrets_directory_has_one_definition(self):
        """Two spellings of one directory, both from Store.secrets_dir: a command
        that spelled `$WK_STORE/secrets` itself would be right in the VM and
        wrong on the host that mounts it there."""
        text = (REPO / "lib" / "wk" / "store.py").read_text()
        self.assertEqual(1, text.count("def secrets_dir(self):"))
        for f in ("cmd/key", "cmd/push"):
            with self.subTest(script=f):
                body = (REPO / f).read_text()
                # The container's mount source (lib/wk/targets.py) is its own
                # view and stays store-relative; these read the files.
                self.assertNotIn('$WK_STORE/secrets', body)
                self.assertNotIn('$WK_STORE/push-keys', body)
        body = (REPO / "lib" / "wk" / "doctor.py").read_text()
        self.assertIn("secrets_dir()", body)
        self.assertNotIn('"secrets"', body)
        self.assertNotIn('"push-keys"', body)


if __name__ == "__main__":
    unittest.main()
