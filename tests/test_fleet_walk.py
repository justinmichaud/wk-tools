"""Fleet walk: `wk status` reaches a remote target over ssh (targets/remote.sh's
`_rsh_q`/`_remote_probe_try`), and this drives that for real against a faked
machine -- WK_TARGET=remote, a made-up WK_REMOTE_HOST, and a stub `ssh` on
PATH that either answers (a normal shell, standing in for a live machine) or
refuses (ssh's own exit 255, standing in for one that never answers) -- so
`cmd/status` genuinely shells out to the stub rather than to a mocked driver
function.

Scoped to the *named* form, `wk status <name> --text`: a bare `wk status`
also walks this host's real bench-device fleet (boot/machines/*.conf, real
ssh and `tailscale status --json`, cmd/status's `report_fleet_devices`) with
no env-based way to skip it, and driving that for real from this test hung
past two minutes in this environment (measured, then killed) -- see the
skipped test below for the seam that is missing. The named form
(cmd/status's `collect()`) skips that walk entirely and answers for exactly
the one target asked about, which is enough to exercise the real ssh
shell-out and the unreachable-by-name reporting faithfully and fast.

Run: python3 -m unittest tests.test_fleet_walk -v
"""
import os
import unittest

from tests.support import WkTest, rand_suffix, run, stub_path


_ANSWERING_SSH = '''#!/bin/sh
# Stand in for a live machine: run the remote command locally, the way a
# real ssh to a reachable host would.
for last; do :; done
exec bash -c "$last"
'''

_REFUSING_SSH = '''#!/bin/sh
# ssh's own exit code for "could not connect".
exit 255
'''


class TestFleetWalkNamedFormRendersAFakedMachine(WkTest):
    def test_reachable_machine_renders_its_block(self):
        with stub_path({"ssh": _ANSWERING_SSH}) as binp:
            name = f"demo-{rand_suffix()}"
            env = {
                "WK_TARGET": "remote",
                "WK_REMOTE_HOST": "fake-reachable-machine",
                "PATH": f"{binp}:{self._real_path()}",
            }
            cp = run("status", name, "--text", env=env, timeout=30)
            self.assertEqual(cp.returncode, 0, cp.stdout)
            self.assertIn("remote", cp.stdout, cp.stdout)
            self.assertIn(name, cp.stdout, cp.stdout)
            self.assertNotIn("unreachable", cp.stdout, cp.stdout)

    def test_non_answering_machine_is_marked_unreachable_by_name(self):
        with stub_path({"ssh": _REFUSING_SSH}) as binp:
            name = f"demo-{rand_suffix()}"
            env = {
                "WK_TARGET": "remote",
                "WK_REMOTE_HOST": "fake-down-machine",
                "PATH": f"{binp}:{self._real_path()}",
                "WK_SSH_TIMEOUT": "2",
            }
            cp = run("status", name, "--text", env=env, timeout=30)
            # cmd/status's own documented contract (cmd/status header): exit
            # 4 is "a machine that will not answer".
            self.assertEqual(cp.returncode, 4, cp.stdout)
            self.assertIn("unreachable", cp.stdout, cp.stdout)
            self.assertIn(
                "remote", cp.stdout,
                f"the unreachable machine should be named, not just reported blind: {cp.stdout}",
            )

    @staticmethod
    def _real_path():
        return os.environ.get("PATH", "/usr/bin:/bin")


class TestFleetWalkBareFormMultiMachine(unittest.TestCase):
    @unittest.skip(
        "needs seam: a bare `wk status --text` (no workspace name) also "
        "walks this host's real bench-device fleet unconditionally -- "
        "cmd/status's collect() calls report_fleet_devices, which reads "
        "boot/machines/*.conf (real files, not registerable from a test "
        "without editing the repo) and shells out to real `ssh`/`tailscale "
        "status --json` for rpi3/rpi4/rpi5/mbp/benchvm. There is no "
        "WK_TARGET-shaped override for that walk the way there is for "
        "workspace targets, so a bare `wk status` from this test reaches "
        "real hardware over the real network with no way to fake or skip "
        "it -- measured hanging past 120s in this environment rather than "
        "finishing with the expected per-board ssh timeout. A faithful "
        "test of 'wk status --text renders every faked machine's block in "
        "one run' needs either an env override for boot/machines' conf "
        "directory (the same shape target_registry_dir() would need for "
        "more than one faked *target* machine too -- see "
        "test_resolution_ambiguous_name_refuses_naming_both below) or a "
        "documented way to run report_fleet_devices against a fake list."
    )
    def test_bare_status_renders_every_faked_machines_block(self):
        pass


class TestResolution(unittest.TestCase):
    @unittest.skip(
        "needs seam: lib/target.sh's ws_target (the only name resolver in "
        "this tree, README/CLAUDE.md 'no registry') walks target_all() and "
        "returns the *first* target whose store has the name -- it has no "
        "ambiguity check at all, so a name that happens to exist on two "
        "targets is silently resolved to whichever target_all() enumerates "
        "first, never refused. Confirmed by reading: no caller of ws_target "
        "compares against a second hit. Seam wanted: either ws_target "
        "collects every match and refuses by naming all of them, or this is "
        "not actually a property of the current design and the handoff line "
        "should be dropped rather than tested."
    )
    def test_a_name_on_two_targets_refuses_naming_both(self):
        pass

    @unittest.skip(
        "needs seam: ws_target's resolution loop (lib/target.sh) never "
        "calls t_info at all -- it only stats this host's own local record "
        "of each target (a directory under that target's store, or, before "
        "the store exists, wk new's own status file) -- so an unreachable "
        "*target machine* cannot affect resolution one way or the other; "
        "the loop has nothing that could report it. Only ws_state (called "
        "*after* a target is already chosen) asks t_info and can answer "
        "'unreachable'. Seam wanted: either name what 'a target that cannot "
        "be probed during resolution' refers to if it is not ws_target (a "
        "different resolver this review missed), or drop the claim."
    )
    def test_an_unreachable_target_is_reported_by_name_during_resolution(self):
        pass


if __name__ == "__main__":
    unittest.main()
