"""cmd/key end to end against a scratch keyring, with a `podman` that fails on PATH; tests/test_wk_key.py holds the
fleet election over a fake machine."""

import os
import subprocess
import sys
import unittest

from tests.support import REPO, WkTest, as_dispatched, bash, stub_path
from tests.test_credcheck import FINE

sys.path.insert(0, str(REPO / "lib"))
from wk.key import cli  # noqa: E402

KEY = REPO / "cmd" / "key"
PODMAN_TRAP = '#!/bin/sh\necho "podman was called" >&2\nexit 1\n'


class _KeyRun(WkTest):
    def key(self, *args, env=None, stubs=None, input=None):
        store = self.tmp / "store"
        secrets = store / "secrets"
        e = {"WK_HOST_SECRETS": str(secrets), "WK_STORE": str(store),
             "WK_NTFY_API": "http://127.0.0.1:1"}
        if env:
            e.update(env)
        with stub_path({"podman": PODMAN_TRAP, **(stubs or {})}) as binp:
            e["PATH"] = f"{binp}:/usr/bin:/bin:/usr/sbin:/sbin"
            cp = subprocess.run([str(KEY), *args], cwd=str(REPO), env={**self._base_env(), **e},
                                input=input, capture_output=True, text=True, timeout=120)
        return cp, secrets

    def _base_env(self):
        env = dict(os.environ)
        for var in ("WK_NAME", "WK_PLACE", "WK_DRIVER", "WK_MARKER",
                    "WK_STORE", "WK_IN_VM"):
            env.pop(var, None)
        env["WK_MACHINES_DIR"] = str(self.tmp / "no-registry")
        (self.tmp / "no-registry").mkdir(exist_ok=True)
        return env


class TestEnsureRunsHere(_KeyRun):
    def test_the_two_halves_go_to_the_two_directories_unreadable_to_anyone_else(self):
        cp, secrets = self.key("ensure")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("podman was called", cp.stderr)
        held = secrets.parent / "push-keys"
        for fork in ("fork", "forkwpe"):
            with self.subTest(fork=fork):
                self.assertTrue((held / f"build_key_{fork}").exists(), cp.stderr)
                self.assertTrue((secrets / f"build_key_{fork}.pub").exists())
                self.assertFalse((secrets / f"build_key_{fork}").exists())
        self.assertEqual(0o700, secrets.stat().st_mode & 0o777)
        self.assertEqual(0o700, held.stat().st_mode & 0o777)
        self.assertEqual(0o600, (held / "build_key_fork").stat().st_mode & 0o777)
        before = (held / "build_key_fork").read_bytes()
        cp, _ = self.key("ensure")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(before, (held / "build_key_fork").read_bytes(), "a second run generated a new key")

    def test_the_public_half_is_the_private_ones_own_and_nothing_sits_beside_the_key(self):
        """ssh refuses an identity whose `.pub` beside it disagrees."""
        _cp, secrets = self.key("ensure")
        held = secrets.parent / "push-keys"
        self.assertFalse((held / "build_key_fork.pub").exists())
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q",
                        "-f", str(self.tmp / "other")], check=True)
        stale = (self.tmp / "other.pub").read_text()
        (secrets / "build_key_fork.pub").write_text(stale)
        (held / "build_key_fork.pub").write_text(stale)
        cp, _ = self.key("ensure")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        derived = subprocess.run(["ssh-keygen", "-y", "-f", str(held / "build_key_fork")],
                                 capture_output=True, text=True, check=True).stdout
        self.assertEqual(derived, (secrets / "build_key_fork.pub").read_text())
        self.assertFalse((held / "build_key_fork.pub").exists())


class TestTheKeysAreReadFromHere(_KeyRun):
    def test_pub_reads_the_public_half_where_every_workspace_reads_it(self):
        _cp, _secrets = self.key("ensure")
        cp, _ = self.key("pub", "fork")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("ssh-ed25519", cp.stdout)

    def test_fingerprints_say_whether_this_machine_holds_a_private_half(self):
        _cp, secrets = self.key("ensure")
        held = secrets.parent / "push-keys"
        (held / "build_key_forkwpe").unlink()
        cp, _ = self.key("fingerprints")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertRegex(cp.stdout, r"fork\s+SHA256:\S+\s+private half here")
        self.assertRegex(cp.stdout, r"forkwpe\s+SHA256:\S+\s+no private half")

# exit 1 is the read_only evidence a refused call gives.
GH_OVERLAPS = '''#!/bin/sh
echo start >> "$GH_LOG"
sleep 1
echo end >> "$GH_LOG"
exit 1
'''

SSH_REFUSES = '#!/bin/sh\nexit 255\n'

GH_SAYS_NOTHING = '#!/bin/sh\nexit 0\n'


class TestCheckAsksAboutEveryCredential(_KeyRun):
    def test_a_bare_key_is_check_on_an_empty_machine_it_reports_every_credential_and_changes_nothing(self):
        self.assertEqual(as_dispatched("key", [], {}), ["check"])
        cp, secrets = self.key("check")
        self.assertNotEqual(0, cp.returncode, cp.stdout + cp.stderr)
        for fork in ("WebKit", "WPEWebKit"):
            self.assertIn(fork, cp.stdout)
        actions = cp.stdout.partition("needs you:")[2]
        self.assertEqual(2, actions.count("wk key deploy"), "one line per fork: " + actions)
        self.assertIn("credentials:", cp.stdout)
        for name in ("github-pat", "bugzilla-api-key", "claude", "litellm",
                     "tailnet", "tailnet-api", "ntfy"):
            with self.subTest(name=name):
                self.assertIn(name, cp.stdout)
        self.assertIn("nothing stored", cp.stdout)
        for word in ("sharing to", "registering", "minted"):
            self.assertNotIn(word, cp.stdout + cp.stderr)
        self.assertFalse((secrets.parent / "push-keys").exists())

    def test_a_broken_credential_fails_the_check_and_its_fix_leaves_the_table(self):
        cp, secrets = self.key("ensure")
        (secrets.parent / "push-keys" / "github-pat").write_text("hunter2\n")
        cp, _ = self.key("check")
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        table, _, actions = cp.stdout.partition("needs you:")
        rows = [l for l in table.splitlines() if l.startswith("    ") and l.strip()]
        self.assertTrue(rows, cp.stdout)
        for line in rows:
            self.assertNotIn("fix:", line)
        self.assertRegex(actions, r"\d+\. github-pat\s+wk key set github-pat --replace")
        self.assertIn("https://github.com/settings/tokens/new", actions)

    def test_a_row_that_fails_without_a_remedy_is_not_called_nothing_to_do(self):
        self.key("ensure")
        cp, _ = self.key("check", stubs={"gh": GH_SAYS_NOTHING,
                                         "ssh": SSH_IS_THE_FORKS_KEY})
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertNotIn("nothing needs you.", cp.stdout)
        self.assertNotIn("needs you:", cp.stdout)

    def test_the_fork_rows_are_asked_at_once(self):
        """Proved by overlap: two serial calls would log start, end, start, end."""
        calls = self.tmp / "gh-calls"
        self.key("ensure")
        cp, _ = self.key("check", env={"GH_LOG": str(calls)},
                         stubs={"gh": GH_OVERLAPS, "ssh": SSH_REFUSES})
        self.assertTrue(calls.exists(), cp.stdout + cp.stderr)
        marks = calls.read_text().split()
        self.assertEqual(marks[:2], ["start", "start"],
                         f"the fork rows ran one after the other: {marks}")

GH_REFUSES = '#!/bin/sh\necho "gh: refused in a test" >&2\nexit 1\n'


class TestSetupDoesWhateverIsMissing(_KeyRun):
    """`wk key setup`: the push keys, every credential not yet held (no terminal here, so none is asked), the report."""

    def setup_run(self):
        home = self.tmp / "home"
        home.mkdir(exist_ok=True)
        return self.key("setup", stubs={"gh": GH_REFUSES}, env={"HOME": str(home)})

    def test_what_it_could_not_settle_is_named_what_it_mints_is_made_and_the_rest_still_runs(self):
        cp, secrets = self.setup_run()
        self.assertNotEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertTrue((secrets.parent / "push-keys" / "build_key_fork").exists(), cp.stdout + cp.stderr)
        self.assertIn(cli.Key(REPO).settable()[-1], cp.stdout)
        left = [l for l in cp.stderr.splitlines() if "not settled:" in l]
        self.assertEqual(1, len(left), cp.stderr)
        for name in ("litellm", "tailnet-api"):
            with self.subTest(name=name):
                self.assertIn(name, left[0])
        topic = secrets.parent / "notify" / "ntfy-topic"
        self.assertTrue(topic.exists(), cp.stdout + cp.stderr)
        self.assertTrue(topic.read_text().strip())

    def test_rotate_turns_over_every_stored_credential(self):
        """Re-minted deploy keys, and each stored credential cleared then asked for or minted anew."""
        _cp, secrets = self.key("ensure")
        held = secrets.parent / "push-keys"
        old_key = (held / "build_key_fork").read_bytes()
        (held / "github-pat").write_text("github_pat_11ABC_notarealtoken\n")
        (secrets / "litellm-key").write_text("sk-litellm-old\n")
        home = self.tmp / "home"
        home.mkdir(exist_ok=True)
        cp, _ = self.key("setup", "--rotate",
                         stubs={"gh": GH_REFUSES},
                         env={"HOME": str(home), "WK_YES": "1"})
        out = cp.stdout + cp.stderr
        self.assertNotEqual(old_key, (held / "build_key_fork").read_bytes(),
                            "the deploy key was not re-minted: " + out)
        self.assertFalse((held / "github-pat").exists(), "the old token outlived its replace")
        self.assertFalse((secrets / "litellm-key").exists())

    def test_one_already_stored_is_left_exactly_as_it_is(self):
        _cp, secrets = self.key("ensure")
        token = secrets.parent / "push-keys" / "github-pat"
        token.write_text("github_pat_11ABC_notarealtoken\n")
        token.chmod(0o600)
        before = token.read_bytes()
        cp, _ = self.setup_run()
        self.assertEqual(before, token.read_bytes())
        self.assertIn("github-pat", cp.stdout)


class TestTheTopicIsMintedNotAsked(_KeyRun):
    """The topic is the whole credential: printed at the mint and by `wk key show`, and by nothing else."""

    SHARED = "a-topic-minted-on-the-first-machine"

    def topic_path(self, secrets):
        return secrets.parent / "notify" / "ntfy-topic"

    def test_a_machine_with_no_topic_mints_one_and_prints_the_subscribe_url_once(self):
        cp, secrets = self.key("set", "ntfy")
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        path = self.topic_path(secrets)
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        topic = path.read_text().strip()
        out = cp.stdout + cp.stderr
        self.assertEqual(1, out.count(topic), out)
        self.assertIn("https://ntfy.sh/" + topic, out)

    def test_only_show_prints_it_again(self):
        _cp, secrets = self.key("set", "ntfy")
        topic = self.topic_path(secrets).read_text().strip()
        for args in (("check",), ("set", "ntfy")):
            cp, _ = self.key(*args)
            with self.subTest(args=args):
                self.assertNotIn(topic, cp.stdout + cp.stderr)
        cp, _ = self.key("show")
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("https://ntfy.sh/" + topic, cp.stdout)

    def test_show_names_the_mint_when_this_machine_holds_no_topic(self):
        cp, secrets = self.key("show")
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertFalse(self.topic_path(secrets).exists())
        self.assertIn("wk key set ntfy", cp.stdout)

    def test_replacing_it_mints_a_different_one(self):
        _cp, secrets = self.key("set", "ntfy")
        first = self.topic_path(secrets).read_text().strip()
        cp, _ = self.key("set", "ntfy", "--replace", env={"WK_YES": "1"})
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        second = self.topic_path(secrets).read_text().strip()
        self.assertNotEqual(first, second)
        self.assertIn(second, cp.stdout + cp.stderr)

    def test_a_topic_from_another_machine_is_taken_on_stdin(self):
        cp, secrets = self.key("set", "ntfy", "--paste",
                               input=self.SHARED + "\n")
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertEqual(self.SHARED,
                         self.topic_path(secrets).read_text().strip())
        self.assertNotIn(self.SHARED, cp.stdout + cp.stderr)

    def test_a_pasted_topic_is_put_to_the_same_rule_and_nothing_names_the_mint(self):
        for given in ("not one word\n", ""):
            with self.subTest(given=given):
                cp, secrets = self.key("set", "ntfy", "--paste", input=given)
                self.assertNotEqual(0, cp.returncode, cp.stdout)
                self.assertFalse(self.topic_path(secrets).exists())
        self.assertIn("wk key set ntfy", cp.stdout + cp.stderr)

# GitHub with the deploy keys not yet registered, and with them registered; `false` answers the read_only query.
GH_NO_KEYS_YET = ('#!/bin/sh\ncase "$*" in *read_only*) echo false ;; esac\nexit 0\n')
GH_HAS_THE_KEYS = ('#!/bin/sh\ncase "$*" in\n'
                   '  *read_only*) echo false ;;\n'
                   '  *) cat "$WK_HOST_SECRETS"/build_key_*.pub 2>/dev/null ;;\n'
                   'esac\nexit 0\n')

# What github.com answers `wk key sshtest` for a key registered on its own fork.
SSH_IS_THE_FORKS_KEY = (
    '#!/bin/sh\ncase "$*" in\n'
    '  *build_key_forkwpe*) echo "Hi justinmichaud/WPEWebKit! You\'ve '
    'successfully authenticated, but GitHub does not provide shell access." ;;\n'
    '  *) echo "Hi justinmichaud/WebKit! You\'ve successfully authenticated, '
    'but GitHub does not provide shell access." ;;\n'
    'esac\nexit 0\n')


def provision_credentials(secrets, tmp):
    """Every credential a machine can hold, each one its rule accepts."""
    store = secrets.parent
    (store / "push-keys" / "github-pat").write_text(FINE + "\n")
    (store / "push-keys" / "bugzilla-api-key").write_text("notarealbugzillakey\n")
    (secrets / "claude-token").write_text("sk-ant-oat01-notarealtoken\n")
    (secrets / "litellm-key").write_text("sk-notarealvirtualkey\n")
    (store / "notify").mkdir(exist_ok=True)
    (store / "notify" / "ntfy-topic").write_text("a-topic-minted-here\n")
    (tmp / "tailscale-authkey").write_text("tskey-auth-k1-abc\n")
    (tmp / "tailscale-api-key").write_text("tskey-api-k1-abc\n")


class TestSetupSaysOneLinePerCredential(_KeyRun):
    """`wk key setup` prints one line per credential and `wk key check`'s table once, within a line budget."""

    EMPTY_BUDGET = 45
    PROVISIONED_BUDGET = 22

    def stubs(self, gh):
        return {"gh": gh, "ssh": SSH_IS_THE_FORKS_KEY}

    def env(self):
        home = self.tmp / "home"
        home.mkdir(exist_ok=True)
        return {"HOME": str(home),
                "WK_TS_AUTHKEY": str(self.tmp / "tailscale-authkey"),
                "WK_TS_API_SECRET": str(self.tmp / "tailscale-api-key"),
                "WK_TAILNET_API": "http://127.0.0.1:1",
                "WK_ANTHROPIC_API": "http://127.0.0.1:1",
                "WK_GITHUB_API": "http://127.0.0.1:1",
                "WK_BUGZILLA_API": "http://127.0.0.1:1"}

    def lines(self, cp):
        return [l for l in (cp.stdout + cp.stderr).splitlines() if l.strip()]

    def test_a_machine_with_nothing_stored_stays_under_its_budget(self):
        cp, _secrets = self.key("setup", stubs=self.stubs(GH_NO_KEYS_YET),
                                env=self.env())
        lines = self.lines(cp)
        self.assertLess(len(lines), self.EMPTY_BUDGET,
                        "\n".join(lines))
        for name in ("github-pat", "bugzilla-api-key", "claude", "litellm",
                     "tailnet", "tailnet-api"):
            with self.subTest(name=name):
                self.assertRegex(cp.stderr, r"%s\s+skipped\s+\S" % name)

    def test_a_machine_that_holds_them_all_stays_under_its_budget(self):
        _cp, secrets = self.key("ensure")
        provision_credentials(secrets, self.tmp)
        cp, _ = self.key("setup", stubs=self.stubs(GH_HAS_THE_KEYS),
                         env=self.env())
        lines = self.lines(cp)
        self.assertLess(len(lines), self.PROVISIONED_BUDGET, "\n".join(lines))
        for name in ("github-pat", "bugzilla-api-key", "claude", "litellm",
                     "tailnet", "tailnet-api", "ntfy"):
            with self.subTest(name=name):
                self.assertRegex(cp.stderr, r"%s\s+stored\s+/" % name)

    def test_the_table_is_one_row_per_credential(self):
        _cp, secrets = self.key("ensure")
        provision_credentials(secrets, self.tmp)
        cp, _ = self.key("check", stubs=self.stubs(GH_HAS_THE_KEYS),
                         env=self.env())
        rows = [l for l in cp.stdout.splitlines()
                if l.startswith("    ") and l.strip()]
        self.assertEqual(9, len(rows), cp.stdout)


class TestAnAuthKeyIsMintedNotOnlyHanded(WkTest):
    """`wk_tailscale_authkey` (lib/common.sh): a machine that can administer the tailnet mints its own key."""

    LIB = '. "%s/lib/common.sh"\n' % REPO

    def _env(self, authkey=None, api=None):
        env = {"WK_TS_AUTHKEY": str(authkey or self.tmp / "no-such-key")}
        env["WK_TS_API_SECRET"] = str(api or self.tmp / "no-such-api")
        return env

    def _api_key(self):
        p = self.tmp / "api"
        p.write_text("tskey-api-kAAAA-secret\n")
        return p

    def test_a_usable_stored_key_is_used_as_it_is(self):
        key = self.tmp / "authkey"
        key.write_text("tskey-auth-kAAAA-secret\n")
        cp = bash(self.LIB + "wk_tailscale_authkey\n", env=self._env(authkey=key))
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), str(key))

    def test_no_key_and_no_way_to_mint_names_the_one_command_that_stores_one(self):
        cp = bash(self.LIB + "wk_tailscale_authkey\n", env=self._env())
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("wk key set tailnet", cp.stdout + cp.stderr)

    def test_a_mint_that_failed_names_both_ways_to_a_key(self):
        cp = bash(self.LIB + "wk_tailscale_authkey\n",
                  env=self._env(api=self._api_key()))
        out = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0, out)
        self.assertIn("wk key set tailnet-api", out)
        self.assertRegex(out, r"wk key set tailnet(?!-api)")

    def test_a_key_is_present_if_usable_or_mintable_and_asking_mints_nothing(self):
        from tests.support import clean_env
        from wk import tailnet
        usable = self.tmp / "usable"
        usable.write_text("tskey-auth-k1-abc\n")
        broad = self.tmp / "broad"
        broad.write_text("tskey-api-k1-abc\n")
        for env, present in ((self._env(api=self._api_key()), True), (self._env(), False),
                             (self._env(authkey=usable), True), (self._env(authkey=broad), False)):
            with self.subTest(env=env):
                self.assertEqual(present, tailnet.Fleet(str(REPO), clean_env(env)).key_present())
        self.assertFalse((self.tmp / "no-such-key").exists())


if __name__ == "__main__":
    unittest.main()
