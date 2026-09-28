"""What resolving a workspace name costs: the walk that decides which target
holds it (`Registry.locate`, lib/wk/targets.py) and the dispatcher that asks
for it.

Three properties, each shown by what the stub machines record rather than asserted about the source:

  * the machines are asked **at once**, not one connect after another -- one
    machine that will not answer costs the walk its own timeout, never
    everybody's;
  * a name **this machine's own environments answer to** costs no ssh at all,
    so the fleet is never in the path of `wk enter <a container workspace>`;
  * a machine that did not answer is **named**, so an answer with a machine
    missing from it does not read as a complete one.

Plus the dispatcher's side of the same cost: `resolve_target` is called once
per invocation, not once per question that needs its answer.

Every fake machine here is a conf in a WK_MACHINES_DIR of this test's own
with a stub `ssh` on PATH, so the real driver code shells out for real and
no test reaches the maintainer's fleet.

Run: python3 -m unittest tests.test_dispatch_speed -v
"""
import os
import subprocess
import sys
import unittest

from tests.support import REPO, WkTest, clean_env, run, stub_path

# A machine that is off: ssh reports its own "could not connect" (255).
_DOWN_SSH = '''#!/bin/sh
exit 255
'''

# A machine that answers only once every machine of the round has been asked, or after `alone` has passed: ssh -G,
# which only resolves config, answers at once.
_BARRIER_SSH = '''#!/bin/sh
case " $* " in *" -G "*) exit 0 ;; esac
echo asked >> "$WK_TEST_SSH_LOG"
alone=0
while [ "$(grep -c asked "$WK_TEST_SSH_LOG")" -lt 2 ] && [ $alone -lt 100 ]; do sleep 0.05; alone=$((alone + 1)); done
echo answered >> "$WK_TEST_SSH_LOG"
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

_MACHINE_CONF = "kind=build\ndriver=remote\nhost={host}\n"

_LOCAL_CONF = (
    "kind=build\ndriver=remote\n"
    "local=1\n"
    "root={root}\n"
    "store={store}\n"
)

# One Registry question in a process of its own, so the stub ssh on PATH is what the driver runs.
_ASK = ("import sys; sys.path.insert(0, sys.argv[1]); from wk import targets\n"
        "a = getattr(targets.Registry(sys.argv[2]), sys.argv[3])(sys.argv[4])\n"
        "print(a if isinstance(a, str) else ' '.join(a))\n")


def ask(fn, ws, env, timeout=120):
    return subprocess.run([sys.executable, "-c", _ASK, str(REPO / "lib"), str(REPO), fn, ws],
                          env=clean_env(env), capture_output=True, text=True, timeout=timeout)


class TestTheFleetIsAskedAtOnce(WkTest):
    """the machines a name could be on are asked in one round, not in turn"""

    def test_two_machines_are_asked_before_either_answers(self):
        """Each stub ssh waits for the other to be asked before it answers, so a walk that asks in turn leaves the first
        waiting alone: it then answers anyway, after `alone`, and the order in the log shows the turn."""
        registry, log = self.tmp / "hosts", self.tmp / "asked"
        registry.mkdir()
        for m in ("fakea", "fakeb"):
            (registry / f"{m}.conf").write_text(_MACHINE_CONF.format(host=f"{m}.invalid"))
        with stub_path({"ssh": _BARRIER_SSH}) as binp:
            cp = ask("locate", "no-such-workspace", {
                "WK_MACHINES_DIR": str(registry),
                "XDG_STATE_HOME": str(self.tmp / "state"),
                "WK_TEST_SSH_LOG": str(log),
                "PATH": f"{binp}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            })
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(log.read_text().split(), ["asked", "asked", "answered", "answered"],
                         "one connect after another, not one round")


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
            cp = ask("ws_target", "here-ws", {
                "WK_MACHINES_DIR": str(registry),
                "XDG_STATE_HOME": str(self.tmp / "state"),
                "WK_TEST_SSH_WITNESS": str(witness),
                "PATH": f"{binp}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            }, timeout=60)
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
        with stub_path({"ssh": _DOWN_SSH}) as binp:
            cp = ask("ws_target", "no-such-workspace", {
                "WK_MACHINES_DIR": str(registry),
                "XDG_STATE_HOME": str(self.tmp / "state"),
                "PATH": f"{binp}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            })
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("could not ask", cp.stderr, cp.stderr)
        self.assertIn("fakedown", cp.stderr, cp.stderr)
        # And it still answers: a machine that is off does not stop a name
        # from resolving to the default (Registry.default).
        self.assertEqual(cp.stdout.strip(), "container", cp.stdout + cp.stderr)


class TestTheListingWalksTheSameWay(WkTest):
    """`wk ls` reads a target's workspaces through the same walk"""

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
            "WK_MACHINES_DIR": str(registry),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "WK_TARGET": "fakebox",
        }, timeout=120)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        for n in names:
            self.assertIn(n, cp.stdout, f"'{n}' missing from the listing:\n{cp.stdout}")


class TestTheDispatcherResolvesOnce(unittest.TestCase):
    """the target is resolved once per invocation, not once per question"""

    def test_only_one_call_site_resolves_the_target(self):
        text = (REPO / "lib" / "wk" / "dispatch.py").read_text()
        calls = text.count("resolve_target(") - text.count("def resolve_target(")
        self.assertEqual(
            calls, 1,
            "resolve_target is called from more than one place in the dispatcher: each "
            "call walks every target that could hold the name, so a second one doubles "
            "what `wk enter` costs. Reuse `resolved`.",
        )


if __name__ == "__main__":
    unittest.main()
