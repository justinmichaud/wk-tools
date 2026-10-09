"""The tokens and deploy keys a machine's injector and push service read: written from what is held, and the /secrets a store publishes."""
import contextlib
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from tests.support import REPO, WkTest, bash, requires_container_place
from tests.test_wk_key import GOOD, KeyTest
from tests.test_wk_secrets import KEY_SH

sys.path.insert(0, str(REPO / "lib"))
from wk import secrets  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Fake, Local, Result  # noqa: E402
from wk.secrets import Secrets  # noqa: E402
from wk.store import Store  # noqa: E402

FORKS = tuple(k[0] for k in secrets.push_keys())


def store_init(env, extra=""):
    """`python3 -m wk.places store-init`, then `extra` (bash with KEY_SH's key_store)."""
    return bash('PYTHONPATH="$WK_ROOT/lib" python3 -m wk.places store-init || exit\n' + KEY_SH + extra, env=env)


class _Keys(WkTest):
    """A key pair per fork: private halves in the held directory, public in the mounted one."""

    def setUp(self):
        super().setUp()
        self.store = self.tmp / "store"
        self.secrets = self.store / "secrets"
        self.held = self.store / "push-keys"
        self.secrets.mkdir(parents=True)
        self.held.mkdir(parents=True)
        (self.store / "ws").mkdir(parents=True)
        for fork in FORKS:
            priv = self.held / f"build_key_{fork}"
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", fork, "-f", str(priv)], check=True)
            shutil.move(str(priv) + ".pub", str(self.secrets / f"build_key_{fork}.pub"))

    def env(self, extra=None):
        return {"WK_HOST_SECRETS": str(self.secrets), "WK_STORE": str(self.store), "WK_MACHINE": "wk-no-such-machine",
                "XDG_STATE_HOME": str(self.tmp / "state"), **(extra or {})}

    def py_secrets(self, machine=None):
        """lib/wk/secrets.py over this host, every command line it runs kept in self.machine.argvs."""
        self.machine = machine or _Recording()
        return Secrets(REPO, self.env(), self.machine)


class _Recording(Local):
    def __init__(self):
        self.argvs = []

    def run(self, argv, input=None, timeout=None):
        self.argvs.append(argv)
        return super().run(argv, input=input, timeout=timeout)


class _FarSideRefuses(_Recording):
    def run(self, argv, input=None, timeout=None):
        if argv[:2] == ["sh", "-c"]:
            self.argvs.append(argv)
            return Result(1)
        return super().run(argv, input=input, timeout=timeout)


class TestTheTokenRoundTrips(_Keys):
    def test_through_a_path_with_a_space_at_mode_600_and_never_as_an_argument(self):
        (self.held / "github-pat").write_text("ghp-not-a-real-token\nsecond\n")
        (self.tmp / "a dir").mkdir()
        pat = self.tmp / "a dir" / "push-github-pat"
        s = self.py_secrets()
        self.assertTrue(s.cred_write(str(pat), "github-pat"))
        self.assertEqual("ghp-not-a-real-token\n", pat.read_text())
        self.assertEqual(0o600, pat.stat().st_mode & 0o777)
        self.assertEqual([pat.name], [p.name for p in pat.parent.iterdir()])
        self.assertNotIn("ghp-not-a-real-token", repr(self.machine.argvs))
        s.cred_clear(str(pat))
        self.assertFalse(pat.exists())


class TestTheStandingCredentials(_Keys):
    HELD = "ghp-not-a-real-token\n"

    def test_the_read_token_is_written_from_the_token_this_device_holds(self):
        (self.held / "github-pat").write_text(self.HELD)
        read_pat = self.tmp / "read-pat"
        self.assertTrue(self.py_secrets().cred_sync(str(read_pat), "github-pat"))
        self.assertEqual(self.HELD, read_pat.read_text())
        self.assertEqual(0o600, read_pat.stat().st_mode & 0o777)

    def test_a_far_side_that_refuses_is_reported(self):
        (self.held / "github-pat").write_text(self.HELD)
        self.assertFalse(self.py_secrets(_FarSideRefuses()).cred_sync(str(self.tmp / "read-pat"), "github-pat"))
        self.assertFalse((self.tmp / "read-pat").exists())

    def test_the_setup_entry_point_writes_every_file_the_injector_reads(self):
        (self.held / "github-pat").write_text(self.HELD)
        (self.held / "bugzilla-api-key").write_text("bz-key\n")
        cp = bash('PYTHONPATH="$WK_ROOT/lib" python3 -m wk.secrets push-converge', env=self.env())
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertEqual(self.HELD, (self.store / "read-github-pat").read_text())
        self.assertEqual(self.HELD, (self.store / "push-github-pat").read_text())
        self.assertEqual("bz-key\n", (self.store / "push-bugzilla-api-key").read_text())

    def test_the_setup_entry_point_refuses_when_a_far_side_did_not_take_them(self):
        (self.held / "github-pat").write_text(self.HELD)
        self.assertFalse(self.py_secrets(_FarSideRefuses()).push_converge_machine())


class TestDoctorNamesWhatTheMachineHolds(WkTest):
    FILES = ("read-github-pat", "push-github-pat", "push-bugzilla-api-key")

    def rows(self, store, name):
        from tests.support import clean_env
        from wk import doctor
        doc = doctor.Doctor(str(REPO), env=clean_env({"WK_STORE": str(store), "WK_IN_VM": "1"}))
        return [r for r in doc.machine_local() if name in r[1]]

    def test_each_is_reported_from_the_machine_and_absent_is_not_a_fault(self):
        store = self.tmp / "store"
        store.mkdir()
        for name in self.FILES + ("push-keys",):
            with self.subTest(name=name):
                rows = self.rows(store, name)
                self.assertEqual(1, len(rows), rows)
                self.assertEqual("unk", rows[0][0], rows)
                self.assertIn("regenerable", rows[0][2])
        for name in self.FILES:
            (store / name).write_text("ghp-not-a-real-token\n")
            state, what, remedy = self.rows(store, name)[0]
            self.assertEqual("ok", state, what)
            self.assertNotIn("ghp-not-a-real-token", what + remedy)



@requires_container_place()
class TestLivePushServiceInTheMachine(unittest.TestCase):
    """Read-only: whether this machine's push service is where every container's mount says."""

    def machine(self, cmd):
        return subprocess.run(["podman", "machine", "ssh", "wk", "--", cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, timeout=60).stdout

    def test_the_unit_is_active_and_the_socket_is_in_the_mounted_directory(self):
        if "YES" not in self.machine("systemctl --user cat wk-push.service >/dev/null 2>&1 && echo YES || echo NO"):
            self.skipTest("the machine has no wk-push.service yet:  ./setup --stage sdk")
        self.assertIn("active", self.machine("systemctl --user is-active wk-push.service"))
        self.assertIn("SOCKET", self.machine('test -S "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/wk/push.sock" && echo SOCKET || echo NONE'))


class TestStoreInitPublishesSecrets(WkTest):
    SEEDED = {"claude-token": "a vm and a build box only\n", "litellm-key": "every kind\n",
              "build_key_fork.pub": "ssh-ed25519 AAAA fork\n"}

    def env(self):
        store = self.tmp / "store"
        return {"WK_STORE": str(store), "WK_STORE_DEFAULT": str(store), "WK_HOST_SECRETS": str(store / "secrets"),
                "XDG_STATE_HOME": str(self.tmp / "state")}

    def publish(self, extra=""):
        keyring_dir = self.tmp / "store" / "secrets"
        keyring_dir.mkdir(parents=True, exist_ok=True)
        for name, text in self.SEEDED.items():
            (keyring_dir / name).write_text(text)
        cp = store_init(self.env(), extra)
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.last = cp
        return keyring_dir, keyring_dir / "view" / "container"

    def test_the_account_and_the_view_and_no_private_half(self):
        keyring_dir, view = self.publish()
        self.assertFalse((keyring_dir / "ssh_config").exists())
        self.assertTrue((keyring_dir / "github-user").read_text().strip())
        self.assertEqual({"github-user", "litellm-key"}, {p.name for p in view.iterdir()})
        for f in keyring_dir.rglob("*"):
            if f.is_file():
                self.assertNotIn("PRIVATE KEY", f.read_text(errors="replace"), f.name)
        first = (keyring_dir / "github-user").read_text()
        self.publish()
        self.assertEqual(first, (keyring_dir / "github-user").read_text())

    def test_a_rotation_reaches_the_view(self):
        _, view = self.publish(extra='printf "rotated\\n" | key_store litellm\n')
        self.assertEqual("rotated\n", (view / "litellm-key").read_text())

    def test_the_bugzilla_login_is_read_from_the_mirror(self):
        _, view = self.publish()
        self.assertFalse((view / "bugzilla-user").exists())
        self.assertIn("wk sync", self.last.stderr)
        mirror = Path(Store(self.env()).mirror_dir())
        user = Secrets(REPO, self.env(), Fake("here")).github_user()
        mirror.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(mirror)], check=True)
        (mirror / "metadata").mkdir()
        (mirror / "metadata" / "contributors.json").write_text(json.dumps([
            {"github": "someone", "emails": ["else@example.test"]},
            {"github": user, "emails": ["me@example.test", "other@example.test"]}]))
        git = ["git", "-C", str(mirror), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(git + ["add", "."], check=True)
        subprocess.run(git + ["commit", "-q", "-m", "contributors"], check=True)
        _, view = self.publish()
        self.assertEqual("me@example.test\n", (view / "bugzilla-user").read_text())

    def test_in_the_vm_a_read_only_secrets_dir_is_read_and_not_written(self):
        keyring_dir = self.tmp / "store" / "secrets"
        for name in ("github-user", "view/container/github-user"):
            (keyring_dir / name).parent.mkdir(parents=True, exist_ok=True)
            (keyring_dir / name).write_text("published by the host\n")
        keyring_dir.chmod(0o500)
        self.addCleanup(keyring_dir.chmod, 0o700)
        cp = store_init({**self.env(), "WK_IN_VM": "1"})
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertEqual({"github-user", "view"}, {p.name for p in keyring_dir.iterdir()})
        self.assertEqual("published by the host\n", (keyring_dir / "github-user").read_text())


class TestEveryStoreRotateAndWithdrawReachesTheInjector(KeyTest):
    """`wk key set` converges the reader's copy and the writer's, always."""

    def setUp(self):
        super().setUp()
        self.w = self.world()
        self.sec = self.w.sec()

    def set(self, **kw):
        with open(os.devnull, "w") as null, contextlib.redirect_stderr(null):
            try:
                return self.key(self.w, **kw.pop("key", {})).set("github-pat", **kw)
            except Refused as e:
                return e.status

    def copies(self):
        return self.w.files.get(self.sec.machine_read_pat()), self.w.files.get(self.sec.machine_pat())

    def test_a_stored_token_reaches_the_reader_and_the_writer(self):
        self.assertEqual(0, self.set(paste=True, value=GOOD))
        self.assertEqual((GOOD + "\n", GOOD + "\n"), self.copies())

    def test_a_rotated_one_replaces_both(self):
        self.set(paste=True, value="good-old")
        os.environ["WK_YES"] = "1"
        self.assertEqual(0, self.set(replace=True, key={"typed": GOOD, "tty": True}))
        self.assertEqual((GOOD + "\n", GOOD + "\n"), self.copies())

    def test_a_withdrawn_one_leaves_neither(self):
        self.set(paste=True, value=GOOD)
        os.environ["WK_YES"] = "1"
        self.assertNotEqual(0, self.set(replace=True))
        self.assertEqual((None, None), self.copies())


if __name__ == "__main__":
    unittest.main()
