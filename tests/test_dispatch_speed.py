"""What resolving a workspace name costs: the walk that decides which target
holds it (`ws_locate`, lib/target.sh) and the dispatcher that asks for it.

Three properties, each measured rather than asserted about the source:

  * the machines are asked **at once**, not one connect after another -- one
    machine that will not answer costs the walk its own timeout, never
    everybody's (lib/par.sh);
  * a name **this machine's own environments answer to** costs no ssh at all,
    so the fleet is never in the path of `wk enter <a container workspace>`;
  * a machine that did not answer is **named**, so an answer with a machine
    missing from it does not read as a complete one.

Plus the dispatcher's side of the same cost: `resolve_target` is called once
per invocation, not once per question that needs its answer.

Every fake machine here is a conf in a WK_TARGET_REGISTRY of this test's own
(lib/target.sh) with a stub `ssh` on PATH, so the real driver code shells out
for real and no test reaches the maintainer's fleet.

Run: python3 -m unittest tests.test_dispatch_speed -v
"""
import os
import subprocess
import time
import unittest

from tests.support import REPO, WkTest, bash, run, stub_path

# A machine that is off: ssh takes its connect timeout and then reports its
# own "could not connect" (255). The sleep is what makes serial and parallel
# tell each other apart.
_SLOW_DOWN_SSH = '''#!/bin/sh
sleep 3
exit 255
'''

# A machine that answers, and records that it was asked: the witness file is
# how a test proves no ssh happened at all rather than that it happened
# quickly.
_WITNESS_SSH = '''#!/bin/sh
echo asked >> "$WK_TEST_SSH_WITNESS"
for last; do :; done
exec bash -c "$last"
'''

_MACHINE_CONF = "WK_TARGET_KIND=remote\nWK_REMOTE_HOST={host}\n"

_LOCAL_CONF = (
    "WK_TARGET_KIND=remote\n"
    "WK_REMOTE_LOCAL=1\n"
    "WK_REMOTE_ROOT={root}\n"
    "WK_REMOTE_STORE={store}\n"
)


class TestTheFleetIsAskedAtOnce(WkTest):
    """the machines a name could be on are asked in one round, not in turn"""

    def _locate(self, machines, binp, name="no-such-workspace"):
        registry = self.tmp / f"hosts-{len(machines)}"
        registry.mkdir()
        for m in machines:
            (registry / f"{m}.conf").write_text(_MACHINE_CONF.format(host=f"{m}.invalid"))
        started = time.monotonic()
        cp = bash(
            'set -euo pipefail\n'
            '. "$WK_ROOT/lib/common.sh"\n'
            '. "$WK_ROOT/lib/target.sh"\n'
            f'ws_locate {name}\n',
            env={
                "WK_TARGET_REGISTRY": str(registry),
                "XDG_STATE_HOME": str(self.tmp / "state"),
                "PATH": f"{binp}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            },
            timeout=120,
        )
        return time.monotonic() - started, cp

    def test_two_machines_cost_what_one_costs(self):
        with stub_path({"ssh": _SLOW_DOWN_SSH}) as binp:
            one, cp1 = self._locate(["fakea"], binp)
            self.assertEqual(cp1.returncode, 0, cp1.stdout + cp1.stderr)
            two, cp2 = self._locate(["fakea", "fakeb"], binp)
            self.assertEqual(cp2.returncode, 0, cp2.stdout + cp2.stderr)
        # Serial would be 2x. The margin is the process startup the second
        # conf adds, not a second machine's worth of ssh.
        self.assertLess(
            two, one + 1.5,
            f"asking two machines took {two:.1f}s where one took {one:.1f}s -- "
            "that is one connect after another, not one round",
        )
        # And the round itself is bounded by one machine's ssh, not by the
        # fleet's size: the stub sleeps 3s per connect.
        self.assertLess(two, 12.0, f"one round of probes took {two:.1f}s")


class TestALocalNameNeverReachesTheFleet(WkTest):
    """a name this machine's own targets answer to costs no ssh"""

    def test_no_ssh_when_a_local_target_has_it(self):
        registry = self.tmp / "hosts"
        registry.mkdir()
        root = self.tmp / "root"
        (root / "ws" / "here-ws").mkdir(parents=True)
        (root / "ws" / "here-ws" / ".wk-ready").write_text("")
        store = self.tmp / "store"
        store.mkdir()
        # One target on this machine that has the workspace, and one machine
        # of its own that would have to be ssh'd to to be asked.
        (registry / "fakelocal.conf").write_text(
            _LOCAL_CONF.format(root=root, store=store))
        (registry / "fakemachine.conf").write_text(
            _MACHINE_CONF.format(host="fakemachine.invalid"))
        witness = self.tmp / "ssh-witness"
        with stub_path({"ssh": _WITNESS_SSH}) as binp:
            cp = bash(
                'set -euo pipefail\n'
                '. "$WK_ROOT/lib/common.sh"\n'
                '. "$WK_ROOT/lib/target.sh"\n'
                'ws_target here-ws\n',
                env={
                    "WK_TARGET_REGISTRY": str(registry),
                    "XDG_STATE_HOME": str(self.tmp / "state"),
                    "WK_TEST_SSH_WITNESS": str(witness),
                    "PATH": f"{binp}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                },
                timeout=60,
            )
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "fakelocal", cp.stdout + cp.stderr)
        self.assertFalse(
            witness.exists(),
            "a workspace on this machine was resolved by asking the fleet over ssh: "
            + witness.read_text() if witness.exists() else "",
        )


class TestAMachineThatDidNotAnswerIsNamed(WkTest):
    """silence is reported, by name, rather than counted as an absence"""

    def test_the_machine_is_named(self):
        registry = self.tmp / "hosts"
        registry.mkdir()
        (registry / "fakedown.conf").write_text(
            _MACHINE_CONF.format(host="fakedown.invalid"))
        with stub_path({"ssh": _SLOW_DOWN_SSH}) as binp:
            cp = bash(
                'set -euo pipefail\n'
                '. "$WK_ROOT/lib/common.sh"\n'
                '. "$WK_ROOT/lib/target.sh"\n'
                'ws_target no-such-workspace\n',
                env={
                    "WK_TARGET_REGISTRY": str(registry),
                    "XDG_STATE_HOME": str(self.tmp / "state"),
                    "PATH": f"{binp}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                },
                timeout=120,
            )
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("could not ask", cp.stderr, cp.stderr)
        self.assertIn("fakedown", cp.stderr, cp.stderr)
        # And it still answers: a machine that is off does not stop a name
        # from resolving to the default (default_target, lib/target.sh).
        self.assertEqual(cp.stdout.strip(), "container", cp.stdout + cp.stderr)


class TestTheListingWalksTheSameWay(WkTest):
    """`wk ls` reads a target's workspaces through the same parallel batch"""

    def test_every_workspace_on_a_local_target_is_listed(self):
        registry = self.tmp / "hosts"
        registry.mkdir()
        root = self.tmp / "root"
        store = self.tmp / "store"
        store.mkdir()
        names = ["ls-one", "ls-two", "ls-three"]
        for n in names:
            (root / "ws" / n).mkdir(parents=True)
            (root / "ws" / n / ".wk-ready").write_text("")
        (registry / "fakebox.conf").write_text(
            _LOCAL_CONF.format(root=root, store=store))
        cp = run("ls", env={
            "WK_TARGET_REGISTRY": str(registry),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "WK_TARGET": "fakebox",
        }, timeout=120)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        for n in names:
            self.assertIn(n, cp.stdout, f"'{n}' missing from the listing:\n{cp.stdout}")


class TestTheDispatcherResolvesOnce(unittest.TestCase):
    """the target is resolved once per invocation, not once per question"""

    def test_only_one_call_site_resolves_the_target(self):
        text = (REPO / "wk").read_text()
        calls = text.count('resolve_target "$@"')
        self.assertEqual(
            calls, 1,
            "resolve_target is called from more than one place in `wk`: each call "
            "walks every target that could hold the name, so a second one doubles "
            "what `wk enter` costs. Reuse $resolved.",
        )


class TestTouchedFilesParse(unittest.TestCase):
    """every file the walk lives in is syntactically valid bash"""

    def test_bash_n(self):
        for rel in ("wk", "lib/target.sh", "lib/par.sh", "lib/common.sh",
                    "lib/reach.sh", "cmd/ls", "cmd/enter"):
            with self.subTest(file=rel):
                cp = subprocess.run(["bash", "-n", str(REPO / rel)],
                                    capture_output=True, text=True)
                self.assertEqual(cp.returncode, 0, cp.stderr)


if __name__ == "__main__":
    unittest.main()
