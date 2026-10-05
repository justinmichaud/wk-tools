"""What resolving a workspace name costs: the walk that decides which place"""
import os
import subprocess
import sys
import unittest

from tests.support import REPO, WkTest, clean_env, run, stub_path

_DOWN_SSH = '''#!/bin/sh
exit 255
'''

_BARRIER_SSH = '''#!/bin/sh
case " $* " in *" -G "*) exit 0 ;; esac
echo asked >> "$WK_TEST_SSH_LOG"
alone=0
while [ "$(grep -c asked "$WK_TEST_SSH_LOG")" -lt 2 ] && [ $alone -lt 100 ]; do sleep 0.05; alone=$((alone + 1)); done
echo answered >> "$WK_TEST_SSH_LOG"
exit 255
'''

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

_ASK = ("import sys; sys.path.insert(0, sys.argv[1]); from wk import places\n"
        "a = getattr(places.Registry(sys.argv[2]), sys.argv[3])(sys.argv[4])\n"
        "print(a if isinstance(a, str) else ' '.join(a))\n")


def ask(fn, ws, env, timeout=120):
    return subprocess.run([sys.executable, "-c", _ASK, str(REPO / "lib"), str(REPO), fn, ws],
                          env=clean_env(env), capture_output=True, text=True, timeout=timeout)


class TestTheFleetIsAskedAtOnce(WkTest):

    def test_two_machines_are_asked_before_either_answers(self):
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

    def test_no_ssh_when_a_local_target_has_it(self):
        registry = self.tmp / "hosts"
        registry.mkdir()
        root = self.tmp / "root"
        (root / "ws" / "here-ws").mkdir(parents=True)
        (root / "ws" / "here-ws" / ".wk-ready").write_text("")
        store = self.tmp / "store"
        store.mkdir()
        (registry / "fakelocal.conf").write_text(
            _LOCAL_CONF.format(root=root, store=store))
        (registry / "fakemachine.conf").write_text(
            _MACHINE_CONF.format(host="fakemachine.invalid"))
        witness = self.tmp / "ssh-witness"
        with stub_path({"ssh": _WITNESS_SSH}) as binp:
            cp = ask("ws_place", "here-ws", {
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

    def test_the_machine_is_named(self):
        registry = self.tmp / "hosts"
        registry.mkdir()
        (registry / "fakedown.conf").write_text(
            _MACHINE_CONF.format(host="fakedown.invalid"))
        with stub_path({"ssh": _DOWN_SSH}) as binp:
            cp = ask("ws_place", "no-such-workspace", {
                "WK_MACHINES_DIR": str(registry),
                "XDG_STATE_HOME": str(self.tmp / "state"),
                "PATH": f"{binp}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            })
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("could not ask", cp.stderr, cp.stderr)
        self.assertIn("fakedown", cp.stderr, cp.stderr)
        self.assertEqual(cp.stdout.strip(), "container", cp.stdout + cp.stderr)


class TestTheListingWalksTheSameWay(WkTest):

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
            "WK_PLACE": "fakebox",
        }, timeout=120)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        for n in names:
            self.assertIn(n, cp.stdout, f"'{n}' missing from the listing:\n{cp.stdout}")


if __name__ == "__main__":
    unittest.main()
