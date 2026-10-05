"""The deploy keys in a real ssh-agent, `wk key push` end to end against it, and the /secrets a store publishes.
tests/test_push_switch.py and tests/test_wk_secrets.py hold the switch's logic over a fake machine.

Run: python3 -m unittest tests.test_push_agent -v
"""
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import unittest
from pathlib import Path

from tests.support import REPO, WkTest, bash, requires_container_target
from tests.test_wk_key import GOOD, KeyTest
from tests.test_wk_secrets import KEY_SH, SOCK

sys.path.insert(0, str(REPO / "lib"))
from wk import secrets, targets  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Fake, Local, Result  # noqa: E402
from wk.secrets import Secrets  # noqa: E402
from wk.store import Store  # noqa: E402

FORKS = ("fork", "forkwpe")


def store_init(env, extra=""):
    """`python3 -m wk.targets store-init`, then `extra` (bash with KEY_SH's key_store)."""
    return bash('PYTHONPATH="$WK_ROOT/lib" python3 -m wk.targets store-init || exit\n' + KEY_SH + extra, env=env)


@unittest.skipUnless(shutil.which("ssh-agent") and shutil.which("ssh-add"), "needs ssh-agent and ssh-add")
class _Agent(WkTest):
    """A real ssh-agent, and a key pair per fork: private halves in the held directory, public in the mounted one."""

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
        self.sock = self.tmp / "agent.sock"
        out = subprocess.run(["ssh-agent", "-s", "-a", str(self.sock)], stdout=subprocess.PIPE, text=True,
                             check=True).stdout
        pid = int(out.split("SSH_AGENT_PID=", 1)[1].split(";", 1)[0])
        self.addCleanup(_kill, pid)

    def env(self, extra=None):
        return {"WK_HOST_SECRETS": str(self.secrets), "WK_STORE": str(self.store),
                "WK_PUSH_AGENT_SOCK": str(self.sock), "WK_PUSH_PAT_FILE": str(self.tmp / "pat"),
                "WK_PUSH_READ_PAT_FILE": str(self.tmp / "read-pat"),
                "WK_PUSH_BUGZILLA_KEY_FILE": str(self.tmp / "bz-key"), "WK_MACHINE": "wk-no-such-machine",
                "XDG_STATE_HOME": str(self.tmp / "state"), **(extra or {})}

    def ssh_add(self, *args):
        return subprocess.run(["ssh-add", *args], env={**os.environ, "SSH_AUTH_SOCK": str(self.sock)},
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def identities(self):
        return [ln for ln in self.ssh_add("-l").stdout.splitlines() if "SHA256:" in ln]

    def py_secrets(self, machine=None):
        """lib/wk/secrets.py over this host, every command line it runs kept in self.machine.argvs."""
        self.machine = machine or _Recording()
        return Secrets(REPO, self.env(), self.machine)


def _kill(pid):
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)


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


class TestSecretsAgainstARealAgent(_Agent):
    def test_each_key_goes_in_on_stdin_and_only_public_halves_come_back(self):
        s = self.py_secrets()
        self.assertEqual([(f, "loaded") for f in FORKS], s.agent_load(str(self.sock)))
        self.assertEqual(len(FORKS), len(self.identities()))
        self.assertIn("SHA256:", "\n".join(s.agent_list(str(self.sock))))
        secret_line = [ln for ln in (self.held / "build_key_fork").read_text().splitlines() if "PRIVATE" not in ln][0]
        for argv in self.machine.argvs:
            self.assertNotIn(secret_line, " ".join(argv))
        pub = self.ssh_add("-L").stdout
        self.assertTrue(all(ln.startswith("ssh-") for ln in pub.splitlines() if ln.strip()), pub)

    def test_an_empty_agent_answers_lists_nothing_and_no_agent_does_not_answer(self):
        s = self.py_secrets()
        self.assertTrue(s.agent_answers(str(self.sock)))
        self.assertEqual([], s.agent_list(str(self.sock)))
        self.assertFalse(s.agent_answers(str(self.tmp / "not-a-socket")))

    def test_the_token_round_trips_through_a_path_with_a_space_at_mode_600(self):
        (self.held / "github-pat").write_text("ghp-not-a-real-token\nsecond\n")
        (self.tmp / "a dir").mkdir()
        pat = self.tmp / "a dir" / "push-github-pat"
        s = self.py_secrets()
        self.assertTrue(s.cred_write(str(pat), "github-pat"))
        self.assertEqual("ghp-not-a-real-token\n", pat.read_text())
        self.assertEqual(0o600, pat.stat().st_mode & 0o777)
        self.assertEqual([pat.name], [p.name for p in pat.parent.iterdir()])
        self.assertTrue(s.cred_present(str(pat)))
        self.assertNotIn("ghp-not-a-real-token", repr(self.machine.argvs))
        s.cred_clear(str(pat))
        self.assertFalse(pat.exists())


class TestTheStandingReadToken(_Agent):
    def read_pat(self):
        return self.tmp / "read-pat"

    def test_it_is_written_from_the_token_this_device_holds(self):
        (self.held / "github-pat").write_text("ghp-not-a-real-token\n")
        self.assertTrue(self.py_secrets().cred_sync(str(self.read_pat()), "github-pat"))
        self.assertEqual("ghp-not-a-real-token\n", self.read_pat().read_text())
        self.assertEqual(0o600, self.read_pat().stat().st_mode & 0o777)

    def test_a_far_side_that_refuses_is_reported(self):
        (self.held / "github-pat").write_text("ghp-not-a-real-token\n")
        self.assertFalse(self.py_secrets(_FarSideRefuses()).cred_sync(str(self.read_pat()), "github-pat"))
        self.assertFalse(self.read_pat().exists())

    def test_the_setup_entry_point_syncs_it(self):
        (self.held / "github-pat").write_text("ghp-not-a-real-token\n")
        cp = bash('PYTHONPATH="$WK_ROOT/lib" python3 -m wk.secrets pat-converge', env=self.env())
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertEqual("ghp-not-a-real-token\n", self.read_pat().read_text())

    def test_the_switch_does_not_touch_it(self):
        (self.held / "github-pat").write_text("ghp-not-a-real-token\n")
        self.read_pat().write_text("ghp-standing\n")
        for action in ("on", "off"):
            with self.subTest(action=action):
                self.run_wk("key", "push", action, env=self.env())
                self.assertEqual("ghp-standing\n", self.read_pat().read_text())


class TestDoctorNamesTheReadToken(WkTest):
    def rows(self, store):
        from tests.support import clean_env
        from wk import doctor
        doc = doctor.Doctor(str(REPO), env=clean_env({"WK_STORE": str(store), "WK_IN_VM": "1"}))
        return [r for r in doc.machine_local() if "read-github-pat" in r[1]]

    def test_it_is_reported_from_the_machine_and_absent_is_not_a_fault(self):
        store = self.tmp / "store"
        store.mkdir()
        rows = self.rows(store)
        self.assertEqual(1, len(rows), rows)
        self.assertEqual("unk", rows[0][0], rows)
        self.assertIn("regenerable", rows[0][2])
        (store / "read-github-pat").write_text("ghp-not-a-real-token\n")
        state, what, remedy = self.rows(store)[0]
        self.assertEqual("ok", state, what)
        self.assertNotIn("ghp-not-a-real-token", what + remedy)


class TestTheSwitchEndToEnd(_Agent):
    def test_on_loads_and_writes_the_config_off_empties_and_status_reads_the_agent(self):
        (self.held / "github-pat").write_text("ghp-not-a-real-token\n")
        self.assertEqual(1, self.run_wk("key", "push", "status", env=self.env()).returncode)
        for action, rc, keys in (("on", 0, len(FORKS)), ("status", 0, len(FORKS)), ("off", 0, 0)):
            with self.subTest(action=action):
                (self.secrets / "ssh_config").unlink(missing_ok=True)
                cp = self.run_wk("key", "push", action, env=self.env())
                self.assertEqual(rc, cp.returncode, cp.stdout)
                self.assertEqual(keys, len(self.identities()))
                self.assertEqual(bool(keys), (self.tmp / "pat").exists())
                if action != "status":
                    cfg = (self.secrets / "ssh_config").read_text()
                    self.assertIn("IdentityFile /secrets/build_key_fork\n", cfg)
        self.assertEqual("justinmichaud", (self.secrets / "github-user").read_text().strip())


class TestTheAgentSocketIsTheTargets(unittest.TestCase):
    def registry(self):
        return targets.Registry(REPO, {"HOME": "/nonexistent", "WK_STORE": "/nonexistent/store"}, Fake("here"))

    def test_the_container_names_the_mounted_socket_and_the_others_none(self):
        self.assertEqual("/run/wk/ssh-agent.sock", self.registry().load("container").agent_sock())
        for kind in ("remote", "local"):
            with self.subTest(target=kind):
                self.assertIsNone(self.registry().load(kind).agent_sock())


def _machine(cmd, timeout=60):
    return subprocess.run(["podman", "machine", "ssh", "wk", "--", cmd], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, timeout=timeout)


RUNTIME = "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/wk"


@requires_container_target()
class TestLiveAgentInTheMachine(unittest.TestCase):
    """Read-only: whether this machine's own agent and injector are where every container's mount says."""

    def need(self, unit):
        cp = _machine("systemctl --user cat %s >/dev/null 2>&1 && echo YES || echo NO" % unit)
        if "YES" not in cp.stdout:
            self.skipTest("the machine has no %s yet:  ./setup --stage sdk" % unit)

    def test_the_unit_is_active_and_the_socket_is_in_the_mounted_directory(self):
        self.need("wk-ssh-agent.service")
        self.assertIn("active", _machine("systemctl --user is-active wk-ssh-agent.service").stdout)
        self.assertIn("SOCKET", _machine('test -S "%s/ssh-agent.sock" && echo SOCKET || echo NONE' % RUNTIME).stdout)

    def test_ssh_add_answers_there(self):
        self.need("wk-ssh-agent.service")
        cp = _machine('SSH_AUTH_SOCK="%s/ssh-agent.sock" ssh-add -l >/dev/null 2>&1; echo rc=$?' % RUNTIME)
        self.assertRegex(cp.stdout, r"rc=[01]\b", cp.stdout)

    def test_the_injector_publishes_its_ca_and_not_its_socket(self):
        self.need("wk-github-inject.service")
        self.assertIn("CA", _machine('test -s "%s/wk-github-ca.pem" && echo CA || echo NONE' % RUNTIME).stdout)
        self.assertIn("NO", _machine('test -S "%s/github-inject.sock" && echo EXPOSED || echo NO' % RUNTIME).stdout)


@requires_container_target()
@unittest.skipUnless(os.environ.get("WK_TEST_LIVE_PUSH") == "1", "throws the real switch; set WK_TEST_LIVE_PUSH=1")
class TestLivePushFromAContainer(unittest.TestCase):
    def wk(self, *args, timeout=180):
        return subprocess.run([str(REPO / "wk"), *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, timeout=timeout)

    def test_a_container_can_push_through_the_agent_while_on(self):
        was_on = self.wk("key", "push", "status").returncode == 0
        self.addCleanup(self.wk, "key", "push", "on" if was_on else "off")
        names = [ln.split()[0] for ln in self.wk("ls", timeout=120).stdout.splitlines()[1:] if ln.split()]
        if not names:
            self.skipTest("no workspace here to push from ('wk new <name>')")
        cp = self.wk("key", "push", "on")
        self.assertEqual(0, cp.returncode, cp.stdout)
        cp = self.wk("enter", names[0], "git", "ls-remote", "git@%s:" % secrets.forks()[0][2], "HEAD")
        self.assertEqual(0, cp.returncode, cp.stdout)


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

    def test_the_aliases_the_account_and_the_view_and_no_private_half(self):
        keyring_dir, view = self.publish()
        self.assertIn("Host github-webkit", (keyring_dir / "ssh_config").read_text())
        self.assertTrue((keyring_dir / "github-user").read_text().strip())
        self.assertEqual({"ssh_config", "github-user", "build_key_fork.pub", "litellm-key"},
                         {p.name for p in view.iterdir()})
        for f in keyring_dir.rglob("*"):
            if f.is_file():
                self.assertNotIn("PRIVATE KEY", f.read_text(errors="replace"), f.name)
        first = (keyring_dir / "ssh_config").read_text()
        self.publish()
        self.assertEqual(first, (keyring_dir / "ssh_config").read_text())

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
        for name in ("ssh_config", "github-user", "view/container/ssh_config"):
            (keyring_dir / name).parent.mkdir(parents=True, exist_ok=True)
            (keyring_dir / name).write_text("published by the host\n")
        keyring_dir.chmod(0o500)
        self.addCleanup(keyring_dir.chmod, 0o700)
        cp = store_init({**self.env(), "WK_IN_VM": "1"})
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertEqual({"ssh_config", "github-user", "view"}, {p.name for p in keyring_dir.iterdir()})
        self.assertEqual("published by the host\n", (keyring_dir / "ssh_config").read_text())


PEER_SSH = '#!/bin/sh\nfor last; do :; done\nexec bash -c "$last"\n'
PEER_WK = '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$WK_TEST_PEER_LOG"\necho "fork       push allowed (in the agent)"\n'


class TestAskingAnotherMachine(WkTest):
    """`wk key push --target <machine>` runs the far side's own `wk key push` over ssh; ssh is a stub that runs it here."""

    def test_the_far_side_runs_the_same_command(self):
        reg, root, binp = self.tmp / "registry", self.tmp / "peer-root", self.tmp / "bin"
        for d in (reg, root / "tools", binp):
            d.mkdir(parents=True)
        for path, text in ((root / "tools" / "wk", PEER_WK), (binp / "ssh", PEER_SSH)):
            path.write_text(text)
            path.chmod(0o755)
        (reg / "peerbox.conf").write_text("kind=peer\nhost=fake-peerbox\npeer=1\nroot=%s\n" % root)
        log = self.tmp / "peer.log"
        cp = self.run_wk("key", "push", "status", "--target", "peerbox", env={
            "PATH": f"{binp}:{os.environ['PATH']}", "WK_MACHINES_DIR": str(reg), "WK_STORE": str(self.tmp / "store"),
            "WK_HOST_SECRETS": str(self.tmp / "secrets"), "WK_TEST_PEER_LOG": str(log)})
        self.assertEqual(["key push status"], log.read_text().split("\n")[:-1], cp.stdout)
        self.assertIn("push allowed", cp.stdout)
        self.assertEqual(0, cp.returncode, cp.stdout)


class TestEveryStoreRotateAndWithdrawReachesTheInjector(KeyTest):
    """`wk key set` converges the reader's copy always, and the writer's while the switch is on."""

    def setUp(self):
        super().setUp()
        self.w = self.world()
        self.w.agents[SOCK] = {"KEY:fork"}
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

    def test_with_the_switch_off_the_writer_gets_nothing(self):
        self.w.agents[SOCK] = set()
        self.set(paste=True, value=GOOD)
        self.assertEqual((GOOD + "\n", None), self.copies())
