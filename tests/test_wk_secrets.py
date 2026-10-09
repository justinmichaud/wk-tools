"""lib/wk/secrets.py over a fake machine that is a filesystem: a private half is `KEY:<fork>` and its public half
`PUB:<fork>`, so ssh-keygen's answers follow from the bytes."""
import contextlib
import io
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import act, guest, repos, secrets  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402

ROOT = str(REPO)
SECRETFILE = os.path.join(ROOT, "lib", "secretfile.py")
CONTRIBUTORS = os.path.join(ROOT, "lib", "contributors.py")
# `key_store <name>` (value on stdin) and `key_clear <name>` for bash, through cli.Key.
KEY_SH = """_key() { WK_STORE="$WK_STORE" WK_STORE_DEFAULT="${WK_STORE_DEFAULT:-}" WK_IN_VM="${WK_IN_VM:-}" PYTHONPATH="$WK_ROOT/lib" python3 -c 'import sys
from wk.key.cli import Key
k = Key(sys.argv[1])
if sys.argv[2] == "clear":
    sys.exit(k.clear(sys.argv[3]))
sys.exit(0 if k.store(sys.argv[3], sys.stdin.read().rstrip("\\n")) else 1)' "$WK_ROOT" "$@"; }
key_store() { _key store "$1"; }
key_clear() { _key clear "$1" </dev/null; }
"""
KEYS = [k[0] for k in secrets.push_keys()]


class World(Fake):
    """One machine: its files, and every stdin it was handed in `inputs`."""

    def __init__(self, base):
        super().__init__("here")
        self.base = base
        self.env = {"HOME": base + "/home", "WK_STORE": base + "/store", "WK_STORE_DEFAULT": base + "/store",
                    "WK_HOST_SECRETS": base + "/store/secrets", "XDG_STATE_HOME": base + "/state",
                    "WK_MACHINE": "wk-test", "WK_MARKER": base + "/no-marker",
                    "WK_REMOTE_MARKER": base + "/no-remote", "WK_MACHINES_DIR": base + "/registry"}
        self.inputs = []
        self.contributors = None
        self.react(["sh", "-c"], self._sh)
        self.react(["python3", SECRETFILE, "read"], lambda a, f: Result(0, f.files.get(a[3], "")))
        self.react(["python3", CONTRIBUTORS, "bugzilla-login"], self._bugzilla_login)
        self.react(["git", "-C"], lambda a, f: Result(0, f.contributors) if f.contributors else Result(128, "", "no mirror"))
        self.react(["ssh-keygen", "-lf"], self._fingerprint)
        self.react(["ssh-keygen", "-y", "-f"], self._public)
        self.react(["chmod"], lambda a, f: Result(0))
        self.react(["mv", "-f"], lambda a, f: Result(0 if f._mv(a[2], a[3]) else 1))

    def sec(self, macos=False):
        return secrets.Secrets(ROOT, self.env, self, macos=macos)

    @property
    def keyring_dir(self):
        return self.env["WK_HOST_SECRETS"]

    @property
    def held(self):
        return self.base + "/store/push-keys"

    def seed(self, forks=tuple(k[0] for k in secrets.push_keys()), pat="ghp-held", bz="bz-held"):
        for f in forks:
            self._set_file("%s/build_key_%s" % (self.held, f), "KEY:%s\n" % f)
            self._set_file("%s/build_key_%s.pub" % (self.keyring_dir, f), "PUB:%s\n" % f)
        if pat:
            self._set_file(self.held + "/github-pat", pat + "\nsecond line\n")
        if bz:
            self._set_file(self.held + "/bugzilla-api-key", bz + "\n")

    def run(self, argv, input=None, timeout=None):
        self.last_input = input or ""
        if input:
            self.inputs.append((tuple(argv), input))
        return super().run(argv, input=input, timeout=timeout)

    def act_run(self, argv, **kw):
        self.effects.append(("act", tuple(argv)))
        if act.dry_run():
            return Result(0)
        return super().act_run(argv, **kw)

    def _mv(self, src, dst):
        if src not in self.files:
            return False
        self.files[dst] = self.files.pop(src)
        return True

    def _path(self, rest):
        return shlex.split(rest)[0]

    def _sh(self, argv, _):
        line, inp = argv[2], self.last_input
        if len(argv) > 3:
            line = line.replace('"$0"', shlex.quote(argv[3]))
        m = re.match(r"umask 077 && cat > (.*)$", line)
        if m:
            self._set_file(self._path(m.group(1)), inp)
            return Result(0)
        m = re.match(r"umask 077 && mkdir -p (.*) && cat > (.*)$", line)
        if m:
            self._set_file(self._path(m.group(2)), inp)
            return Result(0)
        m = re.match(r"rm -f (.*)$", line)
        if m:
            self._drop(self._path(m.group(1)))
            return Result(0)
        return Result(127, "", "the fake has no answer for: " + line)

    def _fingerprint(self, argv, _):
        text = self.files.get(argv[2], "")
        return Result(0, "256 SHA256:%s wk (ED25519)\n" % text.strip()[4:]) if text.startswith("PUB:") else Result(1)

    def _public(self, argv, _):
        text = self.files.get(argv[3], "")
        return Result(0, "PUB:%s\n" % text.strip()[4:]) if text.startswith("KEY:") else Result(1, "", "invalid format")

    def _bugzilla_login(self, argv, _):
        for e in json.loads(self.last_input):
            if e.get("github") == argv[3] and e.get("emails"):
                return Result(0, e["emails"][0] + "\n")
        return Result(1)

    def argvs(self):
        return [e[1] for e in self.effects if e[0] == "run"]

    def acts(self):
        return [e for e in self.effects if e[0] in ("act", "write", "mkdir", "remove")]

    def state(self):
        return dict(self.files)


def quiet(fn, *args):
    with contextlib.redirect_stderr(io.StringIO()) as err:
        result = fn(*args)
    return result, err.getvalue()


class SecretsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wk-test-secrets-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.makedirs(self.tmp + "/store")   # a store this process can write: on a macOS host that is the local one
        p = mock.patch.dict(os.environ, {}, clear=False)
        p.start()
        self.addCleanup(p.stop)
        for v in ("WK_DRY_RUN", "WK_YES", "WK_FORCE", "WK_QUIET", "WK_DESTRUCTIVE", "WK_CONFIRMED", "WK_IN_VM"):
            os.environ.pop(v, None)
        self.addCleanup(act._forced.clear)
        self.w = World(self.tmp)

    def refused(self, fn, *args):
        with self.assertRaises(Refused) as cm:
            quiet(fn, *args)
        return cm.exception.status


class TestWhereThingsAre(SecretsTest):
    def test_a_row_of_the_agent_table_is_kept_by_its_kind(self):
        s = self.w.sec()
        self.assertEqual(s.cred_path("litellm"), self.w.keyring_dir + "/litellm-key")
        self.assertEqual(s.cred_path("claude-login"), self.tmp + "/store/claude-login/.credentials.json")
        self.assertIsNone(s.cred_path("no-such-credential"))

    def test_the_injectors_files_are_in_the_store_unless_named(self):
        s = self.w.sec()
        self.assertEqual(["push-github-pat", "read-github-pat", "push-bugzilla-api-key"],
                         [os.path.basename(f()) for f in (s.machine_pat, s.machine_read_pat, s.machine_bugzilla_key)])
        env = dict(self.w.env, WK_PUSH_PAT_FILE="/p/pat", WK_PUSH_READ_PAT_FILE="/p/read", WK_PUSH_BUGZILLA_KEY_FILE="/p/bz")
        named = secrets.Secrets(ROOT, env, self.w)
        self.assertEqual(["/p/pat", "/p/read", "/p/bz"], [named.machine_pat(), named.machine_read_pat(), named.machine_bugzilla_key()])

    def test_the_machine_is_reached_here_or_in_the_podman_vm(self):
        s = self.w.sec()
        self.assertEqual(["sh", "-c", "true"], s.machine_argv("true"))
        with mock.patch.object(s.store, "is_local", return_value=False):
            self.assertEqual(["podman", "machine", "ssh", "wk-test", "--", "true"], s.machine_argv("true"))


class TestAStoredCredentialIsReadTheOneWay(SecretsTest):
    def _refuse(self, name, verb, out=""):
        path = self.w.sec().cred_path(name)
        self.w.react(["python3", SECRETFILE, verb, path],
                     lambda a, f: Result(2, out, "wk: refusing to read %s: it is not a regular file.\n" % path))
        return path

    def test_a_refused_read_is_bad_and_carries_none_of_its_own_bytes(self):
        path = self._refuse("litellm", "read", out="a-broken-reader-leaked-this\n")
        verdict, err = quiet(self.w.sec().cred_verdict, "litellm")
        self.assertEqual("bad\tthe file at %s could not be read; the refusal above says why" % path, verdict)
        self.assertNotIn("leaked", verdict)
        self.assertIn("refusing to read", err)

    def test_cred_stored_answers_none_for_the_same_refusal(self):
        self._refuse("litellm", "present")
        stored, err = quiet(self.w.sec().cred_stored, "litellm")
        self.assertIsNone(stored)
        self.assertIn("refusing to read", err)


class TestRepoPushKeys(unittest.TestCase):
    """Each repo pushes through its own deploy keys; the default repo's are the forks its checkout is wired with."""

    def test_every_repo_has_its_own_keys_and_aliases_and_only_the_default_repos_are_the_forks(self):
        names = [k[0] for k in secrets.push_keys()]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(names), len({k[2] for k in secrets.push_keys()}))
        for r in repos.names():
            self.assertTrue(repos.Repo(r).push, r)
        self.assertEqual(repos.default().push, secrets.forks())
        others = [k for r in repos.names() if r != repos.default().name for k in repos.Repo(r).push]
        self.assertTrue(others)
        for k in others:
            self.assertNotIn(k, secrets.forks())

    def test_a_cloned_repos_github_repository_is_its_origins(self):
        m = mock.Mock()
        m.run.return_value = Result(0, "git@github.com:someone/wk-tools.git\n")
        rows = repos.push_rows(m, "/tools")
        self.assertEqual([f[1] for f in secrets.forks()], [r[1] for r in rows if r in secrets.forks()])
        self.assertIn("someone/wk-tools", [r[1] for r in rows])

    def test_the_setup_listing_names_every_key(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            secrets.main(["push-keys"])
        self.assertEqual([[k, a] for k, _, a in secrets.push_keys()], [l.split("  ") for l in out.getvalue().splitlines()])


class TestTheDeployKeysAreCopiedWhereTheServiceReadsThem(SecretsTest):
    def sec(self):
        """A store of its own for the keyring: the podman machine's push service reads its own."""
        return secrets.Secrets(ROOT, dict(self.w.env, WK_STORE_DEFAULT=self.tmp + "/held"), self.w, macos=False)

    def test_the_key_goes_in_on_stdin_and_never_as_an_argument(self):
        self.w._set_file(self.tmp + "/held/push-keys/build_key_fork", "KEY:fork\n")
        self.assertTrue(self.sec().push_key_sync("fork"))
        self.assertEqual("KEY:fork\n", self.w.files[self.tmp + "/store/push-keys/build_key_fork"])
        for argv in self.w.argvs():
            self.assertNotIn("KEY:", " ".join(argv))
        self.assertIn("KEY:fork\n", [i for a, i in self.w.inputs])

    def test_a_key_withdrawn_here_is_withdrawn_there(self):
        there = self.tmp + "/store/push-keys/build_key_fork"
        self.w._set_file(there, "KEY:old\n")
        self.assertTrue(self.sec().push_key_sync("fork"))
        self.assertNotIn(there, self.w.files)

    def test_a_refused_read_is_no_key(self):
        self.w._set_file(self.tmp + "/held/push-keys/build_key_fork", "KEY:fork\n")
        self.w.react(["python3", SECRETFILE, "read"], lambda a, f: Result(2, "", "wk: refusing to read"))
        there = self.tmp + "/store/push-keys/build_key_fork"
        self.w._set_file(there, "KEY:old\n")
        quiet(self.sec().push_key_sync, "fork")
        self.assertNotIn(there, self.w.files)

    def test_where_the_service_reads_the_keyring_nothing_is_copied(self):
        self.w.seed()
        self.assertTrue(self.w.sec().push_key_sync("fork"))
        self.assertEqual([], self.w.acts())


class TestTheInjectorsFiles(SecretsTest):
    def test_the_first_line_is_written_under_umask_077_never_as_an_argument_and_cleared(self):
        self.w.seed()
        s, path = self.w.sec(), self.tmp + "/a dir/push-github-pat"
        self.assertTrue(s.cred_write(path, "github-pat"))
        self.assertEqual("ghp-held\n", self.w.files[path])
        for argv in self.w.argvs():
            self.assertNotIn("ghp-held", " ".join(argv))
        s.cred_clear(path)
        self.assertNotIn(path, self.w.files)

    def test_nothing_held_writes_nothing(self):
        """An empty token file would be a token file: the injector would send `Authorization: Bearer`."""
        path = self.tmp + "/pat"
        self.assertFalse(self.w.sec().cred_write(path, "github-pat"))
        self.assertNotIn(path, self.w.files)

    def test_sync_is_write_or_clear(self):
        s, path = self.w.sec(), self.tmp + "/read-pat"
        self.w.files[path] = "ghp-the-old-one\n"
        s.cred_sync(path, "github-pat")
        self.assertNotIn(path, self.w.files)
        self.w.seed()
        s.cred_sync(path, "github-pat")
        self.assertEqual("ghp-held\n", self.w.files[path])

    def test_every_file_the_injector_reads_is_written_from_what_is_held(self):
        self.w.seed()
        self.w.dirs.add(self.w.held)
        self.assertTrue(self.w.sec().push_converge_machine())
        for path, what in ((self.w.sec().machine_read_pat(), "ghp-held\n"), (self.w.sec().machine_pat(), "ghp-held\n"),
                           (self.w.sec().machine_bugzilla_key(), "bz-held\n")):
            self.assertEqual(what, self.w.files[path], path)

    def test_a_credential_withdrawn_here_leaves_no_file_there(self):
        self.w.seed(pat="", bz="")
        self.w.dirs.add(self.w.held)
        for path, _ in self.w.sec().machine_creds():
            self.w.files[path] = "old\n"
        quiet(self.w.sec().push_converge_machine)
        for path, _ in self.w.sec().machine_creds():
            self.assertNotIn(path, self.w.files)

    def test_the_credentials_go_to_every_injector_this_machine_runs(self):
        self.w.seed()
        self.w.dirs.add(self.w.held)
        called = []
        with mock.patch.object(guest, "credentials_converge", lambda root, env, m: called.append(1) or True):
            self.assertTrue(self.w.sec(macos=True).push_deliver())
            self.assertEqual([1], called)
            self.assertEqual("ghp-held\n", self.w.files[self.tmp + "/store/read-github-pat"])
            self.w.sec(macos=False).push_deliver()
        self.assertEqual([1], called, "a Linux host has no guests' injector")

    def test_a_machine_that_cannot_see_the_held_token_leaves_the_injectors_alone(self):
        """The podman machine never mounts the held credentials."""
        read = self.tmp + "/store/read-github-pat"
        self.w._set_file(read, "ghp-held\n")
        _, err = quiet(self.w.sec().push_converge_machine)
        self.assertEqual("ghp-held\n", self.w.files[read])
        self.assertNotIn(("act", ("sh", "-c", "rm -f %s" % read)), self.w.effects)

    def test_a_machine_that_did_not_take_them_is_warned_about(self):
        self.w.seed()
        self.w.dirs.add(self.w.held)
        self.w.react(["sh", "-c"], lambda a, f: Result(255))
        _, err = quiet(self.w.sec().push_converge_machine)
        self.assertIn("did not take every credential and deploy key", err)


class TestSecretsIsPublished(SecretsTest):
    def contributors(self, user="justinmichaud"):
        self.w.contributors = json.dumps([{"github": "someone", "emails": ["else@example.test"]},
                                          {"github": user, "emails": ["me@example.test", "other@example.test"]}])

    def test_the_account_is_published_and_no_ssh_config_names_a_key(self):
        self.w.seed()
        quiet(self.w.sec().publish)
        self.assertNotIn(self.w.keyring_dir + "/ssh_config", self.w.files)
        self.assertEqual("justinmichaud\n", self.w.files[self.w.keyring_dir + "/github-user"])
        for p in self.w.files:
            if p.startswith(self.w.keyring_dir):
                self.assertNotIn("KEY:", self.w.files[p], p)

    def test_the_bugzilla_login_is_read_from_the_mirror_and_absent_without_one(self):
        self.contributors()
        quiet(self.w.sec().publish)
        for f in ("/bugzilla-user", "/view/container/bugzilla-user"):
            self.assertEqual("me@example.test\n", self.w.files[self.w.keyring_dir + f])
        self.w.contributors = None
        _, err = quiet(self.w.sec().publish)
        self.assertNotIn(self.w.keyring_dir + "/bugzilla-user", self.w.files)
        self.assertIn("no Bugzilla login", err)
        self.assertIn("wk sync", err)

    def test_the_view_is_the_delivered_rows_and_the_public_files(self):
        self.w.seed()
        for name in ("claude-token", "litellm-key"):
            self.w.files["%s/%s" % (self.w.keyring_dir, name)] = name + "\n"
        quiet(self.w.sec().publish)
        view = self.w.keyring_dir + "/view/container"
        self.assertEqual({"github-user", "litellm-key"}, set(self.w.listdir(view)))
        self.assertIn(("act", ("chmod", "0600", view + "/litellm-key")), self.w.effects)
        self.assertIn(("act", ("chmod", "0700", view)), self.w.effects)

    def test_a_file_that_belongs_to_no_row_is_taken_out_again(self):
        view = self.w.keyring_dir + "/view/container"
        self.w.files[view + "/claude-token"] = "planted\n"
        self.w.dirs.add(view)
        quiet(self.w.sec().publish)
        self.assertNotIn(view + "/claude-token", self.w.files)

    def test_a_second_publish_changes_nothing(self):
        self.w.seed()
        self.contributors()
        quiet(self.w.sec().publish)
        before = len(self.w.acts())
        quiet(self.w.sec().publish)
        self.assertEqual(before, len(self.w.acts()), self.w.acts()[before:])

    def test_a_publish_killed_after_any_effect_and_rerun_converges(self):
        from tests.killpoints import converges

        def world():
            w = World(self.tmp)
            w.seed()
            return types.SimpleNamespace(fake=w)

        converges(self, world, lambda n: quiet(n.fake.sec().publish), lambda n: n.fake.state())

    def test_in_the_podman_vm_nothing_is_written_and_what_is_there_is_read(self):
        self.w.env["WK_IN_VM"] = "1"
        d = self.w.sec().store.keyring_dir()
        for f in secrets.PUBLISHED:
            self.w.files[os.path.join(d, f)] = "published by the host\n"
        quiet(self.w.sec().store_publish)
        self.assertEqual([], self.w.acts())

    def test_in_the_podman_vm_a_missing_published_file_dies_with_the_remedy(self):
        self.w.env["WK_IN_VM"] = "1"
        d = self.w.sec().store.keyring_dir()
        self.w.files[d + "/github-user"] = "x\n"
        with self.assertRaises(Refused):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.w.sec().store_publish()
        self.assertIn("view/container/github-user", err.getvalue())
        self.assertIn("./setup --stage vmtools", err.getvalue())


class TestTheDeployKeys(SecretsTest):
    def test_a_key_made_here_is_copied_where_the_service_reads_it_and_a_failed_copy_is_a_warning(self):
        env = dict(self.w.env, WK_STORE_DEFAULT=self.tmp + "/held")
        s = secrets.Secrets(ROOT, env, self.w, macos=False)
        self.w._set_file(self.tmp + "/held/push-keys/build_key_fork", "KEY:fork\n")
        self.assertTrue(quiet(s.pub_publish, "fork")[0])
        self.assertEqual("KEY:fork\n", self.w.files[self.tmp + "/store/push-keys/build_key_fork"])
        self.w.react(["sh", "-c"], lambda a, f: Result(1))
        ok, err = quiet(s.pub_publish, "fork")
        self.assertTrue(ok)
        self.assertIn("did not reach this machine's push service", err)

    def test_an_adopted_key_is_kept_once_it_parses_and_published(self):
        s = self.w.sec()
        self.assertTrue(quiet(s.push_key_adopt, "fork", "KEY:fork")[0])
        self.assertEqual("KEY:fork\n", self.w.files[self.w.held + "/build_key_fork"])
        self.assertEqual("PUB:fork\n", self.w.files[self.w.keyring_dir + "/build_key_fork.pub"])

    def test_something_that_is_not_a_key_is_refused_and_leaves_nothing(self):
        self.w.seed(forks=("fork",))
        self.assertFalse(quiet(self.w.sec().push_key_adopt, "fork", "garbage")[0])
        self.assertEqual("KEY:fork\n", self.w.files[self.w.held + "/build_key_fork"])
        self.assertNotIn(self.w.held + "/build_key_fork.new", self.w.files)

    def test_no_public_half_is_left_beside_the_private_one(self):
        """ssh reads a `.pub` beside an identity and refuses the identity when the two disagree."""
        self.w.seed(forks=("fork",))
        self.w.files[self.w.held + "/build_key_fork.pub"] = "PUB:stale\n"
        quiet(self.w.sec().pub_publish, "fork")
        self.assertNotIn(self.w.held + "/build_key_fork.pub", self.w.files)


if __name__ == "__main__":
    unittest.main()
