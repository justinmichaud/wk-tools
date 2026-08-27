"""Tests for the WK_DEBUG per-stage timing and the mirror-unchanged skip
added to cmd/sync (defect: "why is wk sync so slow on rpi5").

cmd/sync executes a real sync top to bottom the moment it runs, so these
tests never source it for real work -- `bash -c '. cmd/sync functions'`
stops it right after the helper functions are defined (the guard at the top
of the file), which is exactly what lets a test load `stage_begin`,
`stage_end` and `snapshot_current` without touching the network, the store,
or a workspace.

Run: python3 -m unittest tests.test_sync -v
"""

import unittest

from tests.support import bash, run


class TestStageTiming(unittest.TestCase):
    """stage_begin/stage_end is the one place `date +%s` arithmetic happens
    in cmd/sync; every WK_DEBUG timing line -- mirror fetch, snapshot
    publish, checkout/reset/clean, each workspace's fetch -- goes through
    it, so testing the pair once covers the shape of all of them."""

    def test_emits_a_stage_line_under_wk_debug(self):
        cp = bash(
            ". cmd/sync functions\n"
            "stage_begin\n"
            "stage_end mystage\n",
            env={"WK_DEBUG": "1"},
        )
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertRegex(cp.stderr, r"stage mystage: \d+s")

    def test_silent_without_wk_debug(self):
        cp = bash(
            ". cmd/sync functions\n"
            "stage_begin\n"
            "stage_end mystage\n",
        )
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertNotIn("stage mystage", cp.stderr)

    def test_functions_seam_loads_without_running_a_real_sync(self):
        # 'functions' as $1 stops cmd/sync right after its helpers are
        # defined, before it touches the network, the store, or a
        # workspace -- exactly what makes the two tests above possible.
        cp = bash(
            ". cmd/sync functions\n"
            "type stage_begin stage_end snapshot_current >/dev/null "
            "&& echo helpers-ok\n",
        )
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("helpers-ok", cp.stdout)


class TestSyncHelpMentionsTiming(unittest.TestCase):
    def test_wk_sync_dash_h_mentions_wk_debug(self):
        cp = run("sync", "-h")
        self.assertIn("WK_DEBUG", cp.stdout)


class TestSnapshotCurrent(unittest.TestCase):
    """snapshot_current <recorded-sha> <mirror-sha> is the pure decision
    behind cmd/sync's fix: a fresh snapshot only matters if it would differ
    from what is already published, so the checkout/reset/clean that
    dominates a sync (measured on rpi5: ~30s against ~10s to fetch the
    mirror) is skippable whenever the previously published base's recorded
    sha already matches the mirror's. Driven on synthetic shas -- no store,
    no mirror, no network needed to exercise the logic itself."""

    def _current(self, recorded, mirror):
        cp = bash(
            ". cmd/sync functions\n"
            f'snapshot_current "{recorded}" "{mirror}"\n'
        )
        return cp.returncode == 0

    def test_matching_shas_are_current(self):
        sha = "d" * 40
        self.assertTrue(self._current(sha, sha))

    def test_differing_shas_are_not_current(self):
        self.assertFalse(self._current("a" * 40, "b" * 40))

    def test_no_recorded_sha_is_not_current(self):
        # An unpublished, or never-verified, snapshot has nothing recorded
        # -- never treated as already matching the mirror.
        self.assertFalse(self._current("", "a" * 40))

    def test_no_mirror_sha_is_not_current(self):
        self.assertFalse(self._current("a" * 40, ""))

    def test_both_empty_is_not_current(self):
        self.assertFalse(self._current("", ""))


class TestSyncScopeDecide(unittest.TestCase):
    """sync_scope_decide <in_workspace 0|1> <workspace-count> <has-tty 0|1>
    [<pick>] is the pure decision behind cmd/sync's "autodetect, then ask":
    bare / --machine / --all is parsed above it and --machine/--all never
    reach this function, but a bare `wk sync` with no workspace named does,
    and this is the whole of what it decides. Driven the same way as
    snapshot_current -- `. cmd/sync functions` loads it with no network, no
    store, no workspace and no terminal needed."""

    def _decide(self, in_ws, n, tty, pick=""):
        cp = bash(
            ". cmd/sync functions\n"
            f'sync_scope_decide "{in_ws}" "{n}" "{tty}" "{pick}"\n'
        )
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return cp.stdout.strip()

    def test_in_workspace_is_that_one_regardless_of_count_or_tty(self):
        # cmd/sync's own call site always passes in_workspace=0 -- WK_NAME
        # already filled in ONLY before this function is ever reached, so
        # this branch exists for a test to drive directly, not for the real
        # call site to take.
        self.assertEqual(self._decide(1, 5, 0), "here")

    def test_no_workspaces_syncs_the_machine(self):
        self.assertEqual(self._decide(0, 0, 0), "empty")
        self.assertEqual(self._decide(0, 0, 1), "empty")

    def test_exactly_one_workspace_is_unambiguous(self):
        self.assertEqual(self._decide(0, 1, 0), "one")
        self.assertEqual(self._decide(0, 1, 1), "one")

    def test_several_and_no_terminal_refuses(self):
        self.assertEqual(self._decide(0, 3, 0), "refuse")

    def test_several_and_a_terminal_asks_before_a_pick_is_given(self):
        self.assertEqual(self._decide(0, 3, 1), "ask")

    def test_picking_the_last_entry_means_all_of_them(self):
        # n=3: entries 1-3 are workspaces, entry 4 ("all of them ... --machine").
        self.assertEqual(self._decide(0, 3, 1, "4"), "all")

    def test_picking_a_workspace_number(self):
        self.assertEqual(self._decide(0, 3, 1, "2"), "pick 2")
        self.assertEqual(self._decide(0, 3, 1, "1"), "pick 1")
        self.assertEqual(self._decide(0, 3, 1, "3"), "pick 3")

    def test_picking_nothing_or_non_numeric_is_invalid(self):
        self.assertEqual(self._decide(0, 3, 1, ""), "ask")  # no pick at all
        self.assertEqual(self._decide(0, 3, 1, "x"), "invalid")
        self.assertEqual(self._decide(0, 3, 1, "2.5"), "invalid")

    def test_picking_a_number_outside_the_menu_is_out_of_range(self):
        self.assertEqual(self._decide(0, 3, 1, "0"), "out-of-range")
        self.assertEqual(self._decide(0, 3, 1, "5"), "out-of-range")


if __name__ == "__main__":
    unittest.main()
