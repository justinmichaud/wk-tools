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
import os
import re
import subprocess
import unittest

from tests.support import REPO, repo_files, WkTest, bash, stub_path

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

# The peer's own `wk`. It answers the two questions the driver asks --
# `wk ls --json` for what it holds, `wk zed <ws> --route` for how to reach
# one -- and records every invocation, so a test can prove a command was
# handed over rather than run here.
_PEER_WK = """#!/bin/sh
printf '%s\\n' "$* ${{WK_ZED_PUBKEY:+key=$WK_ZED_PUBKEY}}${{WK_FORCE:+force=1 }}${{WK_QUIET:+quiet=1 }}" >> "{log}"
case "$1 $2" in
"ls --json")
    printf '%s\\n' '{listing}'
    exit 0 ;;
esac
case "$1 $3" in
"zed --route")
    printf 'user=dev\\nsrc=/src/WebKit\\nproxy=/opt/wk-tools/container/ssh-transport.sh %s\\n' "$2"
    exit 0 ;;
esac
exit 0
"""

_LISTING = ('{"workspaces": [{"name": "peerws", "target": "container", '
            '"state": "running", "base": "-", "arch": "native", "changes": "1M"}]}')


class PeerFixture(WkTest):
    """A WK_ROOT whose registry (WK_TARGET_REGISTRY, lib/target.sh) holds one
    peer and nothing else, so the walk cannot reach the real fleet, plus a
    $HOME of its own: `wk zed` writes an ssh alias, and no test may write
    into the person's real ~/.ssh."""

    def setUp(self):
        super().setUp()
        self.root = self.tmp / "wk-root"
        (self.root / "targets" / "hosts").mkdir(parents=True)
        for entry in REPO.iterdir():
            if entry.name in ("targets", ".git", "__pycache__"):
                continue
            (self.root / entry.name).symlink_to(entry)
        for sh in (REPO / "targets").glob("*.sh"):
            (self.root / "targets" / sh.name).symlink_to(sh)

        self.tools = self.tmp / "peer-tools"
        self.tools.mkdir()
        self.calls = self.tmp / "peer-calls"
        peer_wk = self.tools / "wk"
        peer_wk.write_text(_PEER_WK.format(log=self.calls, listing=_LISTING))
        peer_wk.chmod(0o755)

        (self.root / "targets" / "hosts" / "peerbox.conf").write_text(
            "WK_TARGET_KIND=remote\n"
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
            "WK_TARGET_REGISTRY": str(self.root / "targets" / "hosts"),
            "HOME": str(self.home),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "WK_SSH_TIMEOUT": "5",
        }
        e.update(with_ssh)
        return e

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
        """ws_exists/ws_target find a workspace only the peer's own `wk` knows"""
        with stub_path({"ssh": _FAKE_SSH}) as binp:
            cp = self._bash('''
set -euo pipefail
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/target.sh"
ws_exists peerws   || { echo "ws_exists missed peerws"; exit 1; }
t=$(ws_target peerws)
[ "$t" = peerbox ] || { echo "ws_target said '$t'"; exit 1; }
! ws_exists ghost  || { echo "ws_exists found a ghost"; exit 1; }
''', binp)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

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
echo "delegates=$(t_delegates && echo yes || echo no)"
''', binp)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("info=present", cp.stdout)
        self.assertIn("ghost=absent", cp.stdout)
        self.assertIn("list=peerws,", cp.stdout)
        self.assertIn("delegates=yes", cp.stdout)


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

    def test_making_and_destroying_stays_with_the_owner(self):
        """a lifecycle command is not handed over: a peer owns its own workspaces"""
        cp = self._wk("rm", "peerws", extra_env={"WK_YES": "1"})
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("workstation", cp.stdout)
        self.assertFalse(any(c.startswith("rm ") for c in self.peer_calls()),
                         self.peer_calls())

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
        for path in (REPO / "wk", REPO / "targets" / "remote.sh"):
            self.assertIn("wk_forwarded_env", path.read_text(), path)
        # The tracked tree, not a directory walk: an agent's git worktree
        # under .claude/worktrees is a second copy of every file.
        offenders = [str(f) for f in repo_files()
                     if f.parts[len(REPO.parts)] != "tests"
                     and re.search(r"WK_(FORCE|QUIET|YES|DEBUG):\+",
                                   f.read_text(errors="replace"))]
        self.assertEqual(offenders, [str(REPO / "lib" / "common.sh")], offenders)


if __name__ == "__main__":
    unittest.main()
