"""`wk key` across workstations, end to end, with recording `gh` and `ssh` stubs; tests/test_wk_key.py holds the
election's every branch over a fake machine."""
import os
import subprocess
import sys
import unittest

from tests.support import REPO, WkTest, stub_path
from tests.test_credcheck import FakeGitHub, serve

sys.path.insert(0, str(REPO / "lib"))
from wk.secrets import FORKS  # noqa: E402

KEY = REPO / "cmd" / "key"

# Records every call; answers the key list and, filtered on the base64 body as real gh's --jq does, write access.
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
        for var in ("WK_NAME", "WK_PLACE", "WK_DRIVER", "WK_MARKER",
                    "WK_STORE", "WK_IN_VM"):
            env.pop(var, None)
        store = self.tmp / "store"
        self.secrets = store / "secrets"
        self.held = store / "push-keys"
        env.update({"WK_HOST_SECRETS": str(self.secrets), "WK_STORE": str(store),
                    "WK_NTFY_API": "http://127.0.0.1:1", "WK_YES": "1",
                    "WK_GITHUB_API": "http://127.0.0.1:1",
                    "WK_MACHINES_DIR": str(self.tmp / "reg")})
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
                f"kind={'peer' if peer else 'build'}\nhost=fake-{name}\nroot={root}\n"
                + ("peer=1\n" if peer else ""))
        return {"WK_TEST_PEER_LOG": str(log)}


class TestAdopt(_Shared):
    def test_a_private_key_on_stdin_is_held_and_its_public_derived(self):
        self.key("ensure")
        shared = (self.held / "build_key_fork").read_text()
        cp = self.key("adopt", "forkwpe", input=shared)
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertEqual(shared, (self.held / "build_key_forkwpe").read_text())
        self.assertFalse((self.secrets / "build_key_forkwpe").exists())
        self.assertEqual(0o600, (self.held / "build_key_forkwpe").stat().st_mode & 0o777)
        a = subprocess.run(["ssh-keygen", "-lf", str(self.secrets / "build_key_fork.pub")],
                           capture_output=True, text=True).stdout.split()[1]
        b = subprocess.run(["ssh-keygen", "-lf", str(self.secrets / "build_key_forkwpe.pub")],
                           capture_output=True, text=True).stdout.split()[1]
        self.assertEqual(a, b)

    def test_rubbish_on_stdin_is_refused_and_stores_nothing(self):
        cp = self.key("adopt", "fork", input="not a key\n")
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertFalse((self.held / "build_key_fork").exists())

    def test_an_unknown_fork_is_refused_by_name(self):
        cp = self.key("adopt", "nope", input="x\n")
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("nope", cp.stdout + cp.stderr)


# Answers as github.com for `ssh git@github.com`, and runs any other far command here.
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
  "key verdict "*)            answer "$WK_TEST_PEER_VERDICT" ;;
  "key give "*)               answer "$WK_TEST_PEER_GIVE" ;;
  "key pub "*)                answer "$WK_TEST_PEER_PUB" ;;
  "key sshtest "*)            answer "$WK_TEST_PEER_SSH" ;;
  *)                          cat >/dev/null 2>&1 ;;
esac
exit 0
'''

# A fine-grained token FakeGitHub accepts, and one it does not.
GOOD_PAT = "github_pat_11ABCDEFG_notarealtoken"
OTHER_PAT = "github_pat_11ZZZZZZZ_alsonotarealtoken"


class GitHubKnowsOnePat(FakeGitHub):
    """One token GitHub accepts and any other it refuses, judged per request from the header."""

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

    def setUp(self):
        super().setUp()
        GitHubKnowsOnePat.good = GOOD_PAT
        self.api = serve(GitHubKnowsOnePat, self.addCleanup)
        self.forks = [r[1] for r in FORKS]
        FakeGitHub.reset(repos=list(self.forks),
                         pulls=dict.fromkeys(self.forks, 422))
        self.gh_log = self.tmp / "gh.log"
        self.gh_log.write_text("")
        self.gh_keys = self.tmp / "gh.keys"
        self.gh_keys.write_text("")

    def fleet_env(self, **files):
        """Each keyword is one file the peer answers from: verdict, give, pub, ssh."""
        env = self.fleet()
        for key in ("verdict", "give", "pub", "ssh"):
            path = self.tmp / ("peer." + key)
            if key in files:
                path.write_text(files[key])
            env["WK_TEST_PEER_" + key.upper()] = str(path)
        self.peer_files = {k: self.tmp / ("peer." + k)
                           for k in ("verdict", "give", "pub", "ssh")}
        env.update({"WK_TEST_GH_LOG": str(self.gh_log),
                    "WK_TEST_GH_KEYS": str(self.gh_keys),
                    "WK_GITHUB_API": self.api,
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
        self.gh_keys.write_text("".join(
            (self.secrets / f"build_key_{f}.pub").read_text()
            for f in ("fork", "forkwpe")))

    def setup(self, *args, env=None, **stubs):
        e = {"gh": GH_RECORDER, "ssh": FLEET_SSH}
        e.update(stubs)
        return self.key("setup", *args, stubs=e, env=env)

    def calls(self):
        return [l for l in self.peer_log.read_text().splitlines() if l.strip()]

    def held_pat(self, value):
        """Store `value` here and answer its fingerprint."""
        self.base_env()
        self.held.mkdir(parents=True, exist_ok=True)
        (self.held / "github-pat").write_text(value + "\n")
        return subprocess.run(["python3", str(REPO / "lib" / "secretfile.py"), "fingerprint", str(self.held / "github-pat")],
                              capture_output=True, text=True, check=True).stdout.strip()


class TestEveryCredentialIsTheFleets(_Fleet):

    ELECTED = ("github-pat", "bugzilla-api-key", "claude", "litellm",
               "tailnet", "tailnet-api", "ntfy")

    def test_each_one_is_put_to_the_election(self):
        self.key("ensure")
        self.registered()
        env = self.fleet_env(verdict="absent\tnothing stored\n")
        cp = self.setup(env=env)
        asked = [l.split(" ", 2)[2] for l in self.calls()
                 if l.startswith("key verdict ")]
        for name in self.ELECTED:
            with self.subTest(name=name):
                self.assertIn(name, asked, cp.stdout + cp.stderr)

    def test_one_no_workstation_accepts_is_not_written_over_a_peers(self):
        """No terminal to ask for a fresh one: the peer keeps what it has and the command is named."""
        self.key("ensure")
        self.registered()
        (self.held / "github-pat").write_text(OTHER_PAT + "\n")
        env = self.fleet_env(verdict="bad\tGitHub refuses this one too\n")
        cp = self.setup(env=env)
        out = cp.stdout + cp.stderr
        self.assertNotIn("key set github-pat --paste", self.calls(), out)
        self.assertIn("wk key set github-pat --replace", out)
        self.assertEqual(OTHER_PAT, (self.held / "github-pat").read_text().strip())


class TestCheckAsksEachWorkstationWhatItHolds(_Fleet):

    def check(self, **files):
        self.key("ensure")
        env = self.fleet_env(**files)
        return self.key("check", stubs={"ssh": FLEET_SSH, "gh": GH_RECORDER}, env=env)

    def test_what_a_peer_holds_of_each_credential_is_a_row(self):
        cp = self.check(verdict="ok\tit reaches exactly the forks\n")
        out = cp.stdout + cp.stderr
        for name in ("github-pat", "bugzilla-api-key", "claude", "litellm",
                     "tailnet", "tailnet-api", "ntfy"):
            with self.subTest(name=name):
                self.assertRegex(out, r"peerbox %s\s+it reaches exactly the forks"
                                 % name)

    def test_a_peer_holding_none_of_the_fleets_is_a_fault_with_one_remedy(self):
        cp = self.check(verdict="absent\tnothing stored\n")
        out = cp.stdout + cp.stderr
        self.assertRegex(out, r"peerbox github-pat\s+nothing stored")
        self.assertRegex(out.split("needs you:")[1],
                         r"peerbox github-pat\s+wk key setup\s+\(it puts the fleet's "
                         r"github-pat there\)")
        self.assertNotEqual(0, cp.returncode)

    def test_a_peer_holding_the_same_one_does_not_repeat_its_reach(self):
        """A peer holding the very credential this machine holds is reported as that, its reach once."""
        fp = self.held_pat(GOOD_PAT)
        cp = self.check(verdict="ok\tit reaches exactly the forks\n"
                                "    fingerprint: %s\n" % fp)
        out = cp.stdout + cp.stderr
        self.assertRegex(out, r"peerbox github-pat\s+the one this machine holds")
        self.assertNotRegex(out, r"peerbox github-pat\s+it reaches exactly the forks")

class TestTheFleetSettlesWhatThisMachineCannotUse(_Fleet):
    """A credential missing or refused here is settled by `wk key setup` when a peer holds a working one."""

    def check(self, pat=None, **files):
        self.key("ensure")
        if pat:
            (self.held / "github-pat").write_text(pat + "\n")
        env = self.fleet_env(**files)
        return self.key("check", stubs={"ssh": FLEET_SSH, "gh": GH_RECORDER}, env=env)

    def peer_holds_a_working_one(self, fingerprint="not-the-one-here"):
        return dict(verdict="ok\tit reaches exactly the forks\n"
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
        cp = self.check(**self.peer_holds_a_working_one())
        needs = (cp.stdout + cp.stderr).split("needs you:")[1]
        self.assertEqual(1, len([l for l in needs.splitlines() if "litellm" in l]),
                         needs)

    def test_a_peer_holding_the_same_one_sends_you_to_the_issuer(self):
        fp = self.held_pat(OTHER_PAT)
        cp = self.check(pat=OTHER_PAT, **self.peer_holds_a_working_one(fp))
        needs = (cp.stdout + cp.stderr).split("needs you:")[1]
        self.assertRegex(needs, r"github-pat\s+wk key set github-pat")
        self.assertNotRegex(needs, r"github-pat\s+wk key setup")


class TestGiveIsTheOtherHalfOfAdopt(_Shared):

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

    def test_an_unknown_name_is_refused_by_name(self):
        cp = self.key("give", "nope")
        self.assertNotEqual(0, cp.returncode)
        self.assertIn("nope", cp.stderr)


if __name__ == "__main__":
    unittest.main()
