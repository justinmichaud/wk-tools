"""A machine that exists to be measured, made quiet: bench/mac-quiet-hosts.sh's software-update denial block,
bench/mac-quiet-desktop.sh and its table bench/quiet/macos.tsv, the daemons paused and what is judged of them."""
import contextlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest

from tests.fakes import Fake, FakeClock
from tests.support import REPO, WkTest, bash, func_body, scratch_dir, stub_path

sys.path.insert(0, str(REPO / "lib"))
from wk import reach, screen  # noqa: E402
from wk.machine import Result, lib_argv  # noqa: E402
from wk.quiet import Quiesce  # noqa: E402


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


class ApplyHostsBlockTest(WkTest):
    def setUp(self):
        super().setUp()
        self.hosts = self.tmp / "hosts"

    def assert_one_block(self, text):
        self.assertEqual((1, 1), (text.count(BEGIN), text.count(END)), text)
        for host in EXPECTED_HOSTS:
            self.assertEqual(1, text.count(f"0.0.0.0 {host}"), text)

    def test_fresh_file_gets_one_block_with_every_host(self):
        cp = apply_block(self.hosts)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assert_one_block(self.hosts.read_text())
        self.assertEqual(0, is_present(self.hosts).returncode)

    def test_stale_block_with_different_list_is_replaced(self):
        self.hosts.write_text(f"{BEGIN}\n0.0.0.0 example.com\n{END}\n")
        self.assertNotEqual(0, is_present(self.hosts).returncode)
        cp = apply_block(self.hosts)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertNotIn("example.com", self.hosts.read_text())
        self.assert_one_block(self.hosts.read_text())

    def test_other_lines_survive_byte_for_byte_and_twice_is_once(self):
        preamble = "127.0.0.1\tlocalhost\n255.255.255.255\tbroadcasthost\n::1             localhost\n# a hand-written comment\n"
        self.hosts.write_text(preamble)
        self.assertNotEqual(0, is_present(self.hosts).returncode)
        for _ in range(2):
            cp = apply_block(self.hosts)
            self.assertEqual(cp.returncode, 0, cp.stderr)
            text = self.hosts.read_text()
            self.assertTrue(text.startswith(preamble), text)
            self.assert_one_block(text)

    def test_dry_run_prints_and_changes_nothing(self):
        cp = apply_block(self.hosts, dry="1")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertFalse(self.hosts.exists(), "dry run must not create the file")
        for host in EXPECTED_HOSTS:
            self.assertIn(host, cp.stderr, cp.stderr)
        self.hosts.write_text("127.0.0.1 localhost\n")
        apply_block(self.hosts, dry="1")
        self.assertEqual(self.hosts.read_text(), "127.0.0.1 localhost\n")

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
            self.assertEqual(self.hosts.read_text(), original)
        finally:
            self.hosts.chmod(0o644)


class NoSecondWriterTest(WkTest):
    # A shell redirect or `tee` aimed at /etc/hosts, wherever it appears.
    WRITE_PATTERN = re.compile(r'(>{1,2}\s*"?\$?\{?\w*\}?/etc/hosts)|(\btee\b[^|;\n]*\/etc\/hosts)')

    def test_the_denial_can_be_lifted_and_put_back(self):
        hosts = self.tmp / "hosts"
        hosts.write_text("127.0.0.1 localhost\n")
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
    def test_the_sweep_tries_this_account_then_the_bench_one_and_says_which_answered(self):
        via = Fake("here")
        via.answer(["id", "-un"], out="me\n")
        via.react(["ssh"], lambda a, f: Result(0, "host=benchbox\n") if "bench@10.0.0.9" in a else Result(255))
        self.assertEqual(reach.Survey(reach.Reach(via, {}, peers=[])).identify("10.0.0.9", "")["account"], "bench")
        self.assertEqual([e[1][-2] for e in via.effects if e[1][0] == "ssh"], ["me@10.0.0.9", "bench@10.0.0.9"])


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

    def test_only_a_process_on_the_list_that_can_be_stopped_and_runs_again_is_recorded(self):
        unstoppable = bash(". %r\nwk_quiet_desktop_unstoppable\n" % str(REPO / "bench" / "mac-quiet-desktop.sh")).stdout.split()
        self.assertTrue(unstoppable)
        for ps, want in (("T   /System/Library/x/NotificationCenter\nT   usernoted\n", []),
                         ("S   /System/Library/CoreServices/NotificationCenter\nS   /usr/sbin/usernoted\n",
                          ["NotificationCenter", "usernoted"]),
                         ("".join("S   %s\n" % p for p in unstoppable), []),
                         ("S   MiniBrowser\nS   bash\n", [])):
            with self.subTest(ps=ps):
                self.assertEqual(want, self._restarted(ps))

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


QUIET = REPO / "bench" / "mac-quiet-desktop.sh"
TABLE = REPO / "bench" / "quiet" / "macos.tsv"
DESKTOP = REPO / "vm" / "desktop.sh"
FIRSTBOOT = REPO / "bench" / "mac-bench-firstboot.sh"
VOLUME = REPO / "lib" / "wk" / "sysimage" / "macvolume.py"


# Every call lands in one log, so a test reads what was asked for in order.
STUB = '#!/bin/sh\nprintf \'%s %s\\n\' "$(basename "$0")" "$*" >> "$WK_TEST_CALLS"\nexit 0\n'


# `id -u` decides whether a root-only function runs at all.
ID_ROOT = '#!/bin/sh\n[ "$1" = -u ] && { echo 0; exit 0; }\necho root\n'


# The fields each kind of row carries after its kind, the last one free text.
FIELDS = {"rows": 6, "agents": 4, "daemons": 3, "power": 4, "expected": 3}
KIND = {"rows": "setting", "agents": "agent", "daemons": "daemon", "power": "power", "expected": "expected"}


def _rows(name):
    return [line.split("\t")[1:] for line in TABLE.read_text().splitlines()
            if line.split("\t")[0] == KIND[name]]


def _probe_only_keys():
    """The readings the probe prints that come from no table."""
    body = QUIET.read_text()
    probe = body[body.index("wk_quiet_desktop_probe()"):]
    return set(re.findall(r"printf '([a-z_]+)=", probe))


def findings(func, probe, fix="the remedy"):
    """Every line `func` prints for that probe, each split on tabs."""
    cp = bash(f'''. {str(QUIET)!r}
probe=$(cat <<'P'
{probe}
P
)
{func} "$probe" {fix!r}
''')
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return [tuple(l.split("\t")) for l in cp.stdout.splitlines()]


class TestTheTables(unittest.TestCase):
    def test_every_row_is_complete_of_a_known_kind_and_named_once(self):
        for name, n in FIELDS.items():
            for r in _rows(name):
                with self.subTest(kind=name, row=r):
                    self.assertEqual(n, len(r), r)
        for r in _rows("rows"):
            self.assertIn(r[3], ("bool", "int", "string"))
        kinds = set(KIND.values()) | {"unsettable", "unstoppable"}
        for line in TABLE.read_text().splitlines():
            if line and not line.startswith("#"):
                self.assertIn(line.split("\t")[0], kinds, line)
        names = [r[0] for name in FIELDS for r in _rows(name)] + sorted(_probe_only_keys())
        self.assertEqual(len(names), len(set(names)), [n for n in names if names.count(n) > 1])

    def test_the_signalled_list_holds_every_agent_and_daemon_and_no_expected_process(self):
        rows = bash('. %s\nwk_quiet_desktop_stopped\n' % QUIET).stdout.splitlines()
        watched = {l.split()[1] for l in rows if len(l.split()) > 1}
        for row, at in [(r, 2) for r in _rows("agents")] + [(r, 1) for r in _rows("daemons")]:
            self.assertIn(row[at], watched, row)
        for row in _rows("expected"):
            self.assertNotIn(row[1], watched, row)
        for proc in bash('. %s\nwk_quiet_desktop_unstoppable\n' % QUIET).stdout.split():
            self.assertIn(proc, watched, "a name nobody looks up exempts nothing")


    def test_nothing_keeps_its_own_copy_of_a_setting(self):
        for f in (DESKTOP, FIRSTBOOT, VOLUME, REPO / "vm" / "desktop-probe.sh",
                  REPO / "cmd" / "bench", REPO / "lib" / "wk" / "bench" / "mac.py"):
            text = f.read_text()
            with self.subTest(file=f.name):
                for _name, domain, key, _t, _v, _why in _rows("rows"):
                    self.assertNotIn(f"{domain.lstrip('@')} {key}", text, f"{f.name} writes {key} itself")
                self.assertNotIn("mdutil", text, f"{f.name} turns Spotlight off itself")
                for _name, key, _value, _shown in _rows("power"):
                    self.assertNotIn(f"pmset -a {key}", text, f"{f.name} sets {key} itself")


class TestWhatMustKeepRunning(unittest.TestCase):
    def _judge(self, probe):
        return [f for f in findings("wk_quiet_daemons_findings", probe) if len(f) > 1]

    def _probe(self, state):
        rows = ["%s=%s" % (r[0], state) for r in _rows("expected")]
        rows += ["%s=stopped" % r[0] for r in _rows("agents")]
        rows += ["%s=stopped" % r[0] for r in _rows("daemons")]
        return "\n".join(rows)

    def test_running_is_the_ok_state(self):
        states = {f[0] for f in self._judge(self._probe("running"))}
        self.assertEqual({"ok"}, states)

    def test_stopped_is_a_fault_and_names_no_wk_command_as_the_cause(self):
        """Nothing in this tree stops it, so the remedy is to find what did."""
        found = [f for f in self._judge(self._probe("stopped")) if f[0] == "wrong"]
        self.assertEqual(len(_rows("expected")), len(found), found)
        for f in found:
            self.assertIn("STOPPED", f[1])
            self.assertIn("find what did", f[2])

    def test_absent_is_a_note_and_not_a_refusal(self):
        states = {f[0] for f in self._judge(self._probe("absent"))
                  if "tailscaled" in f[1]}
        self.assertEqual({"note"}, states)


class TestApplyingIt(WkTest):
    def _dscl_stub(self):
        home = self.tmp / "home"
        home.mkdir(exist_ok=True)
        return home, 'printf "NFSHomeDirectory: %s\\n" %s\n' % (home, home)

    def _run(self, script, path_extra=("defaults", "launchctl", "mdutil",
                                       "tmutil", "pmset", "sudo", "killall",
                                       "pgrep")):
        calls = self.tmp / "calls"
        calls.write_text("")
        stubs = {n: STUB for n in path_extra}
        stubs["dscl"] = self._dscl_stub()[1]
        with stub_path(stubs) as binp:
            cp = bash(f'. {str(QUIET)!r}\n{script}\n',
                      env={"PATH": f"{binp}:/usr/bin:/bin",
                           "WK_TEST_CALLS": str(calls)})
        return cp, calls.read_text()

    def test_sourcing_it_changes_nothing(self):
        _, calls = self._run("true")
        self.assertEqual("", calls)

    def test_every_row_is_written_and_finder_and_the_dock_restarted_but_nothing_signalled(self):
        cp, calls = self._run("wk_quiet_desktop_user")
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        for name, domain, key, type_, value, _why in _rows("rows"):
            with self.subTest(setting=name):
                if domain.startswith("@"):
                    self.assertRegex(calls, rf"defaults -currentHost write {domain[1:]} {key}")
                self.assertIn(f"write {domain.lstrip('@')} {key} -{type_} {value}", calls)
        self.assertTrue([r for r in _rows("rows") if r[1].startswith("@")], "no per-hardware-UUID row left in the table")
        self.assertRegex(calls, r"killall -u \S+ Finder Dock")
        self.assertNotIn("bootout", calls)
        self.assertNotRegex(calls, r"launchctl disable")

    def test_a_row_already_right_is_not_written_again(self):
        store = self.tmp / "defaults"
        store.mkdir()
        fake = "\n".join([
            '_wk_qd_defaults() {',
            '  shift; local host=""',
            '  case "$1" in -currentHost) host="ch."; shift ;; esac',
            '  local op="$1" domain="$2" key="$3"; shift 3',
            '  local f="$WK_TEST_DEFAULTS/$host$domain.$key"',
            '  if [ "$op" = read ]; then [ -f "$f" ] && cat "$f" || return 1; return 0; fi',
            '  local value="$2"',
            '  case "$1$2" in -booltrue) value=1 ;; -boolfalse) value=0 ;; esac',
            '  printf %s "$value" > "$f"',
            '  printf "defaults write %s %s\\n" "$domain" "$key" >> "$WK_TEST_CALLS"',
            '}',
        ])
        calls = self.tmp / "calls"
        calls.write_text("")
        env = {"PATH": "%s:/usr/bin:/bin" % self.tmp, "WK_TEST_CALLS": str(calls),
               "WK_TEST_DEFAULTS": str(store)}
        with stub_path(dict({n: STUB for n in ("launchctl", "killall", "sudo")},
                            dscl=self._dscl_stub()[1])) as binp:
            env["PATH"] = "%s:/usr/bin:/bin" % binp
            first = bash(". %r\n%s\nwk_quiet_desktop_user\n" % (str(QUIET), fake), env=env)
            self.assertEqual(0, first.returncode, first.stdout + first.stderr)
            wrote = calls.read_text()
            calls.write_text("")
            second = bash(". %r\n%s\nwk_quiet_desktop_user\n" % (str(QUIET), fake), env=env)
            self.assertEqual(0, second.returncode, second.stdout + second.stderr)
        again = calls.read_text()
        self.assertIn("defaults write", wrote)
        self.assertIn("killall", wrote)
        self.assertNotIn("defaults write", again)
        self.assertNotIn("killall", again)

    def test_another_account_is_written_as_that_account_and_one_that_does_not_exist_is_refused(self):
        cp, calls = self._run("wk_quiet_desktop_user nosuchuser; echo rc=$?")
        self.assertIn("sudo -u nosuchuser defaults", calls)
        self.assertIn("rc=1", cp.stdout)
        self.assertIn("no such account", cp.stdout + cp.stderr)

    def test_the_system_half_refuses_without_root(self):
        cp, calls = self._run("wk_quiet_desktop_system; echo rc=$?")
        self.assertIn("rc=1", cp.stdout)
        self.assertIn("needs root", cp.stdout + cp.stderr)
        self.assertEqual("", calls)

    def test_every_power_key_is_applied_on_its_own(self):
        calls = self.tmp / "calls"
        calls.write_text("")
        with stub_path({n: STUB for n in ("defaults", "mdutil", "tmutil", "pmset")}
                       | {"id": ID_ROOT}) as binp:
            bash(f'. {str(QUIET)!r}\nwk_quiet_desktop_system\n',
                 env={"PATH": f"{binp}:/usr/bin:/bin", "WK_TEST_CALLS": str(calls)})
        text = calls.read_text()
        for _name, key, value, _shown in _rows("power"):
            with self.subTest(key=key):
                self.assertIn(f"pmset -a {key} {value}\n", text)


class TestPausingTheDaemons(WkTest):
    PS = "\n".join("%d /usr/libexec/%s" % (100 + i, r[1])
                   for i, r in enumerate(_rows("daemons")))

    def _signal(self, verb, uid=0, listing=None):
        calls = self.tmp / "calls"
        calls.write_text("")
        listing = self.PS if listing is None else listing
        script = "\n".join([
            ". %r" % str(QUIET),
            'id() { [ "$1" = -u ] && { echo %d; return 0; }; echo tester; }' % uid,
            'ps() { printf %s >> "$WK_TEST_CALLS" "ps $*"; cat "$WK_TEST_PS"; }'
            .replace("printf %s", "printf '%s\\n'"),
            'kill() { printf \'kill %s\\n\' "$*" >> "$WK_TEST_CALLS"; }',
            "%s; echo rc=$?" % verb,
        ])
        ps_file = self.tmp / "ps.txt"
        ps_file.write_text(listing + "\n")
        cp = bash(script, env={"PATH": os.environ["PATH"],
                               "WK_TEST_CALLS": str(calls),
                               "WK_TEST_PS": str(ps_file)})
        return cp, calls.read_text()

    def test_pausing_needs_root_and_says_so(self):
        cp, calls = self._signal("wk_quiet_daemons_pause", uid=501)
        self.assertIn("rc=1", cp.stdout)
        self.assertIn("needs root", cp.stdout + cp.stderr)
        self.assertEqual("", calls)

    def _stoppable(self):
        skip = set(bash(f'. {str(QUIET)!r}\nwk_quiet_desktop_unstoppable\n').stdout.split())
        return [(i, row) for i, row in enumerate(_rows("daemons")) if row[1] not in skip]

    def test_every_daemon_running_but_what_sip_refuses_is_stopped_from_one_listing(self):
        cp, calls = self._signal("wk_quiet_daemons_pause")
        self.assertIn("rc=0", cp.stdout)
        stoppable = self._stoppable()
        for i, row in enumerate(_rows("daemons")):
            with self.subTest(daemon=row[1]):
                (self.assertIn if (i, row) in stoppable else self.assertNotIn)("kill -STOP %d\n" % (100 + i), calls)
        self.assertLess(len(stoppable), len(_rows("daemons")), "SIP refuses none of the table's daemons")
        self.assertEqual(1, calls.count("ps "), calls)
        _, calls = self._signal("wk_quiet_daemons_resume")
        for i, row in stoppable:
            with self.subTest(daemon=row[1]):
                self.assertIn("kill -CONT %d\n" % (100 + i), calls)

    def test_a_daemon_that_is_not_running_is_not_signalled(self):
        cp, calls = self._signal("wk_quiet_daemons_pause", listing="1 /usr/sbin/nothing")
        self.assertIn("rc=0", cp.stdout)
        self.assertNotIn("kill ", calls)

    def test_a_name_that_is_a_prefix_of_another_is_not_confused(self):
        _, calls = self._signal("wk_quiet_daemons_pause",
                                listing="7 /usr/libexec/backupd-helper")
        self.assertIn("kill -STOP 7\n", calls)
        self.assertEqual(1, calls.count("kill "), calls)


class TestTheProbe(WkTest):
    def _probe(self):
        cp = bash(f'. {str(QUIET)!r}\nwk_quiet_desktop_probe\n')
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        return dict(l.split("=", 1) for l in cp.stdout.splitlines() if "=" in l)

    def test_it_answers_for_every_row_in_every_table(self):
        answered = set(self._probe())
        want = {r[0] for name in FIELDS for r in _rows(name)}
        self.assertEqual(set(), want - answered, want - answered)

    def test_it_writes_nothing(self):
        calls = self.tmp / "calls"
        calls.write_text("")
        with stub_path({n: STUB for n in ("defaults", "launchctl", "mdutil",
                                          "pmset", "killall")}) as binp:
            bash(f'. {str(QUIET)!r}\nwk_quiet_desktop_probe\n',
                 env={"PATH": f"{binp}:/usr/bin:/bin", "WK_TEST_CALLS": str(calls)})
        text = calls.read_text()
        self.assertNotIn("write", text)
        self.assertNotIn("bootout", text)
        self.assertNotIn("killall", text)
        self.assertNotRegex(text, r"launchctl disable")
        self.assertNotIn("pmset -a", text)

    @unittest.skipUnless(os.uname().sysname == "Darwin", "asks a real macOS")
    def test_a_daemon_reading_is_one_of_three_words(self):
        got = self._probe()
        for row in _rows("daemons"):
            with self.subTest(daemon=row[1]):
                self.assertIn(got[row[0]], ("running", "stopped", "absent"))


class TestDoNotDisturb(WkTest):
    def _dnd(self, script):
        """HOME a scratch directory: Apple's python3, first on this PATH, writes its bytecode cache under it."""
        return bash('. %r\n%s\n' % (str(QUIET), script), env={"PATH": "/usr/bin:/bin", "HOME": str(self.tmp)})

    def test_each_assertions_file_reads_as_its_state(self):
        for doc, want in ((None, "?nofile"), ("not json at all", "?malformed"),
                          (json.dumps({"data": [{"storeAssertionRecords": [
                              {"assertionEndDateTimestamp": 1.0, "assertionDetails": {}}]}]}), "off"),
                          (json.dumps({"data": [], "header": {}}), "off")):
            with self.subTest(want=want, doc=doc), scratch_dir() as home:
                if doc is not None:
                    path = home / "Library/DoNotDisturb/DB/Assertions.json"
                    path.parent.mkdir(parents=True)
                    path.write_text(doc)
                cp = self._dnd('wk_quiet_dnd_state %r' % str(home))
                self.assertEqual(want, cp.stdout.strip(), cp.stdout + cp.stderr)

    def test_a_file_it_may_not_read_says_so(self):
        db = self.tmp / "Library" / "DoNotDisturb" / "DB"
        db.mkdir(parents=True)
        (db / "Assertions.json").write_text("{}")
        (db / "Assertions.json").chmod(0)
        cp = self._dnd('wk_quiet_dnd_state %r' % str(self.tmp))
        if os.geteuid() == 0:
            self.skipTest("root reads a mode-0 file, so there is no denial to meet")
        self.assertEqual("?denied", cp.stdout.strip(), cp.stdout + cp.stderr)

    def test_a_denied_read_is_left_out_of_the_probe_and_one_that_worked_is_judged(self):
        for state, want in (("?denied", None), ("off", "notifications_dnd=off")):
            with self.subTest(state=state):
                cp = bash(". %s\n_wk_qd_home() { printf /nonexistent; }\nwk_quiet_dnd_state() { printf %s; }\n"
                          "probe() {%s}\nprobe tester\n" % (QUIET, state, func_body(QUIET.read_text(), "wk_quiet_desktop_probe")),
                          env={"PATH": "/usr/bin:/bin"})
                if want:
                    self.assertIn(want, cp.stdout, cp.stdout + cp.stderr)
                else:
                    self.assertNotIn("notifications_dnd", cp.stdout, "a row nothing can read refuses every leg")

    def test_an_unreadable_row_is_unknown_and_not_wrong(self):
        """What the install's own preflight then does with it."""
        cp = bash(". %s\n" % QUIET
                  + "probe=$(printf 'analytics=0\\nspotlight=disabled\\n')\n"
                  + "wk_quiet_desktop_findings \"$probe\" 'the remedy' "
                  + "| awk -F'\\t' '$2 ~ /Do Not Disturb/ { print $1 }'\n")
        self.assertEqual("note", cp.stdout.strip(), cp.stdout + cp.stderr)

    def test_turning_it_on_reads_back_on(self):
        cp = self._dnd('wk_quiet_dnd_on %r' % str(self.tmp))
        self.assertEqual("on", cp.stdout.strip(), cp.stdout + cp.stderr)
        doc = json.loads((self.tmp / "Library/DoNotDisturb/DB/Assertions.json").read_text())
        record = doc["data"][0]["storeAssertionRecords"][0]
        self.assertEqual("com.apple.donotdisturb.mode.default",
                         record["assertionDetails"]["assertionDetailsModeIdentifier"])
        self.assertNotIn("assertionEndDateTimestamp", record)


class TestTheFindings(WkTest):
    def _judge(self, func, probe):
        return [f for f in findings(func, probe) if len(f) == 3]

    def _settled(self):
        out = []
        for name, domain, key, type_, value, _why in _rows("rows"):
            want = {"true": "1", "false": "0"}.get(value, value) if type_ == "bool" else value
            out.append(f"{name}={want}")
        out += [f"{r[0]}=running" for r in _rows("expected")]
        out += [f"{r[0]}=stopped" for r in _rows("agents")]
        out += [f"{r[0]}=stopped" for r in _rows("daemons")]
        out += [f"{r[0]}={r[2]}" for r in _rows("power")]
        out += ["spotlight=Indexing disabled.", "analytics=0",
                "notifications_dnd=on",
                "power_source=AC Power", "cpu_speed_limit=100"]
        return "\n".join(out)

    def test_a_settled_machine_is_all_ok(self):
        for func in ("wk_quiet_desktop_findings", "wk_quiet_cpu_findings",
                     "wk_quiet_daemons_findings"):
            with self.subTest(findings=func):
                states = {f[0] for f in self._judge(func, self._settled())}
                self.assertEqual({"ok"}, states, self._judge(func, self._settled()))

    def test_each_reading_off_the_settled_state_is_judged_by_its_own_row(self):
        for func, old, new, word, kind, says, fix in (
                ("wk_quiet_desktop_findings", "appnap=1\n", "appnap=0\n", "appnap", "wrong", (), "the remedy"),
                ("wk_quiet_desktop_findings", "appnap=1\n", "", "appnap", "note", (), ""),
                ("wk_quiet_desktop_findings", "appnap=1\n", "appnap=?\n", "appnap", "wrong", (), ""),
                ("wk_quiet_daemons_findings", "notifications=stopped", "notifications=running", "banner", "wrong", (), ""),
                ("wk_quiet_daemons_findings", "softwareupdate=stopped", "softwareupdate=running", "softwareupdated", "wrong",
                 ("softwareupdated is running", "scans for updates"), ""),
                ("wk_quiet_daemons_findings", "softwareupdate=stopped", "softwareupdate=absent", "softwareupdated", "ok", (), ""),
                ("wk_quiet_cpu_findings", "power_source=AC Power", "power_source=Battery Power", "battery", "wrong", (), "plug it in"),
                ("wk_quiet_cpu_findings", "cpu_speed_limit=100", "cpu_speed_limit=70", "70%", "wrong", (), ""),
                ("wk_quiet_cpu_findings", "power_highpowermode=1", "power_highpowermode=", "highpowermode", "note", (), "")):
            with self.subTest(new=new or "no " + old.strip()):
                probe = self._settled() + "\n"
                self.assertIn(old, probe)
                judged = self._judge(func, probe.replace(old, new))
                found = [f for f in judged if word in f[1]]
                self.assertEqual([kind], [f[0] for f in found], judged)
                self.assertEqual([], [f for f in judged if f[0] == "wrong" and f not in found])
                for w in says:
                    self.assertIn(w, found[0][1])
                self.assertIn(fix, found[0][2])

    def test_a_row_macos_will_not_set_says_what_it_costs(self):
        unsettable = bash(f'. {str(QUIET)!r}\nwk_quiet_desktop_unsettable\n').stdout.split()
        self.assertTrue(unsettable)
        for name in unsettable:
            with self.subTest(row=name):
                probe = re.sub(rf"^{name}=.*$", f"{name}=?", self._settled(), flags=re.M)
                f = [x for x in self._judge("wk_quiet_desktop_findings", probe)
                     if name in x[1] or x[1].startswith("not so")]
                states = {x[0] for x in f}
                self.assertNotIn("wrong", states, f)
                self.assertIn("note", states, f)

    def test_no_finding_wraps_over_two_lines(self):
        for func in ("wk_quiet_desktop_findings", "wk_quiet_cpu_findings", "wk_quiet_daemons_findings"):
            for line in findings(func, self._settled().replace("appnap=1", "appnap=?")):
                with self.subTest(findings=func, line=line):
                    self.assertEqual(3, len(line), line)


class TestTheTableTravelsWithTheFile(WkTest):
    PROBE = "set -u\nwk_quiet_desktop_power | head -1\nwk_quiet_desktop_stopped | wc -l\n"

    def test_the_script_it_sends_carries_the_table(self):
        script = bash('. %r\nwk_quiet_desktop_script\n' % str(QUIET)).stdout
        cp = bash(script + self.PROBE, cwd=str(self.tmp))
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        lines = cp.stdout.split()
        self.assertEqual("power_displaysleep", lines[0], cp.stdout)
        self.assertEqual(len(_rows("agents")) + len(_rows("daemons")), int(lines[-1]))

    def test_a_copy_sent_without_it_refuses_loudly_and_sources_cleanly(self):
        cp = bash("set -e\n" + QUIET.read_text() + "\necho SOURCED\nwk_quiet_desktop_power || echo rc=$?\n",
                  cwd=str(self.tmp))
        self.assertIn("SOURCED", cp.stdout, cp.stderr)
        self.assertIn("rc=1", cp.stdout, cp.stderr)
        self.assertIn("no quiet table here", cp.stderr)
