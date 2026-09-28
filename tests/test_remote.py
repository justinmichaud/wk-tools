"""A configured machine name resolves as a target, and `wk status`/`wk ls`
reach it. Each docstring is the
phrase of the behaviour it checks.

Every test that touches a real machine over ssh is gated on that specific
machine answering `ssh -o BatchMode=yes <name> true`
(tests.support.requires_machine) and never provisions, reboots or otherwise
mutates it -- these are read-only probes only.

Run: python3 -m unittest tests.test_remote -v
"""
import contextlib
import io
import os
import sys
import unittest

from unittest import mock

from tests.support import (REAL_MACHINES, REPO, WkTest, stub_path,
                           requires_machine)

sys.path.insert(0, str(REPO / "lib"))
from wk import sudo, targets  # noqa: E402


class TestUnregisteredWorkspaceResolves(WkTest):
    """a remote workspace with no registry entry here still resolves"""

    def test_unregistered_remote_workspace_resolves(self):
        """ws_target finds a remote workspace the registry misses"""
        # A registry holding only the fake conf (WK_MACHINES_DIR), so the walk
        # cannot reach the real fleet.
        registry = self.tmp / "hosts"
        registry.mkdir()
        root = self.tmp / "root"
        (root / "ws" / "tws").mkdir(parents=True)
        (root / "ws" / "tws" / ".wk-ready").write_text("")
        store = self.tmp / "store"
        store.mkdir()
        # WK_REMOTE_LOCAL drives the remote driver without ssh; the store is
        # a different directory from the root, so only Remote.info -- not the
        # host-side directory test -- can find the workspace.
        (registry / "fakebox.conf").write_text(
            "kind=build\ndriver=remote\n"
            "local=1\n"
            f"root={root}\n"
            f"store={store}\n"
        )
        reg = targets.Registry(REPO, env=dict(os.environ, WK_MACHINES_DIR=str(registry),
                                              XDG_STATE_HOME=str(self.tmp / "state")))
        self.assertEqual(reg.ws_target("tws"), "fakebox")


class TestMachineAnswers(WkTest):
    """a fan-out (`wk push --all`, `wk key sudo --all`) tells a machine that is
    down from one with no wk-tools, and lets no ssh error text through"""

    def _registry(self, conf):
        """A registry holding exactly one fake machine (WK_MACHINES_DIR), so
        nothing here can reach the real fleet."""
        registry = self.tmp / "hosts"
        registry.mkdir(exist_ok=True)
        (registry / "fakebox.conf").write_text(conf)
        return registry

    def _machine_answers(self, conf):
        # ssh answers as it does for a name nothing resolves; no network is asked.
        ssh = ('echo "ssh: Could not resolve hostname wk-test-unreachable.invalid:'
               ' nodename nor servname provided, or not known" >&2; exit 255')
        with stub_path({"ssh": ssh}) as binp, \
                mock.patch.dict(os.environ, {"PATH": f"{binp}:{os.environ.get('PATH', '/usr/bin:/bin')}"}), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(out):
            env = dict(os.environ, WK_MACHINES_DIR=str(self._registry(conf)),
                       XDG_STATE_HOME=str(self.tmp / "state"), WK_SSH_TIMEOUT="3")
            answered = sudo.machine_answers(targets.Registry(REPO, env=env).load("fakebox"), "fakebox")
        return answered, out.getvalue()

    def test_an_unreachable_machine_is_reported_as_unreachable(self):
        """an ssh destination nothing resolves is 'unreachable', not 'no
        wk-tools', and the line carries ssh's own last word on why -- the
        measurement, not a guess at 'off'"""
        # An .invalid name fails resolution at once, so this needs no route to
        # anything and cannot hang on a real machine's timeout.
        answered, out = self._machine_answers(
            "kind=build\ndriver=remote\nhost=wk-test-unreachable.invalid\n")
        self.assertFalse(answered, out)
        self.assertRegex(out, r"(?m)^fakebox\s+unreachable over ssh: \S", out)
        self.assertNotIn("no wk-tools", out)
        self.assertNotIn("not on the tailnet", out, "a guess where ssh's own reason belongs")
        self.assertEqual(len([l for l in out.splitlines() if "fakebox" in l]), 1,
                         "one line per machine, not ssh's whole stderr: " + out)

    def test_the_machine_itself_is_not_a_far_side(self):
        """the machine this runs on has no far side to answer for it"""
        answered, out = self._machine_answers("kind=build\ndriver=remote\nlocal=1\n")
        self.assertFalse(answered, out)
        self.assertRegex(out, r"(?m)^fakebox\s+not a machine of its own", out)

    def test_push_and_sudo_share_the_one_fan_out_line(self):
        """cmd/push and `wk key sudo` ask the one reason line (machine_answers, status.far_side_reason) rather than deciding it themselves"""
        for path, shared in (("cmd/push", "status.far_side_reason"), ("lib/wk/sudo.py", "machine_answers")):
            with self.subTest(path=path):
                text = (REPO / path).read_text(errors="replace")
                self.assertIn(shared, text)
                self.assertNotIn("no wk-tools there yet", text)


def _configured_remote_machines():
    """{name: kind} for every target conf in machines/ -- pure logic, no ssh."""
    reg = targets.Registry(REPO, env=dict(os.environ, WK_MACHINES_DIR=str(REAL_MACHINES)))
    return {n: reg.kind(n) for n in reg.known()}


class TestRemoteConfsResolve(WkTest):
    def test_remote_confs_resolve(self):
        """a machine name is a target"""
        machines = _configured_remote_machines()
        if not machines:
            self.skipTest(f"no machines configured in {REAL_MACHINES}")
        bad = [n for n, k in machines.items() if not k]
        self.assertEqual(bad, [], f"configured but unresolvable: {bad}")


def _info_reaches(name):
    """Load the driver for `name` and ask it about a workspace that does not
    exist: `info` answers "unreachable" rather than raising, so what this
    catches is the load or the probe dying outright."""
    reg = targets.Registry(REPO, env=dict(os.environ, WK_MACHINES_DIR=str(REAL_MACHINES)))
    return reg.load(name).info("selftest-nonexistent")


class TestRemoteReachable(WkTest):
    """`wk status`/`wk ls` list what is on a configured machine"""

    @requires_machine("buildbox4")
    def test_buildbox4_reachable(self):
        """`wk status`/`wk ls` list what is on a configured machine"""
        self.assertEqual(_info_reaches("buildbox4"), "absent")

    @requires_machine("devbox-arm64-2")
    def test_devbox_arm64_2_reachable(self):
        """`wk status`/`wk ls` list what is on a configured machine"""
        self.assertEqual(_info_reaches("devbox-arm64-2"), "absent")

    @requires_machine("moose")
    def test_moose_reachable(self):
        """`wk status`/`wk ls` list what is on a configured machine"""
        self.assertEqual(_info_reaches("moose"), "absent")


class TestTheMirrorOnTheBox(WkTest):
    """the mirror this driver keeps on a build box is made by the one snippet
    every other mirror in the fleet is made by (mirror_refresh_script)"""

    def setUp(self):
        super().setUp()
        # A build box driven without ssh (WK_REMOTE_LOCAL, lib/wk/targets.py).
        self.registry = self.tmp / "hosts"
        self.registry.mkdir()
        (self.registry / "fakebox.conf").write_text(
            "kind=build\ndriver=remote\n"
            "local=1\n"
            f"root={self.tmp / 'wk'}\n"
            f"store={self.tmp / 'store'}\n"
        )

    def _script(self):
        """The shell Remote._mirror_update sends, to a fake machine: the driver is real, only the far side is not."""
        from wk import targets
        from wk.machine import Fake, Result
        env = dict(os.environ, WK_MACHINES_DIR=str(self.registry), XDG_STATE_HOME=str(self.tmp / "state"))
        t = targets.Registry(REPO, env).load("fakebox")
        t.machine = Fake("fakebox")
        probe = "/far\nLinux\n4\n0.1 0 0\n===MEM===\nMemAvailable: 1024 kB\n===IONICE===\nno\n"
        t.machine.react(["sh", "-c"], lambda argv, f: Result(0, probe if argv[-1] == targets.PROBE_SCRIPT else ""))
        with contextlib.redirect_stderr(io.StringIO()):
            t._mirror_update(str(self.tmp / "wk"))
        return "\n".join(e[1][2] for e in t.machine.effects if e[0] == "run" and e[1][:2] == ("sh", "-c"))

    def test_it_carries_every_default_remote_with_no_tags(self):
        """It carried origin's main alone, so a workspace on the box could not
        take a fork's branch from it and fetched all four upstreams over the
        network instead. Carrying them saves disk rather than costing it:
        every checkout on the box is a `--shared` clone of this one repository."""
        script = self._script()
        for remote in ("origin", "wpe", "fork", "forkwpe"):
            with self.subTest(remote=remote):
                self.assertIn(f"config remote.{remote}.tagOpt --no-tags", script)
        self.assertIn("for r in origin wpe fork forkwpe; do", script)
        self.assertIn('fetch --prune -q "$r"', script)
        self.assertIn("+refs/heads/main:refs/heads/main", script)
        self.assertIn("+refs/heads/*:refs/remotes/fork/*", script)
        self.assertIn("gc.auto 0", script)

    def test_it_names_no_url_of_its_own(self):
        """A second spelling of an upstream's URL is a mirror that carries
        something else than git.REMOTES says."""
        import inspect
        from wk import targets
        body = inspect.getsource(targets.Remote._mirror_update)
        self.assertNotIn("github.com", body, body)
        self.assertIn("mirror_refresh_script", body)

    def test_the_workspace_wiring_names_the_same_mirror(self):
        """Remote.wiring_args tells a checkout on the box where the local copy
        is; naming it twice is how the two drift apart."""
        import inspect
        sys.path.insert(0, str(REPO / "lib"))
        from wk import targets
        body = inspect.getsource(targets.Remote.wiring_args)
        self.assertIn("self.mirror_dir()", body)
        self.assertNotIn("/mirror", body)


if __name__ == "__main__":
    unittest.main()
