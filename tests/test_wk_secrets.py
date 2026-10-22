"""lib/wk/secrets.py: where each credential is kept, how it is read, and how a workspace, an injector and a push
service are given it. Most of it over a fake machine that is a filesystem (a private half is `KEY:<fork>`, its public
half `PUB:<fork>`); a read's link refusal and a delivery's file modes on this one."""
import contextlib
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from tests.support import REPO, bash, clean_env

sys.path.insert(0, str(REPO / "lib"))
import credcheck  # noqa: E402
from wk import act, claudelogin, guest, repos, secrets  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Fake, Local, Result  # noqa: E402
from wk.store import Store  # noqa: E402

ROOT = str(REPO)
SECRETFILE = os.path.join(ROOT, "lib", "secretfile.py")
KEYS = [k[0] for k in secrets.push_keys()]
# Not a credential, and deliberately nothing like one.
PLACEHOLDER = "placeholder-value-for-this-test"


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
        m = re.match(r"(sha256sum <|test -e) (.*?)(?: \|\| test -L .*)?$", line)
        if m:
            text = self.files.get(self._path(m.group(2)))
            if text is None:
                return Result(1)
            return Result(0, hashlib.sha256(text.encode()).hexdigest() + "  -\n" if m.group(1) != "test -e" else "")
        return Result(127, "", "the fake has no answer for: " + line)

    def _fingerprint(self, argv, _):
        text = self.files.get(argv[2], "")
        return Result(0, "256 SHA256:%s wk (ED25519)\n" % text.strip()[4:]) if text.startswith("PUB:") else Result(1)

    def _public(self, argv, _):
        text = self.files.get(argv[3], "")
        return Result(0, "PUB:%s\n" % text.strip()[4:]) if text.startswith("KEY:") else Result(1, "", "invalid format")

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


@mock.patch.object(Store, "macos_host", False)   # a Linux machine, whatever runs the suite
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

    def test_a_second_converge_writes_nothing(self):
        self.w._set_file(self.tmp + "/held/push-keys/build_key_fork", "KEY:fork\n")
        self.w._set_file(self.tmp + "/held/push-keys/github-pat", "ghp-held\n")
        s = self.sec()
        pat = s.machine_pat()
        for _ in range(2):
            self.w.effects.clear()
            self.assertTrue(s.push_key_sync("fork") and s.cred_sync(pat, "github-pat"))
        self.assertEqual([], [e for e in self.w.effects if e[0] == "act"])
        self.assertEqual("ghp-held\n", self.w.files[pat])
        self.w._drop(self.tmp + "/held/push-keys/build_key_fork")
        self.assertTrue(s.push_key_sync("fork"))
        self.assertNotIn(self.tmp + "/store/push-keys/build_key_fork", self.w.files)
        self.w.effects.clear()
        self.assertTrue(s.push_key_sync("fork"))
        self.assertEqual([], [e for e in self.w.effects if e[0] == "act"], "nothing there is nothing to remove")

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
    @mock.patch.object(Store, "macos_host", False)
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


class TestTheAgentTable(SecretsTest):
    """AGENT_SECRETS: one file per name in this machine's store, given to the kinds of place its last column names."""

    def test_every_kind_a_row_names_exists_and_a_kind_given_the_login_is_given_no_token(self):
        kinds = set(secrets.LOGIN_KINDS).union(*(r[4].split(",") for r in secrets.AGENT_SECRETS))
        self.assertEqual({"container", "vm", "remote"}, kinds)
        for kind in secrets.LOGIN_KINDS:
            self.assertNotIn("claude", [r[0] for r in secrets.AGENT_SECRETS if kind in r[4].split(",")])

    def test_each_name_is_its_own_file_and_a_build_box_reads_the_one_it_was_delivered(self):
        s = self.w.sec()
        for name, store_file, home_file, _var, kinds in secrets.AGENT_SECRETS:
            self.assertEqual(self.w.keyring_dir + "/" + store_file, s.cred_path(name))
        box = dict(self.w.env, WK_REMOTE_MARKER=self.tmp + "/.wk-remote", HOME=self.tmp)
        open(self.tmp + "/.wk-remote", "w").close()
        self.assertEqual(self.tmp + "/.wk-litellm-key", secrets.Secrets(ROOT, box, self.w).cred_path("litellm"))

    def test_a_driver_moving_wk_store_does_not_move_them(self):
        """One set per machine: WK_HOST_SECRETS on macOS, the pre-override WK_STORE_DEFAULT elsewhere."""
        env = {"WK_HOST_SECRETS": "/this/device/secrets", "WK_STORE_DEFAULT": "/the/machine/store",
               "WK_STORE": "/some/drivers/own/state", "HOME": self.tmp}
        for system, want in (("Linux", "/the/machine/store/secrets/litellm-key"), ("Darwin", "/this/device/secrets/litellm-key")):
            with self.subTest(system=system), mock.patch("wk.store.os.uname", return_value=mock.Mock(sysname=system)):
                self.assertEqual(want, secrets.Secrets(ROOT, env, Local(), macos=system == "Darwin").cred_path("litellm"))
        sec = self.w.sec()
        self.assertEqual(os.path.dirname(sec.store.keyring_dir()), os.path.dirname(sec.store.keyring_agent_rw_dir()))

    def test_what_a_workspace_missing_one_is_owed_is_this_machines_store(self):
        s = self.w.sec()
        with mock.patch.object(s, "cred_stored", return_value=False):
            self.assertIn("wk key set litellm", s.agent_secret_remedy("litellm"))
        for verdict, says in (("bad\trefused\n    fix: x", "wk key set litellm --replace"), ("ok\tfine", "'wk rm' and 'wk new'")):
            with mock.patch.object(s, "cred_stored", return_value=True), mock.patch.object(s, "cred_verdict", return_value=verdict):
                self.assertIn(says, s.agent_secret_remedy("litellm"))


class WorkspaceShell(Local):
    """A workspace's shell, run here with HOME at its home; every argv kept."""

    def __init__(self, home):
        self.home, self.argvs = home, []

    def run(self, argv, input=None, timeout=None, stream=False):
        self.argvs.append(argv)
        return super().run(["env", "HOME=" + self.home] + list(argv), input=input, timeout=timeout)


class TestTheDelivery(SecretsTest):
    """secrets.deliver, the one way a workspace is given its agent credentials: a container at its first run
    (container/firstrun.sh), a guest at every start, a build box at `wk machine setup`."""

    def deliver(self, kind, held, stale=True):
        home = os.path.join(self.tmp, kind)
        os.makedirs(home)
        for row in secrets.AGENT_SECRETS if stale else ():
            with open(os.path.join(home, row[2]), "w") as f:
                f.write("stale\n")
        m = WorkspaceShell(home)
        ok, err = quiet(secrets.deliver, m, kind, lambda n: held.get(n, ""), "ws")
        return ok, err, home, m

    def test_each_kind_holds_what_its_rows_give_it_and_nothing_else(self):
        held = {r[0]: "%s-%s\nsecond line\n" % (r[0], PLACEHOLDER) for r in secrets.AGENT_SECRETS}
        for kind in ("container", "vm", "remote"):
            with self.subTest(kind=kind):
                ok, err, home, m = self.deliver(kind, held)
                self.assertTrue(ok, err)
                for name, store_file, home_file, _var, kinds in secrets.AGENT_SECRETS:
                    p = os.path.join(home, home_file)
                    if kind not in kinds.split(","):
                        self.assertFalse(os.path.lexists(p), home_file)
                    elif kind == "container":
                        self.assertEqual("/secrets/" + store_file, os.readlink(p))
                    else:
                        self.assertEqual("%s-%s\n" % (name, PLACEHOLDER), open(p).read())
                        self.assertEqual(0o600, os.stat(p).st_mode & 0o777)
                login = os.path.join(home, secrets.LOGIN_DIR, ".credentials.json")
                self.assertEqual(kind in secrets.LOGIN_KINDS, os.path.exists(login))
                if os.path.exists(login):
                    self.assertEqual((claudelogin.placeholder(), 0o600), (open(login).read(), os.stat(login).st_mode & 0o777))
                self.assertNotIn(PLACEHOLDER, repr(m.argvs), "a value is never an argument")

    def test_a_store_with_none_withdraws_what_a_place_holds_and_names_the_command(self):
        for kind in ("container", "vm", "remote"):
            with self.subTest(kind=kind):
                ok, err, home, _ = self.deliver(kind, {})
                self.assertTrue(ok, err)
                for name, _f, home_file, _v, kinds in secrets.AGENT_SECRETS:
                    self.assertFalse(os.path.exists(os.path.join(home, home_file)), home_file)
                    self.assertEqual(kind in kinds.split(","), "wk key set %s" % name in err, err)

    def test_a_refused_read_fails_the_delivery_and_costs_no_other_row(self):
        ok, err, home, _ = self.deliver("remote", {"claude": None, "litellm": "sk-" + PLACEHOLDER}, stale=False)
        self.assertFalse(ok)
        self.assertIn("refused", err)
        self.assertEqual("sk-%s\n" % PLACEHOLDER, open(os.path.join(home, ".wk-litellm-key")).read())

    def test_a_container_is_given_them_by_its_first_run(self):
        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        env = dict(os.environ, HOME=home, PYTHONPATH=ROOT + "/lib")
        cp = subprocess.run([sys.executable, "-m", "wk.secrets", "deliver", "container", "/opt/wk-tools"], capture_output=True,
                            text=True, env=env, timeout=60)
        self.assertEqual(0, cp.returncode, cp.stderr)
        self.assertEqual("/opt/wk-tools/" + secrets.PUSH_CLIENT, subprocess.run(
            ["git", "config", "--global", "core.sshCommand"], env=env, capture_output=True, text=True).stdout.strip())
        self.assertEqual("/secrets/litellm-key", os.readlink(os.path.join(home, ".wk-litellm-key")))
        self.assertIn("wk key set litellm", cp.stderr)

    def test_a_build_box_is_given_them_by_wk_machine_setup(self):
        from wk import machine_cmd
        t = types.SimpleNamespace(_far=lambda: "far")
        with mock.patch.object(secrets, "deliver") as deliver:
            machine_cmd.Machines(ROOT, env=self.w.env, here=self.w).credentials(t, "box")
        self.assertEqual(("far", "remote"), deliver.call_args[0][:2])


class TestTheShellExportsThem(SecretsTest):
    """shell/bashrc, in every shell a person or `wk` starts, exports each row's first non-comment line, and points the
    CLI at the placeholder login only where one was written."""

    SHELLS = (("zsh", "-i", "-c"), ("zsh", "-l", "-c"), ("bash", "-lc"), ("bash", "-c"))

    def exported(self, shell, home):
        script = '. "%s"; ' % (REPO / "shell" / "bashrc") + "; ".join(
            'echo "%s=${%s-}"' % (v, v) for v in [r[3] for r in secrets.AGENT_SECRETS] + ["CLAUDE_SECURESTORAGE_CONFIG_DIR"])
        cp = subprocess.run(list(shell) + [script], cwd=ROOT, capture_output=True, text=True, timeout=120,
                            env={"HOME": home, "TERM": "dumb", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})
        return dict(l.split("=", 1) for l in cp.stdout.splitlines() if "=" in l)

    def test_every_shell_exports_every_row_and_an_absent_or_dangling_one_sets_nothing(self):
        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        first, second = secrets.AGENT_SECRETS[:2]
        with open(os.path.join(home, first[2]), "w") as f:
            f.write("# wk: written by lib/wk/secrets.py\n" + PLACEHOLDER + "\n")
        os.symlink(os.path.join(home, "nothing-here"), os.path.join(home, second[2]))
        for shell in self.SHELLS:
            if shutil.which(shell[0]):
                with self.subTest(shell=" ".join(shell)):
                    self.assertEqual({first[3]: PLACEHOLDER, second[3]: "", "CLAUDE_SECURESTORAGE_CONFIG_DIR": ""},
                                     self.exported(shell, home))
        os.makedirs(os.path.join(home, secrets.LOGIN_DIR))
        self.assertEqual(os.path.join(home, secrets.LOGIN_DIR), self.exported(("bash", "-c"), home)["CLAUDE_SECURESTORAGE_CONFIG_DIR"])

    def test_a_guests_rc_drops_what_an_older_one_exported(self):
        for old in ('export CLAUDE_SECURESTORAGE_CONFIG_DIR="$HOME/.claude-login"\n',
                    'export CLAUDE_SECURESTORAGE_CONFIG_DIR="/Volumes/My Shared Files/agent-rw"\n'):
            with self.subTest(old=old):
                home = tempfile.mkdtemp(dir=self.tmp)
                with open(os.path.join(home, ".zshrc"), "w") as f:
                    f.write("\n# wk-tools: the claude.ai login this host shares over virtiofs\n" + old)
                cp = subprocess.run(["bash", str(REPO / "vm" / "shell-rc.sh"), ROOT], capture_output=True, text=True,
                                    env={"HOME": home, "PATH": os.environ["PATH"]}, timeout=120)
                self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
                self.assertNotIn("CLAUDE_SECURESTORAGE_CONFIG_DIR", open(os.path.join(home, ".zshrc")).read())


class TestNothingButAFileIsReadOrWrittenThroughALink(SecretsTest):
    """lib/secretfile.py, on this machine: a link planted where a credential is kept never turns a read or a write of it
    into one of another credential."""

    REAL = "not-a-real-credential-just-this-tests-bytes"

    def setUp(self):
        super().setUp()
        self.env = {"WK_HOST_SECRETS": self.tmp + "/store/secrets", "WK_STORE": self.tmp + "/store", "HOME": self.tmp}
        self.sec = secrets.Secrets(ROOT, self.env, Local())
        self.cred = self.sec.cred_path("claude-login")
        self.token = self.sec.cred_path("github-pat")
        for p in (self.cred, self.token):
            os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(self.token, "w") as f:
            f.write(self.REAL + "\n")

    def store(self, value):
        from wk.key import cli
        k = cli.Key(ROOT, self.env, machine=Local(), reg=types.SimpleNamespace(machine=None), sec=self.sec)
        return quiet(k.store, "claude-login", value)

    def test_a_link_out_of_it_is_refused_by_every_entry_point(self):
        # O_NOFOLLOW cannot see a hard link; only st_nlink says so.
        for kind, plant, why in (("symlink", lambda: os.symlink(self.token, self.cred), "not a file"),
                                 ("hard link", lambda: os.link(self.token, self.cred), "hard links")):
            with self.subTest(kind):
                plant()
                for entry, run in (("cred_read", lambda: quiet(self.sec.cred_read, "claude-login")),
                                   ("cred_stored", lambda: quiet(self.sec.cred_stored, "claude-login")),
                                   ("store", lambda: self.store("replacement"))):
                    got, err = run()
                    self.assertIn(got, (None, False), entry)
                    self.assertNotIn(self.REAL, str(got) + err, entry)
                    self.assertIn(why, err, entry)
                    self.assertIn(self.cred, err, entry)
                self.assertEqual(self.REAL + "\n", open(self.token).read(), "the write went through the link")
                os.unlink(self.cred)

    def test_a_directory_in_its_place_is_refused_a_missing_one_is_absent_and_a_file_round_trips(self):
        self.assertEqual((False, ""), (quiet(self.sec.cred_stored, "claude-login")[0], quiet(self.sec.cred_read, "claude-login")[0]))
        os.mkdir(self.cred)
        got, err = quiet(self.sec.cred_stored, "claude-login")
        self.assertIsNone(got, err)
        self.assertIn("not a regular file", err)
        os.rmdir(self.cred)
        self.assertTrue(self.store("{-a-: 1}")[0])
        self.assertEqual((True, "{-a-: 1}\n"), (quiet(self.sec.cred_stored, "claude-login")[0], quiet(self.sec.cred_read, "claude-login")[0]))
        self.assertEqual(0o600, os.stat(self.cred).st_mode & 0o777)

    def test_an_injectors_copy_is_written_at_mode_600_and_cleared_through_a_path_with_a_space(self):
        os.makedirs(self.tmp + "/a dir")
        pat = self.tmp + "/a dir/push-github-pat"
        self.assertTrue(self.sec.cred_write(pat, "github-pat"))
        self.assertEqual((self.REAL + "\n", 0o600), (open(pat).read(), os.stat(pat).st_mode & 0o777))
        self.sec.cred_clear(pat)
        self.assertFalse(os.path.exists(pat))

    def test_the_setup_entry_point_writes_every_file_the_injector_reads(self):
        with open(os.path.dirname(self.token) + "/bugzilla-api-key", "w") as f:
            f.write("bz-key\n")
        with mock.patch.dict(os.environ, self.env), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(0, secrets.main(["push-converge"]))
        for f, want in (("read-github-pat", self.REAL), ("push-github-pat", self.REAL), ("push-bugzilla-api-key", "bz-key")):
            self.assertEqual(want + "\n", open(self.tmp + "/store/" + f).read(), f)


class TestDoctorNamesWhatTheMachineHolds(SecretsTest):
    """Each copy and each agent credential is one machine-local row; an absent one is `??`, never missing."""

    def rows(self, store):
        from wk import doctor
        return list(doctor.Doctor(ROOT, env=clean_env({"WK_STORE": store, "WK_IN_VM": "1"})).machine_local())

    def test_each_is_reported_from_the_machine_and_absent_is_not_a_fault(self):
        from wk import doctor
        store = self.tmp + "/store"
        os.makedirs(store + "/secrets")
        copies = ("read-github-pat", "push-github-pat", "push-bugzilla-api-key")
        for name in copies:
            with open(store + "/" + name, "w") as f:
                f.write(PLACEHOLDER + "\n")
        first = secrets.AGENT_SECRETS[0]
        with open(store + "/secrets/" + first[1], "w") as f:
            f.write(PLACEHOLDER + "\n")
        rows = self.rows(store)
        self.assertEqual([], [r for r in rows if r[0] == doctor.MISS], rows)
        self.assertNotIn(PLACEHOLDER, repr(rows))
        for name in copies + ("push-keys",) + tuple("/" + r[1] + " " for r in secrets.AGENT_SECRETS):
            with self.subTest(name=name):
                (row,) = [r for r in rows if name in r[1] + " "]
                self.assertEqual(doctor.UNK if name in ("push-keys", "/%s " % secrets.AGENT_SECRETS[1][1]) else doctor.OK, row[0], row)


class TestAnAuthKeyIsMintedNotOnlyHanded(SecretsTest):
    """`wk_tailscale_authkey` (lib/common.sh) and tailnet.Fleet: a machine that can administer the tailnet mints its own key."""

    def env(self, authkey=None, api=None):
        return {"WK_TS_AUTHKEY": authkey or self.tmp + "/no-such-key", "WK_TS_API_SECRET": api or self.tmp + "/no-such-api"}

    def file(self, name, text):
        with open(self.tmp + "/" + name, "w") as f:
            f.write(text + "\n")
        return self.tmp + "/" + name

    def test_a_usable_key_is_used_and_without_one_the_commands_that_store_one_are_named(self):
        lib = '. "%s/lib/common.sh"\nwk_tailscale_authkey\n' % REPO
        key = self.file("authkey", "tskey-auth-kAAAA-secret")
        cp = bash(lib, env=self.env(authkey=key))
        self.assertEqual((0, key), (cp.returncode, cp.stdout.strip()), cp.stderr)
        for env, named in ((self.env(), (r"wk key set tailnet",)),
                           (self.env(api=self.file("api", "tskey-api-kAAAA-secret")), (r"wk key set tailnet-api", r"wk key set tailnet(?!-api)"))):
            with self.subTest(env=env):
                cp = bash(lib, env=env)
                self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                for pattern in named:
                    self.assertRegex(cp.stdout + cp.stderr, pattern)

    def test_a_key_is_present_if_usable_or_mintable_and_asking_mints_nothing(self):
        from wk import tailnet
        api, usable, broad = self.file("api", "tskey-api-kAAAA-secret"), self.file("usable", "tskey-auth-k1-abc"), self.file("broad", "tskey-api-k1-abc")
        for env, present in ((self.env(api=api), True), (self.env(), False), (self.env(authkey=usable), True), (self.env(authkey=broad), False)):
            with self.subTest(env=env):
                self.assertEqual(present, tailnet.Fleet(ROOT, clean_env(env)).key_present())
        self.assertFalse(os.path.exists(self.tmp + "/no-such-key"))


class TestEachStartConvergesTheInjectorsCopies(unittest.TestCase):
    def test_a_container_start_converges_them(self):
        from wk import places
        c = places.Container("container", ROOT, {"HOME": "/nonexistent", "WK_STORE": "/nonexistent/store"}, Fake("here"))
        with mock.patch.object(secrets.Secrets, "push_converge_machine") as converge:
            c.start("demo")
        self.assertEqual(1, converge.call_count)


if __name__ == "__main__":
    unittest.main()
