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

from tests.support import REPO, WkTest, stub_path
from tests.test_key import SSH_IS_THE_FORKS_KEY, SECURITY_HAS_NOTHING

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
        env.update({"WK_HOST_SECRETS": str(self.secrets), "WK_STORE": str(store),
                    "WK_NTFY_API": "http://127.0.0.1:1",
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

    def fleet(self):
        reg = self.tmp / "reg"
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

    def test_the_peer_gets_the_keys_and_the_token_and_the_box_gets_nothing(self):
        self.key("ensure")
        (self.held / "github-pat").write_text("ghp_notarealtoken\n")
        env = self.fleet()
        self.key("share", stubs={"ssh": PEER_SSH}, env=env)
        calls = [l for l in self.peer_log.read_text().splitlines() if l.strip()]
        self.assertIn("key adopt fork", calls)
        self.assertIn("key adopt forkwpe", calls)
        self.assertIn("key set github-pat --paste", calls)
        # Exactly one workstation was contacted: the build machine adds none.
        self.assertEqual(3, len(calls), calls)


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
