"""The deploy key is one key, the same on every workstation, and no key at all
on a shared build machine.

`wk key ensure` mints it, `wk key deploy` registers the one key per fork under a
single title and fans the private halves out to every peer workstation over the
tailnet (`wk key share`), and a receiving workstation takes one with `wk key
adopt`. `--rotate` removes the old key from GitHub, mints a fresh one and fans
that out -- so the whole fleet turns over from one command. A shared build
machine holds none: `remote/provision.sh` writes an ssh config with no
IdentityFile, so a push there signs with a forwarded agent and nothing rests on
a machine other people are root on.

Nothing here reaches a real machine, GitHub, or the tailnet: `gh` and `ssh` are
recording stubs, and every key is a throwaway generated in the test's own tmp.

Run: python3 -m unittest tests.test_key_shared -v
"""
import os
import subprocess
import unittest
from pathlib import Path

import re
import threading
from http.server import HTTPServer

import pty
import tarfile

from tests.support import REPO, WkTest, stub_path
from tests.test_credcheck import FakeGitHub
from tests.test_key import SSH_IS_THE_FORKS_KEY, SECURITY_HAS_NOTHING
from tests.test_claude_login import FAKE_CLAUDE, SECRET, login, record

KEY = REPO / "cmd" / "key"
PROVISION = REPO / "remote" / "provision.sh"

# A gh that records every call and answers the two questions cmd/key asks: the
# key list (empty, or the ids under the shared title), and read_only=false.
GH_RECORDER = '''#!/bin/sh
printf '%s\\n' "$*" >> "$WK_TEST_GH_LOG"
case "$*" in
  *"-X DELETE"*) exit 0 ;;
  *read_only*)   echo false; exit 0 ;;   # registering: the POST
  *keys*)        cat "$WK_TEST_GH_KEYS" 2>/dev/null; exit 0 ;;  # listing
esac
exit 0
'''



class _Shared(WkTest):
    def base_env(self):
        env = dict(os.environ)
        for var in ("WK_NAME", "WK_TARGET", "WK_TARGET_KIND", "WK_MARKER",
                    "WK_STORE", "WK_IN_VM"):
            env.pop(var, None)
        store = self.tmp / "store"
        self.secrets = store / "secrets"
        self.held = store / "push-keys"
        # WK_YES: what the dispatcher exports for --yes; the fan-out and the
        # rotation ask first, and what is measured here is what they do once answered.
        env.update({"WK_HOST_SECRETS": str(self.secrets), "WK_STORE": str(store),
                    "WK_NTFY_API": "http://127.0.0.1:1", "WK_YES": "1",
                    "WK_GITHUB_API": "http://127.0.0.1:1",
                    "WK_TARGET_REGISTRY": str(self.tmp / "reg")})
        (self.tmp / "reg").mkdir(exist_ok=True)
        return env

    def key(self, *args, stubs=None, input=None, env=None):
        e = self.base_env()
        if env:
            e.update(env)
        with stub_path({**(stubs or {})}) as binp:
            e["PATH"] = f"{binp}:/usr/bin:/bin:/usr/sbin:/sbin"
            return subprocess.run([str(KEY), *args], cwd=str(REPO), env=e,
                                  input=input, capture_output=True, text=True,
                                  timeout=120)

    def fleet(self):
        """One peer workstation and one shared build machine, each with a
        far-side `wk` that records what it was asked (PEER_WK below)."""
        reg = self.tmp / "reg"
        reg.mkdir(exist_ok=True)
        log = self.tmp / "peer.log"
        log.write_text("")
        self.peer_log = log
        for name, peer in (("peerbox", True), ("buildbox", False)):
            root = self.tmp / (name + "-root")
            (root / "tools").mkdir(parents=True, exist_ok=True)
            wk = root / "tools" / "wk"
            wk.write_text(PEER_WK)
            wk.chmod(0o755)
            (reg / f"{name}.conf").write_text(
                f"WK_REMOTE_HOST=fake-{name}\nWK_REMOTE_ROOT={root}\n"
                + ("WK_REMOTE_PEER=1\n" if peer else ""))
        return {"WK_TEST_PEER_LOG": str(log)}


class TestAdopt(_Shared):
    def test_a_private_key_on_stdin_is_stored_and_its_public_derived(self):
        self.key("ensure")
        shared = (self.held / "build_key_fork").read_text()
        cp = self.key("adopt", "forkwpe", input=shared)
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        # The adopted private half is the one that was piped in, and the public
        # half was re-derived from it -- so the two forks now share one key.
        self.assertEqual(shared, (self.held / "build_key_forkwpe").read_text())
        a = subprocess.run(["ssh-keygen", "-lf", str(self.secrets / "build_key_fork.pub")],
                           capture_output=True, text=True).stdout.split()[1]
        b = subprocess.run(["ssh-keygen", "-lf", str(self.secrets / "build_key_forkwpe.pub")],
                           capture_output=True, text=True).stdout.split()[1]
        self.assertEqual(a, b)

    def test_the_private_half_lands_where_nothing_mounts(self):
        self.key("ensure")
        shared = (self.held / "build_key_fork").read_text()
        self.key("adopt", "forkwpe", input=shared)
        self.assertFalse((self.secrets / "build_key_forkwpe").exists(),
                         "a private half in the mounted secrets directory")
        self.assertEqual(0o600, (self.held / "build_key_forkwpe").stat().st_mode & 0o777)

    def test_rubbish_on_stdin_is_refused_and_stores_nothing(self):
        cp = self.key("adopt", "fork", input="not a key\n")
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertFalse((self.held / "build_key_fork").exists())

    def test_an_unknown_fork_is_refused_by_name(self):
        cp = self.key("adopt", "nope", input="x\n")
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("no fork called 'nope'", cp.stdout + cp.stderr)


class TestDeployRegistersOneSharedKey(_Shared):
    def gh_env(self, keys=""):
        log = self.tmp / "gh.log"
        log.write_text("")
        keyfile = self.tmp / "gh.keys"
        keyfile.write_text(keys)
        self.gh_log = log
        return {"WK_TEST_GH_LOG": str(log), "WK_TEST_GH_KEYS": str(keyfile)}

    def stubs(self):
        return {"gh": GH_RECORDER, "ssh": SSH_IS_THE_FORKS_KEY,
                "security": SECURITY_HAS_NOTHING}

    def test_it_registers_one_key_per_fork_under_the_shared_title(self):
        """The registration is what is measured here; whether `check` at the
        end passes is its own stubbed concern (tests/test_key.py)."""
        self.key("ensure")
        self.key("deploy", stubs=self.stubs(), env=self.gh_env())
        posts = [l for l in self.gh_log.read_text().splitlines()
                 if "title=wk shared deploy key" in l]
        self.assertEqual(2, len(posts), self.gh_log.read_text())
        # No per-machine title anywhere: the key is one, shared.
        self.assertNotIn("wk build key (", self.gh_log.read_text())

    def test_a_key_already_registered_is_not_registered_again(self):
        self.key("ensure")
        pubs = "".join((self.secrets / f"build_key_{f}.pub").read_text()
                       for f in ("fork", "forkwpe"))
        self.key("deploy", stubs=self.stubs(), env=self.gh_env(keys=pubs))
        self.assertNotIn("title=wk shared deploy key", self.gh_log.read_text())

    def test_rotate_removes_the_old_key_and_mints_a_fresh_one(self):
        self.key("ensure")
        before = (self.held / "build_key_fork").read_text()
        # gh -X DELETE is what revoke runs; the id comes from --jq, which real gh
        # applies, so the stub just needs to record the DELETE and answer the list.
        keys = '[{"id": 7, "title": "wk shared deploy key", "key": "ssh-ed25519 AAAAold"}]'
        self.key("deploy", "--rotate", stubs={
            "gh": ('#!/bin/sh\nprintf "%s\\n" "$*" >> "$WK_TEST_GH_LOG"\n'
                   'case "$*" in\n'
                   '  *"-X DELETE"*) exit 0 ;;\n'
                   '  *--jq*id*) echo 7 ;;\n'
                   '  *read_only*) echo false ;;\n'
                   '  *keys*) cat "$WK_TEST_GH_KEYS" ;;\n'
                   'esac\nexit 0\n'),
            "ssh": SSH_IS_THE_FORKS_KEY, "security": SECURITY_HAS_NOTHING},
            env=self.gh_env(keys=keys))
        self.assertIn("-X DELETE repos/justinmichaud/WebKit/keys/7",
                      self.gh_log.read_text())
        self.assertNotEqual(before, (self.held / "build_key_fork").read_text(),
                            "rotate left the same private key in place")


# A far-side `wk` that records what it was asked; an ssh that runs the far
# command here, so the fan-out is exercised without a real machine.
PEER_SSH = '#!/bin/sh\nfor last; do :; done\nexec bash -c "$last"\n'
PEER_WK = ('#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$WK_TEST_PEER_LOG"\nexit 0\n')


class TestShareFansOutToWorkstationsOnly(_Shared):
    """`wk key share` sends the deploy keys and the API token to every peer
    workstation over the tailnet, and to no shared build machine -- a build
    machine holds no key and reaches ours through a forwarded agent."""

    def test_the_peer_gets_the_keys_and_the_token_and_the_box_gets_nothing(self):
        self.key("ensure")
        (self.held / "github-pat").write_text("ghp_notarealtoken\n")
        env = self.fleet()
        self.key("share", stubs={"ssh": PEER_SSH}, env=env)
        calls = [l for l in self.peer_log.read_text().splitlines() if l.strip()]
        self.assertIn("key adopt fork", calls)
        self.assertIn("key adopt forkwpe", calls)
        self.assertIn("key set github-pat --paste", calls)
        # The peer is asked whether it holds a claude.ai login of its own, and
        # exactly one workstation was contacted: the build machine adds none.
        self.assertIn("key verdict claude-login", calls)
        self.assertEqual(4, len(calls), calls)

    def test_the_bugzilla_key_rides_the_same_fan_out(self):
        self.key("ensure")
        (self.held / "github-pat").write_text("ghp_notarealtoken\n")
        (self.held / "bugzilla-api-key").write_text("notarealbugzillakey\n")
        env = self.fleet()
        self.key("share", stubs={"ssh": PEER_SSH}, env=env)
        calls = [l for l in self.peer_log.read_text().splitlines() if l.strip()]
        self.assertIn("key set github-pat --paste", calls)
        self.assertIn("key set bugzilla-api-key --paste", calls)
        self.assertEqual(5, len(calls), calls)


# A peer whose `wk key verdict claude-login` answers what the test put in a
# file, and whose `adopt claude-login` keeps what arrived on stdin for the test.
PEER_WK_WITH_LOGIN = ('#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$WK_TEST_PEER_LOG"\n'
                      'case "$*" in\n'
                      '  "key verdict claude-login") cat "$WK_TEST_PEER_VERDICT" ;;\n'
                      '  "key adopt claude-login") cat > "$WK_TEST_PEER_STDIN"; printf \'ok\\ttaken\\n\' ;;\n'
                      'esac\n'
                      'exit 0\n')


class TestCheckAsksEachWorkstationForItsLogin(_Shared):
    """The claude.ai login is never fanned out, so `wk key check` -- what
    `deploy` ends with -- asks each peer workstation for its own verdict and
    names the one still without a usable login."""

    def check(self, verdict):
        self.key("ensure")
        env = self.fleet()
        for name in ("peerbox", "buildbox"):
            (self.tmp / (name + "-root") / "tools" / "wk").write_text(PEER_WK_WITH_LOGIN)
        answer = self.tmp / "peer.verdict"
        answer.write_text(verdict)
        env.update({"WK_TEST_PEER_VERDICT": str(answer),
                    "WK_TEST_GH_LOG": str(self.tmp / "gh.log"),
                    "WK_TEST_GH_KEYS": str(self.tmp / "gh.keys")})
        (self.tmp / "gh.keys").write_text("")
        return self.key("check", stubs={"ssh": PEER_SSH, "gh": GH_RECORDER,
                                        "security": SECURITY_HAS_NOTHING}, env=env)

    def test_a_peer_without_a_login_is_named_with_the_command_to_run_there(self):
        cp = self.check("absent\tnothing stored\n")
        out = cp.stdout + cp.stderr
        self.assertIn("claude.ai login on the other workstations", out)
        self.assertRegex(out, r"peerbox\s+no login: .*wk key share --to peerbox")
        self.assertNotIn("buildbox", out.split("claude.ai login")[1])
        self.assertNotEqual(0, cp.returncode)
        self.assertIn("key verdict claude-login", self.peer_log.read_text())

    def test_a_peer_whose_login_the_cli_emptied_is_told_to_rotate_there(self):
        cp = self.check("bad\tno accessToken.\n")
        out = cp.stdout + cp.stderr
        self.assertRegex(out, r"peerbox\s+no accessToken\.")
        self.assertIn("fix: from a terminal here, 'wk key share --to peerbox' logs in for it", out)

    def test_a_peer_with_a_usable_login_is_reported_and_not_faulted(self):
        cp = self.check("ok\tscopes: user:inference user:profile; organization: Example Org.\n")
        out = cp.stdout + cp.stderr
        self.assertRegex(out, r"peerbox\s+scopes: user:inference user:profile")
        self.assertNotIn("wk key share --to peerbox", out.split("claude.ai login")[1])


class TestShareLogsInForAWorkstationWithoutALogin(_Shared):
    """The login is one holder's, so it is not copied: `wk key share` logs in
    here, into a directory of its own, once for each peer whose own verdict is
    not usable, sends the two files over as a tar, and keeps nothing."""

    def share(self, verdict, terminal=True, to=(), leaves=None):
        self.key("ensure")
        env = self.fleet()
        for name in ("peerbox", "buildbox"):
            (self.tmp / (name + "-root") / "tools" / "wk").write_text(PEER_WK_WITH_LOGIN)
        answer = self.tmp / "peer.verdict"
        answer.write_text(verdict)
        self.taken = self.tmp / "peer.stdin"
        self.taken.write_bytes(b"")
        self.claude_log = self.tmp / "claude.log"
        self.claude_log.write_text("")
        leaves_text = login() if leaves is None else leaves
        leaves = self.tmp / "leaves"
        leaves.write_text(leaves_text)
        leaves_record = self.tmp / "leaves-record"
        leaves_record.write_text(record())
        env.update({"WK_TEST_PEER_VERDICT": str(answer), "WK_TEST_PEER_STDIN": str(self.taken),
                    "WK_TEST_CLAUDE_LOG": str(self.claude_log), "WK_TEST_LOGIN": str(leaves),
                    "WK_TEST_RECORD": str(leaves_record)})
        e = self.base_env()
        e.update(env)
        with stub_path({"ssh": PEER_SSH, "claude": FAKE_CLAUDE}) as binp:
            e["PATH"] = f"{binp}:/usr/bin:/bin:/usr/sbin:/sbin"
            if not terminal:
                return subprocess.run([str(KEY), "share", *to], cwd=str(REPO), env=e,
                                      input="", capture_output=True, text=True, timeout=120)
            master, slave = pty.openpty()
            try:
                cp = subprocess.run([str(KEY), "share", *to], cwd=str(REPO), env=e,
                                    stdin=slave, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, timeout=120)
            finally:
                os.close(slave)
                os.close(master)
            cp.stderr = ""
            return cp

    def test_a_peer_without_a_login_is_logged_in_for_and_sent_the_two_files(self):
        cp = self.share("absent\tnothing stored\n")
        self.assertEqual(0, cp.returncode, cp.stdout)
        self.assertIn("key adopt claude-login", self.peer_log.read_text())
        self.assertIn("argv: auth login", self.claude_log.read_text())
        with tarfile.open(str(self.taken)) as tar:
            names = set(tar.getnames())
            self.assertEqual({".credentials.json", ".claude.json"}, names)
            self.assertEqual(login(), tar.extractfile(".credentials.json").read().decode())
        self.assertIn("peerbox: logged in for it", cp.stdout)
        self.assertNotIn(SECRET, cp.stdout)

    def test_the_login_made_for_a_peer_is_kept_nowhere_here(self):
        self.share("absent\tnothing stored\n")
        store = self.secrets.parent
        self.assertFalse((store / "agent-rw" / ".credentials.json").exists())
        # The directory the login was made in names itself in the CLI's log, and is gone.
        made_in = [l.split(": ", 1)[1] for l in self.claude_log.read_text().splitlines()
                   if l.startswith("store: ")]
        self.assertEqual(1, len(made_in), self.claude_log.read_text())
        self.assertFalse(os.path.exists(made_in[0]), made_in[0])

    def test_a_peer_with_a_usable_login_is_left_alone(self):
        cp = self.share("ok\tscopes: user:inference user:profile\n")
        self.assertEqual(0, cp.returncode, cp.stdout)
        self.assertNotIn("key adopt claude-login", self.peer_log.read_text())
        self.assertEqual("", self.claude_log.read_text())

    def test_without_a_terminal_the_login_is_not_attempted_and_the_command_is_named(self):
        cp = self.share("absent\tnothing stored\n", terminal=False)
        out = cp.stdout + cp.stderr
        self.assertNotIn("key adopt claude-login", self.peer_log.read_text())
        self.assertEqual("", self.claude_log.read_text())
        self.assertIn("wk key share --to peerbox", out)
        self.assertNotEqual(0, cp.returncode, out)

    def test_a_login_the_rule_refuses_is_discarded_not_sent(self):
        """A login lacking the scope remote control needs never leaves here."""
        cp = self.share("absent\tnothing stored\n", leaves=login(scopes=["user:inference"]))
        self.assertNotIn("key adopt claude-login", self.peer_log.read_text())
        self.assertIn("not one a workspace can use", cp.stdout)
        self.assertNotEqual(0, cp.returncode, cp.stdout)


class TestABuildMachineHoldsNoKey(unittest.TestCase):
    """remote/provision.sh writes the ssh config that selects the deploy key by
    fork alias. On a shared build machine that config names no IdentityFile --
    the key is a forwarded agent's -- and no push-keys directory is made."""

    def _config(self, tmp):
        """The ssh config remote/provision.sh writes, produced the same way it
        does: the alias blocks with an empty dir (the agent-forward form)."""
        return subprocess.run(
            ["bash", "-c",
             '. "$1/lib/common.sh"; . "$1/lib/store.sh"; wk_ssh_alias_blocks ""',
             "_", str(REPO)],
            capture_output=True, text=True, cwd=str(REPO)).stdout

    def test_the_config_names_no_identity_file(self):
        cfg = self._config(None)
        self.assertIn("Host github-webkit", cfg)
        self.assertIn("HostName github.com", cfg)
        self.assertNotIn("IdentityFile", cfg)
        self.assertNotIn("IdentitiesOnly", cfg)

    def test_provisioning_makes_no_push_keys_and_removes_any_at_rest(self):
        text = PROVISION.read_text()
        self.assertNotIn('ensure_dir "$ROOT/push-keys"', text)
        self.assertIn('rm -rf "$ROOT/push-keys"', text)
        self.assertIn('wk_ssh_alias_blocks ""', text)


if __name__ == "__main__":
    unittest.main()


class TestTheTokenAloneToOnePeer(TestShareFansOutToWorkstationsOnly):
    """`wk key share --to <peer> --only github-pat` sends the token and
    nothing else, to that peer and no other: the half a machine with a
    refused token asks a peer for."""

    def test_only_the_token_travels_and_only_to_the_named_peer(self):
        self.key("ensure")
        (self.held / "github-pat").write_text("ghp_notarealtoken\n")
        env = self.fleet()
        cp = self.key("share", "--to", "peerbox", "--only", "github-pat",
                      stubs={"ssh": PEER_SSH}, env=env)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        calls = [l for l in self.peer_log.read_text().splitlines() if l.strip()]
        self.assertEqual(["key set github-pat --paste"], calls)

    def test_the_bugzilla_key_travels_alone_too(self):
        self.key("ensure")
        (self.held / "github-pat").write_text("ghp_notarealtoken\n")
        (self.held / "bugzilla-api-key").write_text("notarealbugzillakey\n")
        env = self.fleet()
        cp = self.key("share", "--to", "peerbox", "--only", "bugzilla-api-key",
                      stubs={"ssh": PEER_SSH}, env=env)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        calls = [l for l in self.peer_log.read_text().splitlines() if l.strip()]
        self.assertEqual(["key set bugzilla-api-key --paste"], calls)

    def test_a_machine_that_is_not_a_peer_is_refused(self):
        self.key("ensure")
        env = self.fleet()
        cp = self.key("share", "--to", "buildbox", stubs={"ssh": PEER_SSH}, env=env)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("not a peer workstation", cp.stderr)
        self.assertEqual("", self.peer_log.read_text().strip())

    def test_only_takes_the_one_credential_that_travels_alone(self):
        self.key("ensure")
        env = self.fleet()
        cp = self.key("share", "--only", "claude", stubs={"ssh": PEER_SSH}, env=env)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("--only takes github-pat or bugzilla-api-key", cp.stderr)


class TestARefusedTokenIsTakenFromAPeerFirst(TestShareFansOutToWorkstationsOnly):
    """The token is one for the fleet: when GitHub refuses the one stored
    here, `wk key deploy` asks each peer to share its own before anyone is
    asked to mint a new one. The peer's share is answered with WK_YES,
    because this run already asked its question."""

    def setUp(self):
        super().setUp()
        self.server = HTTPServer(("127.0.0.1", 0), FakeGitHub)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        FakeGitHub.user_status = 401
        FakeGitHub.seen = []

    def test_deploy_asks_the_peer_for_its_token(self):
        self.key("ensure")
        (self.held / "github-pat").write_text("ghp_revokedone\n")
        gh_log = self.tmp / "gh.log"; gh_log.write_text("")
        gh_keys = self.tmp / "gh.keys"; gh_keys.write_text("")
        env = {**self.fleet(), "WK_GITHUB_API": "http://127.0.0.1:%d" % self.server.server_port,
               "WK_TEST_GH_LOG": str(gh_log), "WK_TEST_GH_KEYS": str(gh_keys)}
        cp = self.key("deploy", stubs={"ssh": PEER_SSH, "gh": GH_RECORDER,
                                       "security": SECURITY_HAS_NOTHING}, env=env)
        out = cp.stdout + cp.stderr
        calls = [l for l in self.peer_log.read_text().splitlines() if l.strip()]
        asked = [c for c in calls if re.fullmatch(r"key share --to \S+ --only github-pat", c)]
        self.assertEqual(1, len(asked), calls)
        self.assertIn("asking peerbox for its github-pat", out)
        # The stub peer sends nothing back, so the refused token is still here and named, not fanned out.
        self.assertIn("wk key set github-pat --replace", out)
        self.assertNotIn("key set github-pat --paste", calls)
        self.assertEqual("ghp_revokedone", (self.held / "github-pat").read_text().strip())
