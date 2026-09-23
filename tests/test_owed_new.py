"""`wk new` over a workspace with no `base-id` remakes it, owed
(docs/PLAN.md): "catches: 'already exists' answered about a
half-made thing". The decision lives in `ws_state` (lib/target.sh): a
target that needs a base snapshot (`t_needs_base`) but whose workspace
directory has no `base-id` file reports `creating`, not `present` -- so
`wk new` resumes/remakes it instead of refusing "already exists". Driven
directly against the decision function with `t_info`/`t_needs_base`/
`wk_ws_dir` stubbed: no podman, no VM, no real driver. The creation driver's
order -- the SDK refreshed under its lock, before the store lock and the
create -- is tests/test_wk_workspace.py's, read off a fake machine.

Run: python3 -m unittest tests.test_owed_new -v
"""
import unittest

from tests.support import (WkTest, rand_suffix, requires_container_target,
                          run, scratch_dir)


class TestWorkspaceWithNoBaseIdIsStillCreating(WkTest):
    def _state(self, env_present, needs_base, has_base_id):
        with scratch_dir() as ws:
            if has_base_id:
                (ws / "base-id").write_text("some-snapshot-id\n")
            cp = self.bash(f'''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/target.sh"
t_info() {{ echo {"present" if env_present else "absent"}; }}
t_needs_base() {{ return {0 if needs_base else 1}; }}
wk_ws_dir() {{ echo "{ws}"; }}
ws_state somews
''')
        return cp

    def test_a_workspace_needing_a_base_with_none_recorded_is_creating(self):
        """The environment exists (a container/vm was made) but the base-id
        pin was never written -- an interrupted `wk new`, not a finished
        one. 'already exists' would be wrong here: `wk new` has to resume
        it, not refuse it."""
        cp = self._state(env_present=True, needs_base=True, has_base_id=False)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "creating", cp.stdout + cp.stderr)

    def test_the_same_workspace_once_base_id_is_recorded_is_present(self):
        """Contrast: once base-id exists, the same environment reports
        present -- so 'creating' above is not ws_state being broken outright."""
        cp = self._state(env_present=True, needs_base=True, has_base_id=True)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "present", cp.stdout + cp.stderr)

    def test_a_target_with_no_base_at_all_never_needs_the_file(self):
        """A target that does not use base snapshots (t_needs_base false --
        e.g. a remote target, whose base is a repository, not a pinned
        snapshot) is present without one."""
        cp = self._state(env_present=True, needs_base=False, has_base_id=False)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "present", cp.stdout + cp.stderr)


class TestNewKillStopsTheCreation(unittest.TestCase):
    """A creation outlives its terminal, so its record names the command that
    stops it, `wk new <name> --kill` (the record itself is
    tests/test_wk_workspace.py's). What it leaves is half-made, which is `wk
    rm`'s to clear."""

    def test_the_help_block_says_what_it_does(self):
        cp = run("new", "-h")
        self.assertIn("--kill", cp.stdout)

    @requires_container_target()
    def test_it_takes_nothing_that_belongs_to_a_creation(self):
        cp = run("new", "kill-probe-%s" % rand_suffix(), "--kill", "--no-wait")
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("stops the creation already running", cp.stdout)

    @requires_container_target()
    def test_with_no_creation_running_it_says_so_and_ends_well(self):
        cp = run("new", "kill-probe-%s" % rand_suffix(), "--kill")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("no new is running", cp.stdout)

if __name__ == "__main__":
    unittest.main()
