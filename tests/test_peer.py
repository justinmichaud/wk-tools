"""A workspace another workstation owns: resolved through the peer's own `wk`, and every command about it handed
over, against a stub ssh that runs the far side in this shell and a stub peer `wk` that records what it was asked."""
import contextlib
import io
import os
import subprocess
import sys
import unittest

from unittest import mock

from tests.support import REPO, WkTest, stub_path

sys.path.insert(0, str(REPO / "lib"))
from wk import places, pr, workspace  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Fake  # noqa: E402

# `ssh <opts> <host> <command>` run here, with no WK_* variable carried across, as a real ssh carries none.
_FAKE_SSH = """#!/bin/sh
for v in $(env | sed -n 's/^\\(WK_[A-Za-z0-9_]*\\)=.*/\\1/p'); do unset "$v"; done
while [ $# -gt 0 ]; do
    case "$1" in
        -o|-i|-p|-l|-F|-W) shift 2 ;;
        -*) shift ;;
        *) shift; break ;;
    esac
done
exec /bin/sh -c "$*"
"""

# The peer's own `wk`: answers `ls --json` and `zed --route`, stops listing what it removed, and logs every call.
_PEER_WK = """#!/bin/sh
printf '%s\\n' "$* ${{WK_ZED_PUBKEY:+key=$WK_ZED_PUBKEY}}${{WK_FORCE:+force=1 }}${{WK_QUIET:+quiet=1 }}${{WK_YES:+yes=1 }}" >> "{log}"
case "$1 $2" in
"ls --json")
    if [ -f "{removed}" ]; then printf '%s\\n' '{{"workspaces": []}}'; else printf '%s\\n' '{listing}'; fi
    exit 0 ;;
esac
case "$1 $3" in
"zed --route")
    printf 'user=dev\\nsrc=/src/WebKit\\nproxy=/opt/wk-tools/container/ssh-transport %s\\n' "$2"
    exit 0 ;;
esac
case "$1" in
rm) : > "{removed}"; exit 0 ;;
new) [ "$2" = refusedws ] && exit 3; exit 0 ;;
esac
exit 0
"""

_LISTING = ('{"workspaces": [{"name": "peerws", "place": "container", '
            '"state": "running", "base": "-", "arch": "native", "changes": "1M"}]}')


class PeerFixture(WkTest):
    """A WK_ROOT whose fleet is one peer, and a $HOME of its own for `wk zed`'s ssh alias."""

    def setUp(self):
        super().setUp()
        self.root = self.tmp / "wk-root"
        (self.root / "machines").mkdir(parents=True)
        for entry in REPO.iterdir():
            if entry.name in ("machines", ".git", "__pycache__"):
                continue
            (self.root / entry.name).symlink_to(entry)

        self.tools = self.tmp / "peer-tools"
        self.tools.mkdir()
        self.calls = self.tmp / "peer-calls"
        self.removed = self.tmp / "peer-removed"
        peer_wk = self.tools / "wk"
        peer_wk.write_text(_PEER_WK.format(log=self.calls, listing=_LISTING,
                                           removed=self.removed))
        peer_wk.chmod(0o755)

        (self.root / "machines" / "peerbox.conf").write_text(
            "kind=peer\ndriver=remote\n"
            "peer=1\n"
            "host=peerbox\n"
            f"root={self.tmp / 'remote-root'}\n"
            f"tools={self.tools}\n"
            f"store={self.tmp / 'store'}\n"
        )
        self.home = self.tmp / "home"
        self.home.mkdir()

    def env(self, extra=None):
        e = {
            "WK_ROOT": str(self.root),
            "WK_MACHINES_DIR": str(self.root / "machines"),
            "HOME": str(self.home),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "WK_SSH_TIMEOUT": "5",
        }
        e.update(extra or {})
        return e

    def registry(self):
        return places.Registry(self.root, env=dict(os.environ, **self.env()))

    def _wk(self, *args, extra_env=None):
        with stub_path({"ssh": _FAKE_SSH}) as binp:
            env = dict(os.environ)
            env.update(self.env({"PATH": f"{binp}:{os.environ['PATH']}"}))
            for v in ("WK_MARKER", "WK_FORCE", "WK_QUIET"):
                env.pop(v, None)
            env.update(extra_env or {})
            return subprocess.run(
                [str(self.root / "wk"), *args],
                cwd=str(self.root), env=env, timeout=120,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def peer_calls(self):
        if not self.calls.exists():
            return []
        return [line for line in self.calls.read_text().splitlines() if line]


class TestPeerResolution(PeerFixture):
    @contextlib.contextmanager
    def faked_ssh(self):
        with stub_path({"ssh": _FAKE_SSH}) as binp, \
                mock.patch.dict(os.environ, {"PATH": f"{binp}:{os.environ['PATH']}"}):
            yield

    def test_peer_workspace_resolves(self):
        with self.faked_ssh():
            self.assertEqual(self.registry().ws_place("peerws"), "peerbox")

    def test_only_a_workstation_keeps_its_own_records(self):
        (self.root / "machines" / "buildbox.conf").write_text(
            "kind=build\ndriver=remote\nhost=buildbox\n")
        reg = self.registry()
        self.assertTrue(reg.load("peerbox").peer)
        self.assertFalse(reg.load("buildbox").peer)


class TestPeerDelegation(PeerFixture):

    def test_destroying_one_is_asked_of_the_peer(self):
        cp = self._wk("rm", "peerws", extra_env={"WK_YES": "1"})
        self.assertEqual(cp.returncode, 0, cp.stdout)
        asked = [c for c in self.peer_calls() if c.startswith("rm ")]
        self.assertEqual(len(asked), 1, self.peer_calls())
        self.assertIn("rm peerws", asked[0])
        self.assertIn("yes=1", asked[0],
                      "the peer was left a question with no terminal to ask it on")

    def test_the_question_is_asked_here_and_names_the_machine(self):
        cp = self._wk("rm", "peerws")
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("peerws@peerbox", cp.stdout, cp.stdout)
        self.assertFalse([c for c in self.peer_calls() if c.startswith("rm ")],
                         self.peer_calls())

    def test_making_one_is_the_peers_own_wk_new(self):
        cp = self._wk("new", "newws", "--on", "peerbox", "--arch", "armhf", "--pr", "1234")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("new newws --arch armhf --pr 1234 ", self.peer_calls())
        self.assertEqual(0, self._wk("new", "newws", "--on", "peerbox", "--kill").returncode)
        self.assertIn("new newws --kill ", self.peer_calls())
        self.assertFalse((self.tmp / "remote-root").exists(), "a checkout was made here for the peer")

    def test_zed_opens_the_peers_new_workspace_from_here(self):
        cp = self._wk("new", "newws", "--on", "peerbox", "--zed", "--dry-run")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual(self.peer_calls(), [], "a dry run hands nothing over")
        self.assertRegex(cp.stdout, r"would run: .* new newws'")
        self.assertRegex(cp.stdout, r"would run: \S*/cmd/zed newws")

    def test_a_refused_creation_is_the_peers_refusal(self):
        cp = self._wk("new", "refusedws", "--on", "peerbox")
        self.assertEqual(cp.returncode, 3, cp.stdout)
        self.assertIn("peerbox did not create 'refusedws'; what its own wk said is above.", cp.stdout)

    def test_this_machine_named_as_a_peer_is_its_own_default(self):
        (self.root / "machines" / "me.conf").write_text("kind=peer\npeer=1\nlocal=1\n")
        reg = places.Registry(self.root, env=dict(os.environ, **self.env()), machine=Fake("host"))
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()) as err:
            workspace.new_front(reg, None, "newws", {"place": "me"}, pr)
        self.assertIn("'me' is this machine, and its workspaces are made at its own default", err.getvalue())
        self.assertEqual(reg.machine.effects, [])

    def test_every_command_the_integration_run_types_reaches_the_peer(self):
        """tests/test_dev_integration.py's moose steps, typed here."""
        steps = [(("start", "peerws"), "start peerws "),
                (("ai", "claude", "peerws", "-p", "hi"), "ai claude peerws -p hi "),
                (("build", "peerws", "jsc-debug", "--detach"), "build peerws jsc-debug --detach "),
                (("status", "peerws", "--wait", "--timeout", "60"), "status --no-fleet --records peerws "),
                (("status", "peerws", "--log"), "status peerws --log "),
                (("enter", "peerws", "--", "bash", "-lc", "true"), "enter peerws -- bash -lc true "),
                (("sync", "peerws"), "sync peerws "),
                (("doctor", "peerws"), "doctor peerws ")]
        for argv, asked in steps:
            with self.subTest(argv=argv):
                cp = self._wk(*argv)
                self.assertEqual(cp.returncode, 0, cp.stdout)
                self.assertIn(asked, self.peer_calls())
        cp = self._wk("ls", "--json", extra_env={"WK_PLACE": "peerbox"})
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("ls --continued --json ", self.peer_calls())

    def test_a_here_command_stays_here(self):
        cp = self._wk("zed", "peerws", "--url")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("ssh://wk-peerws/src/WebKit", cp.stdout)
        self.assertTrue(any(c.startswith("zed peerws --route") for c in self.peer_calls()),
                        self.peer_calls())
        self.assertFalse(any("--url" in c for c in self.peer_calls()), self.peer_calls())

        alias = (self.home / ".ssh" / "config.d" / "wk").read_text()
        self.assertIn("Host wk-peerws", alias)
        self.assertIn("User dev", alias)
        self.assertIn(
            "ProxyCommand ssh peerbox /opt/wk-tools/container/ssh-transport peerws",
            alias)

    def test_the_peer_authorises_the_asking_machines_key(self):
        self._wk("zed", "peerws", "--url")
        pub = self.tmp / "state" / "wk" / "ssh" / "zed_ed25519.pub"
        self.assertTrue(pub.exists(), "no zed key was generated for this machine")
        route = [c for c in self.peer_calls() if c.startswith("zed peerws --route")]
        self.assertEqual(len(route), 1, self.peer_calls())
        self.assertIn(pub.read_text().split()[1], route[0])


class TestDelegatedGlobalFlags(PeerFixture):
    def test_force_and_quiet_cross_as_environment_and_only_when_asked(self):
        self.assertEqual(self._wk("status", "peerws", "--log").returncode, 0)
        self.assertFalse(any("force=1" in c or "quiet=1" in c for c in self.peer_calls()), self.peer_calls())
        for flag, seen in (("--force", "force=1"), ("--quiet", "quiet=1")):
            cp = self._wk("status", "peerws", "--log", flag)
            self.assertEqual(cp.returncode, 0, cp.stdout)
            self.assertIn(seen, self.peer_calls()[-1])
            self.assertNotIn(flag, self.peer_calls()[-1])


if __name__ == "__main__":
    unittest.main()
