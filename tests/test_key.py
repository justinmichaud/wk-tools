"""cmd/key: the keys are this machine's own, and every subverb acts here.

The secrets directory is this device's (wk_secrets_dir, lib/store.sh); on macOS
the podman machine reads it as a read-only mount rather than holding it, so
`wk key ensure`, `wk key pub` and `wk key fingerprints` are ssh-keygen and a
file on this side, with no `podman machine ssh` in the path and nothing that
has to be running.

Run: python3 -m unittest tests.test_key -v
"""

import json
import os
import subprocess
import unittest

from tests.support import REPO, WkTest, func_body, stub_path
from tests.test_credcheck import FINE, login

KEY = REPO / "cmd" / "key"

# A `podman` that fails loudly if anything calls it: what this file is mostly
# about is that nothing does.
PODMAN_TRAP = '#!/bin/sh\necho "podman was called" >&2\nexit 1\n'


class _KeyRun(WkTest):
    """cmd/key against a scratch secrets directory, with the trap on PATH."""

    def key(self, *args, env=None, stubs=None, input=None):
        # wk_secrets_dir (lib/store.sh) reads WK_HOST_SECRETS on a macOS host
        # and $WK_STORE/secrets everywhere else. Pointing both at one directory
        # is what a real machine looks like, and is what makes these tests read
        # the directory the command actually wrote on either platform.
        #
        # WK_NTFY_API: the ntfy rule asks ntfy.sh whether it serves the topic,
        # and port 1 refuses at once -- the same answer a machine with no
        # network gives, and the branch that reports one unverified. No test
        # here reaches ntfy.sh.
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
        for var in ("WK_NAME", "WK_TARGET", "WK_TARGET_KIND", "WK_MARKER",
                    "WK_STORE", "WK_IN_VM"):
            env.pop(var, None)
        env["WK_TARGET_REGISTRY"] = str(self.tmp / "no-registry")
        (self.tmp / "no-registry").mkdir(exist_ok=True)
        return env


class TestEnsureRunsHere(_KeyRun):
    def test_the_two_halves_go_to_the_two_directories(self):
        """The private half is generated where it lives for good -- the
        directory nothing mounts -- and only the public one is copied to the
        directory every workspace reads."""
        cp, secrets = self.key("ensure")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("podman was called", cp.stderr)
        held = secrets.parent / "push-keys"
        for fork in ("fork", "forkwpe"):
            with self.subTest(fork=fork):
                self.assertTrue((held / f"build_key_{fork}").exists(), cp.stderr)
                self.assertTrue((secrets / f"build_key_{fork}.pub").exists())
                self.assertFalse((secrets / f"build_key_{fork}").exists(),
                                 "a private half is in the directory every workspace mounts")

    def test_the_directory_it_makes_is_not_readable_by_anyone_else(self):
        _cp, secrets = self.key("ensure")
        held = secrets.parent / "push-keys"
        self.assertEqual(0o700, secrets.stat().st_mode & 0o777)
        self.assertEqual(0o700, held.stat().st_mode & 0o777)
        self.assertEqual(0o600, (held / "build_key_fork").stat().st_mode & 0o777)

    def test_a_second_run_generates_nothing_new(self):
        _cp, secrets = self.key("ensure")
        held = secrets.parent / "push-keys"
        before = (held / "build_key_fork").read_bytes()
        cp, _ = self.key("ensure")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(before, (held / "build_key_fork").read_bytes())

    def test_the_public_half_is_re_asserted_from_the_private_one(self):
        """A secrets directory recreated without it would leave every
        workspace's ssh config naming an identity that is not there."""
        _cp, secrets = self.key("ensure")
        (secrets / "build_key_fork.pub").unlink()
        cp, _ = self.key("ensure")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertTrue((secrets / "build_key_fork.pub").exists())


class TestTheKeysAreReadFromHere(_KeyRun):
    def test_pub_reads_the_public_half_where_every_workspace_reads_it(self):
        _cp, _secrets = self.key("ensure")
        cp, _ = self.key("pub", "fork")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("ssh-ed25519", cp.stdout)

    def test_fingerprints_say_whether_this_machine_holds_a_private_half(self):
        """Where the private half is is not the switch: it is always in the
        directory nothing mounts, and whether it is loaded is `wk push`."""
        _cp, secrets = self.key("ensure")
        held = secrets.parent / "push-keys"
        (held / "build_key_forkwpe").unlink()
        cp, _ = self.key("fingerprints")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertRegex(cp.stdout, r"fork\s+SHA256:\S+\s+private half here")
        self.assertRegex(cp.stdout, r"forkwpe\s+SHA256:\S+\s+no private half")

    def test_nothing_here_reaches_the_podman_machine(self):
        """the hop is gone, not merely unused"""
        text = KEY.read_text()
        for gone in ("podman machine ssh", "IN_VM_SSH", "in_vm "):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, text)


class TestEnsureIsOneImplementation(WkTest):
    def test_deploy_ensures_through_this_same_file(self):
        """`wk key deploy` walks the machines and asks each to make its
        missing keys; for this one that is this file's own `ensure` arm, not a
        second copy of ssh-keygen"""
        text = KEY.read_text()
        self.assertIn('ensure_keys() { "$0" ensure', text)
        self.assertIn("ssh-keygen -t ed25519", text)
        self.assertEqual(1, text.count("ssh-keygen -t ed25519"))


# `gh` marking when it starts and when it ends, so a test can see whether two
# rows' calls overlap; exit 1 is the read_only evidence a refused call gives.
GH_OVERLAPS = '''#!/bin/sh
echo start >> "$GH_LOG"
sleep 1
echo end >> "$GH_LOG"
exit 1
'''

# No row may dial github.com from a test: this is the answer a key that cannot
# authenticate gives.
SSH_REFUSES = '#!/bin/sh\nexit 255\n'


class TestCheckAsksAboutEveryCredential(_KeyRun):
    """`wk key check` is the one report over all of them, and every line of it
    comes from an answer taken at that moment: the deploy keys through the same
    rule table as the rest (lib/credcheck.py's deploy-key row), and one line per
    credential this machine can hold."""

    def test_a_machine_with_no_keys_names_the_remedy_for_each_fork(self):
        cp, _secrets = self.key("check")
        self.assertNotEqual(0, cp.returncode, cp.stdout + cp.stderr)
        for fork in ("WebKit", "WPEWebKit"):
            self.assertIn(fork, cp.stdout)
        self.assertIn("wk key deploy", cp.stdout)

    def test_every_credential_is_reported_and_absence_is_not_a_fault(self):
        cp, _secrets = self.key("check")
        self.assertIn("credentials:", cp.stdout)
        for name in ("github-pat", "claude", "litellm", "claude-login",
                     "tailnet", "tailnet-api", "ntfy"):
            with self.subTest(name=name):
                self.assertIn(name, cp.stdout)
        self.assertIn("nothing stored", cp.stdout)

    def test_a_stored_credential_that_breaks_its_rule_fails_the_check(self):
        cp, secrets = self.key("ensure")
        (secrets.parent / "push-keys" / "github-pat").write_text("hunter2\n")
        cp, _ = self.key("check")
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("does not start like a GitHub personal access token",
                      cp.stdout)

    def test_every_row_is_one_line_plus_at_most_the_fix(self):
        """`wk key check` is an aligned table, so a row is a summary line and,
        for a credential that cannot do its job, the `fix:` line -- never the
        whole detail wrapped across the column."""
        cp, secrets = self.key("ensure")
        (secrets.parent / "push-keys" / "github-pat").write_text("hunter2\n")
        cp, _ = self.key("check")
        rows = [l for l in cp.stdout.splitlines()
                if l.startswith("    ") and l.strip()]
        for line in rows:
            with self.subTest(line=line):
                self.assertNotIn("it must ", line)
        self.assertTrue(any("fix:" in l for l in rows), cp.stdout)

    def test_the_fork_rows_are_asked_at_once(self):
        """Every row of the table is an independent probe -- a `gh api` call
        per fork per machine, an ssh test beside it, an HTTPS reach per
        credential -- so they are asked together and replayed in the table's
        order. Proved by overlap rather than by a clock: two serial calls
        would log start, end, start, end."""
        calls = self.tmp / "gh-calls"
        self.key("ensure")
        cp, _ = self.key("check", env={"GH_LOG": str(calls)},
                         stubs={"gh": GH_OVERLAPS, "ssh": SSH_REFUSES})
        self.assertTrue(calls.exists(), cp.stdout + cp.stderr)
        marks = calls.read_text().split()
        self.assertEqual(marks[:2], ["start", "start"],
                         f"the fork rows ran one after the other: {marks}")

    def test_the_switch_is_not_mistaken_for_where_the_private_half_is(self):
        """A private half is always in the directory nothing mounts, so its
        path says nothing about `wk push`; a guard on that path would make
        every key report the switch instead of GitHub's answer."""
        self.assertNotIn("push is off", KEY.read_text())


# A `gh` that refuses every call: `setup` and `deploy` must not reach GitHub
# from a test, and a refused API call is also what a machine with no network
# gives.
GH_REFUSES = '#!/bin/sh\necho "gh: refused in a test" >&2\nexit 1\n'

# `wk key set claude-login` reads what the Claude CLI stored on this machine
# (a Keychain item on macOS, ~/.claude/.credentials.json on Linux), so a test
# that walks every credential needs both of those pointed away from the
# maintainer's real login -- a scratch HOME and a `security` that answers
# nothing, the pair tests/test_claude_login.py uses.
SECURITY_HAS_NOTHING = '#!/bin/sh\nexit 1\n'


class TestSetupDoesWhateverIsMissing(_KeyRun):
    """`wk key setup` is the one command a new machine needs: the push keys,
    then every credential this machine has not got, then the report. Each step
    is independent -- one credential nobody can be asked for here (there is no
    terminal) must not leave the ones after it unset -- and a second run asks
    for nothing that is already stored."""

    def setup_run(self):
        home = self.tmp / "home"
        home.mkdir(exist_ok=True)
        return self.key("setup",
                        stubs={"gh": GH_REFUSES,
                               "security": SECURITY_HAS_NOTHING},
                        env={"HOME": str(home)})

    def test_it_reaches_every_credential_and_reports_them_all(self):
        cp, _secrets = self.setup_run()
        self.assertNotEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("credentials:", cp.stdout)
        for name in ("github-pat", "claude", "litellm", "claude-login",
                     "tailnet", "tailnet-api", "ntfy"):
            with self.subTest(name=name):
                self.assertIn(name, cp.stdout)

    def test_a_credential_it_cannot_ask_for_does_not_end_the_run(self):
        """No terminal, so every prompt refuses; the last credential in the
        table still gets its turn and the report still comes out."""
        cp, _secrets = self.setup_run()
        self.assertIn("Re-run interactively", cp.stderr)
        last = subprocess.run(["bash", "-c",
                               '. "%s/lib/common.sh"; . "%s/lib/store.sh"; '
                               'wk_cred_settable | tail -1' % (REPO, REPO)],
                              capture_output=True, text=True).stdout.strip()
        self.assertIn(last, cp.stdout)

    def test_it_makes_the_push_keys_on_the_way(self):
        cp, secrets = self.setup_run()
        self.assertTrue((secrets.parent / "push-keys" / "build_key_fork").exists(),
                        cp.stdout + cp.stderr)

    def test_a_credential_wk_mints_is_made_rather_than_asked_for(self):
        """The walk ends with every credential this machine can hold: one it is
        given is asked for, one it mints is simply made, so a new machine has a
        working ntfy topic without anybody inventing a name."""
        cp, secrets = self.setup_run()
        topic = secrets.parent / "notify" / "ntfy-topic"
        self.assertTrue(topic.exists(), cp.stdout + cp.stderr)
        self.assertTrue(topic.read_text().strip())
        self.assertIn("printed once", cp.stdout + cp.stderr)
        self.assertNotIn("ntfy.sh topic this machine's notifications go to",
                         cp.stdout + cp.stderr, "it asked for one instead")

    def test_one_already_stored_is_left_exactly_as_it_is(self):
        _cp, secrets = self.key("ensure")
        token = secrets.parent / "push-keys" / "github-pat"
        token.write_text("github_pat_11ABC_notarealtoken\n")
        token.chmod(0o600)
        before = token.read_bytes()
        cp, _ = self.setup_run()
        self.assertEqual(before, token.read_bytes())
        self.assertNotIn("GitHub personal access token", cp.stderr,
                         "asked for one it already has")
        self.assertIn("github-pat", cp.stdout)


class TestTheTopicIsMintedNotAsked(_KeyRun):
    """`wk key set ntfy` mints the topic, the way `wk key deploy` generates a
    deploy key rather than asking for one: a name a person invents is short and
    guessable, which lib/credcheck.py's rule can report and never prevent.

    The topic name is the whole credential, so the mint is the one moment it is
    shown -- a phone has to be pointed at it once. Every reader of the stored
    one reports on it without printing it (lib/wknotify.py's _out), which is
    what makes printing it here safe to do exactly once."""

    SHARED = "a-topic-minted-on-the-first-machine"

    def topic_path(self, secrets):
        return secrets.parent / "notify" / "ntfy-topic"

    def test_a_machine_with_no_topic_ends_up_with_one(self):
        cp, secrets = self.key("set", "ntfy")
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        path = self.topic_path(secrets)
        self.assertTrue(path.exists(), cp.stdout + cp.stderr)
        self.assertTrue(path.read_text().strip())
        self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_nothing_asks_a_person_for_a_name(self):
        cp, _secrets = self.key("set", "ntfy")
        self.assertNotIn("paste it", cp.stdout + cp.stderr)

    def test_it_prints_the_subscribe_url_once(self):
        cp, secrets = self.key("set", "ntfy")
        topic = self.topic_path(secrets).read_text().strip()
        out = cp.stdout + cp.stderr
        self.assertEqual(1, out.count(topic), out)
        self.assertIn("https://ntfy.sh/" + topic, out)
        for fact in ("app", "iOS", "Android"):
            with self.subTest(fact=fact):
                self.assertIn(fact, out)

    def test_nothing_prints_it_a_second_time(self):
        """`wk key check` and a re-run of `set` report on the stored topic;
        neither is the moment a phone is pointed at it."""
        _cp, secrets = self.key("set", "ntfy")
        topic = self.topic_path(secrets).read_text().strip()
        for args in (("check",), ("set", "ntfy")):
            cp, _ = self.key(*args)
            with self.subTest(args=args):
                self.assertNotIn(topic, cp.stdout + cp.stderr)

    def test_replacing_it_mints_a_different_one(self):
        _cp, secrets = self.key("set", "ntfy")
        first = self.topic_path(secrets).read_text().strip()
        cp, _ = self.key("set", "ntfy", "--replace")
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        second = self.topic_path(secrets).read_text().strip()
        self.assertNotEqual(first, second)
        self.assertIn(second, cp.stdout + cp.stderr)

    def test_a_topic_from_another_machine_is_taken_on_stdin(self):
        """A maintainer moving a topic between machines: the second machine
        holds the first's, so one phone subscription covers both. On stdin,
        because an argument is visible in `ps`."""
        cp, secrets = self.key("set", "ntfy", "--paste",
                               input=self.SHARED + "\n")
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertEqual(self.SHARED,
                         self.topic_path(secrets).read_text().strip())
        self.assertNotIn(self.SHARED, cp.stdout + cp.stderr)

    def test_a_topic_from_another_machine_is_put_to_the_same_rule(self):
        cp, secrets = self.key("set", "ntfy", "--paste",
                               input="not one word\n")
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("one word of letters", cp.stdout + cp.stderr)
        self.assertFalse(self.topic_path(secrets).exists())

    def test_nothing_on_stdin_stores_nothing_and_names_the_mint(self):
        cp, secrets = self.key("set", "ntfy", "--paste", input="")
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("wk key set ntfy", cp.stdout + cp.stderr)
        self.assertFalse(self.topic_path(secrets).exists())

    def test_paste_carries_a_handed_credential_too(self):
        """--paste takes any credential's value on stdin so a second workstation
        can hold the same one as the first -- how `wk key share` fans the API
        token out. A malformed one is still put to the rule and refused."""
        cp, _secrets = self.key("set", "github-pat", "--paste",
                                input="not-a-token\n")
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertNotIn("--paste is for a credential wk mints itself",
                         cp.stdout + cp.stderr)
        self.assertIn("does not start like a GitHub personal access token",
                      cp.stdout + cp.stderr)

    def test_paste_cannot_carry_a_claude_login(self):
        """The one credential --paste refuses: a claude.ai login is a browser
        flow, not a value."""
        cp, _secrets = self.key("set", "claude-login", "--paste",
                                input='{"claudeAiOauth":{}}\n')
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("--paste cannot carry a claude.ai login",
                      cp.stdout + cp.stderr)

# A `gh` whose answers are GitHub's for a machine whose deploy keys are not
# registered yet (an empty key list; every registration accepted) and for one
# where they are (the list carries the public halves). `read_only` is in the
# arguments of both the registration and the one query `wk key check` makes of
# a registered key, and `false` is the answer to that query.
GH_NO_KEYS_YET = ('#!/bin/sh\ncase "$*" in *read_only*) echo false ;; esac\nexit 0\n')
GH_HAS_THE_KEYS = ('#!/bin/sh\ncase "$*" in\n'
                   '  *read_only*) echo false ;;\n'
                   '  *) cat "$WK_HOST_SECRETS"/build_key_*.pub 2>/dev/null ;;\n'
                   'esac\nexit 0\n')

# `wk key sshtest` asks github.com what a deploy key authenticates as; this is
# that answer for a key registered on its own fork, keyed by the file ssh was
# handed, so a line budget is measured without a network in it.
SSH_IS_THE_FORKS_KEY = (
    '#!/bin/sh\ncase "$*" in\n'
    '  *build_key_forkwpe*) echo "Hi justinmichaud/WPEWebKit! You\'ve '
    'successfully authenticated, but GitHub does not provide shell access." ;;\n'
    '  *) echo "Hi justinmichaud/WebKit! You\'ve successfully authenticated, '
    'but GitHub does not provide shell access." ;;\n'
    'esac\nexit 0\n')


class TestSetupSaysOneLinePerCredential(_KeyRun):
    """What `wk key setup` prints is one line per credential -- name, what
    happened to it, and the path or the one-line reason -- the prompt sequence
    for the ones it has to ask for, and `wk key check`'s table once. The budget
    is asserted because verbosity arrives one defensible line at a time.

    Both runs are headless, so no prompt is printed; a terminal adds the three
    lines the rule carries (what it is, the page that mints one, the field that
    page cannot fill) per credential asked for."""

    EMPTY_BUDGET = 45
    PROVISIONED_BUDGET = 20

    def stubs(self, gh):
        return {"gh": gh, "ssh": SSH_IS_THE_FORKS_KEY,
                "security": SECURITY_HAS_NOTHING}

    def env(self):
        home = self.tmp / "home"
        home.mkdir(exist_ok=True)
        return {"HOME": str(home),
                "WK_TS_AUTHKEY": str(self.tmp / "tailscale-authkey"),
                "WK_TS_API_SECRET": str(self.tmp / "tailscale-api-key"),
                "WK_TAILNET_API": "http://127.0.0.1:1",
                "WK_ANTHROPIC_API": "http://127.0.0.1:1",
                "WK_GITHUB_API": "http://127.0.0.1:1"}

    def lines(self, cp):
        return [l for l in (cp.stdout + cp.stderr).splitlines() if l.strip()]

    def test_a_machine_with_nothing_stored_stays_under_its_budget(self):
        cp, _secrets = self.key("setup", stubs=self.stubs(GH_NO_KEYS_YET),
                                env=self.env())
        lines = self.lines(cp)
        self.assertLess(len(lines), self.EMPTY_BUDGET,
                        "\n".join(lines))
        for name in ("github-pat", "claude", "litellm", "claude-login",
                     "tailnet", "tailnet-api"):
            with self.subTest(name=name):
                self.assertRegex(cp.stderr, r"%s\s+skipped\s+\S" % name)

    def provision(self, secrets):
        """Every credential this machine can hold, each one its rule accepts."""
        store = secrets.parent
        (store / "push-keys" / "github-pat").write_text(FINE + "\n")
        (secrets / "claude-token").write_text("sk-ant-oat01-notarealtoken\n")
        (secrets / "litellm-key").write_text("sk-notarealvirtualkey\n")
        (store / "agent-rw").mkdir(exist_ok=True)
        (store / "agent-rw" / ".credentials.json").write_text(login())
        # The account record `claude auth login` writes beside the credential, in the CLI's config home (cmd/key points CLAUDE_CONFIG_DIR there); the rule reads its organization, which remote control needs.
        (store / "agent-rw" / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"organizationUuid": "org-1",
                              "organizationName": "Example Org"}}))
        (store / "notify").mkdir(exist_ok=True)
        (store / "notify" / "ntfy-topic").write_text("a-topic-minted-here\n")
        (self.tmp / "tailscale-authkey").write_text("tskey-auth-k1-abc\n")
        (self.tmp / "tailscale-api-key").write_text("tskey-api-k1-abc\n")

    def test_a_machine_that_holds_them_all_stays_under_its_budget(self):
        """Nothing to ask for and nothing to register: one line each saying
        where it is, then the table."""
        _cp, secrets = self.key("ensure")
        self.provision(secrets)
        cp, _ = self.key("setup", stubs=self.stubs(GH_HAS_THE_KEYS),
                         env=self.env())
        lines = self.lines(cp)
        self.assertLess(len(lines), self.PROVISIONED_BUDGET, "\n".join(lines))
        for name in ("github-pat", "claude", "litellm", "claude-login",
                     "tailnet", "tailnet-api", "ntfy"):
            with self.subTest(name=name):
                self.assertRegex(cp.stderr, r"%s\s+stored\s+/" % name)

    def test_the_table_is_one_row_per_credential(self):
        """A row is the verdict's summary line; the rest of a detail -- what the
        credential must do, and the fix -- is what a refusal prints."""
        _cp, secrets = self.key("ensure")
        self.provision(secrets)
        cp, _ = self.key("check", stubs=self.stubs(GH_HAS_THE_KEYS),
                         env=self.env())
        rows = [l for l in cp.stdout.splitlines()
                if l.startswith("    ") and l.strip()]
        self.assertEqual(9, len(rows), cp.stdout)


class TestTheOldNamesSayWhatReplacedThem(_KeyRun):
    """Four ways in became two, and a tombstone names the one that is left
    rather than printing a usage line."""

    def test_each_one_names_its_replacement(self):
        for old, want in (("register", "wk key deploy"),
                          ("claude", "wk key set claude"),
                          ("tailnet", "wk key set tailnet"),
                          ("tailnet-api", "wk key set tailnet-api")):
            with self.subTest(old=old):
                cp, _ = self.key(old)
                self.assertNotEqual(0, cp.returncode, cp.stdout + cp.stderr)
                self.assertIn(want, cp.stderr)
                self.assertIn("wk key setup", cp.stderr)

    def test_the_claude_one_names_both_of_claudes_credentials(self):
        """Which of the two `wk key claude` meant was never in the name."""
        cp, _ = self.key("claude")
        self.assertIn("claude-login", cp.stderr)

    def test_a_tombstone_needs_no_github_login(self):
        """A refusal that first demands `gh auth login` is a refusal nobody
        can read; the declaration clears `needs` for these too."""
        decl = [l for l in KEY.read_text().splitlines()
                if l.startswith("# wk: sub ")][0]
        cleared = decl.split("needs=")[0].split()[3].split(",")
        for old in ("register", "claude", "tailnet", "tailnet-api"):
            with self.subTest(old=old):
                self.assertIn(old, cleared)


class TestTheTailnetKeyScope(WkTest):
    """wk_tailscale_key_reject (lib/common.sh): tailscale spells three very
    different powers with one prefix. An auth key enrolls a node; an API access
    token administers the tailnet; an OAuth client secret mints tokens of its
    own. All three start `tskey-`, and this key is copied onto every card
    written from here -- so only the narrow one is accepted, and the properties
    that cannot be read from a key are reported as unverified rather than
    claimed."""

    def _reject(self, key):
        cp = self.bash(f'. "{REPO}/lib/common.sh"\n'
                       f'if why=$(wk_tailscale_key_reject {key!r}); then echo ACCEPTED\n'
                       f'else printf "REJECTED: %s\\n" "$why"; fi\n')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout

    def test_an_auth_key_is_accepted(self):
        self.assertIn("ACCEPTED", self._reject("tskey-auth-k123CNTRL-abcdef"))

    def test_an_api_access_token_is_refused_as_too_broad(self):
        out = self._reject("tskey-api-k123CNTRL-abcdef")
        self.assertIn("REJECTED", out)
        self.assertIn("API access token", out)
        self.assertIn("administers", out)

    def test_an_oauth_client_secret_is_refused_as_too_broad(self):
        for key in ("tskey-client-k123-abc", "tskey-oauth-k123-abc"):
            with self.subTest(key=key):
                out = self._reject(key)
                self.assertIn("REJECTED", out)
                self.assertIn("OAuth client secret", out)

    def test_a_tskey_that_is_none_of_them_is_refused_by_shape(self):
        out = self._reject("tskey-something-else")
        self.assertIn("REJECTED", out)
        self.assertIn("tskey-auth-", out)

    def test_nothing_and_nonsense_are_refused(self):
        self.assertIn("REJECTED", self._reject(""))
        self.assertIn("REJECTED", self._reject("hunter2"))

    def test_the_presence_check_uses_the_same_rule(self):
        """`wk doctor`'s read-only probe and the prompt cannot disagree about
        what a usable key is -- one function answers for both."""
        for key, present in (("tskey-auth-k1-abc", True),
                             ("tskey-api-k1-abc", False),
                             ("nonsense", False)):
            path = self.tmp / f"key-{present}-{key[:10]}"
            path.write_text(key + "\n")
            cp = self.bash(f'. "{REPO}/lib/common.sh"\n'
                           f'wk_tailscale_authkey_present && echo YES || echo NO\n',
                           env={"WK_TS_AUTHKEY": str(path)})
            with self.subTest(key=key):
                self.assertIn("YES" if present else "NO", cp.stdout)

    def test_the_card_helper_refuses_the_broad_ones_too(self):
        """The rule lives where the privilege is as well: admin/wk-card-priv
        writes what it is handed onto a card that leaves the building."""
        text = (REPO / "admin" / "wk-card-priv").read_text()
        self.assertIn("'^tskey-auth-'", text,
                      "the card helper still accepts any tskey- value")


if __name__ == "__main__":
    unittest.main()


class TestABareKeyChangesNothing(_KeyRun):
    """`wk key` with no verb is `wk key check`: a report. What writes to
    another machine or to GitHub -- the fan-out to peer workstations, the
    revocation `--rotate` starts with -- asks first, defaulting to No."""

    def test_the_default_verb_is_check(self):
        self.assertIn('ACTION="${1:-check}"', KEY.read_text())

    def test_it_prints_the_report_and_nothing_else(self):
        bare, _ = self.key()
        check, _ = self.key("check")
        self.assertIn("credentials:", bare.stdout)
        self.assertEqual(check.stdout, bare.stdout)
        for word in ("sharing to", "registering", "minted"):
            self.assertNotIn(word, bare.stdout + bare.stderr)

    def test_the_fan_out_asks_before_writing_over_a_peer(self):
        """deploy and setup go through deploy_keys, which asks before
        register_shared_keys and share_keys; the share arm asks itself."""
        body = func_body(KEY.read_text(), "deploy_keys")
        self.assertIn("confirm ", body)
        self.assertLess(body.index("confirm "), body.index("share_keys"),
                        "share_keys runs before the question is asked")
        arm = KEY.read_text().split("\nshare)", 1)[1].split("\ndeploy)", 1)[0]
        self.assertLess(arm.index("confirm "), arm.index("share_keys"), arm)

    def test_a_declined_fan_out_is_not_reported_as_done(self):
        arm = KEY.read_text().split("\nshare)", 1)[1].split("\ndeploy)", 1)[0]
        self.assertIn('die "not shared', arm, arm)
        body = func_body(KEY.read_text(), "deploy_keys")
        self.assertIn("return 3", body, body)

    def test_rotate_asks_before_revoking_on_github(self):
        body = func_body(KEY.read_text(), "deploy_keys")
        self.assertLess(body.index("confirm "), body.index("rotate_keys"),
                        "rotate_keys runs before the question is asked")
