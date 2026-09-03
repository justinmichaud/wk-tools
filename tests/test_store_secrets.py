"""The secrets directory is this device's own, and nothing crosses into the
podman machine to reach it.

`wk key set`, `wk key ensure` and `wk push` used to be answered inside the
podman VM, because that is where /var/lib/wk/secrets was: storing a token or
throwing the push switch meant starting a virtual machine first. The directory
is now this host's (wk_secrets_dir, lib/store.sh) and the machine mounts it
read-only, so every one of those runs here.

That is testable as an absence, which is what this file is: `podman` on PATH
leaves a witness file behind and then fails. Every command below has to
succeed, and the witness must never appear.

Run: python3 -m unittest tests.test_store_secrets -v
"""
import os
import subprocess
import unittest

from tests.support import REPO, WkTest, bash, stub_path

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
    """A scratch secrets directory, a scratch store, and the witness."""

    def setUp(self):
        super().setUp()
        self.secrets = self.tmp / "secrets"
        self.store = self.tmp / "store"
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
            cp = bash('. "$WK_ROOT/lib/common.sh"\n. "$WK_ROOT/lib/store.sh"\n' + script,
                      env=self.env({**(env or {}),
                                    "PATH": f"{binp}:{os.environ['PATH']}"}))
        return cp


class TestTheStoreFunctionsReadAndWriteHere(_Here):
    def test_store_read_and_clear_round_trip(self):
        cp = self.sh(
            f'printf "%s\\n" {PLACEHOLDER} | wk_agent_secret_store claude\n'
            'printf "stored=[%s]\\n" "$(wk_agent_secret claude)"\n'
            'wk_agent_secret_clear claude\n'
            'printf "cleared=[%s]\\n" "$(wk_agent_secret claude)"\n')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn(f"stored=[{PLACEHOLDER}]", cp.stdout)
        self.assertIn("cleared=[]", cp.stdout)
        self.assertFalse(self.witness.exists(), cp.stderr)

    def test_the_path_is_the_host_directory_and_not_the_store(self):
        cp = self.sh('printf "path=%s\\n" "$(wk_agent_secret_path claude)"\n'
                     'printf "keys=%s\\n" "$(wk_secrets_dir)"\n'
                     'printf "held=%s\\n" "$(wk_push_held_dir)"\n')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        if os.uname().sysname != "Darwin":
            self.skipTest("the two spellings are one path where the store is this machine's")
        self.assertIn(f"path={self.secrets}/claude-token", cp.stdout)
        self.assertIn(f"keys={self.secrets}", cp.stdout)
        self.assertIn(f"held={self.secrets.parent}/push-keys", cp.stdout)

    def test_a_deploy_key_is_read_from_here_too(self):
        """From the directory nothing mounts, which is where every private half
        lives now -- `wk push` loads it into an agent rather than moving it."""
        held = self.secrets.parent / "push-keys"
        held.mkdir(parents=True)
        (held / "build_key_fork").write_text(f"{PLACEHOLDER}-fork\n")
        cp = self.sh('printf "[%s]\\n" "$(wk_push_key fork)"')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn(f"[{PLACEHOLDER}-fork]", cp.stdout)
        self.assertFalse(self.witness.exists(), cp.stderr)

    def test_the_github_token_is_beside_the_private_halves(self):
        """It publishes, so it is in the directory nothing mounts and not in
        the one every workspace reads."""
        held = self.secrets.parent / "push-keys"
        held.mkdir(parents=True)
        (held / "github-pat").write_text(f"{PLACEHOLDER}-pat\n")
        cp = self.sh('printf "path=%s\\nvalue=[%s]\\n" '
                     '"$(wk_github_pat_path)" "$(wk_github_pat)"')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn(f"path={held}/github-pat", cp.stdout)
        self.assertIn(f"value=[{PLACEHOLDER}-pat]", cp.stdout)
        self.assertFalse(self.witness.exists(), cp.stderr)


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


class TestNoForwardingIsLeftInTheSource(unittest.TestCase):
    """The hop is gone from the files that had it, not merely unused."""

    def test_the_two_commands_never_reach_into_the_machine(self):
        for f in ("cmd/key", "cmd/push"):
            with self.subTest(script=f):
                self.assertNotIn("podman machine ssh", (REPO / f).read_text())

    def test_the_library_hops_for_the_agent_and_for_nothing_else(self):
        """The credentials are read here. The ssh-agent and the injector are on
        the machine that runs the workspaces -- there is nowhere else they
        could be -- so push_agent_exec is the one place that reaches it, and
        the secrets reader is not."""
        text = (REPO / "lib" / "store.sh").read_text()
        self.assertEqual(1, text.count("podman machine ssh"))
        exec_fn = text[text.index("push_agent_exec() {"):]
        exec_fn = exec_fn[:exec_fn.index("\n}\n")]
        self.assertIn("podman machine ssh", exec_fn)
        reader = text[text.index("_wk_secret_read() {"):]
        reader = reader[:reader.index("\n}\n")]
        self.assertNotIn("podman", reader)

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
        """Two spellings of one directory, both from wk_secrets_dir: a command
        that spelled `$WK_STORE/secrets` itself would be right in the VM and
        wrong on the host that mounts it there."""
        text = (REPO / "lib" / "store.sh").read_text()
        self.assertEqual(1, text.count("wk_secrets_dir() {"))
        for f in ("cmd/key", "cmd/push", "cmd/doctor"):
            with self.subTest(script=f):
                body = (REPO / f).read_text()
                # The mount source in targets/container.sh is the container's
                # view and stays store-relative; these three read the files.
                self.assertNotIn('$WK_STORE/secrets', body)
                self.assertNotIn('$WK_STORE/push-keys', body)


if __name__ == "__main__":
    unittest.main()
