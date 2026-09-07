"""bench/mac-quiet-desktop.sh -- what a macOS machine that exists to be measured
is, and the one place it is written down.

A guest and a bench install are the same kind of machine for this purpose: a
window that gets looked at, nobody at the keyboard, and a number coming out at
the end. A widget that animates, a notification banner, a Setup Assistant pane,
a Spotlight scan and a clock the machine took down itself each cost the
measurement, and each used to be turned off in one of those two places and not
the other. So the settings are four tables, the appliers read the tables, the
probe reads the machine, and the findings judge one against the other -- and
this file measures the tables and every caller.

The `defaults`, `launchctl`, `mdutil`, `pmset`, `pgrep` and `killall` calls run
against stubs on PATH: what is under test is which settings are asked for and
how, not what macOS does with them -- that is measured on a real guest by
`wk vm check`.

Run: python3 -m unittest tests.test_mac_quiet_desktop -v
"""
import os
import re
import unittest

from tests.support import REPO, WkTest, bash, func_body, stub_path

QUIET = REPO / "bench" / "mac-quiet-desktop.sh"
DESKTOP = REPO / "vm" / "desktop.sh"
FIRSTBOOT = REPO / "bench" / "mac-bench-firstboot.sh"
VOLUME = REPO / "bench" / "mac-bench-volume.sh"
VM_DRIVER = REPO / "targets" / "vm.sh"
QUIESCE = REPO / "cmd" / "quiesce"

# Every call lands in one log, so a test reads what was asked for in order.
STUB = '#!/bin/sh\nprintf \'%s %s\\n\' "$(basename "$0")" "$*" >> "$WK_TEST_CALLS"\nexit 0\n'
# `id -u` decides whether a root-only function runs at all.
ID_ROOT = '#!/bin/sh\n[ "$1" = -u ] && { echo 0; exit 0; }\necho root\n'
ID_USER = '#!/bin/sh\n[ "$1" = -u ] && { echo 501; exit 0; }\necho tester\n'
# A machine where every daemon in the table is running.
PGREP_ALL = '#!/bin/sh\necho 4242\nexit 0\n'

# How many whitespace-separated fields each table's rows carry before the
# free-text tail. Read by the splitter so a row's prose never becomes a field.
FIELDS = {"rows": 6, "agents": 4, "daemons": 3, "power": 4}


def _rows(name):
    src = QUIET.read_text()
    body = src[src.index(f"wk_quiet_desktop_{name}() {{"):]
    body = body[body.index("<<'ROWS'") + 8:body.index("\nROWS\n")]
    return [l.split(None, FIELDS[name] - 1) for l in body.strip().splitlines()]


def _probe_only_keys():
    """The readings the probe prints that come from no table."""
    body = QUIET.read_text()
    probe = body[body.index("wk_quiet_desktop_probe()"):]
    return set(re.findall(r"printf '([a-z_]+)=", probe))


class TestTheTables(unittest.TestCase):
    def test_the_agents_that_draw_on_their_own_are_named(self):
        """chronod redraws a desktop widget on its own timer; NotificationCenter
        draws a banner over whatever is being measured. Both were measured
        stopping on a Tahoe 26.4 clone, 2026-09-05."""
        labels = [r[1] for r in _rows("agents")]
        self.assertIn("com.apple.chronod", labels)
        self.assertIn("com.apple.notificationcenterui", labels)

    def test_a_lever_that_was_disproved_is_not_carried(self):
        """Setup Assistant's MiniBuddy pane is submitted by runningboardd on
        behalf of loginwindow: `launchctl disable gui/<uid>/com.apple.mbuseragent`
        is recorded and ignored, measured on a clone that showed the pane on
        three consecutive boots with it disabled. A row that does nothing is
        machinery around a fault, and the window probe is what catches it."""
        self.assertNotIn("mbuseragent", QUIET.read_text())

    def test_spotlights_indexer_is_turned_off_and_not_stopped(self):
        """`mdutil` asks mds over XPC and never returns while mds is held
        stopped -- measured in the rehearsal guest on 2026-09-05, where it
        deadlocked `wk quiesce off` through the helper's own status verb. With
        indexing off, mds has nothing to do, so it is left alone."""
        stopped = [r[1] for r in _rows("daemons")]
        for proc in ("mds", "mds_stores", "mdworker", "mdbulkimport"):
            self.assertNotIn(proc, stopped)
        self.assertIn("mdutil -i off -a", QUIET.read_text())

    def test_every_row_is_complete(self):
        for r in _rows("rows"):
            with self.subTest(row=r):
                self.assertEqual(6, len(r), r)
                self.assertIn(r[3], ("bool", "int", "string"))

    def test_every_agent_row_names_the_process_and_why(self):
        for r in _rows("agents"):
            with self.subTest(agent=r[1]):
                self.assertEqual(4, len(r), r)

    def test_every_daemon_row_says_what_it_costs(self):
        """The report prints `why` when one is still running under a run, so a
        row without one is a refusal that does not say why."""
        for r in _rows("daemons"):
            with self.subTest(daemon=r[1]):
                self.assertEqual(3, len(r), r)
                self.assertGreater(len(r[2].split()), 2, r)

    def test_every_power_row_names_where_pmset_prints_it_back(self):
        """`pmset -a disablesleep 1` is read back as SleepDisabled, so the
        setting name and the reading name are two columns, not one."""
        for r in _rows("power"):
            with self.subTest(key=r[1]):
                self.assertEqual(4, len(r), r)

    def test_the_names_are_distinct(self):
        """Every name is a key in one probe reading; two rows sharing one means
        the second silently answers for the first."""
        names = [r[0] for name in FIELDS for r in _rows(name)]
        names += sorted(_probe_only_keys())
        self.assertEqual(len(names), len(set(names)),
                         [n for n in names if names.count(n) > 1])


class TestApplyingIt(WkTest):
    def _run(self, script, path_extra=("defaults", "launchctl", "mdutil",
                                       "tmutil", "pmset", "sudo", "killall",
                                       "pgrep")):
        calls = self.tmp / "calls"
        calls.write_text("")
        stubs = {n: STUB for n in path_extra}
        with stub_path(stubs) as binp:
            cp = bash(f'. {str(QUIET)!r}\n{script}\n',
                      env={"PATH": f"{binp}:/usr/bin:/bin",
                           "WK_TEST_CALLS": str(calls)})
        return cp, calls.read_text()

    def test_sourcing_it_changes_nothing(self):
        """It is streamed into a guest ahead of another script; a side effect
        on source would fire wherever it is read."""
        _, calls = self._run("true")
        self.assertEqual("", calls)

    def test_every_row_is_written(self):
        cp, calls = self._run("wk_quiet_desktop_user")
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        for name, domain, key, type_, value, _why in _rows("rows"):
            with self.subTest(setting=name):
                self.assertIn(f"write {domain.lstrip('@')} {key} -{type_} {value}", calls)

    def test_a_row_already_right_is_not_written_again(self):
        """`wk vm start` settles a running guest's desktop on every start, so
        the second pass has to be silent: Finder and the Dock are restarted
        only when a setting actually moved."""
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
        with stub_path({n: STUB for n in ("launchctl", "killall", "sudo")}) as binp:
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

    def test_finder_and_the_dock_are_restarted_when_a_row_moved(self):
        """Both read CreateDesktop, launchanim and show-recents once, when they
        start, so a write nothing re-reads is a setting that is not in force."""
        _, calls = self._run("wk_quiet_desktop_user")
        self.assertRegex(calls, r"killall -u \S+ Finder Dock")

    def test_a_per_hardware_uuid_key_is_written_that_way(self):
        """`tart clone` remints the hardware UUID, so a `@` row set in the
        golden base does not reach the clone unless it is written again."""
        _, calls = self._run("wk_quiet_desktop_user")
        host = [r for r in _rows("rows") if r[1].startswith("@")]
        self.assertTrue(host, "no per-hardware-UUID row left in the table")
        for name, domain, key, _t, _v, _why in host:
            with self.subTest(setting=name):
                self.assertRegex(calls, rf"defaults -currentHost write {domain[1:]} {key}")

    def test_every_agent_is_on_the_list_that_gets_signalled(self):
        """Neither `disable` nor `bootout` holds one down -- measured
        2026-09-07, 17 of the 21 running again within the second, because
        macOS starts them on demand. They are stopped by signal now, on the
        one list `_wk_qd_daemons_signal` reads."""
        stopped = bash(f'. {str(QUIET)!r}\nwk_quiet_desktop_stopped\n').stdout
        listed = {line.split()[1] for line in stopped.splitlines() if line.split()}
        for row in _rows("agents"):
            with self.subTest(agent=row[2]):
                self.assertIn(row[2], listed)
        for row in _rows("daemons"):
            with self.subTest(daemon=row[1]):
                self.assertIn(row[1], listed)

    def test_applying_the_settings_signals_nothing_itself(self):
        """One enforcement: the signal, sent by the privileged half. Writing a
        preference must not also reach for launchd."""
        _, calls = self._run("wk_quiet_desktop_user")
        self.assertNotIn("bootout", calls)
        self.assertNotRegex(calls, r"launchctl disable")

    def test_the_probe_asks_the_process_not_launchd(self):
        """launchd's disabled list said `off` for an agent that was running,
        and `pgrep` alone cannot tell a stopped process from a running one --
        so every row is read as absent, stopped or running."""
        body = QUIET.read_text()
        probe = body[body.index("wk_quiet_desktop_probe()"):]
        self.assertIn("_wk_qd_procstate", probe)
        self.assertNotIn("print-disabled", probe)
        self.assertNotIn("pgrep -x", probe)

    def test_another_account_is_written_as_that_account(self):
        """A bench install's first boot runs as root before anyone has logged
        in; an unqualified write would land in root's own domain."""
        _, calls = self._run("wk_quiet_desktop_user nosuchuser || true")
        self.assertIn("sudo -u nosuchuser defaults", calls)

    def test_an_account_that_does_not_exist_is_refused(self):
        cp, _ = self._run("wk_quiet_desktop_user nosuchuser; echo rc=$?")
        self.assertIn("rc=1", cp.stdout)
        self.assertIn("no such account", cp.stdout + cp.stderr)

    def test_the_system_half_refuses_without_root(self):
        """It writes /Library/Preferences and stops Spotlight; saying so beats
        a run of `defaults` that quietly does nothing."""
        cp, calls = self._run("wk_quiet_desktop_system; echo rc=$?")
        self.assertIn("rc=1", cp.stdout)
        self.assertIn("needs root", cp.stdout + cp.stderr)
        self.assertEqual("", calls)

    def test_every_power_key_is_applied_on_its_own(self):
        """pmset applies nothing at all from a command line naming a key this
        model does not have, so one key per call is what makes the rest land."""
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
    """`ps` and `kill` are shell functions here rather than PATH stubs, because
    `kill` is a bash builtin and the point of the design is that the listing is
    taken once, before anything is signalled."""

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
        """A `kill -STOP` a user cannot deliver to a root daemon fails silently
        for all but the ones it owns, which would report a quiet machine that
        is not."""
        cp, calls = self._signal("wk_quiet_daemons_pause", uid=501)
        self.assertIn("rc=1", cp.stdout)
        self.assertIn("needs root", cp.stdout + cp.stderr)
        self.assertEqual("", calls)

    def _stoppable(self):
        skip = set(bash(f'. {str(QUIET)!r}\nwk_quiet_desktop_unstoppable\n').stdout.split())
        return [(i, row) for i, row in enumerate(_rows("daemons")) if row[1] not in skip]

    def test_every_daemon_running_is_stopped(self):
        cp, calls = self._signal("wk_quiet_daemons_pause")
        self.assertIn("rc=0", cp.stdout)
        for i, row in self._stoppable():
            with self.subTest(daemon=row[1]):
                self.assertIn("kill -STOP %d\n" % (100 + i), calls)

    def test_resume_sends_the_other_signal_to_the_same_list(self):
        _, calls = self._signal("wk_quiet_daemons_resume")
        for i, row in self._stoppable():
            with self.subTest(daemon=row[1]):
                self.assertIn("kill -CONT %d\n" % (100 + i), calls)

    def test_what_sip_refuses_is_never_signalled(self):
        """`kill -STOP` on a platform binary answers EPERM however it is sent,
        so trying is noise in every log a run leaves behind."""
        _, calls = self._signal("wk_quiet_daemons_pause")
        skip = bash(f'. {str(QUIET)!r}\nwk_quiet_desktop_unstoppable\n').stdout.split()
        self.assertTrue(skip)
        rows = {row[1]: 100 + i for i, row in enumerate(_rows("daemons"))}
        for proc in skip:
            if proc in rows:
                with self.subTest(daemon=proc):
                    self.assertNotIn("kill -STOP %d\n" % rows[proc], calls)

    def test_the_machine_is_listed_once_and_never_asked_again(self):
        """Measured in the rehearsal guest on 2026-09-05: `pgrep` never returns
        once sysmond is stopped, so a loop that re-asked the machine after each
        signal stopped itself half way through the table and left no way back."""
        _, calls = self._signal("wk_quiet_daemons_pause")
        self.assertEqual(1, calls.count("ps "), calls)
        body = func_body(QUIET.read_text(), "_wk_qd_daemons_signal")
        self.assertNotIn("pgrep", body)
        self.assertNotIn("killall", body)

    def test_a_daemon_that_is_not_running_is_not_signalled(self):
        cp, calls = self._signal("wk_quiet_daemons_pause", listing="1 /usr/sbin/nothing")
        self.assertIn("rc=0", cp.stdout)
        self.assertNotIn("kill ", calls)

    def test_a_name_that_is_a_prefix_of_another_is_not_confused(self):
        """`backupd` is a prefix of `backupd-helper`, and a substring match
        would stop the helper twice and the daemon never."""
        _, calls = self._signal("wk_quiet_daemons_pause",
                                listing="7 /usr/libexec/backupd-helper")
        self.assertIn("kill -STOP 7\n", calls)
        self.assertEqual(1, calls.count("kill "), calls)

    def test_quiesce_keeps_no_list_of_its_own(self):
        """A second list is a daemon that gets paused and never resumed, or the
        other way round."""
        text = QUIESCE.read_text()
        self.assertIn("wk_quiet_daemons_pause", text)
        self.assertIn("wk_quiet_daemons_resume", text)
        for row in _rows("daemons"):
            with self.subTest(daemon=row[1]):
                self.assertNotIn(row[1], text)


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


class TestTheFindings(WkTest):
    """One judge for both kinds of measured Mac, driven against a written-out
    probe: what a real machine says is a fact about that machine."""

    def _judge(self, func, probe, fix="the remedy"):
        cp = bash(f'''. {str(QUIET)!r}
probe=$(cat <<'P'
{probe}
P
)
{func} "$probe" {fix!r}
''')
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        return [tuple(l.split("\t")) for l in cp.stdout.splitlines()
                if len(l.split("\t")) == 3]

    def _settled(self):
        out = []
        for name, domain, key, type_, value, _why in _rows("rows"):
            want = {"true": "1", "false": "0"}.get(value, value) if type_ == "bool" else value
            out.append(f"{name}={want}")
        out += [f"{r[0]}=stopped" for r in _rows("agents")]
        out += [f"{r[0]}=stopped" for r in _rows("daemons")]
        out += [f"{r[0]}={r[2]}" for r in _rows("power")]
        out += ["spotlight=Indexing disabled.", "analytics=0",
                "power_source=AC Power", "cpu_speed_limit=100"]
        return "\n".join(out)

    def test_a_settled_machine_is_all_ok(self):
        for func in ("wk_quiet_desktop_findings", "wk_quiet_cpu_findings",
                     "wk_quiet_daemons_findings"):
            with self.subTest(findings=func):
                states = {f[0] for f in self._judge(func, self._settled())}
                self.assertEqual({"ok"}, states, self._judge(func, self._settled()))

    def test_a_setting_at_the_wrong_value_is_wrong_and_names_the_remedy(self):
        probe = self._settled().replace("appnap=1", "appnap=0")
        wrong = [f for f in self._judge("wk_quiet_desktop_findings", probe)
                 if f[0] == "wrong"]
        self.assertEqual(1, len(wrong), wrong)
        self.assertIn("appnap", wrong[0][1])
        self.assertEqual("the remedy", wrong[0][2])

    def test_a_row_macos_will_not_set_says_what_it_costs(self):
        """TCC drops a write to com.apple.universalaccess however it is sent,
        and askForPasswordDelay cannot hold once askForPassword is 0. Failing
        on them refuses every leg for ever, so they are notes that name the
        cost -- and a note is not a fault."""
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

    def test_a_key_the_probe_never_answered_is_unknown_not_off(self):
        """A machine whose copy of this file is older answers nothing for a row
        added since. Silence is not off."""
        probe = "\n".join(l for l in self._settled().splitlines()
                          if not l.startswith("appnap="))
        f = [x for x in self._judge("wk_quiet_desktop_findings", probe)
             if "appnap" in x[1]]
        self.assertEqual(["note"], [x[0] for x in f], f)

    def test_a_key_the_probe_asked_for_and_did_not_find_is_wrong(self):
        """`?` is the probe asking macOS and being told there is no such key."""
        probe = self._settled().replace("appnap=1", "appnap=?")
        f = [x for x in self._judge("wk_quiet_desktop_findings", probe)
             if "appnap" in x[1]]
        self.assertEqual(["wrong"], [x[0] for x in f], f)

    def test_an_agent_still_running_is_wrong_and_says_what_it_does(self):
        probe = self._settled().replace("notifications=stopped", "notifications=running")
        wrong = [f for f in self._judge("wk_quiet_daemons_findings", probe)
                 if f[0] == "wrong"]
        self.assertEqual(1, len(wrong), wrong)
        self.assertIn("banner", wrong[0][1])

    def test_a_daemon_still_running_is_wrong_and_says_what_it_costs(self):
        probe = self._settled().replace("softwareupdate=stopped",
                                        "softwareupdate=running")
        wrong = [f for f in self._judge("wk_quiet_daemons_findings", probe)
                 if f[0] == "wrong"]
        self.assertEqual(1, len(wrong), wrong)
        self.assertIn("softwareupdated is running", wrong[0][1])
        self.assertIn("scans for updates", wrong[0][1])

    def test_a_daemon_that_is_not_there_at_all_is_fine(self):
        probe = self._settled().replace("softwareupdate=stopped",
                                        "softwareupdate=absent")
        self.assertEqual([], [f for f in self._judge("wk_quiet_daemons_findings", probe)
                              if f[0] == "wrong"])

    def test_a_machine_on_battery_is_refused(self):
        probe = self._settled().replace("power_source=AC Power",
                                        "power_source=Battery Power")
        wrong = [f for f in self._judge("wk_quiet_cpu_findings", probe) if f[0] == "wrong"]
        self.assertEqual(1, len(wrong), wrong)
        self.assertIn("battery", wrong[0][1])
        self.assertIn("plug it in", wrong[0][2])

    def test_a_clock_already_held_down_is_refused(self):
        """The one thing on Apple silicon that says the run would be measuring
        a throttled machine rather than the change."""
        probe = self._settled().replace("cpu_speed_limit=100", "cpu_speed_limit=70")
        wrong = [f for f in self._judge("wk_quiet_cpu_findings", probe) if f[0] == "wrong"]
        self.assertEqual(1, len(wrong), wrong)
        self.assertIn("70%", wrong[0][1])

    def test_a_lever_this_model_does_not_have_is_a_note_not_a_fault(self):
        """highpowermode raises the fans; a fanless Mac has no such setting and
        a report that called that a failure would be red on every run."""
        probe = self._settled().replace("power_highpowermode=1",
                                        "power_highpowermode=")
        f = [x for x in self._judge("wk_quiet_cpu_findings", probe)
             if "highpowermode" in x[1]]
        self.assertEqual(["note"], [x[0] for x in f], f)

    def test_no_finding_wraps_over_two_lines(self):
        """render_findings reads a line at a time, so a remedy on a second line
        is a remedy nobody sees."""
        for func in ("wk_quiet_desktop_findings", "wk_quiet_cpu_findings",
                     "wk_quiet_daemons_findings"):
            cp = bash(f'''. {str(QUIET)!r}
probe=$(cat <<'P'
{self._settled().replace("appnap=1", "appnap=?")}
P
)
{func} "$probe" "the remedy"
''')
            for line in cp.stdout.splitlines():
                with self.subTest(findings=func, line=line):
                    self.assertEqual(3, len(line.split("\t")), line)


class TestBothKindsOfMeasuredMacGetIt(unittest.TestCase):
    """The whole point of the file: a setting cannot be true of a guest and not
    of a bench install."""

    def test_a_guest_is_sent_it_with_the_script_that_uses_it(self):
        """vm/desktop.sh sources nothing -- it is streamed into a guest that has
        no wk-tools on disk -- so both callers send the two files together."""
        self.assertIn("wk_quiet_desktop_user", DESKTOP.read_text())
        for caller in (VM_DRIVER, REPO / "vm" / "provision-base.sh"):
            with self.subTest(caller=caller.name):
                self.assertIn("mac-quiet-desktop.sh", caller.read_text())

    def test_the_sudo_shell_carries_what_the_system_half_calls(self):
        """`declare -f` copies one function, and wk_quiet_desktop_system reads
        the power table and the pmset helper through the shell it lands in."""
        body = DESKTOP.read_text()
        line = [l for l in body.splitlines() if "wk_quiet_desktop_system" in l
                and "declare -f" in l]
        self.assertEqual(1, len(line), body)
        for fn in ("wk_quiet_desktop_power", "_wk_qd_pmset"):
            self.assertIn(fn, line[0])

    def test_the_probe_is_sent_it_too(self):
        self.assertIn("wk_quiet_desktop_probe", (REPO / "vm" / "desktop-probe.sh").read_text())
        driver = VM_DRIVER.read_text()
        body = driver[driver.index("vm_desktop_probe() {"):]
        self.assertIn("mac-quiet-desktop.sh", body[:body.index("\n}\n")])

    def test_the_guest_report_judges_it_through_the_shared_findings(self):
        """`wk vm check` and a bench-mode preflight read the same table the same
        way, or a guest is called settled on a row a bench install fails."""
        driver = VM_DRIVER.read_text()
        body = driver[driver.index("vm_desktop_findings() {"):]
        body = body[:body.index("\n}\n")]
        self.assertIn("wk_quiet_desktop_findings", body)
        self.assertIn("wk_quiet_cpu_findings", body)
        self.assertIn("wk_quiet_desktop_findings", (REPO / "lib" / "quiet.sh").read_text())

    def test_a_bench_install_gets_the_file_and_runs_it(self):
        self.assertIn("wk-bench-quiet-desktop.sh", VOLUME.read_text(),
                      "nothing installs it into the image")
        first = FIRSTBOOT.read_text()
        self.assertIn("wk_quiet_desktop_system", first)
        self.assertIn("wk_quiet_desktop_user", first)

    def test_a_bench_install_can_run_wk_quiesce_at_all(self):
        """`wk quiesce` refuses to start without the privileged helper, and
        nothing else on a benchmark install ever runs ./setup."""
        self.assertIn("wk-quiesce-priv", FIRSTBOOT.read_text())

    def test_the_bench_install_names_the_account_being_measured(self):
        """Its first boot is root, and the account that gets measured is the
        bench user -- not root, whose desktop nobody ever looks at."""
        first = FIRSTBOOT.read_text()
        self.assertIn('wk_quiet_desktop_user "$BENCH_USER"', first)

    def test_nothing_keeps_its_own_copy_of_a_setting(self):
        """A second spelling anywhere is a setting that can drift out of the
        table and be true of one kind of measured Mac and not the other."""
        for f in (DESKTOP, FIRSTBOOT, VOLUME, REPO / "vm" / "desktop-probe.sh",
                  REPO / "cmd" / "bench"):
            text = f.read_text()
            with self.subTest(file=f.name):
                for _name, domain, key, _t, _v, _why in _rows("rows"):
                    self.assertNotIn(f"{domain.lstrip('@')} {key}", text,
                                     f"{f.name} writes {key} itself")
                self.assertNotIn("mdutil", text, f"{f.name} turns Spotlight off itself")
                for _name, key, _value, _shown in _rows("power"):
                    self.assertNotIn(f"pmset -a {key}", text,
                                     f"{f.name} sets {key} itself")


if __name__ == "__main__":
    unittest.main()
