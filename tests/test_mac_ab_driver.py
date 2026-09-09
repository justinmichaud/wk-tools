"""The host side of the macOS A/B lane: `bench/mac-ab.sh`'s refusals and the
boot driver (`boot/mac-volume.sh`) that reports the Mac from another machine.

Neither needs a Mac to be exercised. Every decision here is a shell function
whose one input is what the Mac answered, so each test lifts the function and
hands it that answer; one test asks the real machine and skips without it.

Run: python3 -m unittest tests.test_mac_ab_driver -v
"""
import json
import re
import shlex
import subprocess
import unittest

from tests.support import (REPO, WkTest, bash, func_body, requires_machine,
                           scratch_dir, temp_store)

MACAB = REPO / "bench" / "mac-ab.sh"
DRIVER = REPO / "boot" / "mac-volume.sh"
MBP = REPO / "boot" / "machines" / "mbp.conf"

# Measured on tolken 2026-09-08: the firmware's boot-volume already names the
# bench volume group, which is what makes the restart need no human.
BENCH_GROUP = "73C12614-1130-40DF-B9B9-9CA73D10F3AA"
HOST_GROUP = "1981BBBF-8B67-4DED-A3E5-41A2550DE0FB"
BOOT_VOLUME = ("EF57347C-0000-AA11-AA11-00306543ECAC:"
               "D7E4E11B-F6E7-5A4C-B4D2-C5241524E3A9:" + BENCH_GROUP)

# Interface 1's reading on the bench install (screen=[1470,956], six of six
# browser-check.json files from 2026-09-07) and on the host install.
BENCH_DISPLAY = {"count": 1, "displays": [
    {"id": 1, "builtin": True, "main": True, "active": True, "online": True,
     "mirrored": False, "asleep": False, "points": [1470, 956]}]}
HOST_DISPLAY = {"count": 1, "displays": [
    {"id": 1, "builtin": True, "main": True, "active": False, "online": True,
     "mirrored": False, "asleep": True, "points": [1280, 832]}]}


def macab_func(name):
    return func_body(MACAB.read_text(), name)


def _lift_between(text, first, last):
    a = text.index(first)
    return text[a:text.index(last, a)]


_RESTARTABLE_CK = _lift_between(
    MACAB.read_text(), "    if mv_reboot_ready; then", "\n    log \"\" >&2")


class TestTheFirmwareDefaultIsAsserted(WkTest):
    """A restart only starts an A/B if the firmware's own default is the bench
    volume, so the lane asserts that rather than reporting it."""

    def _fw(self, boot_volume, bench_grp=BENCH_GROUP, host_grp=HOST_GROUP):
        script = """. "$WK_ROOT/lib/common.sh"
VOLUME="WK Bench"
mac_wkmac() {
    case "$1" in
        boot-volume)  printf '%%s' %s ;;
        volume-group) case "$2" in
                          /) printf '%%s' %s ;;
                          *) printf '%%s' %s ;;
                      esac ;;
    esac
}
firmware_default_is_bench() {%s}
if firmware_default_is_bench; then printf 'PASS %%s' "$FW_DETAIL"
else printf 'FAIL %%s' "$FW_DETAIL"; fi
""" % (shlex.quote(boot_volume), shlex.quote(host_grp),
       shlex.quote(bench_grp), macab_func("firmware_default_is_bench"))
        cp = bash(script)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout

    def test_the_bench_volume_group_as_the_default_passes(self):
        out = self._fw(BOOT_VOLUME)
        self.assertTrue(out.startswith("PASS"), out)
        self.assertIn(BENCH_GROUP, out)
        self.assertIn("no human", out)

    def test_the_host_install_as_the_default_fails(self):
        out = self._fw("EF57347C-0000-AA11-AA11-00306543ECAC:x:" + HOST_GROUP)
        self.assertTrue(out.startswith("FAIL"), out)
        self.assertIn("the host install", out)

    def test_a_default_matching_neither_install_fails(self):
        out = self._fw("a:b:11111111-2222-3333-4444-555555555555")
        self.assertTrue(out.startswith("FAIL"), out)
        self.assertIn("neither install", out)

    def test_an_unreadable_boot_volume_fails(self):
        out = self._fw("")
        self.assertTrue(out.startswith("FAIL"), out)
        self.assertIn("no boot-volume", out)

    def test_the_failure_names_both_remedies(self):
        text = MACAB.read_text()
        block = text[text.index('ck no "firmware default"'):text.index('log "" >&2')]
        self.assertIn("wk boot $MACHINE", block)
        self.assertIn("startup manager", block)
        self.assertIn("--plant", block)

    def test_it_is_a_check_and_not_a_note(self):
        """A `log` line about the firmware would leave the lane restarting a
        machine that comes straight back to host mode."""
        text = MACAB.read_text()
        self.assertIn('ck yes "firmware default"', text)
        self.assertNotIn("Reported, never asserted", text)

    @requires_machine("tolken")
    def test_the_real_firmware_default_on_tolken_is_the_bench_volume(self):
        def wkmac(*args):
            cp = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "tolken",
                 "cd ~/Development/wk-tools && python3 lib/wkmac.py " + " ".join(args)],
                capture_output=True, text=True, timeout=60)
            return cp.stdout.strip()

        self.assertEqual(wkmac("boot-volume").rsplit(":", 1)[-1],
                         wkmac("volume-group", "'/Volumes/WK Bench'"))


class TestOnlyTheBuiltInDisplay(WkTest):
    """An external monitor changes the compositing, the refresh rate and which
    GPU the window lands on, and MotionMark's score is the area it draws."""

    def _check(self, answer):
        script = """. "$WK_ROOT/lib/common.sh"
HOST=fakemac
mac_wkmac() { printf '%%s' %s; }
mac_display_check() {%s}
if mac_display_check; then printf 'PASS %%s' "$DISPLAY_READ"
else printf 'FAIL %%s' "$DISPLAY_READ"; fi
""" % (shlex.quote(answer), macab_func("mac_display_check"))
        cp = bash(script)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout

    def test_the_measured_reading_passes(self):
        out = self._check(json.dumps(BENCH_DISPLAY))
        self.assertTrue(out.startswith("PASS"), out)
        self.assertIn("builtin 1470x956", out)

    def test_the_host_installs_own_reading_passes_too(self):
        """The mode differs between the two installs; what this asks is the
        count and which panel, not the mode."""
        out = self._check(json.dumps(HOST_DISPLAY))
        self.assertTrue(out.startswith("PASS"), out)
        self.assertIn("builtin 1280x832", out)

    def test_two_online_displays_fail(self):
        doc = {"count": 2, "displays": [
            dict(BENCH_DISPLAY["displays"][0]),
            {"id": 2, "builtin": False, "online": True, "points": [3840, 2160]}]}
        out = self._check(json.dumps(doc))
        self.assertTrue(out.startswith("FAIL"), out)
        self.assertIn("2 online display(s)", out)
        self.assertIn("external 3840x2160", out)

    def test_a_display_that_is_not_the_built_in_panel_fails(self):
        doc = {"count": 1, "displays": [
            {"id": 7, "builtin": False, "online": True, "points": [2560, 1440]}]}
        out = self._check(json.dumps(doc))
        self.assertTrue(out.startswith("FAIL"), out)
        self.assertIn("not the built-in panel", out)

    def test_no_display_at_all_fails(self):
        out = self._check(json.dumps({"count": 0, "displays": []}))
        self.assertTrue(out.startswith("FAIL"), out)
        self.assertIn("0 online display(s)", out)

    def test_an_offline_display_is_not_counted_as_one(self):
        doc = {"count": 2, "displays": [
            dict(BENCH_DISPLAY["displays"][0]),
            {"id": 2, "builtin": False, "online": False, "points": [3840, 2160]}]}
        out = self._check(json.dumps(doc))
        self.assertTrue(out.startswith("PASS"), out)

    def test_a_reading_that_could_not_be_taken_fails(self):
        self.assertTrue(self._check("").startswith("FAIL"))
        self.assertIn("did not print JSON", self._check("not json at all"))

    def test_the_preflight_check_refuses_rather_than_warns(self):
        text = MACAB.read_text()
        self.assertIn('ck no "one display"', text)
        block = text[text.index('ck no "one display"'):text.index('if firmware_default_is_bench')]
        self.assertIn("no", block.lower())
        self.assertIn("--force crosses it", block)

    def test_force_does_not_cross_the_check_before_the_restart(self):
        """The second reading is seconds before the transition, and there is
        no number to save by crossing it."""
        script = """. "$WK_ROOT/lib/common.sh"
DRY=""; GO=restart; FORCE=1; WK_FORCE=1; export WK_FORCE
HOST=fakemac; VOLUME="WK Bench"
mac_display_check() { DISPLAY_READ="2 online display(s)"; return 1; }
mac_boottime() { printf 1 ; }
mac_sh() { printf 'THE MACHINE WAS TOLD\\n'; }
phase_go() {%s}
phase_go
""" % macab_func("phase_go")
        cp = bash(script)
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("THE MACHINE WAS TOLD", cp.stdout + cp.stderr)
        self.assertIn("2 online display(s)", cp.stdout + cp.stderr)

    def test_the_restart_is_reported_as_needing_nobody(self):
        body = macab_func("phase_go")
        self.assertIn("firmware default", body)
        self.assertIn("nobody has to be at the keyboard", body)


class TestThePinnedDisplayIsConfig(WkTest):
    """The expectation is one line of `boot/machines/<machine>.conf`, and a
    plant without it would let two runs at different resolutions compare."""

    def test_mbp_declares_the_bench_installs_measured_mode(self):
        cp = bash('. "$WK_ROOT/lib/common.sh"; . "$WK_ROOT/boot/machines.sh"\n'
                  'machine_load mbp && printf "%s" "$NODE_DISPLAY"')
        # 1280x832 at scale 2 is exactly the 2560x1664 panel: no frame is
        # rendered larger than the panel and downsampled.
        self.assertEqual(cp.stdout, "builtin 1280x832", cp.stdout + cp.stderr)

    def _plant(self, node_display):
        """A machine conf of the test's own, whose NODE_SSH does not resolve:
        preflight fails on the first reading and --dry-run carries on to the
        plant, which is the refusal under test."""
        with scratch_dir() as tmp:
            (tmp / "fakemac.conf").write_text(
                'NODE_SSH="wk-test-no-such-host.invalid"\n'
                "NODE_DRIVER=mac-volume\n"
                "NODE_ROLE=workstation\n"
                "NODE_OS=any\n"
                'NODE_VOLUME="WK Bench"\n'
                + node_display +
                'NODE_NOTE="a machine conf that exists only for this test"\n')
            cp = bash(
                '"$WK_ROOT/bench/mac-ab.sh" --machine fakemac --dry-run 2>&1',
                env={"WK_MACHINES_DIR": str(tmp), "WK_SSH_TIMEOUT": "1"},
                timeout=120)
            return cp

    def test_a_machine_conf_with_no_node_display_refuses_the_plant(self):
        cp = self._plant("")
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("declares no NODE_DISPLAY", cp.stdout)
        self.assertIn("fakemac.conf", cp.stdout)

    def test_a_pinned_mode_gets_past_that_refusal(self):
        cp = self._plant('NODE_DISPLAY="builtin 1470x956"\n')
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertNotIn("NODE_DISPLAY", cp.stdout)
        self.assertIn("is not visible from", cp.stdout)


def job_writer():
    """The python block phase_plant writes job.json with."""
    text = MACAB.read_text()
    m = re.search(r"python3 - <<'PYEOF' > \"\$task_dir/job.json\"\n(.*?)\nPYEOF\n",
                  text, re.S)
    assert m, "phase_plant no longer writes job.json through a python heredoc"
    return m.group(1)


def write_job(**env):
    fields = {"WK_JOB_PLANS": "jetstream3 motionmark", "WK_JOB_ROUNDS": "5",
              "WK_JOB_MAX_ROUNDS": "40", "WK_JOB_DETECT": "0.3",
              "WK_JOB_TIMEOUT": "1800", "WK_JOB_SETTLE": "90",
              "WK_JOB_A": "arm-a", "WK_JOB_B": "arm-b",
              "WK_JOB_TOOLS": "/var/wk/wk-tools", "WK_JOB_BY": "moose",
              "WK_JOB_STAMP": "20260908T000000Z"}
    fields.update(env)
    cp = subprocess.run(["python3", "-c", job_writer()], env=fields,
                        capture_output=True, text=True, timeout=30)
    assert cp.returncode == 0, cp.stderr
    return json.loads(cp.stdout)


class TestTheCountDefault(WkTest):
    """Every leg of a run with count=1 warns that no p-value can be computed
    for it, so the default is two iterations per leg."""

    def test_the_scripts_default_is_two(self):
        line = [l for l in MACAB.read_text().splitlines() if l.startswith("COUNT=")]
        self.assertEqual(line, ["COUNT=2"], line)

    def test_the_default_reaches_job_json(self):
        self.assertEqual(write_job(WK_JOB_COUNT="2")["count"], "2")

    def test_the_help_block_says_what_it_costs(self):
        head = "\n".join(MACAB.read_text().splitlines()[:30])
        self.assertIn("--count", head)
        self.assertIn("p-value", head)
        self.assertIn("--detect", head)


class TestTheJobCarriesTheDisplay(WkTest):
    def test_the_pinned_display_reaches_job_json(self):
        job = write_job(WK_JOB_DISPLAY="builtin 1470x956")
        self.assertEqual(job["display"], "builtin 1470x956")

    def test_the_plant_passes_node_display_and_nothing_else(self):
        body = macab_func("phase_plant")
        self.assertIn('WK_JOB_DISPLAY="$NODE_DISPLAY"', body)


class TestTheRunIsVisibleWhereRunsAreListed(WkTest):
    """`wk status` lists $WK_STORE/bench/*/task.json, so the plant records a
    real task there rather than a side-file of its own."""

    def test_the_side_file_is_gone(self):
        self.assertNotIn("mac-ab-job.json", MACAB.read_text())

    def test_the_job_lives_under_the_task(self):
        body = macab_func("phase_plant")
        self.assertIn('> "$task_dir/job.json"', body)
        self.assertIn('put_file "$task_dir/job.json"', body)

    def test_the_plant_records_a_task_through_bench_task_new(self):
        body = macab_func("phase_plant")
        self.assertIn("bench_task_new", body)

    def test_the_task_it_writes_is_one_wk_status_can_read(self):
        """The same call phase_plant makes, against the real store machinery:
        what `wk status` prints comes from `wkdata task-status`."""
        fields = re.search(r'bench_task_new "\$task" (.*?)--command',
                           macab_func("phase_plant"), re.S).group(1)
        self.assertIn("devices=", fields)
        self.assertIn("plans=", fields)
        self.assertIn("slots=", fields)
        self.assertIn("rounds=", fields)
        with temp_store() as store:
            script = """. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/bench.sh"
MACHINE=mbp; CONFIG=mac-release-pgo; PLANS="jetstream3 motionmark"
ROUNDS=5; A_ID=arm-a; B_ID=arm-b
task=20260908T000000Z-mbp-mac-ab
bench_task_new "$task" %s--command "wk bench mac-ab"
python3 "$WK_ROOT/lib/wkdata.py" task-status "$WK_STORE/bench/20260908T000000Z-mbp-mac-ab"
""" % fields
            cp = bash(script, env={"WK_STORE": store["WK_STORE"]})
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("subject=arm-a vs arm-b", cp.stdout)
            self.assertIn("mbp", cp.stdout)
            self.assertIn("planned=20", cp.stdout)

    def test_no_progress_field_is_stored(self):
        body = macab_func("phase_plant")
        self.assertNotIn("progress", body)

    def test_collect_records_the_outcome_onto_the_same_task(self):
        body = macab_func("phase_collect")
        self.assertIn('bench_task_dir "$stamp-$MACHINE-mac-ab"', body)
        self.assertIn('autorun.state', body)
        self.assertIn("collect_runs_into_task", body)

    def _collect(self, tsv):
        """The volume, as a local directory: every command the collect sends
        is a `cat` or a `tar` of a path, so the stub is the same shell without
        the ssh. The result directories are the volume's own, env.json and
        all, which is why they are copied rather than composed."""
        with temp_store() as store, scratch_dir() as vol:
            for rid in ("r0", "r1", "r2", "r3"):
                d = vol / "results" / rid
                d.mkdir(parents=True)
                (d / "env.json").write_text(json.dumps(
                    {"plan": "jetstream3", "workspace": "wk-bench",
                     "config": "mac-release-pgo", "wall_time_s": "60"}))
                (d / "result.json").write_text('{"debugOutput": []}')
            runs = vol / "ab" / "20260908T000000Z" / "runs.tsv"
            runs.parent.mkdir(parents=True)
            runs.write_text(tsv)
            task = store["path"] / "bench" / "20260908T000000Z-mbp-mac-ab"
            (task / "runs").mkdir(parents=True)
            script = """. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/bench.sh"
MACHINE=mbp
mac() { bash -c "$1"; }
collect_runs_into_task() {%s}
collect_runs_into_task %s %s %s
""" % (macab_func("collect_runs_into_task"), shlex.quote(str(task)),
       shlex.quote(str(vol)), shlex.quote(str(runs)))
            cp = bash(script, env={"WK_STORE": store["WK_STORE"]})
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            recorded = {}
            for d in sorted((task / "runs").iterdir()):
                recorded[d.name] = json.loads((d / "env.json").read_text())
            return recorded, cp.stdout + cp.stderr

    def test_each_clean_leg_lands_as_a_run_paired_with_its_round_and_arm(self):
        recorded, out = self._collect(
            "0\tA\tsid-a\tr0\tclean\tjetstream3\n"
            "1\tA\tsid-a\tr1\tclean\tjetstream3\n"
            "1\tB\tsid-b\tr2\tclean\tjetstream3\n")
        self.assertEqual(sorted(recorded), ["r1", "r2"], out)
        self.assertEqual(recorded["r1"]["machine"], "mbp")
        self.assertEqual(recorded["r1"]["ab"], {"round": "1", "arm": "a", "staged": "sid-a"})
        self.assertEqual(recorded["r2"]["ab"]["arm"], "b")
        self.assertEqual(recorded["r1"]["plan"], "jetstream3")

    def test_the_warmup_round_is_not_recorded(self):
        recorded, out = self._collect("0\tA\tsid-a\tr0\tclean\tjetstream3\n"
                                      "1\tA\tsid-a\tr1\tclean\tjetstream3\n")
        self.assertNotIn("r0", recorded, out)

    def test_a_contaminated_leg_is_not_recorded(self):
        """The lane refuses to compare a leg a software-update scan ran
        across, so the store does not carry it either."""
        recorded, out = self._collect("1\tA\tsid-a\tr1\tscanned\tjetstream3\n"
                                      "1\tB\tsid-b\tr2\tclean\tjetstream3\n")
        self.assertEqual(sorted(recorded), ["r2"], out)

    def test_nothing_clean_says_so_and_records_nothing(self):
        recorded, out = self._collect("0\tA\tsid-a\tr0\tclean\tjetstream3\n")
        self.assertEqual(recorded, {})
        self.assertIn("no clean leg after the warmup round", out)


class TestTheDriverAnswersFromAnotherMachine(WkTest):
    """Every board's driver probes over the tailnet from anywhere; this one
    reports `unknown from here` only if it refuses to try."""

    PRE = """. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/boot/machines.sh"
NODE_NAME=mbp
NODE_SSH=fakemac
NODE_VOLUME="WK Bench"
NODE_DISPLAY="builtin 1470x956"
. "$WK_ROOT/boot/mac-volume.sh"
"""

    def _driver(self, script, env=None):
        return bash(self.PRE + script, env=env)

    def test_it_is_probeable_off_the_mac(self):
        cp = self._driver('if b_probeable; then echo YES; else echo NO; fi')
        self.assertEqual(cp.stdout.strip(), "YES", cp.stdout + cp.stderr)

    def test_ssh_answering_with_no_marker_is_host_mode(self):
        cp = self._driver('m_ssh() { printf "READY\\n"; }\n'
                          'b_probe; printf "%s|%s" "$MODE" "$MODE_CHANNEL"')
        self.assertEqual(cp.stdout, "host|host", cp.stdout + cp.stderr)

    def test_a_marker_on_the_answering_install_is_bench_mode(self):
        cp = self._driver('m_ssh() { printf "perf-macos-tolken-2026-08\\nREADY\\n"; }\n'
                          'b_probe; printf "%s" "$MODE"')
        self.assertEqual(cp.stdout, "bench perf-macos-tolken-2026-08",
                         cp.stdout + cp.stderr)

    def test_no_answer_is_unreachable_and_not_unknown(self):
        cp = self._driver('m_ssh() { return 255; }\n'
                          'b_probe; printf "%s|%s" "$MODE" "$MODE_CHANNEL"')
        self.assertEqual(cp.stdout, "unreachable|none", cp.stdout + cp.stderr)

    def _silent_media(self, store):
        return self._driver('m_ssh() { return 255; }\nb_media',
                            env={"WK_STORE": store})

    def test_a_silence_with_a_planted_job_names_both_states_it_cannot_tell_apart(self):
        with temp_store() as store:
            task = store["path"] / "bench" / "20260908T010203Z-mbp-mac-ab"
            task.mkdir(parents=True)
            (task / "job.json").write_text("{}")
            cp = self._silent_media(store["WK_STORE"])
            out = cp.stdout
            self.assertIn("20260908T010203Z", out)
            self.assertIn("measuring", out)
            self.assertIn("halted with the result on the volume", out)

    def test_a_silence_with_no_planted_job_is_a_plain_outage(self):
        with temp_store() as store:
            cp = self._silent_media(store["WK_STORE"])
            self.assertIn("no job is planted", cp.stdout)
            self.assertNotIn("measuring", cp.stdout)

    def test_the_newest_planted_task_is_the_one_reported(self):
        with temp_store() as store:
            for stamp in ("20260901T000000Z", "20260908T010203Z"):
                d = store["path"] / "bench" / (stamp + "-mbp-mac-ab")
                d.mkdir(parents=True)
                (d / "job.json").write_text("{}")
            cp = self._silent_media(store["WK_STORE"])
            self.assertIn("20260908T010203Z", cp.stdout)
            self.assertNotIn("20260901T000000Z", cp.stdout)

    def test_evidence_off_the_mac_says_what_it_cannot_see(self):
        with temp_store() as store:
            cp = self._driver('m_ssh() { return 255; }\nb_evidence',
                              env={"WK_STORE": store["WK_STORE"]})
            self.assertIn("booted_volume=unknown", cp.stdout)
            self.assertIn("firmware_default=unknown", cp.stdout)
            self.assertIn("bench_display=builtin 1470x956", cp.stdout)
            self.assertIn("planted_job=none", cp.stdout)

    def test_evidence_over_ssh_reads_the_same_facts_as_it_does_locally(self):
        stub = ("m_ssh() { return 0; }\n"
                "mv_wkmac() {\n"
                "    case \"$1\" in\n"
                "        volume-name)  printf 'Macintosh HD' ;;\n"
                "        boot-volume)  printf 'a:b:" + BENCH_GROUP + "' ;;\n"
                "        volume-group) case \"$2\" in\n"
                "                          /) printf '" + HOST_GROUP + "' ;;\n"
                "                          *) printf '" + BENCH_GROUP + "' ;;\n"
                "                      esac ;;\n"
                "    esac\n"
                "}\nb_evidence\n")
        cp = self._driver(stub)
        self.assertIn("booted_volume=Macintosh HD", cp.stdout)
        self.assertIn("attached at /Volumes/WK Bench", cp.stdout)
        self.assertIn("a plain reboot is expected to enter bench mode", cp.stdout)

    def test_nothing_about_the_mac_is_stored_between_reads(self):
        """Every fact above is recomputed; the only file the driver keeps is
        the record of a person's arming."""
        text = DRIVER.read_text()
        self.assertNotIn("cache", text.lower())
        self.assertEqual(text.count("NODE_RECORD="), 1)


class TestTheRestartIsTheOneImplementation(WkTest):
    """A graceful restart is declined by any application that will not quit --
    measured 2026-09-08, with a console user logged in and osascript automation
    working -- so an unattended lane uses the boot driver's helper reboot, which
    no application can refuse, and refuses up front where that is not installed."""

    def test_phase_go_restarts_through_the_boot_driver(self):
        body = macab_func("phase_go")
        self.assertIn("b_reboot", body)

    def test_phase_go_hand_rolls_no_restart_of_its_own(self):
        body = macab_func("phase_go")
        for spelling in ("aevtrrst", "shutdown -r", "shutdown $flag"):
            self.assertNotIn(spelling, body, spelling)

    def test_the_driver_is_loaded_so_b_reboot_exists(self):
        self.assertIn('load_driver "$NODE_DRIVER"', MACAB.read_text())

    def test_the_helper_reboot_is_asked_for_through_one_reader(self):
        """b_reboot answers from the Mac and from another machine, so the lane
        and `wk boot` restart it the same way. `mv_priv` is the one spelling of
        asking the helper, and it goes through `m_ssh`, the one reader."""
        text = (REPO / "boot" / "mac-volume.sh").read_text()
        body = func_body(text, "b_reboot")
        self.assertIn("mv_priv", body)
        self.assertNotIn("sudo -n", body)
        self.assertIn('mv_priv() { m_ssh "sudo -n', text)
        # Every ask goes through it: the path is read where mv_priv builds the
        # command and in the one is-it-installed test, and nowhere else.
        self.assertEqual(2, text.count("$BOOT_HELPER"), text.count("$BOOT_HELPER"))

    def test_preflight_refuses_a_mac_it_cannot_restart(self):
        for ready, want in ((0, "ok"), (1, "FAIL")):
            with self.subTest(ready=ready):
                cp = bash(
                    '. "$WK_ROOT/lib/common.sh"\n'
                    'PF_FAIL=0\n'
                    'ck() {%s}\n'
                    'mv_reboot_ready() { return %d; }\n'
                    'HOST=tolken; MACHINE=mbp\n'
                    '%s\n'
                    'echo "PF_FAIL=$PF_FAIL"'
                    % (macab_func("ck"), ready, _RESTARTABLE_CK))
                out = cp.stdout + cp.stderr
                self.assertIn("restartable", out, out)
                self.assertIn("PF_FAIL=%d" % (0 if ready == 0 else 1), out, out)
                if want == "FAIL":
                    # A wk command, not an incantation to run over there by hand.
                    self.assertIn("wk boot mbp --prepare", out, out)
                    self.assertNotIn("./setup --stage quiesce", out, out)
                    self.assertIn("planted and correct", out, out)


class TestAFailedNotifyCostsNothing(WkTest):
    """A notification that did not go out must not cost a measurement."""

    def _notify(self, wk_exit):
        with scratch_dir() as tmp:
            wk = tmp / "wk"
            wk.write_text("#!/bin/sh\necho 'no ntfy topic on this machine' >&2\nexit %d\n" % wk_exit)
            wk.chmod(0o755)
            script = """. "$WK_ROOT/lib/common.sh"
WK_ROOT=%s
notify() {%s}
notify "a headline" "a detail"
printf 'rc=%%s' "$?"
""" % (shlex.quote(str(tmp)), macab_func("notify"))
            return bash(script)

    def test_a_failing_notify_returns_zero_and_warns(self):
        cp = self._notify(1)
        self.assertIn("rc=0", cp.stdout, cp.stdout + cp.stderr)
        self.assertIn("could not send the notification", cp.stdout + cp.stderr)

    def test_a_notify_that_worked_says_nothing(self):
        cp = self._notify(0)
        self.assertIn("rc=0", cp.stdout)
        self.assertNotIn("could not send", cp.stdout + cp.stderr)

    def test_no_call_site_treats_it_as_fatal(self):
        text = MACAB.read_text()
        for line in text.splitlines():
            if line.strip().startswith("notify "):
                self.assertNotIn("|| die", line)
                self.assertNotIn("|| exit", line)

    def test_it_is_called_only_at_the_moments_the_driver_knows(self):
        """Silence is the expected end -- the bench install powers the machine
        off -- so nothing is notified about it: it cannot tell "finished and
        powered off" from "still measuring". What is notified is the plant and
        the two ways the transition can fail visibly."""
        text = MACAB.read_text()
        calls = [l.strip() for l in text.splitlines() if l.strip().startswith('notify "')]
        self.assertEqual(len(calls), 3, calls)
        self.assertTrue(any("planted" in c for c in calls))
        self.assertTrue(any("came back to host mode" in c for c in calls))
        self.assertTrue(any("never rebooted" in c for c in calls))
        self.assertFalse(any("gone silent" in c for c in calls), calls)

class TestThePlantedTreeIsVerifiedWhole(WkTest):
    """A sentinel is not a verification. The plant used to check one file's byte
    count, so a tree stale in any other file landed looking right and behaved as
    an older lane -- discovered after the reboot, in bench mode, where nothing
    can report it."""

    def test_the_probe_file_check_is_gone(self):
        body = func_body(MACAB.read_text(), "put_tree")
        self.assertNotIn("probe", body)
        self.assertIn("treehash.py", body)

    def test_both_sides_exclude_the_same_names(self):
        """One list, read twice. Two lists is two file sets and two digests of
        different things, which reads as a corrupted tree on every plant."""
        text = MACAB.read_text()
        self.assertIn('TREE_SKIP=', text)
        body = func_body(text, "put_tree")
        self.assertEqual(2, body.count("$TREE_SKIP"), body)

    def test_the_local_arguments_are_an_array_and_not_a_split_string(self):
        """`sh_quote .git` is `'.git'` with the quotes in it: word-split without
        quote removal, the local side excludes a name no file has and hashes a
        different file set than the far side, whose shell does remove them."""
        body = func_body(MACAB.read_text(), "put_tree")
        self.assertIn('local_args+=(--exclude "$x")', body)
        self.assertIn('"${local_args[@]}"', body)


class TestOneReaderOfTheBootTime(WkTest):
    """"Did it actually reboot" is decided by kern.boottime, and the driver had
    a second copy of that reading whose pattern was anchored on `sec = ` alone.
    `.*sec *= *` matches greedily to the last one in
    `{ sec = 1788835009, usec = 104495 }`, so that copy answered 104495 -- the
    microseconds -- and the test for "the same boot as before" compared those."""

    SYSCTL = "{ sec = 1788835009, usec = 104495 } Mon Sep  7 20:36:49 2026"

    def test_the_driver_has_no_reader_of_its_own(self):
        text = MACAB.read_text()
        # The die message still names kern.boottime as its evidence, which is
        # what the operator has to know; what must be gone is the reading.
        self.assertNotIn("sysctl", text)
        self.assertNotIn("mac_boottime", text)
        self.assertIn("BOOT_BEFORE=$(b_boot_id)", text)

    def test_the_one_reader_answers_seconds_and_not_microseconds(self):
        """The brace is what makes it the seconds: bracketed, the pattern cannot
        slide onto `usec`."""
        body = func_body(DRIVER.read_text(), "_mac_boottime")
        script = ("m_ssh() { printf '%s\\n' " + repr(self.SYSCTL).replace("'", '"')
                  + "; }\n_mac_boottime() {" + body + "}\n_mac_boottime\n")
        cp = bash(script)
        self.assertEqual("1788835009", cp.stdout.strip(), cp.stdout + cp.stderr)

    def test_the_pattern_the_driver_retired_answered_the_microseconds(self):
        """The discriminating half: without this the test above passes against
        either pattern on a machine whose usec happens to be long."""
        cp = bash("printf '%s\\n' " + repr(self.SYSCTL).replace("'", '"')
                  + " | sed -n 's/.*sec *= *\\([0-9]*\\).*/\\1/p'\n")
        self.assertEqual("104495", cp.stdout.strip(), cp.stdout + cp.stderr)


class ForceCrossesBarriersAndNothingElse(WkTest):
    """One flag, one meaning. It used to reach job.json, where the autorun
    turned it into `--force` on every leg -- so crossing a preflight barrier
    silently disabled each leg's own quiet-machine gate."""

    def test_the_job_carries_no_force(self):
        text = MACAB.read_text()
        self.assertNotIn("WK_JOB_FORCE", text)
        self.assertNotIn('"force"', text)


if __name__ == "__main__":
    unittest.main()
