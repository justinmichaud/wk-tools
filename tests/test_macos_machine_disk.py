"""The podman machine's disk is a declared size, not whatever it was made with
(host/macos/machine.sh).

Every container workspace lives on that disk, so a machine created when the
figure was smaller has to be grown rather than left -- a setting only applied
at `podman machine init` is one a rebuild silently loses. podman grows a disk
and refuses to shrink one, so the smaller case is reported instead.

The reconcile block is lifted out of the stage and driven against a `podman`
stub that records its argv, the tests/test_linux_machine_dry_run.py idiom: no
machine is created, started or stopped.

Run: python3 -m unittest tests.test_macos_machine_disk -v
"""
import re
import subprocess
import unittest

from tests.support import REPO, WkTest, stub_path

STAGE = REPO / "host" / "macos" / "machine.sh"

PODMAN_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$WK_TEST_PODMAN_LOG"
case "$*" in
    *"--format {{.Resources.CPUs}}"*)     echo "$WK_TEST_CPUS" ;;
    *"--format {{.Resources.Memory}}"*)   echo "$WK_TEST_MEM" ;;
    *"--format {{.Resources.DiskSize}}"*) echo "$WK_TEST_DISK" ;;
    *"--format {{.State}}"*)              echo stopped ;;
    *"df -Pk /var"*)
        # Bigger once the grow has been asked for, which is what `changed` reports on.
        if grep -q growpart "$WK_TEST_PODMAN_LOG"; then
            printf 'Filesystem 1024-blocks Used Available Capacity Mounted\\n/dev/vda4 524288000 0 0 0%% /var\\n'
        else
            printf 'Filesystem 1024-blocks Used Available Capacity Mounted\\n/dev/vda4 209715200 0 0 0%% /var\\n'
        fi ;;
esac
exit 0
"""


def reconcile_block():
    """From `_cur_cpus=` to the end of the `if` that sets the resources."""
    text = STAGE.read_text()
    start = text.index("_cur_cpus=$(podman machine inspect")
    end = text.index("\nfi\n", text.index("podman machine set", start)) + 4
    return text[start:end]


class TestTheDiskIsGrownToTheDeclaredSize(WkTest):
    def _run(self, cur_disk, want_disk="500", cpus="9", mem="20480", dry=False):
        block = reconcile_block()
        log = self.tmp / "podman.log"
        log.write_text("")
        with stub_path({"podman": PODMAN_STUB}) as binp:
            cp = subprocess.run(
                ["bash", "-c",
                 f'. "{REPO}/lib/common.sh"\n'
                 f'WK_MACHINE=wk; _cores={cpus}; _mem={mem}; _disk={want_disk}\n'
                 + ("WK_DRY_RUN=1\n" if dry else "")
                 + 'WK_RESERVE_CORES=1; WK_RESERVE_MB=1024\n'
                 # The stage continues past this block; its last line here is a
                 # conditional start, whose false is not a failure.
                 + block + "\ntrue\n"],
                capture_output=True, text=True, timeout=60,
                # WK_DEBUG, because a no-op reports through `unchanged`, which
                # is debug-level: ./setup narrates what it changed, not what it did not.
                env={"PATH": f"{binp}:/usr/bin:/bin", "HOME": str(self.tmp),
                     "WK_DEBUG": "1",
                     "WK_TEST_PODMAN_LOG": str(log), "WK_TEST_CPUS": cpus,
                     "WK_TEST_MEM": mem, "WK_TEST_DISK": cur_disk})
        return cp, log.read_text()

    def test_a_smaller_disk_is_grown(self):
        cp, sent = self._run(cur_disk="200")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertRegex(sent, r"machine set wk .*--disk-size 500",
                         f"the disk was not grown: {sent!r}")

    def test_a_disk_already_the_right_size_is_left_alone(self):
        cp, sent = self._run(cur_disk="500")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("machine set", sent, f"an unchanged machine was set: {sent!r}")
        self.assertIn("500", cp.stdout + cp.stderr, "the size it kept is not reported")

    def test_a_bigger_disk_is_reported_not_shrunk(self):
        """podman refuses to shrink one, and nothing here silently degrades."""
        cp, sent = self._run(cur_disk="800")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("--disk-size", sent, "it tried to shrink a disk")
        out = cp.stdout + cp.stderr
        self.assertIn("800", out)
        self.assertIn("only grows", out, "the refusal does not say why")

    def test_growing_stops_the_machine_first_and_starts_it_after(self):
        cp, sent = self._run(cur_disk="200")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        order = [l for l in sent.splitlines() if re.search(r"machine (stop|set|start)", l)]
        self.assertTrue(order, sent)
        self.assertIn("set", order[0] if "set" in order[0] else "".join(order))


    def test_the_guest_filesystem_is_grown_to_the_disk(self):
        """podman resizes the image and nothing inside it, so a 500 GiB disk
        with a 200 GiB filesystem on it is the declared size being a lie."""
        cp, sent = self._run(cur_disk="200")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("growpart", sent, "the partition was never grown")
        self.assertIn("xfs_growfs", sent, "the filesystem was never grown")
        self.assertRegex(cp.stdout + cp.stderr, r"filesystem grew to \d+ GiB")

    def test_the_filesystem_is_checked_even_when_the_disk_is_unchanged(self):
        """A disk grown by an earlier run whose filesystem did not follow is
        put right by the next one, rather than waiting for another resize."""
        _, sent = self._run(cur_disk="500")
        self.assertNotIn("machine set", sent)
        self.assertIn("growpart", sent, "a machine at its size is never checked")

    def test_a_dry_run_changes_nothing(self):
        cp, sent = self._run(cur_disk="200", dry=True)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for word in ("machine set", "machine stop", "growpart"):
            self.assertNotIn(word, sent, f"a dry run ran '{word}'")
        self.assertIn("dry run", cp.stdout + cp.stderr)


class TestTheDeclaredSizeFitsThreeLanes(unittest.TestCase):
    def test_the_default_holds_the_fleet_this_repo_builds(self):
        """A yocto lane measured 84 GB of build tree beside a shared sstate and
        download cache; rpi3, rpi4 and rpi5 at once is about 450 GB."""
        m = re.search(r'^_disk="\$\{WK_DISK_GB:-(\d+)\}"', STAGE.read_text(), re.M)
        self.assertIsNotNone(m, "host/macos/machine.sh declares no disk size")
        self.assertGreaterEqual(int(m.group(1)), 450,
                                "the podman machine is too small for three yocto lanes")


if __name__ == "__main__":
    unittest.main()
