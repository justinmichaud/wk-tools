"""The podman machine's disk is a declared size, not whatever it was made with
(host/macos/machine.sh)."""
import subprocess
import unittest

from tests.support import REPO, WkTest, stub_path

STAGE = REPO / "host" / "macos" / "machine.sh"

PODMAN_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$WK_TEST_PODMAN_LOG"
case "$*" in
    "machine inspect wk")
        printf '[{"State":"stopped","Resources":{"CPUs":%s,"Memory":%s,"DiskSize":%s}}]\\n' \\
            "$WK_TEST_CPUS" "$WK_TEST_MEM" "$WK_TEST_DISK" ;;
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
    start = text.index("wk_eval wk.places podman-vm _cur_cpus=")
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
                 f'WK_MACHINE=wk; export WK_MACHINE; _cores={cpus}; _mem={mem}; _disk={want_disk}\n'
                 + ("WK_DRY_RUN=1\n" if dry else "")
                 + 'WK_RESERVE_CORES=1; WK_RESERVE_MB=1024\n'
                 + block + "\ntrue\n"],
                capture_output=True, text=True, timeout=60,
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

    def test_the_guest_filesystem_is_grown_to_the_disk(self):
        cp, sent = self._run(cur_disk="200")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("growpart", sent, "the partition was never grown")
        self.assertIn("xfs_growfs", sent, "the filesystem was never grown")
        self.assertRegex(cp.stdout + cp.stderr, r"filesystem grew to \d+ GiB")

    def test_the_filesystem_is_checked_even_when_the_disk_is_unchanged(self):
        _, sent = self._run(cur_disk="500")
        self.assertNotIn("machine set", sent)
        self.assertIn("growpart", sent, "a machine at its size is never checked")

    def test_a_dry_run_changes_nothing(self):
        cp, sent = self._run(cur_disk="200", dry=True)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for word in ("machine set", "machine stop", "growpart"):
            self.assertNotIn(word, sent, f"a dry run ran '{word}'")
        self.assertIn("dry run", cp.stdout + cp.stderr)


if __name__ == "__main__":
    unittest.main()
