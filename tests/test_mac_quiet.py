"""Tests for bench/mac-quiet-hosts.sh -- the /etc/hosts software-update
denial block shared by lib/wk/sysimage/macvolume.py's provision and
mac-bench-firstboot.sh, its list read from bench/quiet/macos-hosts.txt."""

import contextlib
import io
import re
import shlex
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from tests.support import bash
from wk import screen
from wk.clock import FakeClock
from wk.machine import Fake, Result, lib_argv
from wk.quiet import Quiesce

REPO = Path(__file__).resolve().parent.parent
QUIET_HOSTS = REPO / "bench" / "mac-quiet-hosts.sh"

# The markers the script itself writes into /etc/hosts, read from it: a second
# copy here would let the two drift and the test would still pass.
_MARKERS = dict(re.findall(r'^(WK_BENCH_HOSTS_(?:BEGIN|END))="([^"]*)"$',
                           QUIET_HOSTS.read_text(), re.M))
BEGIN = _MARKERS["WK_BENCH_HOSTS_BEGIN"]
END = _MARKERS["WK_BENCH_HOSTS_END"]

EXPECTED_HOSTS = [l for l in (REPO / "bench" / "quiet" / "macos-hosts.txt").read_text().splitlines()
                  if l and not l.startswith("#")]


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

    def test_the_list_is_the_data_file_and_no_list_is_no_block(self):
        """A copy of the script with no list beside it must not write, or accept, an empty block."""
        lone = self.tmp / "mac-quiet-hosts.sh"
        lone.write_text(QUIET_HOSTS.read_text())
        self.hosts.write_text(f"{BEGIN}\n{END}\n")
        cp = subprocess.run(["bash", "-c", f". {shlex.quote(str(lone))}; wk_bench_hosts_present {self.hosts} || echo absent; "
                             f"wk_bench_hosts_apply {self.hosts} || echo refused"], capture_output=True, text=True)
        self.assertIn("absent", cp.stdout, cp.stderr)
        self.assertIn("refused", cp.stdout, cp.stderr)
        self.assertIn("no quiet/macos-hosts.txt", cp.stderr)
        self.assertEqual(f"{BEGIN}\n{END}\n", self.hosts.read_text())

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

    # A shell redirect or `tee` aimed at /etc/hosts, wherever it appears.
    WRITE_PATTERN = re.compile(r'(>{1,2}\s*"?\$?\{?\w*\}?/etc/hosts)|(\btee\b[^|;\n]*\/etc\/hosts)')

    def test_the_denial_can_be_lifted_and_put_back(self):
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

    def test_no_second_hosts_writer_in_bench(self):
        offenders = []
        for path in sorted((REPO / "bench").glob("*.sh")):
            if path == QUIET_HOSTS:
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                if self.WRITE_PATTERN.search(line):
                    offenders.append(f"{path.name}:{lineno}: {line.strip()}")
        self.assertEqual(offenders, [], "a second /etc/hosts writer exists:\n" + "\n".join(offenders))

class TestTheDaemonsEnvironment(unittest.TestCase):

    SOURCED = ("mac-pyobjc.sh", "mac-quiet-desktop.sh", "mac-quiet-hosts.sh")

    def test_each_one_survives_an_empty_environment_under_set_u(self):
        for name in self.SOURCED:
            cp = subprocess.run(
                ["/usr/bin/env", "-i", "/bin/bash", "-c",
                 f'set -euo pipefail; . "{REPO}/bench/{name}"; echo SOURCED-OK'],
                capture_output=True, text=True, timeout=30)
            self.assertIn("SOURCED-OK", cp.stdout,
                          f"bench/{name} dies when sourced by a daemon: "
                          f"{cp.stdout}{cp.stderr}")

class TestWhatIsStoppedIsWhatIsJudged(unittest.TestCase):

    TABLE = REPO / "bench" / "mac-quiet-desktop.sh"

    def test_that_list_holds_both_halves_of_the_machine(self):
        cp = bash(f'. "{self.TABLE}"\nwk_quiet_desktop_stopped\n')
        listed = {l.split()[1] for l in cp.stdout.splitlines() if l.split()}
        for proc in ("NotificationCenter", "usernoted", "chronod"):
            self.assertIn(proc, listed, "an agent is missing from the list")
        for proc in ("softwareupdated", "backupd", "ReportCrash"):
            self.assertIn(proc, listed, "a daemon is missing from the list")

    def test_the_findings_renderer_still_changes_nothing(self):
        m = Fake("mac")
        m.answer([], out="")
        m._set_file("/etc/wk-image", "id=perf-macos-tolken\n")
        with tempfile.TemporaryDirectory() as store, contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(io.StringIO()):
            Quiesce(REPO, m, FakeClock(), {"WK_STORE": store, "HOME": store}, macos=True).status()
        ran = [e[1] for e in m.effects if e[0] in ("run", "run_tty")]
        self.assertTrue(any("wk_quiet_daemons_findings" in " ".join(a) for a in ran), ran)
        self.assertEqual([e for e in m.effects if e[0] not in ("run", "run_tty")], [])
        for argv in ran:
            self.assertNotIn("wk_quiet_daemons_pause", " ".join(argv))
            if argv[:1] == ("sudo",):
                self.assertIn("defaults read", " ".join(argv), argv)

class TestASweepCanNameABenchInstall(unittest.TestCase):

    def test_the_sweep_tries_the_bench_account(self):
        """lib/wk/reach.py's Survey.identify, driven: this account first, then the bench install's."""
        from wk import reach
        from wk.machine import Fake, Result
        via = Fake("here")
        via.answer(["id", "-un"], out="me\n")
        via.answer(["ssh"], rc=255)
        reach.Survey(reach.Reach(via, {}, peers=[])).identify("10.0.0.9", "")
        dests = [e[1][-2] for e in via.effects if e[1][0] == "ssh"]
        self.assertEqual(dests, ["me@10.0.0.9", "bench@10.0.0.9"])

    def test_it_says_which_account_answered(self):
        from wk import reach
        from wk.machine import Fake, Result
        via = Fake("here")
        via.answer(["id", "-un"], out="me\n")
        via.answer(["ssh"], rc=255)
        via.react(["ssh"], lambda a, f: Result(0, "host=benchbox\n") if "bench@10.0.0.9" in a else Result(255))
        self.assertEqual(reach.Survey(reach.Reach(via, {}, peers=[])).identify("10.0.0.9", "")["account"], "bench")

class TestTheWatchSeesAPausedAgentComeBack(unittest.TestCase):

    def _mac(self, ps_output):
        """A Fake Mac whose must-not-run table is the real one and whose `ps` is the test's, so every state is reachable."""
        m = Fake("mac")
        for fn in ("wk_quiet_desktop_stopped", "wk_quiet_desktop_unstoppable"):
            m.react(lib_argv(str(REPO), screen.DESKTOP, fn),
                    lambda argv, fake: Result(0, subprocess.run(argv, capture_output=True, text=True).stdout))
        m.answer(["ps"], out=ps_output)
        m.answer(lib_argv(str(REPO), screen.WINDOWS, "wk_window_probe"), out="windows=?\n")
        return m

    def _restarted(self, ps_output):
        return screen.restarted(self._mac(ps_output), REPO)

    def test_a_machine_where_they_are_all_stopped_records_nothing(self):
        self.assertEqual([], self._restarted(
            "T   /System/Library/x/NotificationCenter\n"
            "T   usernoted\n"))

    def test_a_banner_daemon_running_again_is_recorded(self):
        self.assertEqual(["NotificationCenter", "usernoted"], self._restarted(
            "S   /System/Library/CoreServices/NotificationCenter\n"
            "S   /usr/sbin/usernoted\n"))

    def test_a_process_the_kernel_will_not_stop_is_not_a_finding(self):
        unstoppable = bash(". %r\nwk_quiet_desktop_unstoppable\n"
                           % str(REPO / "bench" / "mac-quiet-desktop.sh")).stdout.split()
        self.assertTrue(unstoppable)
        self.assertEqual([], self._restarted(
            "".join("S   %s\n" % p for p in unstoppable)))

    def test_a_process_that_is_not_on_the_list_is_not_a_finding(self):
        self.assertEqual([], self._restarted("S   MiniBrowser\nS   bash\n"))

    def test_the_watch_records_it_where_the_leg_reads_it(self):
        m, sampled = self._mac(""), threading.Event()

        def ps(argv, fake):
            sampled.set()
            return Result(0, "S   /usr/sbin/usernoted\n")
        m.react(["ps"], ps)
        m.answer(["uname", "-s"], out="Darwin\n")
        watch = screen.Watch(m, REPO, FakeClock(), {"WK_SCREEN_WATCH_SECONDS": "0.01"})
        watch.start()
        self.assertTrue(sampled.wait(30))
        self.assertIn("%s\trunning again: usernoted" % FakeClock().iso(), watch.stop())

    def test_off_a_mac_the_watch_asks_nothing(self):
        m = Fake("linux")
        m.answer(["uname", "-s"], out="Linux\n")
        watch = screen.Watch(m, REPO, FakeClock(), {"WK_SCREEN_WATCH_SECONDS": "0.01"})
        watch.start()
        self.assertEqual([], watch.stop())
        self.assertEqual([["uname", "-s"]], [list(e[1]) for e in m.effects if e[0] == "run"])

    def _watched(self, m):
        m.answer(["uname", "-s"], out="Darwin\n")
        watch = screen.Watch(m, REPO, FakeClock(), {"WK_SCREEN_WATCH_SECONDS": "0.01"})
        watch.start()
        return [l.split("\t", 1)[1] for l in watch.stop()]

    def test_a_screen_that_could_not_be_asked_is_a_finding_once(self):
        """A missing probe or no compiler is no evidence the screen was clear."""
        self.assertEqual([screen.UNASKED], self._watched(self._mac("")))

    def test_a_watch_that_fails_is_a_finding(self):
        m = self._mac("")
        m.react(["ps"], lambda argv, fake: (_ for _ in ()).throw(OSError("ps went away")))
        seen = self._watched(m)
        self.assertIn("the screen watch stopped: OSError: ps went away", seen)

    def test_a_watch_that_hangs_does_not_hold_the_run(self):
        m, stuck = self._mac(""), threading.Event()
        m.react(["ps"], lambda argv, fake: stuck.wait(30) and Result(0, ""))
        m.answer(["uname", "-s"], out="Darwin\n")
        watch = screen.Watch(m, REPO, FakeClock(), {"WK_SCREEN_WATCH_SECONDS": "0.01"})
        watch.start()
        seen = [l.split("\t", 1)[1] for l in watch.stop(wait=0.2)]
        stuck.set()
        self.assertIn("the screen watch did not answer within 0.2s of the run ending", seen)

    def test_every_reading_is_asked_under_a_ceiling(self):
        m = self._mac("")
        asked = []
        orig = m.run
        m.run = lambda argv, input=None, timeout=None: asked.append(timeout) or orig(argv, input=input, timeout=timeout)
        self._watched(m)
        self.assertTrue(asked)
        self.assertNotIn(None, asked)

    def test_it_costs_one_process_per_sample_and_not_forty(self):
        """It samples beside the thing being measured: one `ps` for every name on the table, no `pgrep` each."""
        m = self._mac("")
        screen.restarted(m, REPO)
        ran = [e[1] for e in m.effects if e[0] == "run"]
        self.assertEqual(1, sum(1 for a in ran if a[:1] == ("ps",)))
        self.assertFalse([a for a in ran if a[:1] == ("pgrep",)])
        self.assertEqual(3, len(ran))


if __name__ == "__main__":
    unittest.main()
