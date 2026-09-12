"""`wk rm` against a machine that does not answer, and against a name that
is not there -- driven through the real remote driver (targets/remote.sh)
with a stub `ssh` on PATH and a registry of this test's own, so no test
reaches the maintainer's fleet.

The properties:

  * a far side that cannot be reached loses nothing here: the record of the
    workspace stays, so a re-run finds exactly what the first run reported;
  * the refusal quotes ssh's own last word ("Host key verification failed."),
    not a guess that the machine is off;
  * a name nothing answers to is refused before the one confirmation, and the
    other names in the batch still go.

Run: python3 -m unittest tests.test_rm_remote -v
"""
import os
import unittest

from tests.support import WkTest, bash, run, stub_path

_HOSTKEY_SSH = """#!/bin/sh
echo "Host key verification failed." >&2
exit 255
"""

_MACHINE_CONF = "WK_TARGET_KIND=remote\nWK_REMOTE_HOST={host}\n"

_LOCAL_CONF = (
    "WK_TARGET_KIND=remote\n"
    "WK_REMOTE_LOCAL=1\n"
    "WK_REMOTE_ROOT={root}\n"
    "WK_REMOTE_STORE={store}\n"
)


class TestAnUnreachableMachineKeepsItsRecord(WkTest):
    def setUp(self):
        super().setUp()
        self.registry = self.tmp / "hosts"
        self.registry.mkdir()
        (self.registry / "fakebox.conf").write_text(_MACHINE_CONF.format(host="fakebox.invalid"))
        self.state = self.tmp / "state"
        # The record `wk new --target fakebox` leaves here (WK_STORE, targets/remote.sh).
        self.record = self.state / "wk" / "remote" / "fakebox" / "ws" / "fakews"
        self.record.mkdir(parents=True)

    def _env(self, binp, **extra):
        env = {
            "WK_TARGET_REGISTRY": str(self.registry),
            "XDG_STATE_HOME": str(self.state),
            "PATH": f"{binp}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        }
        env.update(extra)
        return env

    def test_rm_refuses_with_ssh_words_and_keeps_the_record(self):
        with stub_path({"ssh": _HOSTKEY_SSH}) as binp:
            cp = run("rm", "fakews", env=self._env(binp, WK_YES="1", WK_TARGET="fakebox"),
                     input="", timeout=120)
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("Host key verification failed.", cp.stdout, cp.stdout)
        self.assertNotIn("removed remote workspace", cp.stdout,
                         "reported a removal ssh never carried out: " + cp.stdout)
        self.assertNotIn("not on the tailnet", cp.stdout, "a guess where ssh's reason belongs")
        self.assertTrue(self.record.is_dir(),
                        "the record was dropped, so a re-run would find nothing to retry")

    def test_a_name_with_no_record_is_refused_before_the_prompt(self):
        """no record here and no answer from the machine: nothing this end can
        see to destroy, so it says why -- ssh's words -- and asks nobody"""
        with stub_path({"ssh": _HOSTKEY_SSH}) as binp:
            cp = run("rm", "nope", env=self._env(binp, WK_TARGET="fakebox"), input="", timeout=120)
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("fakebox did not answer: Host key verification failed.", cp.stdout, cp.stdout)
        self.assertNotIn("destroy workspace", cp.stdout,
                         "asked before finding out nothing could be seen: " + cp.stdout)

    def test_the_locate_walk_names_the_machine_and_ssh_reason(self):
        with stub_path({"ssh": _HOSTKEY_SSH}) as binp:
            cp = bash(
                'set -euo pipefail\n'
                '. "$WK_ROOT/lib/common.sh"\n'
                '. "$WK_ROOT/lib/target.sh"\n'
                'ws_locate no-such-workspace\n',
                env=self._env(binp), timeout=120,
            )
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("could not ask fakebox over ssh: Host key verification failed.",
                      cp.stderr, cp.stderr)


class TestAnAbsentNameIsRefusedBeforeThePrompt(WkTest):
    def setUp(self):
        super().setUp()
        self.registry = self.tmp / "hosts"
        self.registry.mkdir()
        self.root = self.tmp / "root"
        self.store = self.tmp / "store"
        (self.root / "ws").mkdir(parents=True)
        self.store.mkdir()
        (self.registry / "fakelocal.conf").write_text(
            _LOCAL_CONF.format(root=self.root, store=self.store))
        self.env = {
            "WK_TARGET_REGISTRY": str(self.registry),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "WK_TARGET": "fakelocal",
        }

    def _make(self, name):
        (self.root / "ws" / name).mkdir()
        (self.root / "ws" / name / ".wk-ready").write_text("")
        (self.store / "ws" / name).mkdir(parents=True)

    def test_no_prompt_for_a_name_that_is_not_there(self):
        cp = run("rm", "nope", env=self.env, input="", timeout=120)
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("no such workspace: nope", cp.stdout, cp.stdout)
        self.assertNotIn("destroy workspace", cp.stdout,
                         "asked before finding out there was nothing to destroy: " + cp.stdout)

    def test_the_rest_of_the_batch_still_goes(self):
        self._make("keep-not")
        cp = run("rm", "keep-not", "nope", env=dict(self.env, WK_YES="1"), input="", timeout=120)
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("no such workspace: nope", cp.stdout, cp.stdout)
        self.assertIn("workspace 'keep-not' destroyed", cp.stdout, cp.stdout)
        self.assertFalse((self.root / "ws" / "keep-not").exists(), cp.stdout)
        self.assertFalse((self.store / "ws" / "keep-not").exists(), cp.stdout)


if __name__ == "__main__":
    unittest.main()
