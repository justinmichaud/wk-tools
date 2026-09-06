"""Tests for bench/mac-quiet-hosts.sh -- the /etc/hosts software-update
denial block shared by mac-bench-volume.sh's do_provision and
mac-bench-firstboot.sh.

Hardware-free: every test drives the shared shell functions against a
temp file passed as the hosts path, never the real /etc/hosts, and never
needs sudo (the functions themselves never call sudo -- that only happens
in do_provision's caller, which this suite does not run).
"""

import re
import shlex
import subprocess
import unittest
from pathlib import Path

from tests.support import func_body

REPO = Path(__file__).resolve().parent.parent
QUIET_HOSTS = REPO / "bench" / "mac-quiet-hosts.sh"
FIRSTBOOT = REPO / "bench" / "mac-bench-firstboot.sh"
VOLUME_SH = REPO / "bench" / "mac-bench-volume.sh"

# The markers the script itself writes into /etc/hosts, read from it: a second
# copy here would let the two drift and the test would still pass.
_MARKERS = dict(re.findall(r'^(WK_BENCH_HOSTS_(?:BEGIN|END))="([^"]*)"$',
                           QUIET_HOSTS.read_text(), re.M))
BEGIN = _MARKERS["WK_BENCH_HOSTS_BEGIN"]
END = _MARKERS["WK_BENCH_HOSTS_END"]

EXPECTED_HOSTS = [
    "swscan.apple.com",
    "swdist.apple.com",
    "swcdn.apple.com",
    "swdownload.apple.com",
    "mesu.apple.com",
    "gdmf.apple.com",
    "updates.cdn-apple.com",
    "updates-http.cdn-apple.com",
    "xp.apple.com",
]


def _run(function, *args):
    quoted = " ".join(shlex.quote(str(a)) for a in args)
    script = f". {shlex.quote(str(QUIET_HOSTS))}; {function} {quoted}"
    return subprocess.run(
        ["bash", "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )


def apply_block(path, dry=None):
    args = [path] if dry is None else [path, dry]
    return _run("wk_bench_hosts_apply", *args)


def is_present(path):
    return _run("wk_bench_hosts_present", path)


class SharedFileSanityTest(unittest.TestCase):
    def test_shared_file_exists_and_parses(self):
        self.assertTrue(QUIET_HOSTS.is_file(), QUIET_HOSTS)
        cp = subprocess.run(["bash", "-n", str(QUIET_HOSTS)], capture_output=True, text=True)
        self.assertEqual(cp.returncode, 0, cp.stderr)


class ApplyHostsBlockTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-quiet-hosts-"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)
        self.hosts = self.tmp / "hosts"

    def _block_count(self, text):
        return text.count(BEGIN)

    def test_fresh_file_gets_one_block_with_every_host(self):
        # The path does not exist yet -- the function must create it.
        cp = apply_block(self.hosts)
        self.assertEqual(cp.returncode, 0, cp.stderr)

        text = self.hosts.read_text()
        self.assertEqual(self._block_count(text), 1, text)
        self.assertEqual(text.count(END), 1, text)
        for host in EXPECTED_HOSTS:
            self.assertIn(f"0.0.0.0 {host}", text, text)

    def test_applying_twice_leaves_one_block(self):
        apply_block(self.hosts)
        cp = apply_block(self.hosts)
        self.assertEqual(cp.returncode, 0, cp.stderr)

        text = self.hosts.read_text()
        self.assertEqual(self._block_count(text), 1, text)
        for host in EXPECTED_HOSTS:
            self.assertEqual(text.count(host), 1, f"{host} appears more than once:\n{text}")

    def test_stale_block_with_different_list_is_replaced(self):
        stale = f"{BEGIN}\n0.0.0.0 example.com\n{END}\n"
        self.hosts.write_text(stale)

        cp = apply_block(self.hosts)
        self.assertEqual(cp.returncode, 0, cp.stderr)

        text = self.hosts.read_text()
        self.assertEqual(self._block_count(text), 1, text)
        self.assertNotIn("example.com", text, text)
        for host in EXPECTED_HOSTS:
            self.assertIn(f"0.0.0.0 {host}", text, text)

    def test_other_lines_survive_byte_for_byte(self):
        preamble = "127.0.0.1\tlocalhost\n255.255.255.255\tbroadcasthost\n::1             localhost\n# a hand-written comment\n"
        self.hosts.write_text(preamble)

        cp = apply_block(self.hosts)
        self.assertEqual(cp.returncode, 0, cp.stderr)

        text = self.hosts.read_text()
        self.assertTrue(text.startswith(preamble), text)
        self.assertEqual(self._block_count(text), 1, text)

        # Re-applying over a file that already carries the block plus the
        # preamble must not disturb the preamble either.
        cp = apply_block(self.hosts)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        text2 = self.hosts.read_text()
        self.assertTrue(text2.startswith(preamble), text2)
        self.assertEqual(self._block_count(text2), 1, text2)

    def test_present_true_after_apply(self):
        apply_block(self.hosts)
        cp = is_present(self.hosts)
        self.assertEqual(cp.returncode, 0, cp.stderr)

    def test_present_false_before_apply(self):
        self.hosts.write_text("127.0.0.1 localhost\n")
        cp = is_present(self.hosts)
        self.assertNotEqual(cp.returncode, 0)

    def test_present_false_for_stale_list(self):
        stale = f"{BEGIN}\n0.0.0.0 example.com\n{END}\n"
        self.hosts.write_text(stale)
        cp = is_present(self.hosts)
        self.assertNotEqual(cp.returncode, 0)

    def test_dry_run_prints_and_changes_nothing(self):
        self.assertFalse(self.hosts.exists())
        cp = apply_block(self.hosts, dry="1")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertFalse(self.hosts.exists(), "dry run must not create the file")
        for host in EXPECTED_HOSTS:
            self.assertIn(host, cp.stderr, cp.stderr)

    def test_dry_run_does_not_touch_existing_file(self):
        original = "127.0.0.1 localhost\n"
        self.hosts.write_text(original)
        cp = apply_block(self.hosts, dry="1")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(self.hosts.read_text(), original)

    def test_read_back_check_fails_when_write_does_not_land(self):
        original = "127.0.0.1 localhost\n"
        self.hosts.write_text(original)
        self.hosts.chmod(0o444)  # read-only: a plain `>` write must fail
        try:
            cp = apply_block(self.hosts)
            self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            # the file must be exactly what it was -- the write never landed
            self.assertEqual(self.hosts.read_text(), original)
        finally:
            self.hosts.chmod(0o644)


class NoSecondWriterTest(unittest.TestCase):
    """Static checks: both consumers call the one shared function, and
    nothing else in bench/ writes /etc/hosts on its own."""

    # A shell redirect or `tee` aimed at /etc/hosts, wherever it appears.
    WRITE_PATTERN = re.compile(r'(>{1,2}\s*"?\$?\{?\w*\}?/etc/hosts)|(\btee\b[^|;\n]*\/etc\/hosts)')

    def test_firstboot_invokes_shared_function(self):
        # Sources the installed copy of the shared file (do_build_pkg/
        # do_repair lay it down as wk-bench-quiet-hosts.sh next to this
        # script) and calls its function -- not a reimplementation.
        text = FIRSTBOOT.read_text()
        self.assertIn("quiet-hosts.sh", text, "firstboot does not source the shared file")
        self.assertIn("wk_bench_hosts_apply", text)

    def test_do_provision_invokes_shared_function(self):
        text = VOLUME_SH.read_text()
        self.assertIn("mac-quiet-hosts.sh", text, "do_provision does not source the shared file")
        self.assertIn("wk_bench_hosts_apply", text)
        # Both writers of a benchmark install -- the provisioning package and
        # the re-arm onto one already installed -- read one payload table, so
        # neither can be given a file the other is not.
        self.assertIn("wk-bench-quiet-hosts.sh", func_body(text, "bench_payload_files"))
        for writer in ("do_build_pkg", "do_repair"):
            self.assertIn("stage_payload", func_body(text, writer), writer)

    def test_the_denial_can_be_lifted_and_put_back(self):
        """A benchmark install has to fetch the Command Line Tools once, and
        softwareupdate cannot reach Apple through the denial."""
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".hosts", delete=False) as fh:
            fh.write("127.0.0.1 localhost\n")
            hosts = fh.name
        self.addCleanup(lambda: __import__("os").unlink(hosts))
        script = (f'. "{QUIET_HOSTS}"\n'
                  f'wk_bench_hosts_apply {hosts} >/dev/null && echo applied\n'
                  f'wk_bench_hosts_present {hosts} && echo present\n'
                  f'wk_bench_hosts_remove {hosts} && echo removed\n'
                  f'wk_bench_hosts_present {hosts} || echo gone\n'
                  f'wk_bench_hosts_remove {hosts} && echo idempotent\n'
                  f'cat {hosts}')
        cp = subprocess.run(["bash", "-euo", "pipefail", "-c", script],
                            capture_output=True, text=True)
        for word in ("applied", "present", "removed", "gone", "idempotent"):
            self.assertIn(word, cp.stdout, cp.stdout + cp.stderr)
        self.assertNotIn("0.0.0.0", cp.stdout.split("idempotent")[-1])
        self.assertIn("127.0.0.1 localhost", cp.stdout, "it kept what it did not write")

    def test_first_boot_takes_the_tools_before_it_denies_the_servers(self):
        """The order is the whole point: denied first, and the install has no
        python3 and can measure nothing."""
        text = FIRSTBOOT.read_text()
        self.assertIn("wk_bench_hosts_remove", text)
        self.assertLess(text.index("CommandLineTools"), text.index("wk_bench_hosts_apply"))
        self.assertLess(text.index("CommandLineTools"), text.index("wk_pyobjc_install"))

    def test_no_second_hosts_writer_in_bench(self):
        offenders = []
        for path in sorted((REPO / "bench").glob("*.sh")):
            if path == QUIET_HOSTS:
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                if self.WRITE_PATTERN.search(line):
                    offenders.append(f"{path.name}:{lineno}: {line.strip()}")
        self.assertEqual(offenders, [], "a second /etc/hosts writer exists:\n" + "\n".join(offenders))

    def test_shared_file_never_calls_sudo(self):
        # The function must work unprivileged against a plain temp file --
        # privilege is the caller's problem (do_provision's `sudo bash -c`).
        # Comments may still explain that in prose, so only real code lines
        # are checked.
        for lineno, line in enumerate(QUIET_HOSTS.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            self.assertNotRegex(code, r"\bsudo\b", f"mac-quiet-hosts.sh:{lineno} calls sudo: {line}")


if __name__ == "__main__":
    unittest.main()
