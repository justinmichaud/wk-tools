"""cmd/selftest's `remote` section: a configured machine name resolves as a
target, and `wk status`/`wk ls` reach it. Each docstring is the
phrase of the behaviour it checks.

Every test that touches a real machine over ssh is gated on that specific
machine answering `ssh -o BatchMode=yes <name> true`
(tests.support.requires_machine) and never provisions, reboots or otherwise
mutates it -- these are read-only probes only.

Run: python3 -m unittest tests.test_remote -v
"""
import unittest

from tests.support import (REAL_REGISTRY, REPO, WkTest, bash, func_body,
                           requires_machine)


class TestUnregisteredWorkspaceResolves(WkTest):
    """a remote workspace with no registry entry here still resolves"""

    def test_unregistered_remote_workspace_resolves(self):
        """ws_exists/ws_target find a remote workspace the registry misses"""
        # A registry holding only the fake conf (WK_TARGET_REGISTRY,
        # lib/target.sh), so the walk cannot reach the real fleet.
        registry = self.tmp / "hosts"
        registry.mkdir()
        root = self.tmp / "root"
        (root / "ws" / "tws").mkdir(parents=True)
        (root / "ws" / "tws" / ".wk-ready").write_text("")
        store = self.tmp / "store"
        store.mkdir()
        # WK_REMOTE_LOCAL drives the remote driver without ssh; the store is
        # a different directory from the root, so only t_info -- not the
        # host-side directory test -- can find the workspace.
        (registry / "fakebox.conf").write_text(
            "WK_TARGET_KIND=remote\n"
            "WK_REMOTE_LOCAL=1\n"
            f"WK_REMOTE_ROOT={root}\n"
            f"WK_REMOTE_STORE={store}\n"
        )
        cp = bash('''
set -euo pipefail
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/target.sh"
ws_exists tws || { echo "ws_exists missed tws"; exit 1; }
t=$(ws_target tws)
[ "$t" = fakebox ] || { echo "ws_target said '$t'"; exit 1; }
! ws_exists not-a-workspace || { echo "ws_exists found a ghost"; exit 1; }
''', env={
            "WK_TARGET_REGISTRY": str(registry),
            "XDG_STATE_HOME": str(self.tmp / "state"),
        })
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)


class TestMachineAnswers(WkTest):
    """a fan-out (`wk push --all`, `wk sudo --all`) tells a machine that is
    down from one with no wk-tools, and lets no ssh error text through"""

    def _registry(self, conf):
        """A registry holding exactly one fake machine (WK_TARGET_REGISTRY,
        lib/target.sh), so nothing here can reach the real fleet."""
        registry = self.tmp / "hosts"
        registry.mkdir(exist_ok=True)
        (registry / "fakebox.conf").write_text(conf)
        return registry

    def _machine_answers(self, conf):
        return bash('''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/target.sh"
load_target fakebox
machine_answers fakebox && echo "rc=0" || echo "rc=$?"
''', env={"WK_TARGET_REGISTRY": str(self._registry(conf)),
            "XDG_STATE_HOME": str(self.tmp / "state"), "WK_SSH_TIMEOUT": "3"})

    def test_an_unreachable_machine_is_reported_as_unreachable(self):
        """an ssh destination nothing resolves is 'unreachable', not 'no wk-tools'"""
        # An .invalid name fails resolution at once, so this needs no route to
        # anything and cannot hang on a real machine's timeout.
        cp = self._machine_answers(
            "WK_TARGET_KIND=remote\nWK_REMOTE_HOST=wk-test-unreachable.invalid\n")
        out = cp.stdout + cp.stderr
        self.assertIn("rc=1", out, out)
        self.assertRegex(out, r"(?m)^fakebox\s+unreachable over ssh", out)
        self.assertNotIn("no wk-tools", out)
        self.assertNotIn("ssh:", out, "ssh's own error text leaked through")

    def test_the_machine_itself_is_not_a_far_side(self):
        """the machine this runs on has no far side to answer for it"""
        cp = self._machine_answers("WK_TARGET_KIND=remote\nWK_REMOTE_LOCAL=1\n")
        out = cp.stdout + cp.stderr
        self.assertIn("rc=1", out, out)
        self.assertRegex(out, r"(?m)^fakebox\s+not a machine of its own", out)

    def test_push_and_sudo_share_the_one_fan_out_line(self):
        """cmd/push and cmd/sudo ask machine_answers rather than deciding it themselves"""
        for cmd in ("push", "sudo"):
            with self.subTest(cmd=cmd):
                text = (REPO / "cmd" / cmd).read_text(errors="replace")
                self.assertIn("machine_answers", text)
                self.assertNotIn("no wk-tools there yet", text)


def _configured_remote_machines():
    """Every machine conf in targets/hosts whose kind resolves to `remote`
    -- pure logic, no ssh, mirrors target_kind()'s own resolution."""
    cp = bash(f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/target.sh"
d=$(target_registry_dir)
for f in "$d"/*.conf; do
    [ -f "$f" ] || continue
    n=$(basename "$f" .conf)
    echo "$n:$(target_kind "$n" 2>/dev/null || echo '?')"
done
''', env={"WK_TARGET_REGISTRY": str(REAL_REGISTRY)})
    out = {}
    for line in cp.stdout.splitlines():
        if ":" in line:
            n, k = line.split(":", 1)
            out[n] = k
    return out


class TestRemoteConfsResolve(WkTest):
    def test_remote_confs_resolve(self):
        """a machine name is a target"""
        machines = _configured_remote_machines()
        if not machines:
            self.skipTest(f"no machines configured in {REAL_REGISTRY}")
        bad = [n for n, k in machines.items() if k in ("", "?")]
        self.assertEqual(bad, [], f"configured but unresolvable: {bad}")


def _t_info_reaches(name):
    """Mirrors cmd/selftest's chk_remote_reachable: load the driver for
    `name` and ask t_info about a workspace that does not exist. t_info
    itself never fails (an unreachable machine is a *word* it prints, not a
    nonzero exit) -- what this actually catches is load_target or the probe
    dying outright, which is the same thing the shell version caught."""
    cp = bash(f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/lib/resources.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/lib/target.sh"
( load_target "{name}"; t_info selftest-nonexistent >/dev/null 2>&1 )
''', env={"WK_TARGET_REGISTRY": str(REAL_REGISTRY)}, timeout=30)
    return cp


class TestRemoteReachable(WkTest):
    """`wk status`/`wk ls` list what is on a configured machine"""

    @requires_machine("buildbox4")
    def test_buildbox4_reachable(self):
        """`wk status`/`wk ls` list what is on a configured machine"""
        cp = _t_info_reaches("buildbox4")
        self.assertEqual(cp.returncode, 0, f"buildbox4: {cp.stdout + cp.stderr}")

    @requires_machine("devbox-arm64-2")
    def test_devbox_arm64_2_reachable(self):
        """`wk status`/`wk ls` list what is on a configured machine"""
        cp = _t_info_reaches("devbox-arm64-2")
        self.assertEqual(cp.returncode, 0, f"devbox-arm64-2: {cp.stdout + cp.stderr}")

    @requires_machine("moose")
    def test_moose_reachable(self):
        """`wk status`/`wk ls` list what is on a configured machine"""
        cp = _t_info_reaches("moose")
        self.assertEqual(cp.returncode, 0, f"moose: {cp.stdout + cp.stderr}")


class TestTheMirrorOnTheBox(WkTest):
    """the mirror this driver keeps on a build box is made by the one snippet
    every other mirror in the fleet is made by (mirror_refresh_script)"""

    def setUp(self):
        super().setUp()
        # A build box driven without ssh (WK_REMOTE_LOCAL, targets/remote.sh).
        self.registry = self.tmp / "hosts"
        self.registry.mkdir()
        (self.registry / "fakebox.conf").write_text(
            "WK_TARGET_KIND=remote\n"
            "WK_REMOTE_LOCAL=1\n"
            f"WK_REMOTE_ROOT={self.tmp / 'wk'}\n"
            f"WK_REMOTE_STORE={self.tmp / 'store'}\n"
        )

    def _script(self):
        """The shell _remote_mirror_update sends, with the ssh wrapper
        replaced by a recorder: the driver is real, only the far side is not."""
        seen = self.tmp / "sent"
        cp = bash(f'''
set -euo pipefail
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/resources.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"
load_target fakebox >/dev/null 2>&1
_rsh_q() {{ printf '%s\\n' "$*" > {str(seen)!r}; }}
_remote_mirror_update "$(_remote_root)" >/dev/null 2>&1
''', env={"WK_TARGET_REGISTRY": str(self.registry),
          "XDG_STATE_HOME": str(self.tmp / "state")})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return seen.read_text()

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
        something else than wk_remotes says (lib/store.sh)."""
        text = (REPO / "targets" / "remote.sh").read_text()
        body = func_body(text, "_remote_mirror_update")
        self.assertNotIn("github.com", body, body)
        self.assertIn("mirror_refresh_script", body)

    def test_the_workspace_wiring_names_the_same_mirror(self):
        """t_wiring_args tells a checkout on the box where the local copy is;
        naming it twice is how the two drift apart."""
        body = func_body((REPO / "targets" / "remote.sh").read_text(), "t_wiring_args")
        self.assertIn("t_mirror_dir", body)
        self.assertNotIn("/mirror'", body)


if __name__ == "__main__":
    unittest.main()
