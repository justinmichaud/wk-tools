"""cmd/key: the keys are this machine's own, and every subverb acts here.

The secrets directory is this device's (wk_secrets_dir, lib/store.sh); on macOS
the podman machine reads it as a read-only mount rather than holding it, so
`wk key ensure`, `wk key pub` and `wk key fingerprints` are ssh-keygen and a
file on this side, with no `podman machine ssh` in the path and nothing that
has to be running.

Run: python3 -m unittest tests.test_key -v
"""

import os
import subprocess
import unittest

from tests.support import REPO, WkTest, stub_path

KEY = REPO / "cmd" / "key"

# A `podman` that fails loudly if anything calls it: what this file is mostly
# about is that nothing does.
PODMAN_TRAP = '#!/bin/sh\necho "podman was called" >&2\nexit 1\n'


class _KeyRun(WkTest):
    """cmd/key against a scratch secrets directory, with the trap on PATH."""

    def key(self, *args, env=None):
        secrets = self.tmp / "secrets"
        e = {"WK_HOST_SECRETS": str(secrets),
             "WK_STORE": str(self.tmp / "store")}
        if env:
            e.update(env)
        with stub_path({"podman": PODMAN_TRAP}) as binp:
            e["PATH"] = f"{binp}:/usr/bin:/bin:/usr/sbin:/sbin"
            cp = subprocess.run([str(KEY), *args], cwd=str(REPO), env={**self._base_env(), **e},
                                capture_output=True, text=True, timeout=120)
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
        held = self.tmp / "push-keys"
        for fork in ("fork", "forkwpe"):
            with self.subTest(fork=fork):
                self.assertTrue((held / f"build_key_{fork}").exists(), cp.stderr)
                self.assertTrue((secrets / f"build_key_{fork}.pub").exists())
                self.assertFalse((secrets / f"build_key_{fork}").exists(),
                                 "a private half is in the directory every workspace mounts")

    def test_the_directory_it_makes_is_not_readable_by_anyone_else(self):
        _cp, secrets = self.key("ensure")
        held = self.tmp / "push-keys"
        self.assertEqual(0o700, secrets.stat().st_mode & 0o777)
        self.assertEqual(0o700, held.stat().st_mode & 0o777)
        self.assertEqual(0o600, (held / "build_key_fork").stat().st_mode & 0o777)

    def test_a_second_run_generates_nothing_new(self):
        _cp, _secrets = self.key("ensure")
        held = self.tmp / "push-keys"
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
        """Where the private half is is no longer the switch: it is always in
        the directory nothing mounts, and whether it is loaded is `wk push`."""
        _cp, _secrets = self.key("ensure")
        held = self.tmp / "push-keys"
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
    def test_register_ensures_through_this_same_file(self):
        """`wk key register` walks the machines and asks each to make its
        missing keys; for this one that is this file's own `ensure` arm, not a
        second copy of ssh-keygen"""
        text = KEY.read_text()
        self.assertIn('ensure_keys() { "$0" ensure', text)
        self.assertIn("ssh-keygen -t ed25519", text)
        self.assertEqual(1, text.count("ssh-keygen -t ed25519"))


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
