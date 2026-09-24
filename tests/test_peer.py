"""A workspace another workstation owns: resolving it, and the one place a
command about it is handed over. Each docstring is the phrase of the
behaviour it checks.

A peer's workspaces are its containers and its guests, in its own store --
nothing this side has a path to, and nothing under the remote root the
`remote` driver otherwise reads. So the driver asks the peer's own `wk`
(targets/remote.sh, "peers") and `wk` hands the whole command over
(delegate_target/delegate_run), which is what makes `wk logs`, `wk status`,
`wk build` and the rest work on one without a branch of their own.

No peer is needed to test that: a scratch WK_ROOT holds one fake machine's
conf, a stub `ssh` first on PATH runs what would have crossed the network in
this shell, and a stub `wk` at the far end answers and records what it was
asked -- the same technique tests.test_remote and the disk-logic tests use to
drive real driver code against a fake of the thing it talks to.

Run: python3 -m unittest tests.test_peer -v
"""
import contextlib
import io
import os
import re
import subprocess
import sys
import unittest

from tests.support import REPO, repo_files, WkTest, bash, stub_path

sys.path.insert(0, str(REPO / "lib"))
from wk import targets  # noqa: E402
from wk.act import Refused  # noqa: E402

# Runs locally what `ssh <opts> <host> <command>` would have run over there.
# Every option is dropped, then the destination, and what is left is the
# command -- which is how _rsh/_rsh_q/t_wk_tty all spell it.
#
# Every WK_* variable is dropped first, because a real ssh carries none of
# this shell's environment: without that, a fake that runs the command here
# would let one leak across and prove nothing about what the far side was
# actually told. What the command string carries in front of `wk` is exactly
# what arrives.
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

# The peer's own `wk`. It answers the questions the driver asks -- `wk ls
# --json` for what it holds, `wk zed <ws> --route` for how to reach one --
# destroys what it is asked to destroy, and records every invocation, so a
# test can prove a command was handed over rather than run here. Its `rm`
# converges: what it has removed it stops listing, which is the evidence
# `wk rm` reads back before it reports a workspace gone.
_PEER_WK = """#!/bin/sh
printf '%s\\n' "$* ${{WK_ZED_PUBKEY:+key=$WK_ZED_PUBKEY}}${{WK_FORCE:+force=1 }}${{WK_QUIET:+quiet=1 }}${{WK_YES:+yes=1 }}" >> "{log}"
case "$1 $2" in
"ls --json")
    if [ -f "{removed}" ]; then printf '%s\\n' '{{"workspaces": []}}'; else printf '%s\\n' '{listing}'; fi
    exit 0 ;;
esac
case "$1 $3" in
"zed --route")
    printf 'user=dev\\nsrc=/src/WebKit\\nproxy=/opt/wk-tools/container/ssh-transport.sh %s\\n' "$2"
    exit 0 ;;
esac
case "$1" in
rm) : > "{removed}"; exit 0 ;;
esac
exit 0
"""

_LISTING = ('{"workspaces": [{"name": "peerws", "target": "container", '
            '"state": "running", "base": "-", "arch": "native", "changes": "1M"}]}')


class PeerFixture(WkTest):
    """A WK_ROOT whose registry (WK_MACHINES_DIR, lib/target.sh) holds one
    peer and nothing else, so the walk cannot reach the real fleet, plus a
    $HOME of its own: `wk zed` writes an ssh alias, and no test may write
    into the person's real ~/.ssh."""

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
            "KIND=peer\nWK_TARGET_KIND=remote\n"
            "WK_REMOTE_PEER=1\n"
            "WK_REMOTE_HOST=peerbox\n"
            f"WK_REMOTE_ROOT={self.tmp / 'remote-root'}\n"
            f"WK_REMOTE_TOOLS={self.tools}\n"
            f"WK_REMOTE_STORE={self.tmp / 'store'}\n"
        )
        self.home = self.tmp / "home"
        self.home.mkdir()

    def env(self, extra=None):
        with_ssh = dict(extra or {})
        e = {
            "WK_ROOT": str(self.root),
            "WK_MACHINES_DIR": str(self.root / "machines"),
            "HOME": str(self.home),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "WK_SSH_TIMEOUT": "5",
        }
        e.update(with_ssh)
        return e

    def registry(self):
        return targets.Registry(self.root, env=dict(os.environ, **self.env()))

    def peer_calls(self):
        if not self.calls.exists():
            return []
        return [line for line in self.calls.read_text().splitlines() if line]


class TestPeerResolution(PeerFixture):
    """a workspace a peer owns resolves like any other"""

    def _bash(self, script, binp):
        return bash(script, env=self.env({"PATH": f"{binp}:{os.environ['PATH']}"}),
                    cwd=str(self.root))

    def test_peer_workspace_resolves(self):
        """ws_target finds a workspace only the peer's own `wk` knows"""
        with stub_path({"ssh": _FAKE_SSH}) as binp:
            cp = self._bash('''
set -euo pipefail
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/target.sh"
t=$(ws_target peerws)
[ "$t" = peerbox ] || { echo "ws_target said '$t'"; exit 1; }
''', binp)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_only_a_workstation_keeps_its_own_records(self):
        """a workstation's workspaces are its own, so a removal is its own `wk
        rm`; a build box's are recorded on the workstation that made them"""
        (self.root / "machines" / "buildbox.conf").write_text(
            "KIND=build\nWK_TARGET_KIND=remote\nWK_REMOTE_HOST=buildbox\n")
        reg = self.registry()
        self.assertTrue(reg.load("peerbox").peer)
        self.assertFalse(reg.load("buildbox").peer)

    def test_peer_info_and_list(self):
        """t_info answers present/absent for a peer, and t_list names what it holds"""
        with stub_path({"ssh": _FAKE_SSH}) as binp:
            cp = self._bash('''
set -euo pipefail
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/target.sh"
load_target peerbox
echo "info=$(t_info peerws)"
echo "ghost=$(t_info ghost)"
echo "list=$(t_list | cut -f1 | tr '\\n' ',')"
''', binp)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("info=present", cp.stdout)
        self.assertIn("ghost=absent", cp.stdout)
        self.assertIn("list=peerws,", cp.stdout)


class TestPeerDelegation(PeerFixture):
    """`wk` hands a command about a peer's workspace to the peer"""

    def _wk(self, *args, extra_env=None):
        with stub_path({"ssh": _FAKE_SSH}) as binp:
            env = dict(os.environ)
            env.update(self.env({"PATH": f"{binp}:{os.environ['PATH']}"}))
            env.update(extra_env or {})
            env.pop("WK_MARKER", None)
            return subprocess.run(
                [str(self.root / "wk"), *args],
                cwd=str(self.root), env=env, timeout=120,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def test_command_runs_on_the_peer(self):
        """`wk logs <ws>` runs `wk logs <ws>` over there, not a thing here"""
        cp = self._wk("logs", "peerws")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("logs peerws ", self.peer_calls())

    def test_destroying_one_is_asked_of_the_peer(self):
        """`wk rm <ws>` of a workspace a peer keeps the record of is that
        peer's own `wk rm`, run over there with the answer given here"""
        cp = self._wk("rm", "peerws", extra_env={"WK_YES": "1"})
        self.assertEqual(cp.returncode, 0, cp.stdout)
        asked = [c for c in self.peer_calls() if c.startswith("rm ")]
        self.assertEqual(len(asked), 1, self.peer_calls())
        self.assertIn("rm peerws", asked[0])
        self.assertIn("yes=1", asked[0],
                      "the peer was left a question with no terminal to ask it on")
        self.assertIn("destroyed on peerbox", cp.stdout, cp.stdout)
        self.assertIn("workspace 'peerws' destroyed", cp.stdout, cp.stdout)

    def test_the_question_is_asked_here_and_names_the_machine(self):
        """one confirmation, on the machine the person typed it on, naming the
        workspace and the machine it is on -- nothing crosses until it is
        answered"""
        cp = self._wk("rm", "peerws")
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("peerws@peerbox", cp.stdout, cp.stdout)
        self.assertFalse([c for c in self.peer_calls() if c.startswith("rm ")],
                         self.peer_calls())

    def test_making_one_stays_with_the_owner(self):
        """the other half of the lifecycle is still typed over there: this
        driver would make a plain checkout under ~/wk, which is not what a
        workstation's workspaces are"""
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()) as err:
            self.registry().load("peerbox").create("newws")
        self.assertIn("ssh peerbox wk new newws", err.getvalue())
        self.assertFalse(self.peer_calls())

    def test_a_here_command_stays_here(self):
        """`wk zed` is declared `here`: it asks the peer for a route and opens it from this machine"""
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
            "ProxyCommand ssh peerbox /opt/wk-tools/container/ssh-transport.sh peerws",
            alias)

    def test_the_peer_authorises_the_asking_machines_key(self):
        """the key that travels with the route request is this machine's, not the peer's"""
        self._wk("zed", "peerws", "--url")
        pub = self.tmp / "state" / "wk" / "ssh" / "zed_ed25519.pub"
        self.assertTrue(pub.exists(), "no zed key was generated for this machine")
        route = [c for c in self.peer_calls() if c.startswith("zed peerws --route")]
        self.assertEqual(len(route), 1, self.peer_calls())
        self.assertIn(pub.read_text().split()[1], route[0])


class TestDelegatedGlobalFlags(PeerFixture):
    """the dispatcher's global flags cross the hop with the command"""

    def _wk(self, *args):
        with stub_path({"ssh": _FAKE_SSH}) as binp:
            env = dict(os.environ)
            env.update(self.env({"PATH": f"{binp}:{os.environ['PATH']}"}))
            env.pop("WK_MARKER", None)
            env.pop("WK_FORCE", None)
            env.pop("WK_QUIET", None)
            return subprocess.run(
                [str(self.root / "wk"), *args],
                cwd=str(self.root), env=env, timeout=120,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def _driver(self, script):
        with stub_path({"ssh": _FAKE_SSH}) as binp:
            return bash(f'''
set -euo pipefail
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/target.sh"
load_target peerbox
{script}
''', env=self.env({"PATH": f"{binp}:{os.environ['PATH']}"}), cwd=str(self.root))

    def test_force_reaches_the_peers_wk(self):
        """`wk <cmd> <ws> --force` is forced over there too: the barrier it
        crosses is raised on the machine that runs the command"""
        cp = self._wk("logs", "peerws", "--force")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertTrue(any("force=1" in c for c in self.peer_calls()),
                        self.peer_calls())

    def test_force_travels_as_environment_not_as_an_argument(self):
        """an older `wk` over there ignores a variable it does not know and
        dies on a flag it does not"""
        self._wk("logs", "peerws", "--force")
        self.assertFalse(any("--force" in c for c in self.peer_calls()),
                         self.peer_calls())

    def test_quiet_reaches_the_peers_wk(self):
        """--quiet is the far side's narration, not this side's"""
        cp = self._wk("logs", "peerws", "--quiet")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertTrue(any("quiet=1" in c for c in self.peer_calls()),
                        self.peer_calls())

    def test_nothing_is_forced_when_nothing_asked(self):
        """the prefix is empty without the flag -- no command is forced by
        merely being delegated"""
        cp = self._wk("logs", "peerws")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertFalse(any("force=1" in c for c in self.peer_calls()),
                         self.peer_calls())

    def test_a_pty_carries_the_same_environment(self):
        """t_wk_tty differs from t_wk in the transport and nothing else:
        `wk ai claude <ws>` is interactive, so the tty path is the one a person
        meets when they type --force"""
        cp = self._driver('WK_FORCE=1 t_wk plain peerws\n'
                          'WK_FORCE=1 t_wk_tty tty peerws')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        calls = self.peer_calls()
        self.assertTrue(any(c.startswith("plain peerws") and "force=1" in c for c in calls), calls)
        self.assertTrue(any(c.startswith("tty peerws") and "force=1" in c for c in calls), calls)

    def test_one_implementation_builds_the_forwarded_environment(self):
        """every hop asks lib/common.sh for it, so a flag added to one is
        not missing from the other (CLAUDE.md, "one implementation per rule")"""
        for path in (REPO / "lib" / "target.sh", REPO / "targets" / "remote.sh"):
            self.assertIn("wk_forwarded_env", path.read_text(), path)
        self.assertIn("vm_wk_cmd", (REPO / "lib" / "wk" / "dispatch.py").read_text(),
                      "the dispatcher's VM hop builds its command elsewhere than vm_wk_cmd")
        # The tracked tree, not a directory walk: an agent's git worktree
        # under .claude/worktrees is a second copy of every file.
        offenders = [str(f) for f in repo_files()
                     if f.parts[len(REPO.parts)] != "tests"
                     and re.search(r"WK_(FORCE|QUIET|YES|DEBUG):\+",
                                   f.read_text(errors="replace"))]
        self.assertEqual(offenders, [str(REPO / "lib" / "common.sh")], offenders)


if __name__ == "__main__":
    unittest.main()
