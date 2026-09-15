"""The fleet holds one deploy key per fork and one of every credential but the
claude.ai login, and a shared build machine holds none of them at all.

Which one it holds is elected rather than pushed: `wk key setup` asks every
workstation what it holds and whether its issuer still accepts it, at that
moment, and the best working answer is taken here (`wk key give`) and put on
every workstation holding a different one (`wk key adopt`, `wk key set
--paste`). Age decides nothing, a machine already holding the winner is left
alone, and a credential nobody could judge is never written over another
machine's. `--rotate` elects nothing: it removes the old key from GitHub, mints
a fresh one and puts that everywhere, so the whole fleet turns over from one
command. A shared build
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

import threading
from http.server import HTTPServer

import pty
import tarfile

from tests.support import REPO, WkTest, stub_path
from tests.test_credcheck import FakeGitHub
from tests.test_key import (SSH_IS_THE_FORKS_KEY, SECURITY_HAS_NOTHING,
                            provision_credentials)
from tests.test_claude_login import FAKE_CLAUDE, SECRET, login, record

KEY = REPO / "cmd" / "key"
PROVISION = REPO / "remote" / "provision.sh"

# A gh that records every call and answers the two questions cmd/key asks: the
# key list, and whether one key is registered on that repository with write
# access. The second is filtered the way real gh's --jq filters it, on the
# base64 body in the query, so a machine whose key is not in the list is told
# so rather than told yes.
GH_RECORDER = '''#!/bin/sh
printf '%s\\n' "$*" >> "$WK_TEST_GH_LOG"
case "$*" in
  *"-X DELETE"*) exit 0 ;;
  *read_only*)                           # the verdict's query, and the POST
    body=$(printf '%s' "$*" | sed -n 's/.*contains("\\([^"]*\\)").*/\\1/p')
    [ -z "$body" ] || ! grep -qF "$body" "$WK_TEST_GH_KEYS" 2>/dev/null || echo false
    exit 0 ;;
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


# A far-side `wk` that records what it was asked and answers from files the test
# writes, so a peer can be given any position: what it holds, what its verdict
# on that is, and what it hands over when the election picks it. An ssh that
# runs the far command here, so a fan-out is exercised without a real machine.
PEER_SSH = '#!/bin/sh\nfor last; do :; done\nexec bash -c "$last"\n'

# `wk key setup` uses ssh for both things at once: asking github.com what a
# deploy key authenticates as, and running the far side's own wk. One stub
# answers as GitHub for the first and runs the command here for the second.
FLEET_SSH = ('#!/bin/sh\ncase "$*" in\n'
             '  *git@github.com*) '
             'case "$*" in\n'
             '    *build_key_forkwpe*) echo "Hi justinmichaud/WPEWebKit! You\'ve '
             'successfully authenticated, but GitHub does not provide shell access." ;;\n'
             '    *) echo "Hi justinmichaud/WebKit! You\'ve successfully '
             'authenticated, but GitHub does not provide shell access." ;;\n'
             '  esac\n'
             '  exit 0 ;;\n'
             'esac\n'
             'for last; do :; done\nexec bash -c "$last"\n')
PEER_WK = '''#!/bin/sh
printf '%s\\n' "$*" >> "$WK_TEST_PEER_LOG"
answer() { [ -n "$1" ] && [ -f "$1" ] && cat "$1"; return 0; }
case "$*" in
  "key verdict claude-login") answer "$WK_TEST_PEER_LOGIN" ;;
  "key verdict "*)            answer "$WK_TEST_PEER_VERDICT" ;;
  "key give "*)               answer "$WK_TEST_PEER_GIVE" ;;
  "key pub "*)                answer "$WK_TEST_PEER_PUB" ;;
  "key sshtest "*)            answer "$WK_TEST_PEER_SSH" ;;
  "key adopt claude-login")   cat > "$WK_TEST_PEER_STDIN"; printf 'ok\\ttaken\\n' ;;
  *)                          cat >/dev/null 2>&1 ;;
esac
exit 0
'''

# What `ssh -T git@github.com` answers for a registered deploy key.
GITHUB_SAYS_HI = ("Hi justinmichaud/WebKit! You've successfully authenticated, "
                  "but GitHub does not provide shell access.\n")

# A fine-grained token FakeGitHub accepts, and one it does not.
GOOD_PAT = "github_pat_11ABCDEFG_notarealtoken"
OTHER_PAT = "github_pat_11ZZZZZZZ_alsonotarealtoken"


class GitHubKnowsOnePat(FakeGitHub):
    """One token GitHub accepts and any other it refuses, judged per request
    from the header: an election needs a winner and a loser, and the winner has
    to be accepted again on the machine that took it."""

    good = ""

    def _judge(self):
        ok = GitHubKnowsOnePat.good in self.headers.get("Authorization", "")
        FakeGitHub.user_status = 200 if ok else 401
        FakeGitHub.pulls = dict.fromkeys(FakeGitHub.repos, 422 if ok else 403)

    def do_GET(self):
        self._judge()
        return FakeGitHub.do_GET(self)

    def do_POST(self):
        self._judge()
        return FakeGitHub.do_POST(self)


class _Fleet(_Shared):
    """One peer workstation, one shared build machine, and a GitHub that
    answers about this machine's own token."""

    def setUp(self):
        super().setUp()
        GitHubKnowsOnePat.good = GOOD_PAT
        self.server = HTTPServer(("127.0.0.1", 0), GitHubKnowsOnePat)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.forks = subprocess.run(
            ["bash", "-c", '. "$1/lib/common.sh"; . "$1/lib/store.sh"; '
                           'wk_push_forks | awk "NF {print \\$2}"', "_", str(REPO)],
            capture_output=True, text=True, cwd=str(REPO)).stdout.split()
        FakeGitHub.user_status = 200
        FakeGitHub.scopes = ""
        FakeGitHub.expiry = ""
        FakeGitHub.repos = list(self.forks)
        FakeGitHub.repos_status = 200
        FakeGitHub.repos_answer = None
        FakeGitHub.pulls = dict.fromkeys(self.forks, 422)
        FakeGitHub.seen = []
        self.gh_log = self.tmp / "gh.log"
        self.gh_log.write_text("")
        self.gh_keys = self.tmp / "gh.keys"
        self.gh_keys.write_text("")

    def fleet_env(self, **files):
        """The two far-side machines, and the files the peer answers from.
        Each keyword is one answer: verdict, login, give, pub, ssh, stdin."""
        env = self.fleet()
        for key in ("verdict", "login", "give", "pub", "ssh", "stdin"):
            path = self.tmp / ("peer." + key)
            if key in files:
                path.write_text(files[key])
            env["WK_TEST_PEER_" + key.upper()] = str(path)
        self.peer_files = {k: self.tmp / ("peer." + k)
                           for k in ("verdict", "login", "give", "pub", "ssh", "stdin")}
        env.update({"WK_TEST_GH_LOG": str(self.gh_log),
                    "WK_TEST_GH_KEYS": str(self.gh_keys),
                    "WK_GITHUB_API": "http://127.0.0.1:%d" % self.server.server_port,
                    "WK_BUGZILLA_API": "http://127.0.0.1:1",
                    "WK_ANTHROPIC_API": "http://127.0.0.1:1",
                    "WK_TAILNET_API": "http://127.0.0.1:1",
                    "WK_LITELLM_API": "http://127.0.0.1:1",
                    "WK_TS_AUTHKEY": str(self.tmp / "tailscale-authkey"),
                    "WK_TS_API_SECRET": str(self.tmp / "tailscale-api-key"),
                    "HOME": str(self.tmp / "home")})
        (self.tmp / "home").mkdir(exist_ok=True)
        return env

    def registered(self):
        """GitHub answering that this machine's own public halves are the
        registered ones, so its deploy keys are the ones that work."""
        self.gh_keys.write_text("".join(
            (self.secrets / f"build_key_{f}.pub").read_text()
            for f in ("fork", "forkwpe")))

    def setup(self, *args, env=None, **stubs):
        e = {"gh": GH_RECORDER, "ssh": FLEET_SSH,
             "security": SECURITY_HAS_NOTHING}
        e.update(stubs)
        return self.key("setup", *args, stubs=e, env=env)

    def calls(self):
        return [l for l in self.peer_log.read_text().splitlines() if l.strip()]


class TestSetupPutsOneWorkingCredentialOnEveryWorkstation(_Fleet):
    """`wk key setup` converges the fleet: the deploy keys and the GitHub API
    token go to every peer workstation over the tailnet, and to no shared build
    machine -- a build machine holds nothing at rest and reaches the elected key
    through a forwarded agent."""

    def test_the_peer_takes_the_keys_and_the_token_and_the_box_takes_nothing(self):
        self.key("ensure")
        self.registered()
        (self.held / "github-pat").write_text(GOOD_PAT + "\n")
        env = self.fleet_env(verdict="absent\tnothing stored\n",
                             login="ok\tscopes: user:inference user:profile\n")
        cp = self.setup(env=env)
        calls = self.calls()
        self.assertIn("key adopt fork", calls, cp.stdout + cp.stderr)
        self.assertIn("key adopt forkwpe", calls)
        self.assertIn("key set github-pat --paste", calls)
        # Only the workstation was contacted: nothing was asked of the build
        # machine at all, not even what it holds.
        self.assertNotIn("buildbox", self.peer_log.read_text())

    def test_a_peer_already_holding_the_elected_one_is_left_alone(self):
        """Two machines holding one credential fingerprint alike, so it is not
        sent again and the run says so rather than reporting a change."""
        self.key("ensure")
        self.registered()
        (self.held / "github-pat").write_text(GOOD_PAT + "\n")
        fp = subprocess.run(["python3", str(REPO / "lib" / "secretfile.py"),
                             "fingerprint", str(self.held / "github-pat")],
                            capture_output=True, text=True).stdout.strip()
        env = self.fleet_env(
            verdict="ok\tholds it\n    fingerprint: %s\n" % fp,
            pub=(self.secrets / "build_key_fork.pub").read_text(),
            login="ok\tscopes: user:inference user:profile\n")
        cp = self.setup(env=env)
        calls = self.calls()
        self.assertNotIn("key set github-pat --paste", calls, cp.stdout + cp.stderr)
        self.assertNotIn("key adopt fork", calls,
                         "the peer already holds that deploy key")
        # The other fork's key is a different one there, so that one does travel.
        self.assertIn("key adopt forkwpe", calls)

    def test_one_nobody_could_judge_is_not_written_over_a_peers(self):
        """Being offline is a state, not a verdict: with no Bugzilla login to
        judge against, neither machine's key is established and neither is
        overwritten with the other."""
        self.key("ensure")
        self.registered()
        (self.held / "github-pat").write_text(GOOD_PAT + "\n")
        (self.held / "bugzilla-api-key").write_text("notarealbugzillakey\n")
        env = self.fleet_env(verdict="absent\tnothing stored\n",
                             login="ok\tscopes: user:inference user:profile\n")
        cp = self.setup(env=env)
        out = cp.stdout + cp.stderr
        self.assertNotIn("key set bugzilla-api-key --paste", self.calls(), out)
        self.assertIn("bugzilla-api-key: no workstation's could be judged", out)
        self.assertIn("key set github-pat --paste", self.calls(),
                      "one credential nobody could judge stopped the rest")


class TestEveryCredentialIsTheFleets(_Fleet):
    """One of each on every workstation, not only the deploy keys and the two
    API tokens: a working LiteLLM key, Claude token, tailnet key or ntfy topic
    on any workstation is the one the fleet ends up holding. The claude.ai login
    is the single exception -- made per machine, never given and never pasted
    over -- because a copy is a second holder of one refresh token."""

    ELECTED = ("github-pat", "bugzilla-api-key", "claude", "litellm",
               "tailnet", "tailnet-api", "ntfy")

    def test_each_one_is_put_to_the_election(self):
        """Every workstation is asked what it holds of each of them; nothing is
        left to be this machine's own but the login."""
        self.key("ensure")
        self.registered()
        env = self.fleet_env(verdict="absent\tnothing stored\n",
                             login="ok\tscopes: user:inference user:profile\n")
        cp = self.setup(env=env)
        asked = [l.split(" ", 2)[2] for l in self.calls()
                 if l.startswith("key verdict ")]
        for name in self.ELECTED:
            with self.subTest(name=name):
                self.assertIn(name, asked, cp.stdout + cp.stderr)

    def test_a_peers_working_litellm_key_is_taken_when_this_machine_holds_none(self):
        """The shape a machine with no LiteLLM key was left in: the fleet holds
        one that works, so it is taken here rather than asked for."""
        self.key("ensure")
        self.registered()
        env = self.fleet_env(
            verdict="ok\tai.igalia.com accepts it\n    fingerprint: aaaaaaaaaaaa\n",
            give="sk-notarealvirtualkey\n",
            login="ok\tscopes: user:inference user:profile\n")
        cp = self.setup(env=env)
        out = cp.stdout + cp.stderr
        self.assertIn("key give litellm", self.calls(), out)
        self.assertEqual("sk-notarealvirtualkey",
                         (self.secrets / "litellm-key").read_text().strip(),
                         "the working key was not taken: " + out)

    def test_one_this_machine_holds_goes_to_a_peer_holding_none(self):
        """The other direction, with a credential the rule can judge without a
        network: the peer has none, so this machine's is put there."""
        self.key("ensure")
        self.registered()
        env = self.fleet_env(verdict="absent\tnothing stored\n",
                             login="ok\tscopes: user:inference user:profile\n")
        (self.tmp / "tailscale-authkey").write_text("tskey-auth-k1-abc\n")
        cp = self.setup(env=env)
        self.assertIn("key set tailnet --paste", self.calls(),
                      cp.stdout + cp.stderr)

    def test_the_login_is_never_elected_given_or_pasted(self):
        self.key("ensure")
        self.registered()
        env = self.fleet_env(
            verdict="ok\tit reaches exactly the forks\n    fingerprint: aaaaaaaaaaaa\n",
            give=GOOD_PAT + "\n",
            login="ok\tscopes: user:inference user:profile\n")
        cp = self.setup(env=env)
        calls = self.calls()
        self.assertNotIn("key give claude-login", calls, cp.stdout + cp.stderr)
        self.assertNotIn("key set claude-login --paste", calls)


class TestTheBestWorkingOneWins(_Fleet):
    """The election is decided by evidence taken at that moment, not by where a
    credential was last stored or how new it is: a peer holding one its issuer
    accepts beats one here that it refuses, and this machine takes it."""

    def refused_here(self):
        """A token GitHub no longer accepts, stored here and newer than
        anything else in the fleet: newness is not what decides."""
        (self.held / "github-pat").write_text(OTHER_PAT + "\n")

    def test_the_peers_token_is_taken_when_this_machines_is_refused(self):
        self.key("ensure")
        self.registered()
        self.refused_here()
        env = self.fleet_env(
            verdict="ok\tit reaches exactly the forks\n    fingerprint: aaaaaaaaaaaa\n",
            give=GOOD_PAT + "\n",
            login="ok\tscopes: user:inference user:profile\n")
        cp = self.setup(env=env)
        out = cp.stdout + cp.stderr
        self.assertIn("key give github-pat", self.calls(), out)
        self.assertEqual(GOOD_PAT, (self.held / "github-pat").read_text().strip(),
                         "the working token was not taken: " + out)
        self.assertIn("peerbox holds one its issuer accepts", out)

    def test_nothing_is_typed_or_minted_while_the_fleet_holds_a_working_one(self):
        self.key("ensure")
        self.registered()
        self.refused_here()
        env = self.fleet_env(
            verdict="ok\tit reaches exactly the forks\n    fingerprint: aaaaaaaaaaaa\n",
            give=GOOD_PAT + "\n",
            login="ok\tscopes: user:inference user:profile\n")
        cp = self.setup(env=env)
        self.assertNotIn("GitHub personal access token", cp.stderr,
                         "asked a person to mint one the fleet already has")

    def test_a_peers_deploy_key_is_taken_when_github_refuses_this_ones(self):
        """The registered key is the one that works, wherever it is held: this
        machine's is not registered, the peer's is, so the peer's is taken and
        the fleet holds one key."""
        self.key("ensure")
        peers_key = (self.held / "build_key_fork").read_text()
        peers_pub = (self.secrets / "build_key_fork.pub").read_text()
        # This machine mints a different key; the registered one stays the peer's.
        (self.held / "build_key_fork").unlink()
        self.key("ensure")
        self.gh_keys.write_text(peers_pub)
        env = self.fleet_env(verdict="absent\tnothing stored\n", pub=peers_pub,
                             give=peers_key, ssh=GITHUB_SAYS_HI,
                             login="ok\tscopes: user:inference user:profile\n")
        cp = self.setup(env=env)
        out = cp.stdout + cp.stderr
        self.assertIn("key give fork", self.calls(), out)
        self.assertEqual(peers_key, (self.held / "build_key_fork").read_text(),
                         "the key GitHub accepts was not taken: " + out)


    def test_one_no_workstation_accepts_is_not_written_over_a_peers(self):
        """Nobody holds a token GitHub accepts, so a fresh one is asked for
        here -- and with no terminal to ask at, the peer keeps what it has and
        the command to run is named."""
        self.key("ensure")
        self.registered()
        self.refused_here()
        env = self.fleet_env(verdict="bad\tGitHub refuses this one too\n",
                             login="ok\tscopes: user:inference user:profile\n")
        cp = self.setup(env=env)
        out = cp.stdout + cp.stderr
        self.assertNotIn("key set github-pat --paste", self.calls(), out)
        self.assertIn("no workstation holds one its issuer accepts", out)
        self.assertIn("wk key set github-pat --replace", out)
        self.assertEqual(OTHER_PAT, (self.held / "github-pat").read_text().strip())


class TestNothingMovesUntilTheQuestionIsAnswered(_Fleet):
    """`wk key setup` overwrites what the other workstations hold, so it asks
    once, first, and declines without a terminal. Declined, no peer is touched
    and this machine's own credentials are still set up."""

    def test_a_declined_run_writes_to_no_peer(self):
        self.key("ensure")
        self.registered()
        (self.held / "github-pat").write_text(GOOD_PAT + "\n")
        env = self.fleet_env(verdict="absent\tnothing stored\n")
        env["WK_YES"] = ""          # the dispatcher's --yes is what sets it
        cp = self.setup(env=env)
        out = cp.stdout + cp.stderr
        for wrote in ("key adopt", "key set"):
            self.assertNotIn(wrote, self.peer_log.read_text(), out)
        self.assertIn("peerbox was left exactly as it is", out)

    def test_a_declined_run_still_reports_this_machine(self):
        self.key("ensure")
        (self.held / "github-pat").write_text(GOOD_PAT + "\n")
        env = self.fleet_env(verdict="absent\tnothing stored\n")
        env["WK_YES"] = ""
        cp = self.setup(env=env)
        self.assertIn("credentials:", cp.stdout)
        self.assertIn("github-pat", cp.stdout)


class TestCheckAsksEachWorkstationWhatItHolds(_Fleet):
    """`wk key check` -- what `setup` ends with -- asks every peer workstation
    what it holds of each credential and whether it has a claude.ai login of
    its own, and names the one still without."""

    def check(self, **files):
        self.key("ensure")
        env = self.fleet_env(**files)
        return self.key("check", stubs={"ssh": FLEET_SSH, "gh": GH_RECORDER,
                                        "security": SECURITY_HAS_NOTHING}, env=env)

    def test_a_peer_without_a_login_is_named_with_the_command_to_run_here(self):
        cp = self.check(login="absent\tnothing stored\n",
                        verdict="ok\tit reaches exactly the forks\n")
        out = cp.stdout + cp.stderr
        self.assertIn("the other workstations", out)
        self.assertRegex(out, r"peerbox claude-login\s+no claude.ai login of its own")
        self.assertRegex(out.split("needs you:")[1],
                         r"peerbox claude-login\s+.*wk key setup")
        self.assertNotIn("buildbox", out.split("the other workstations")[1])
        self.assertNotEqual(0, cp.returncode)

    def test_what_a_peer_holds_of_each_credential_is_a_row(self):
        cp = self.check(login="ok\tscopes: user:inference user:profile\n",
                        verdict="ok\tit reaches exactly the forks\n")
        out = cp.stdout + cp.stderr
        for name in ("github-pat", "bugzilla-api-key", "claude", "litellm",
                     "tailnet", "tailnet-api", "ntfy"):
            with self.subTest(name=name):
                self.assertRegex(out, r"peerbox %s\s+it reaches exactly the forks"
                                 % name)

    def test_a_peer_holding_none_of_the_fleets_is_a_fault_with_one_remedy(self):
        cp = self.check(login="ok\tscopes: user:inference user:profile\n",
                        verdict="absent\tnothing stored\n")
        out = cp.stdout + cp.stderr
        self.assertRegex(out, r"peerbox github-pat\s+nothing stored")
        self.assertRegex(out.split("needs you:")[1],
                         r"peerbox github-pat\s+wk key setup\s+\(it puts the fleet's "
                         r"github-pat there\)")
        self.assertNotEqual(0, cp.returncode)

    def test_a_peer_holding_the_same_one_does_not_repeat_its_reach(self):
        """A credential is one credential wherever the fleet holds it, so what
        it can do is reported once. A peer holding the very credential this
        machine holds, with its issuer answering the same, says so; the
        sentence belongs to the `credentials:` row above."""
        self.base_env()          # it is what names the directories below
        self.held.mkdir(parents=True, exist_ok=True)
        (self.held / "github-pat").write_text(GOOD_PAT + "\n")
        fp = subprocess.run(
            ["python3", str(REPO / "lib" / "secretfile.py"), "fingerprint",
             str(self.held / "github-pat")],
            capture_output=True, text=True, check=True).stdout.strip()
        cp = self.check(login="ok\tscopes: user:inference user:profile\n",
                        verdict="ok\tit reaches exactly the forks\n"
                                "    fingerprint: %s\n" % fp)
        out = cp.stdout + cp.stderr
        self.assertRegex(out, r"peerbox github-pat\s+the one this machine holds")
        self.assertNotRegex(out, r"peerbox github-pat\s+it reaches exactly the forks")

    def test_a_peer_holding_a_different_one_reports_it_whole(self):
        """The other side of that branch: a peer whose credential is not this
        machine's is the whole verdict, and the election that settles it is
        named once, against the credential."""
        cp = self.check(login="ok\tscopes: user:inference user:profile\n",
                        verdict="ok\tit reaches exactly the forks\n"
                                "    fingerprint: not-the-one-here\n")
        out = cp.stdout + cp.stderr
        self.assertRegex(out, r"peerbox github-pat\s+it reaches exactly the forks")
        self.assertRegex(out.split("needs you:")[1],
                         r"github-pat\s+wk key setup\s+\(peerbox holds one")


class TestTheFleetSettlesWhatThisMachineCannotUse(_Fleet):
    """A credential this machine has none of, or one its issuer refuses, is not
    a trip to the issuer for a fresh one when another workstation holds one that
    works: the remedy `wk key check` names is the one command that takes it.
    A peer holding the very credential this machine holds settles nothing --
    both are the same bytes -- so that row keeps the issuer's remedy."""

    def check(self, pat=None, **files):
        self.key("ensure")
        if pat:
            (self.held / "github-pat").write_text(pat + "\n")
        env = self.fleet_env(**files)
        return self.key("check", stubs={"ssh": FLEET_SSH, "gh": GH_RECORDER,
                                        "security": SECURITY_HAS_NOTHING}, env=env)

    def peer_holds_a_working_one(self, fingerprint="not-the-one-here"):
        return dict(login="ok\tscopes: user:inference user:profile\n",
                    verdict="ok\tit reaches exactly the forks\n"
                            "    fingerprint: %s\n" % fingerprint)

    def test_one_this_machine_has_none_of_is_taken_rather_than_asked_for(self):
        cp = self.check(**self.peer_holds_a_working_one())
        needs = (cp.stdout + cp.stderr).split("needs you:")[1]
        self.assertRegex(needs, r"github-pat\s+wk key setup\s+\(peerbox holds one "
                                r"its issuer accepts\)")

    def test_one_the_issuer_refuses_here_is_settled_by_the_fleet(self):
        cp = self.check(pat=OTHER_PAT, **self.peer_holds_a_working_one())
        out = cp.stdout + cp.stderr
        needs = out.split("needs you:")[1]
        self.assertRegex(needs, r"github-pat\s+wk key setup\s+\(peerbox holds one "
                                r"its issuer accepts\)")
        self.assertNotRegex(needs, r"\n\s+\d+\. github-pat\s+wk key set github-pat")

    def test_one_credential_is_one_line_to_type(self):
        """This machine's row and the peer's are one fault, and one command
        settles every workstation, so it is named once."""
        cp = self.check(**self.peer_holds_a_working_one())
        needs = (cp.stdout + cp.stderr).split("needs you:")[1]
        self.assertEqual(1, len([l for l in needs.splitlines() if "litellm" in l]),
                         needs)

    def test_a_peer_holding_the_same_one_sends_you_to_the_issuer(self):
        """Two machines, one credential: taking it changes nothing, so the row
        keeps the remedy that mints a working one."""
        self.base_env()
        self.held.mkdir(parents=True, exist_ok=True)
        (self.held / "github-pat").write_text(OTHER_PAT + "\n")
        fp = subprocess.run(
            ["python3", str(REPO / "lib" / "secretfile.py"), "fingerprint",
             str(self.held / "github-pat")],
            capture_output=True, text=True, check=True).stdout.strip()
        cp = self.check(pat=OTHER_PAT, **self.peer_holds_a_working_one(fp))
        needs = (cp.stdout + cp.stderr).split("needs you:")[1]
        self.assertRegex(needs, r"github-pat\s+wk key set github-pat")
        self.assertNotRegex(needs, r"github-pat\s+wk key setup")


class TestGiveIsTheOtherHalfOfAdopt(_Shared):
    """A workstation running the election takes the winner with `wk key give`,
    over ssh and on stdout -- the other half of `wk key adopt`."""

    def test_it_prints_the_private_half_of_a_deploy_key(self):
        self.key("ensure")
        cp = self.key("give", "fork")
        self.assertEqual((self.held / "build_key_fork").read_text(), cp.stdout)

    def test_it_prints_a_stored_credential(self):
        self.key("ensure")
        (self.held / "github-pat").write_text(GOOD_PAT + "\n")
        (self.held / "github-pat").chmod(0o600)
        cp = self.key("give", "github-pat")
        self.assertEqual(GOOD_PAT, cp.stdout.strip())

    def test_it_never_hands_over_the_claude_ai_login(self):
        cp = self.key("give", "claude-login")
        self.assertNotEqual(0, cp.returncode)
        self.assertIn("never handed over", cp.stderr)

    def test_an_unknown_name_is_refused_by_name(self):
        cp = self.key("give", "nope")
        self.assertNotEqual(0, cp.returncode)
        self.assertIn("no fork or credential called 'nope'", cp.stderr)


class TestSetupLogsInForAWorkstationWithoutALogin(_Fleet):
    """The login is one holder's, so it is not copied: `wk key setup` logs in
    here, into a directory of its own, once for each peer whose own verdict is
    not usable, sends the two files over as a tar, and keeps nothing."""

    def setUp(self):
        super().setUp()
        self.key("ensure")
        self.registered()
        # Every other credential already here and accepted, so a run reaches the
        # peer's login without stopping at a prompt on the way.
        provision_credentials(self.secrets, self.tmp)

    def share(self, verdict, terminal=True, leaves=None):
        self.claude_log = self.tmp / "claude.log"
        self.claude_log.write_text("")
        leaves_text = login() if leaves is None else leaves
        leaves_file = self.tmp / "leaves"
        leaves_file.write_text(leaves_text)
        leaves_record = self.tmp / "leaves-record"
        leaves_record.write_text(record())
        env = self.fleet_env(login=verdict, verdict="absent\tnothing stored\n")
        self.taken = self.peer_files["stdin"]
        self.taken.write_bytes(b"")
        env.update({"WK_TEST_CLAUDE_LOG": str(self.claude_log),
                    "WK_TEST_LOGIN": str(leaves_file),
                    "WK_TEST_RECORD": str(leaves_record),
                    "WK_TS_AUTHKEY": str(self.tmp / "tailscale-authkey"),
                    "WK_TS_API_SECRET": str(self.tmp / "tailscale-api-key")})
        e = self.base_env()
        e.update(env)
        with stub_path({"ssh": FLEET_SSH, "claude": FAKE_CLAUDE,
                        "gh": GH_RECORDER,
                        "security": SECURITY_HAS_NOTHING}) as binp:
            e["PATH"] = f"{binp}:/usr/bin:/bin:/usr/sbin:/sbin"
            if not terminal:
                return subprocess.run([str(KEY), "setup"], cwd=str(REPO), env=e,
                                      input="", capture_output=True, text=True,
                                      timeout=300)
            master, slave = pty.openpty()
            try:
                cp = subprocess.run([str(KEY), "setup"], cwd=str(REPO), env=e,
                                    stdin=slave, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, timeout=300)
            finally:
                os.close(slave)
                os.close(master)
            cp.stderr = ""
            return cp

    def test_a_peer_without_a_login_is_logged_in_for_and_sent_the_two_files(self):
        cp = self.share("absent\tnothing stored\n")
        self.assertIn("key adopt claude-login", self.peer_log.read_text(), cp.stdout)
        self.assertIn("argv: auth login", self.claude_log.read_text())
        with tarfile.open(str(self.taken)) as tar:
            names = set(tar.getnames())
            self.assertEqual({".credentials.json", ".claude.json"}, names)
            self.assertEqual(login(), tar.extractfile(".credentials.json").read().decode())
        self.assertIn("peerbox: logged in for it", cp.stdout)
        self.assertNotIn(SECRET, cp.stdout)

    def test_the_login_made_for_a_peer_is_kept_nowhere_here(self):
        """One login is made, in a directory of its own, and that directory is
        gone afterwards -- this machine's own login is not where it landed and
        is not what was sent."""
        before = (self.secrets.parent / "agent-rw" / ".credentials.json").read_bytes()
        self.share("absent\tnothing stored\n")
        store = self.secrets.parent
        made_in = [l.split(": ", 1)[1] for l in self.claude_log.read_text().splitlines()
                   if l.startswith("store: ")]
        self.assertEqual(1, len(made_in), self.claude_log.read_text())
        self.assertNotIn(str(store), made_in[0])
        self.assertFalse(os.path.exists(made_in[0]), made_in[0])
        self.assertEqual(before,
                         (store / "agent-rw" / ".credentials.json").read_bytes())

    def test_a_peer_with_a_usable_login_is_left_alone(self):
        cp = self.share("ok\tscopes: user:inference user:profile\n")
        self.assertNotIn("key adopt claude-login", self.peer_log.read_text(), cp.stdout)
        self.assertEqual("", self.claude_log.read_text())

    def test_without_a_terminal_the_login_is_not_attempted_and_the_command_is_named(self):
        cp = self.share("absent\tnothing stored\n", terminal=False)
        out = cp.stdout + cp.stderr
        self.assertNotIn("key adopt claude-login", self.peer_log.read_text())
        self.assertEqual("", self.claude_log.read_text())
        self.assertIn("wk key setup", out)

    def test_a_login_the_rule_refuses_is_discarded_not_sent(self):
        """A login lacking the scope remote control needs never leaves here."""
        cp = self.share("absent\tnothing stored\n", leaves=login(scopes=["user:inference"]))
        self.assertNotIn("key adopt claude-login", self.peer_log.read_text())
        self.assertIn("not one a workspace can use", cp.stdout)


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
