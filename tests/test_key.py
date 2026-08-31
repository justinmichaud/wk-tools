"""cmd/key: which machine runs the step that makes the keys, and where the
tooling it runs lives.

The private halves never leave the store, and on macOS that store is inside
the podman machine -- so `wk key register` reaches its ensure step through
`podman machine ssh`, and the tooling it names on the far side is
/opt/wk-tools, the VM's read-only mount of this repo. On a Linux workstation
there is no VM: in_vm is a local shell and the tooling is this checkout.

Both facts come from one decision (IN_VM_SSH / IN_VM_ROOT) so that the arm
in_vm takes and the path ensure_keys names cannot disagree -- naming
/opt/wk-tools while running locally is a `wk key register` that dies with
"No such file or directory" on every Linux workstation.
"""

import re
import unittest

from tests.support import REPO, WkTest, bash, stub_path

KEY = REPO / "cmd" / "key"


def _lift_block():
    """The `if is_macos ... fi` that decides IN_VM_SSH/IN_VM_ROOT/IN_VM_ENV,
    plus in_vm() and ensure_keys(), sliced out of cmd/key so the test runs
    the code that ships rather than a copy of it."""
    lines = KEY.read_text().splitlines()
    start = next(i for i, l in enumerate(lines)
                 if l.startswith('if is_macos && [ -z "${WK_IN_VM:-}" ]; then'))
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == "fi")
    decision = "\n".join(lines[start:end + 1])

    s = next(i for i, l in enumerate(lines) if l.startswith("in_vm() {"))
    e = next(i for i in range(s, len(lines)) if lines[i] == "}")
    in_vm = "\n".join(lines[s:e + 1])

    m = re.search(r"^ensure_keys\(\).*$", KEY.read_text(), re.M)
    assert m, "ensure_keys() not found in cmd/key"
    return decision, in_vm, m.group(0)


DECISION, IN_VM, ENSURE_KEYS = _lift_block()

PRELUDE = f"""
set -euo pipefail
. "{REPO}/lib/common.sh"
WK_ROOT=/checkout
WK_MACHINE=testvm
is_macos() {{ [ "$FAKE_OS" = macos ]; }}
{DECISION}
"""


class TestEnsureRunsWhereTheKeysAre(WkTest):
    """ensure_keys asks in_vm to run cmd/key ensure; the path it names has to
    be the path on whichever side in_vm lands on."""

    # in_vm is replaced by a printer: what is under test is the command
    # ensure_keys composes, not the transport that carries it.
    SCRIPT = PRELUDE + f"""
in_vm() {{ printf '%s\\n' "$*"; }}
{ENSURE_KEYS}
ensure_keys
"""

    def test_on_a_linux_workstation_it_names_this_checkout(self):
        """no VM, so the far side is this shell and /opt/wk-tools is not there"""
        cp = bash(self.SCRIPT, env={"FAKE_OS": "linux"})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("'/checkout/cmd/key' ensure", cp.stdout)
        self.assertNotIn("/opt/wk-tools", cp.stdout)
        self.assertNotIn("WK_IN_VM=1", cp.stdout)

    def test_on_macos_it_names_the_mount_inside_the_vm(self):
        """the store is in the podman machine, and so is the tooling"""
        cp = bash(self.SCRIPT, env={"FAKE_OS": "macos"})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("WK_IN_VM=1 '/opt/wk-tools/cmd/key' ensure", cp.stdout)

    def test_inside_the_vm_it_is_local_again(self):
        """already in the VM (WK_IN_VM=1), so there is nothing to ssh into"""
        cp = bash(self.SCRIPT, env={"FAKE_OS": "macos", "WK_IN_VM": "1"})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("'/checkout/cmd/key' ensure", cp.stdout)
        self.assertNotIn("/opt/wk-tools", cp.stdout)


class TestInVmTakesTheMatchingArm(WkTest):
    """The real in_vm, with a `podman` on PATH that fails if it is called:
    the arm in_vm takes has to match the root ensure_keys names, or one of
    the two is talking about a machine the other is not."""

    PODMAN_TRAP = '#!/bin/sh\necho "podman was called" >&2\nexit 1\n'

    SCRIPT = PRELUDE + f"""
{IN_VM}
in_vm "echo ran-here"
"""

    def test_linux_runs_it_locally(self):
        with stub_path({"podman": self.PODMAN_TRAP}) as binp:
            cp = bash(self.SCRIPT,
                      env={"FAKE_OS": "linux", "PATH": f"{binp}:/usr/bin:/bin"})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("ran-here", cp.stdout)
        self.assertNotIn("podman was called", cp.stderr)

    def test_macos_goes_through_the_podman_machine(self):
        with stub_path({"podman": self.PODMAN_TRAP}) as binp:
            cp = bash(self.SCRIPT,
                      env={"FAKE_OS": "macos", "PATH": f"{binp}:/usr/bin:/bin"})
        self.assertIn("podman was called", cp.stderr)


if __name__ == "__main__":
    unittest.main()


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
