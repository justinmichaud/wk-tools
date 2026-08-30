"""cmd/gc owed by docs/HANDOFF-test-runner.md:

  - honours a pre-set $WK_ROOT (its own comment: the container half pipes
    this file into `bash -s`, which sets $0 to "bash" -- a wrong root
    derived from $0 would source nothing and die immediately)
  - sources the rest of the tree (lib/image.sh, image/profiles.sh,
    boot/machines.sh, image/pmos.sh) optionally, through `_source_if_there`,
    not a hard `.` -- the container half's copy of the tree is only as new
    as the last `wk sync --tools container`

Driven as `bash -s <cmd/gc`, the same way the container half invokes it, with
WK_STORE/XDG_STATE_HOME/WK_TARGET_REGISTRY pointed at scratch and WK_MACHINE
named so nothing exists under it: this fleet-blind, so `cmd/gc`'s own
podman-VM branch never finds a running machine to forward into (the tests/
support.py rule -- no test here reaches a real podman machine or the fleet).

Run: python3 -m unittest tests.test_owed_gc -v
"""
import unittest

from tests.support import REPO, WkTest, bash, rand_suffix, scratch_dir

GC = REPO / "cmd" / "gc"


class TestGcHonoursAPresetWkRoot(WkTest):
    def test_bash_s_with_a_wrong_argv0_still_finds_its_root(self):
        """$0 is 'bash' under `bash -s`, exactly the case the file's own
        header comment names; only WK_ROOT (not $0) may be trusted to find
        lib/common.sh and the rest of the tree."""
        with scratch_dir() as store, scratch_dir() as state, scratch_dir() as reg:
            cp = self.bash(
                'exec bash -s -- < "$GC"',
                env={
                    "WK_ROOT": str(REPO),
                    "WK_TARGET": "vm",
                    "WK_VM_STORE": str(store),
                    "XDG_STATE_HOME": str(state),
                    "WK_TARGET_REGISTRY": str(reg),
                    "WK_MACHINE": f"wk-test-no-such-machine-{rand_suffix()}",
                    "GC": str(GC),
                },
            )
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        # Evidence it ran as the real script (sourced lib/common.sh etc.,
        # reached the podman-machine check) rather than dying on `dirname
        # bash` finding no lib/ to source at all.
        self.assertIn("podman machine is not running", out, out)


class TestGcSourcesTheTreeOptionally(WkTest):
    """Static: `_source_if_there` (defined in cmd/gc, guarding four sources)
    is what the file's own comment says makes this work on a container whose
    tree copy is stale -- a hard `.` of a file that does not exist yet would
    fail there. Checked in the source rather than by deleting one of the
    four files from a checkout, which would make every other test here
    (import-time, same tree) fail with it."""

    OPTIONAL = (
        "lib/image.sh",
        "image/profiles.sh",
        "boot/machines.sh",
        "image/pmos.sh",
    )

    def test_source_if_there_is_defined_and_checks_before_sourcing(self):
        text = GC.read_text()
        self.assertIn(
            '_source_if_there() { [ -f "$1" ] && . "$1"; return 0; }', text,
            "cmd/gc no longer defines _source_if_there the way its own comment describes",
        )

    def test_the_optional_files_go_through_it_not_a_hard_source(self):
        text = GC.read_text()
        for rel in self.OPTIONAL:
            with self.subTest(rel=rel):
                self.assertIn(f'_source_if_there "$WK_ROOT/{rel}"', text,
                               f"{rel} is not sourced through _source_if_there")
                # A hard `. "$WK_ROOT/<rel>"` anywhere would fail exactly
                # where the guarded one is meant to degrade.
                self.assertNotIn(f'. "$WK_ROOT/{rel}"', text)

    def test_the_load_bearing_libs_are_hard_sourced_not_optional(self):
        """lib/common.sh, lib/store.sh, lib/target.sh, lib/detach.sh are
        never stale on the container half in the way the optional four can
        be (they are the container image's own baseline), so a hard source
        is the right refusal if one goes missing."""
        text = GC.read_text()
        for rel in ("lib/common.sh", "lib/store.sh", "lib/target.sh", "lib/detach.sh"):
            with self.subTest(rel=rel):
                self.assertIn(f'. "$WK_ROOT/{rel}"', text, f"{rel} is not hard-sourced")


if __name__ == "__main__":
    unittest.main()
