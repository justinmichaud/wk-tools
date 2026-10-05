"""bench/mac-quiet-desktop.sh and its table, bench/quiet/macos.tsv -- what a
macOS machine that exists to be measured is, and the one place it is written down."""
import json
import os
import re
import unittest

from tests.support import REPO, WkTest, bash, func_body, stub_path

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


class TestTheTables(unittest.TestCase):

    def test_spotlights_indexer_is_turned_off_and_not_stopped(self):
        stopped = [r[1] for r in _rows("daemons")]
        for proc in ("mds", "mds_stores", "mdworker", "mdbulkimport"):
            self.assertNotIn(proc, stopped)

    def test_every_row_is_complete(self):
        for name, n in FIELDS.items():
            for r in _rows(name):
                with self.subTest(kind=name, row=r):
                    self.assertEqual(n, len(r), r)
        for r in _rows("rows"):
            self.assertIn(r[3], ("bool", "int", "string"))

    def test_the_names_are_distinct(self):
        names = [r[0] for name in FIELDS for r in _rows(name)]
        names += sorted(_probe_only_keys())
        self.assertEqual(len(names), len(set(names)),
                         [n for n in names if names.count(n) > 1])


class TestWhatSipWillNotLetGo(unittest.TestCase):

    def test_every_name_is_a_process_the_table_asks_about(self):
        """A name nobody looks up is an exemption that exempts nothing."""
        rows = bash('. %s\nwk_quiet_desktop_stopped\n' % QUIET).stdout.splitlines()
        watched = {l.split()[1] for l in rows if len(l.split()) > 1}
        named = bash('. %s\nwk_quiet_desktop_unstoppable\n' % QUIET).stdout.split()
        for proc in named:
            with self.subTest(proc=proc):
                self.assertIn(proc, watched)


class TestWhatMustKeepRunning(unittest.TestCase):

    def test_it_is_not_in_the_table_that_gets_signalled(self):
        stopped = bash('. %r\nwk_quiet_desktop_stopped\n' % str(QUIET)).stdout.split()
        for row in _rows("expected"):
            with self.subTest(proc=row[1]):
                self.assertNotIn(row[1], stopped)

    def test_every_row_says_what_it_costs(self):
        for row in _rows("expected"):
            with self.subTest(proc=row[1]):
                self.assertRegex(row[2], r"[0-9]")
                self.assertGreater(len(row[2].split()), 8, row)

    def _judge(self, probe):
        cp = bash(""". %r
probe=$(cat <<'P'
%s
P
)
wk_quiet_daemons_findings "$probe" 'the remedy'
""" % (str(QUIET), probe))
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        return [l.split("\t") for l in cp.stdout.splitlines() if "\t" in l]

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

    def test_every_row_is_written(self):
        cp, calls = self._run("wk_quiet_desktop_user")
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        for name, domain, key, type_, value, _why in _rows("rows"):
            with self.subTest(setting=name):
                self.assertIn(f"write {domain.lstrip('@')} {key} -{type_} {value}", calls)

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

    def test_finder_and_the_dock_are_restarted_when_a_row_moved(self):
        _, calls = self._run("wk_quiet_desktop_user")
        self.assertRegex(calls, r"killall -u \S+ Finder Dock")

    def test_a_per_hardware_uuid_key_is_written_that_way(self):
        _, calls = self._run("wk_quiet_desktop_user")
        host = [r for r in _rows("rows") if r[1].startswith("@")]
        self.assertTrue(host, "no per-hardware-UUID row left in the table")
        for name, domain, key, _t, _v, _why in host:
            with self.subTest(setting=name):
                self.assertRegex(calls, rf"defaults -currentHost write {domain[1:]} {key}")

    def test_every_agent_is_on_the_list_that_gets_signalled(self):
        stopped = bash(f'. {str(QUIET)!r}\nwk_quiet_desktop_stopped\n').stdout
        listed = {line.split()[1] for line in stopped.splitlines() if line.split()}
        for row in _rows("agents"):
            with self.subTest(agent=row[2]):
                self.assertIn(row[2], listed)
        for row in _rows("daemons"):
            with self.subTest(daemon=row[1]):
                self.assertIn(row[1], listed)

    def test_applying_the_settings_signals_nothing_itself(self):
        _, calls = self._run("wk_quiet_desktop_user")
        self.assertNotIn("bootout", calls)
        self.assertNotRegex(calls, r"launchctl disable")

    def test_another_account_is_written_as_that_account(self):
        _, calls = self._run("wk_quiet_desktop_user nosuchuser || true")
        self.assertIn("sudo -u nosuchuser defaults", calls)

    def test_an_account_that_does_not_exist_is_refused(self):
        cp, _ = self._run("wk_quiet_desktop_user nosuchuser; echo rc=$?")
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
        _, calls = self._signal("wk_quiet_daemons_pause")
        skip = bash(f'. {str(QUIET)!r}\nwk_quiet_desktop_unstoppable\n').stdout.split()
        self.assertTrue(skip)
        rows = {row[1]: 100 + i for i, row in enumerate(_rows("daemons"))}
        for proc in skip:
            if proc in rows:
                with self.subTest(daemon=proc):
                    self.assertNotIn("kill -STOP %d\n" % rows[proc], calls)

    def test_the_machine_is_listed_once_and_never_asked_again(self):
        _, calls = self._signal("wk_quiet_daemons_pause")
        self.assertEqual(1, calls.count("ps "), calls)

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

    def test_no_file_is_not_off(self):
        cp = self._dnd('wk_quiet_dnd_state %r' % str(self.tmp / "absent"))
        self.assertEqual("?nofile", cp.stdout.strip(), cp.stdout + cp.stderr)

    def test_a_file_it_may_not_read_says_so(self):
        db = self.tmp / "Library" / "DoNotDisturb" / "DB"
        db.mkdir(parents=True)
        (db / "Assertions.json").write_text("{}")
        (db / "Assertions.json").chmod(0)
        cp = self._dnd('wk_quiet_dnd_state %r' % str(self.tmp))
        if os.geteuid() == 0:
            self.skipTest("root reads a mode-0 file, so there is no denial to meet")
        self.assertEqual("?denied", cp.stdout.strip(), cp.stdout + cp.stderr)

    def test_a_file_that_is_not_this_json_says_so(self):
        db = self.tmp / "Library" / "DoNotDisturb" / "DB"
        db.mkdir(parents=True)
        (db / "Assertions.json").write_text("not json at all")
        cp = self._dnd('wk_quiet_dnd_state %r' % str(self.tmp))
        self.assertEqual("?malformed", cp.stdout.strip(), cp.stdout + cp.stderr)

    def test_a_denied_read_is_left_out_of_the_probe_rather_than_refused(self):
        script = (". %s\n" % QUIET
                  + '_wk_qd_home() { printf "/nonexistent"; }\n'
                  + 'wk_quiet_dnd_state() { printf "?denied"; }\n'
                  + "probe() {%s}\nprobe tester\n"
                  % func_body(QUIET.read_text(), "wk_quiet_desktop_probe"))
        cp = bash(script, env={"PATH": "/usr/bin:/bin"})
        self.assertNotIn("notifications_dnd", cp.stdout,
                         "a row nothing can read refuses every leg:\n" + cp.stdout)

    def test_a_reading_that_worked_is_still_judged(self):
        script = (". %s\n" % QUIET
                  + '_wk_qd_home() { printf "/nonexistent"; }\n'
                  + 'wk_quiet_dnd_state() { printf "off"; }\n'
                  + "probe() {%s}\nprobe tester\n"
                  % func_body(QUIET.read_text(), "wk_quiet_desktop_probe"))
        cp = bash(script, env={"PATH": "/usr/bin:/bin"})
        self.assertIn("notifications_dnd=off", cp.stdout, cp.stdout + cp.stderr)

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

    def test_an_assertion_that_lapses_is_off(self):
        path = self.tmp / "Library/DoNotDisturb/DB/Assertions.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"data": [{"storeAssertionRecords": [
            {"assertionEndDateTimestamp": 1.0, "assertionDetails": {}}]}]}))
        cp = self._dnd('wk_quiet_dnd_state %r' % str(self.tmp))
        self.assertEqual("off", cp.stdout.strip(), cp.stdout + cp.stderr)

    def test_no_records_is_off(self):
        path = self.tmp / "Library/DoNotDisturb/DB/Assertions.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"data": [], "header": {}}))
        cp = self._dnd('wk_quiet_dnd_state %r' % str(self.tmp))
        self.assertEqual("off", cp.stdout.strip(), cp.stdout + cp.stderr)

class TestTheFindings(WkTest):

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

    def test_a_setting_at_the_wrong_value_is_wrong_and_names_the_remedy(self):
        probe = self._settled().replace("appnap=1", "appnap=0")
        wrong = [f for f in self._judge("wk_quiet_desktop_findings", probe)
                 if f[0] == "wrong"]
        self.assertEqual(1, len(wrong), wrong)
        self.assertIn("appnap", wrong[0][1])
        self.assertEqual("the remedy", wrong[0][2])

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

    def test_a_key_the_probe_never_answered_is_unknown_not_off(self):
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
        probe = self._settled().replace("cpu_speed_limit=100", "cpu_speed_limit=70")
        wrong = [f for f in self._judge("wk_quiet_cpu_findings", probe) if f[0] == "wrong"]
        self.assertEqual(1, len(wrong), wrong)
        self.assertIn("70%", wrong[0][1])

    def test_a_lever_this_model_does_not_have_is_a_note_not_a_fault(self):
        probe = self._settled().replace("power_highpowermode=1",
                                        "power_highpowermode=")
        f = [x for x in self._judge("wk_quiet_cpu_findings", probe)
             if "highpowermode" in x[1]]
        self.assertEqual(["note"], [x[0] for x in f], f)

    def test_no_finding_wraps_over_two_lines(self):
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

    def test_nothing_keeps_its_own_copy_of_a_setting(self):
        for f in (DESKTOP, FIRSTBOOT, VOLUME, REPO / "vm" / "desktop-probe.sh",
                  REPO / "cmd" / "bench", REPO / "lib" / "wk" / "bench" / "mac.py"):
            text = f.read_text()
            with self.subTest(file=f.name):
                for _name, domain, key, _t, _v, _why in _rows("rows"):
                    self.assertNotIn(f"{domain.lstrip('@')} {key}", text,
                                     f"{f.name} writes {key} itself")
                self.assertNotIn("mdutil", text, f"{f.name} turns Spotlight off itself")
                for _name, key, _value, _shown in _rows("power"):
                    self.assertNotIn(f"pmset -a {key}", text,
                                     f"{f.name} sets {key} itself")


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

    def test_every_row_is_one_of_the_kinds_a_reader_asks_for(self):
        kinds = set(KIND.values()) | {"unsettable", "unstoppable"}
        for line in TABLE.read_text().splitlines():
            if line and not line.startswith("#"):
                with self.subTest(row=line):
                    self.assertIn(line.split("\t")[0], kinds)


if __name__ == "__main__":
    unittest.main()
