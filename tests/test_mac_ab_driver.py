"""The host side of the macOS A/B lane: `bench/mac-ab.sh`'s refusals and the
boot driver (`lib/wk/boot/mac.py`) that reports the Mac from another machine.

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
from tests.test_mac_volume import mac_board
from wk.machine import Result

MACAB = REPO / "bench" / "mac-ab.sh"
MBP = REPO / "machines" / "mbp.conf"

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
    MACAB.read_text(), "    if b_restart_ready; then", "\n    log \"\" >&2")


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
    def test_the_real_firmware_default_on_tolken_names_one_of_its_installs(self):
        """Which of the two it names is a transient -- `wk boot mbp` arms the
        bench volume and the hand-back at the end of a job blesses the host
        install back -- so what is invariant is that the reading resolves to an
        install on that disk at all, which is what preflight compares against."""
        def wkmac(*args):
            cp = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "tolken",
                 "cd ~/Development/wk-tools && python3 lib/wkmac.py " + " ".join(args)],
                capture_output=True, text=True, timeout=60)
            return cp.stdout.strip()

        # The volume is only a volume from host mode: in bench mode it *is* the
        # root and is not mounted under /Volumes at all, so there is nothing to
        # ask about and the reading would compare a group against nothing.
        bench = wkmac("volume-group", "'/Volumes/WK Bench'")
        if not bench:
            self.skipTest("tolken answers in bench mode, where 'WK Bench' is /")
        host = wkmac("volume-group", "/")
        self.assertIn(wkmac("boot-volume").rsplit(":", 1)[-1], (bench, host))


class TestOnlyTheBuiltInDisplay(WkTest):
    """An external monitor changes the compositing, the refresh rate and which
    GPU the window lands on, and MotionMark's score is the area it draws."""

    def _check(self, answer):
        script = """. "$WK_ROOT/lib/common.sh"
MACHINE=fakemac
b_display() { printf 'builtin 1470x956'; }
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
        self.assertIn("not the builtin panel this machine declares", out)

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
        block = _lift_between(text, 'ck no "one display"', "firmware_default_is_bench")
        self.assertIn("no", block.lower())
        self.assertIn("--force crosses it", block)

    def test_force_does_not_cross_the_check_before_the_restart(self):
        """The second reading is seconds before the transition, and there is
        no number to save by crossing it."""
        script = """. "$WK_ROOT/lib/common.sh"
DRY=""; GO=restart; FORCE=1; WK_FORCE=1; export WK_FORCE
MACHINE=fakemac; VOLUME="WK Bench"
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
    """The expectation is one line of `machines/<machine>.conf`, and a
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
                "KIND=mac\nNODE_DRIVER=mac-volume\n"
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
        self.assertIn("declares no display", cp.stdout)
        self.assertIn("fakemac.conf", cp.stdout)

    def test_a_pinned_mode_gets_past_that_refusal(self):
        cp = self._plant('NODE_DISPLAY="builtin 1470x956"\n')
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertNotIn("NODE_DISPLAY", cp.stdout)
        self.assertIn("nothing on fakemac is readable right now", cp.stdout)


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

    def test_the_plant_passes_the_machines_own_declaration_and_nothing_else(self):
        """Read once through the driver (b_display), so a machine whose mode is
        not in its conf -- a guest's, which its target declares -- reaches the
        job the same way."""
        body = macab_func("phase_plant")
        self.assertIn("declared=$(b_display)", body)
        self.assertIn('WK_JOB_DISPLAY="$declared"', body)
        self.assertNotIn("$NODE_DISPLAY", body)


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
NODE_BENCH_SSH=fakemac-bench
. "$WK_ROOT/boot/mac-volume.sh"
"""

    def _driver(self, script, env=None):
        return bash(self.PRE + script, env=env)

    def test_it_is_probeable_off_the_mac(self):
        cp = self._driver('if b_probeable; then echo YES; else echo NO; fi')
        self.assertEqual(cp.stdout.strip(), "YES", cp.stdout + cp.stderr)

    def test_nothing_about_the_mac_is_stored_between_reads(self):
        """Every fact above is recomputed; the only file the driver keeps is
        the record of a person's arming."""
        text = (REPO / "lib" / "wk" / "boot" / "mac.py").read_text()
        self.assertNotIn("cache", text.lower())
        self.assertEqual(1, text.count('lead=("mac-record.sh",)'))



class TestTheMeasuredInstallIsReachedOnItsOwnNode(WkTest):
    """Two installs, two tailnet nodes. The benchmark one answers as
    NODE_BENCH_SSH while it measures and needs no password, where the host
    install stops for one at boot -- so a finished run used to be unreadable
    from the moment the hand-back rebooted until the host install was back,
    while the install holding it answered on the tailnet throughout."""

    PRE = """. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/boot/machines.sh"
NODE_NAME=mbp
NODE_SSH=fakemac
NODE_BENCH_SSH=fakemac-bench
NODE_VOLUME="WK Bench"
. "$WK_ROOT/boot/mac-volume.sh"
"""


    def _ssh_line(self, script="", env=None):
        """i_ssh (boot/machines.sh) with `ssh` stubbed: the command line the
        bench channel actually builds for this driver."""
        cp = bash(self.PRE + script
                  + 'ssh() { printf "%s\\n" "$*"; }\ni_ssh true\n', env=env)
        return cp.stdout.strip()

    def test_the_destination_is_the_ssh_config_alias_and_not_an_address(self):
        """dotfiles/ssh/config declares that install by name -- user `bench`,
        its own pinned host key under the alias -- so the name is the whole
        address and nothing resolves it to one."""
        self.assertTrue(self._ssh_line().endswith("fakemac-bench true"),
                        self._ssh_line())

    def test_it_neither_forces_root_nor_unpins_the_host_key(self):
        """`-l root` and an unpinned key are a written Pi image's shape: that
        system regenerates its host key on every write and has no other login.
        A personalised macOS install has one stable key and a `bench` account."""
        line = self._ssh_line()
        self.assertNotIn("-l root", line)
        self.assertNotIn("StrictHostKeyChecking", line)

    def test_wk_mac_bench_ssh_moves_the_destination(self):
        line = self._ssh_line(env={"WK_MAC_BENCH_SSH": "somewhere-else"})
        self.assertIn("somewhere-else", line)
        self.assertNotIn("fakemac-bench", line)

    def test_the_staging_root_and_the_home_answer_on_that_channel(self):
        cp = bash(self.PRE + 'MODE_CHANNEL=bench\n'
                  'printf "%s %s" "$(b_bench_root)" "$(b_bench_home)"')
        self.assertEqual("/var/wk /Users/bench", cp.stdout, cp.stdout + cp.stderr)

    def test_the_lane_reads_through_the_channel_and_probes_before_it_does(self):
        text = MACAB.read_text()
        self.assertIn("mac() {\n    r_ssh \"$@\"\n}", text)
        self.assertRegex(text, r"load_driver \"\$NODE_DRIVER\"[^\n]*\n\nb_probe",
                         "the lane reads before it knows which install answered")

    def test_the_lane_asks_the_probe_rather_than_re_reading_the_marker(self):
        """One reading of which mode it is in, and it is the one that chose the
        channel every other reading travels on. The volume's *own* marker, read
        from host mode at a /Volumes path, is a different question and stays."""
        self.assertNotIn("/etc/wk-image 2>/dev/null", MACAB.read_text())

    def test_no_refusal_is_left_that_names_bench_mode_as_unreadable(self):
        for fn in ("bench_root", "phase_status", "phase_collect"):
            with self.subTest(fn=fn):
                body = func_body(MACAB.read_text(), fn)
                self.assertNotIn("once the machine returns", body)
                self.assertNotIn("host-mode verb", body)


def driver_root(channel):
    """The staging root lib/wk/boot/mac.py resolves for mbp on `channel`, off a FakeMac."""
    fake, d = mac_board("mac-volume")
    if channel == "bench":
        fake.enter_bench()
    d.probe()
    return d.bench_root()


class TestCollectReadsARunFromBenchMode(WkTest):
    """`--collect` with the Mac still in bench mode. The whole of phase_collect
    runs against a stand-in for that install: the staging root is what the
    driver (lib/wk/boot/mac.py) resolves on that channel of a FakeMac, and the
    stub rewrites that prefix onto a directory here -- so both channels read
    one tree through one code path and the only difference is the prefix,
    which is the claim."""

    #  <scratch>/WK Bench - Data/private/var/wk  is the volume as host mode
    #  reaches it, and  /var/wk  is the same bytes as the install itself does.
    def _collect(self, channel, tsv):
        with temp_store() as store, scratch_dir() as tmp:
            data = tmp / "WK Bench - Data"
            vol = data / "private" / "var" / "wk"
            for rid in ("r1", "r2"):
                d = vol / "results" / rid
                d.mkdir(parents=True)
                (d / "env.json").write_text(json.dumps(
                    {"plan": "speedometer3", "workspace": "wk-bench",
                     "config": "mac-release-pgo", "wall_time_s": "60"}))
            (vol / "autorun.state").write_text(
                "job_stamp=20260908T000000Z\nphase=done\noutcome=ran\n")
            runs = vol / "ab" / "20260908T000000Z" / "runs.tsv"
            runs.parent.mkdir(parents=True)
            runs.write_text(tsv)
            task = store["path"] / "bench" / "20260908T000000Z-mbp-mac-ab"
            (task / "runs").mkdir(parents=True)
            script = """set -euo pipefail
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/bench.sh"
. "$WK_ROOT/boot/machines.sh"
NODE_NAME=mbp
NODE_SSH=fakemac
NODE_BENCH_SSH=fakemac-bench
NODE_VOLUME="WK Bench"
MACHINE=mbp
VOLUME="WK Bench"
MODE_CHANNEL=%s
MODE="bench perf-macos-tolken-2026-08"
BROOT=""
ROOT=%s
VOL=%s
b_bench_root() { printf '%%s' "$ROOT"; }
# ssh joins its arguments into one remote command line, and this is that shell.
r_ssh() {
    local c="$*"
    bash -c "${c//"$ROOT"/"$VOL"}"
}
bench_root() {%s}
mac() {%s}
mac_sh() { mac bash -lc "$(sh_quote "$*")"; }
bwk() {%s}
collect_runs_into_task() {%s}
phase_collect() {%s}
phase_collect
""" % (channel, shlex.quote(driver_root(channel)), shlex.quote(str(vol)),
       macab_func("bench_root"), macab_func("mac"), macab_func("bwk"),
       macab_func("collect_runs_into_task"), macab_func("phase_collect"))
            cp = bash(script, env={"WK_STORE": store["WK_STORE"]})
            recorded = sorted(d.name for d in (task / "runs").iterdir())
            return cp, recorded, cp.stdout + cp.stderr

    TSV = ("0\tA\tsid-a\tr0\tclean\tspeedometer3\n"
           "1\tA\tsid-a\tr1\tclean\tspeedometer3\n"
           "1\tB\tsid-b\tr2\tclean\tspeedometer3\n")

    def test_the_result_is_read_with_the_mac_still_in_bench_mode(self):
        cp, recorded, out = self._collect("bench", self.TSV)
        self.assertEqual(cp.returncode, 0, out)
        self.assertEqual(recorded, ["r1", "r2"], out)

    def test_the_same_collect_off_the_volume_in_host_mode_records_the_same_legs(self):
        """One implementation, two channels."""
        cp, recorded, out = self._collect("host", self.TSV)
        self.assertEqual(cp.returncode, 0, out)
        self.assertEqual(recorded, ["r1", "r2"], out)
        self.assertIn("private/var/wk", out)

    def test_the_summary_failing_over_there_does_not_lose_the_result(self):
        """`wk bench ab-summary` runs from the planted tree on the measured
        install; a stand-in has none, and the numbers are still recorded."""
        cp, recorded, out = self._collect("bench", self.TSV)
        self.assertIn("summary could not be produced", out)
        self.assertEqual(recorded, ["r1", "r2"], out)


class TestStatusCarriesTheLegs(WkTest):
    """`--status` is the command that answers "how far has it got", so the
    per-leg timings belong in it. Reaching past it with an ssh of one's own
    leaves the gap in place for the next person."""

    def _legs(self, started="2026-09-09T18:08:20Z", tsv=None, older=True):
        with scratch_dir() as root:
            (root / "job.json").write_text(json.dumps({
                "plans": ["speedometer3", "jetstream3", "motionmark"],
                "rounds": 2,
                "arms": [{"label": "A", "id": "sid-a"}, {"label": "B", "id": "sid-b"}]}))
            state = "job_stamp=20260909T180544Z\n"
            if started:
                state += "started_at=%s\n" % started
            state += "ok_speedometer3_A_0=1\nok_speedometer3_B_0=1\nok_speedometer3_A_1=1\n"
            (root / "autorun.state").write_text(state)
            legs = [("20260909T181045Z-speedometer3-sid-a", 91),
                    ("20260909T181218Z-speedometer3-sid-b", 92),
                    ("20260909T181351Z-speedometer3-sid-a", 31),
                    ("20260909T181500Z-motionmark-sid-b", None)]
            if older:
                legs.insert(0, ("20260101T000000Z-speedometer3-sid-a", 42))
            for name, wall in legs:
                d = root / "results" / name
                d.mkdir(parents=True)
                env = {"plan": name.split("-")[1]}
                if wall is not None:
                    env["wall_time_s"] = str(wall)
                (d / "env.json").write_text(json.dumps(env))
            runs = root / "ab" / "20260909T180544Z" / "runs.tsv"
            runs.parent.mkdir(parents=True)
            runs.write_text(tsv if tsv is not None else
                            "1\tA\tsid-a\t20260909T181351Z-speedometer3-sid-a\tclean\tspeedometer3\n")
            cp = bash('python3 "$WK_ROOT/lib/wkdata.py" ab-legs %s' % shlex.quote(str(root)))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            return cp.stdout

    def test_it_counts_what_ran_against_what_the_job_planned(self):
        """Two warmup legs, then rounds x plans x arms -- the warmup round runs
        the first plan only, one leg per arm."""
        self.assertIn("3 of 14 planned", self._legs())

    def test_an_older_experiments_results_are_not_this_jobs(self):
        """The volume keeps every result it has ever produced."""
        out = self._legs()
        self.assertNotIn("20260101", out)
        self.assertNotIn(" 42s", out)

    def test_a_leg_the_map_names_carries_its_round_and_arm(self):
        self.assertRegex(self._legs(), r"1\s+A\s+speedometer3\s+31s\s+clean")

    def test_the_warmup_legs_are_the_ones_before_any_measured_round(self):
        out = self._legs()
        self.assertRegex(out, r"warmup\s+A\s+speedometer3\s+91s")
        self.assertRegex(out, r"warmup\s+B\s+speedometer3\s+92s")

    def test_the_leg_in_flight_is_not_called_a_warmup(self):
        """A row reaches the map when its leg ends, so the running leg is never
        in it -- and calling it a warmup misreports which round is under way."""
        out = self._legs()
        self.assertRegex(out, r"-\s+B\s+motionmark\s+running")
        import re as _re
        self.assertEqual(2, len([l for l in out.splitlines()
                                 if _re.match(r"warmup\s+[AB]\s", l)]))

    def test_a_job_that_has_not_started_says_so_rather_than_listing_the_volume(self):
        out = self._legs(started="")
        self.assertIn("no leg of this job", out)

    def test_an_empty_warmup_directory_is_reported_and_not_passed_over(self):
        """The warmup round exists to carry a profile the measured rounds
        cannot take, so an empty capture directory is that round wasted --
        and it is the command's job to say so, not a person's to go and look."""
        self.assertRegex(self._legs(), r"warmup captures: none in .*/warmup")

    def test_a_capture_that_landed_is_named(self):
        with scratch_dir() as root:
            (root / "job.json").write_text(json.dumps({"plans": ["speedometer3"], "rounds": 1, "arms": []}))
            (root / "autorun.state").write_text("job_stamp=S\nstarted_at=2026-01-01T00:00:00Z\n")
            leg = root / "results" / "20260101T000100Z-speedometer3-sid-a"
            leg.mkdir(parents=True)
            (leg / "env.json").write_text(json.dumps({"plan": "speedometer3", "wall_time_s": "30"}))
            w = root / "ab" / "S" / "warmup"
            w.mkdir(parents=True)
            (w / "speedometer3-A.json.gz").write_bytes(b"")
            cp = bash('python3 "$WK_ROOT/lib/wkdata.py" ab-legs %s' % shlex.quote(str(root)))
            self.assertIn("warmup captures: speedometer3-A.json.gz", cp.stdout)

    def test_status_asks_for_them_through_the_one_sender(self):
        body = func_body(MACAB.read_text(), "phase_status")
        self.assertIn("mac_py wkdata.py ab-legs", body)
        self.assertIn('mac_wkmac() { mac_py wkmac.py "$@"; }', MACAB.read_text())


class TestTheWaitReadsBothNodes(WkTest):
    """Bench mode is a positive reading now, not the absence of one: the
    install answers as its own node while it measures."""

    def _wait(self, probe):
        script = ('. "$WK_ROOT/lib/common.sh"\n'
                  'sleep() { :; }\n'
                  'BOOT_BEFORE=1786800000\n'
                  'b_boot_id() { printf "%s" "${BOOT_NOW:-1786900000}"; }\n'
                  + probe
                  + 'MACHINE=mbp\nphase_wait() {%s}\nphase_wait 1\n'   # the limit is wall-clock time, and `sleep` is stubbed out above
                  % func_body(MACAB.read_text(), "phase_wait"))
        return bash(script)

    def test_a_bench_answer_is_reported_as_the_run(self):
        cp = self._wait('b_probe() { MODE="bench perf-x"; MODE_CHANNEL=bench; }\n')
        self.assertEqual("bench", cp.stdout, cp.stdout + cp.stderr)
        self.assertIn("BENCH mode (perf-x)", cp.stderr)

    def test_a_host_answer_on_a_new_boot_is_the_way_back(self):
        cp = self._wait('b_probe() { MODE=host; MODE_CHANNEL=host; }\n')
        self.assertEqual("host", cp.stdout, cp.stdout + cp.stderr)

    def test_a_host_answer_on_the_same_boot_never_rebooted(self):
        cp = self._wait('b_probe() { MODE=host; MODE_CHANNEL=host; }\n'
                        'BOOT_NOW=1786800000\n')
        self.assertEqual("noreboot", cp.stdout, cp.stdout + cp.stderr)

    def test_the_same_boot_is_asked_only_of_the_host_install(self):
        """In bench mode `kern.boottime` is another install's, so comparing it
        with the one taken before the restart says nothing."""
        cp = self._wait('b_probe() { MODE="bench perf-x"; MODE_CHANNEL=bench; }\n'
                        'BOOT_NOW=1786800000\n')
        self.assertEqual("bench", cp.stdout, cp.stdout + cp.stderr)

    def test_silence_on_both_nodes_is_bounded_and_says_so(self):
        cp = self._wait('b_probe() { MODE=unreachable; MODE_CHANNEL=none; }\n')
        self.assertEqual("silent", cp.stdout, cp.stdout + cp.stderr)
        self.assertIn("neither node", cp.stderr)


class TestHowAMachineGetsBackIsTheDriversAnswer(WkTest):
    """A one-shot is spent by the boot that took it, so a plain reboot returns
    the board. A firmware default is sticky: the Mac's next boot enters
    whatever the evidence above says it names, and what hands the machine back
    is the job it was armed for. Telling an operator to reboot a Mac in bench
    mode contradicts the `firmware_default=` line printed directly above it."""

    def _status_tail(self, arming):
        return bash('. "$WK_ROOT/lib/common.sh"\n'
                    'MACHINE=mbp\nBOOT_ARMING=%s\nMODE="bench perf-x"\n'
                    'BOOTED=now\nARMED_IMG=""\nNODE_ROOT=""\n'
                    'read_state() { :; }\nb_evidence() { echo "firmware_default=x"; }\n'
                    'machine_quiet_siblings() { printf "0 0"; }\n'
                    'cmd_status() {%s}\ncmd_status\n'
                    % (arming, func_body((REPO / "cmd" / "boot").read_text(), "cmd_status")))

    def test_a_sticky_firmware_default_is_not_undone_by_a_reboot(self):
        out = self._status_tail("command").stderr
        self.assertIn("hands the machine back when it ends", out)
        self.assertNotIn("a plain reboot returns it to host mode", out)

    def test_a_spent_one_shot_still_says_a_reboot_returns_it(self):
        out = self._status_tail("one-shot").stderr
        self.assertIn("a plain reboot returns it to host mode", out)

    def _arm_tail(self, extra):
        return bash('. "$WK_ROOT/lib/common.sh"\n'
                    'MACHINE=mbp\nIMAGE="WK Bench"\nARM_WATCHDOG=600\n' + extra
                    + 'log "  ---"\n')

    def test_the_arm_epilogue_offers_no_watchdog_where_none_is_written(self):
        """`--keep` cancels a self-return watchdog, and what says whether one
        was written is ARM_WATCHDOG -- this arming's own record. Not the
        driver's self-disarm: cmd/sysimage stages the watchdog onto every
        fleet card, including boards whose driver has no self-disarm at all,
        and keying on the driver told rpi5 it had none while its watchdog
        rebooted it mid-run (2026-09-10)."""
        text = (REPO / "cmd" / "boot").read_text()
        body = func_body(text, "cmd_arm")
        self.assertIn('if [ -n "$ARM_WATCHDOG" ]; then', body)
        self.assertNotIn("command -v b_self_disarm_sh", body)
        self.assertNotIn("/boot/firmware/wk-diag.txt", text,
                         "a medium-specific path where the driver has a verb")


class TestTheWatchdogBelongsToTheSystemThatCarriesIt(WkTest):
    """`wk boot <m> --keep` cancels a self-return watchdog, and whether the
    running system carries one is asked of that system (b_watchdog_present),
    not of its driver. A Mac's benchmark install carries none: what ends its
    run is the job it is running."""

    def _keep(self, extra):
        return bash('. "$WK_ROOT/lib/common.sh"\n'
                    'MACHINE=mbp\nDRY=\nread_state() { :; }\n'
                    'r_sudo() { echo TOUCHED; }\n' + extra
                    + 'cmd_keep() {%s}\ncmd_keep\n'
                    % func_body((REPO / "cmd" / "boot").read_text(), "cmd_keep"))

    def test_a_system_carrying_no_watchdog_has_nothing_to_claim(self):
        cp = self._keep('MODE="bench perf-x"\nb_watchdog_present() { return 1; }\n')
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("no self-return watchdog", cp.stderr)
        self.assertNotIn("TOUCHED", cp.stdout)

    def test_a_system_that_carries_one_is_claimed(self):
        cp = self._keep('MODE="bench perf-x"\nb_watchdog_present() { return 0; }\n')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("TOUCHED", cp.stdout)

    def test_the_system_is_not_asked_in_host_mode(self):
        cp = self._keep("MODE=host\nb_watchdog_present() { return 0; }\n")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("not in bench mode", cp.stderr)


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
        fake, d = mac_board("mac-volume")
        d.probe()
        d.reboot()
        asks = [(fn, p.get("WK_VERB")) for fn, name, p in fake.asked if name == "mac-priv.sh"]
        self.assertEqual(asks, [("m_ssh", "reboot")])
        self.assertTrue(fake.on_rescue() and fake.boots == 2, "the helper's reboot did not happen")
        onboard = [f.name for f in (REPO / "boot" / "onboard").iterdir() if "sudo -n" in f.read_text()]
        self.assertEqual(sorted(onboard), ["mac-own.sh", "mac-priv.sh"], "the helper is asked from one file")

    def test_preflight_refuses_a_mac_it_cannot_restart(self):
        for ready, want in ((0, "ok"), (1, "FAIL")):
            with self.subTest(ready=ready):
                cp = bash(
                    '. "$WK_ROOT/lib/common.sh"\n'
                    'PF_FAIL=0\n'
                    'ck() {%s}\n'
                    'b_restart_ready() { return %d; }\n'
                    'b_restart_detail() { printf "no boot helper on tolken"; }\n'
                    'MACHINE=mbp\n'
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


class TestTheTwoMachinesAreNamedApart(WkTest):
    """The machine being measured and the machine that *manages* it are one
    machine for a Mac booting its own second volume and two for a guest, whose
    manager is the Mac running it. The lane had one variable for both -- an ssh
    destination that was also every message's label -- so a driver whose target
    has no ssh destination at all could not start."""

    def test_the_lane_names_no_ssh_destination_of_its_own(self):
        text = MACAB.read_text()
        self.assertNotIn("$HOST", text)
        self.assertNotIn("mac_ssh ", text, "boot/machines.sh's by-destination ssh")

    def test_a_machine_with_no_ssh_destination_is_not_refused_up_front(self):
        """benchvm sets no NODE_SSH: a tart guest's address is in no ssh config
        and changes every boot, which is why its driver defines its own m_ssh."""
        text = MACAB.read_text()
        self.assertNotIn("sets no NODE_SSH", text)
        cp = bash('"$WK_ROOT/bench/mac-ab.sh" --machine benchvm --dry-run 2>&1',
                  env={"WK_SSH_TIMEOUT": "1"}, timeout=180)
        out = cp.stdout + cp.stderr
        self.assertIn("preflight for an unattended A/B on benchvm", out)
        self.assertNotIn("NODE_SSH", out)

    def test_the_measured_machine_is_reached_through_the_one_reader(self):
        """r_ssh, so the reader follows whichever install answered: the lane
        must own no ssh of its own and must not pin itself to host mode."""
        text = MACAB.read_text()
        self.assertIn("mac() {\n    r_ssh \"$@\"\n}", text)
        self.assertNotIn("m_ssh ", text, "a host-mode-only read of the measured machine")

    def test_the_manager_is_reached_through_the_drivers_hook(self):
        text = MACAB.read_text()
        self.assertIn('mgr() { b_manage "$@"; }', text)
        # The three things that belong to the manager and not to the measured
        # machine: its wk-tools, the builds it runs, and the patch they apply.
        self.assertIn("mgr_sh \"cd $(sh_quote \"$(mgr_tools)\") && ./wk $*\"", text)
        self.assertIn("mgr_sh \"ssh -o BatchMode=yes", func_body(text, "guest_sh"))
        self.assertIn('mgr "cat > /tmp/wk-ab.patch"', text)

    def test_each_driver_answers_both_halves(self):
        for rel, hooks in (
                ("boot/mac-volume.sh", ("b_manage", "b_manage_name", "b_manage_tools",
                                        "b_manage_prepare", "b_bench_home",
                                        "b_bench_put", "b_bench_put_file",
                                        "b_restart_ready", "b_restart_detail")),
                ("boot/mac-guest.sh", ("b_manage", "b_manage_name", "b_manage_tools",
                                       "b_manage_prepare", "b_bench_home",
                                       "b_bench_put", "b_bench_put_file",
                                       "b_restart_ready", "b_restart_detail", "b_display"))):
            # the driver's shim, or boot/machines.sh's, which asks the same class
            text = (REPO / rel).read_text() + (REPO / "boot" / "machines.sh").read_text()
            for hook in hooks:
                with self.subTest(driver=rel, hook=hook):
                    self.assertIn(f"\n{hook}() {{", "\n" + text)



class TestStagingIsTheDriversAndVerifiedHere(WkTest):
    """`wk bench stage` already delivers through b_bench_put / b_bench_put_file.
    A second delivery in the lane is a second thing to keep in step, and it was
    the one that had to know how a volume's path escapes."""

    def test_the_lane_delivers_through_the_driver_and_owns_no_transport(self):
        text = MACAB.read_text()
        self.assertIn("b_bench_put_file \"$1\" \"$2\"", func_body(text, "put_file"))
        self.assertIn('b_bench_put "$src" "$dst"', func_body(text, "put_tree"))
        for verb in ("tar -cf -", "rsync ", "scp "):
            for fn in ("put_file", "put_tree"):
                self.assertNotIn(verb, func_body(text, fn),
                                 f"{fn} carries a transport of its own ({verb})")

    def test_what_landed_is_still_judged_here(self):
        """A transport that wrote nothing exits 0, and a tree stale in one file
        looks right until the reboot, where nothing can report it."""
        self.assertIn("wc -c <", func_body(MACAB.read_text(), "put_file"))
        self.assertIn("treehash.py", func_body(MACAB.read_text(), "put_tree"))



class TestTheMeasuredHomeIsTheDrivers(WkTest):
    """It was derived from the volume's own path, which is a volume driver's
    fact and no other's."""

    def test_the_lane_derives_no_path_of_its_own(self):
        body = func_body(MACAB.read_text(), "bench_home")
        self.assertIn("b_bench_home", body)
        self.assertNotIn("Users/bench", body)



class TestAFailedNotifyCostsNothing(WkTest):
    """A notification that did not go out must not cost a measurement."""

    def _notify(self, notify_exit):
        script = """. "$WK_ROOT/lib/common.sh"
wk_notify() { echo 'wk-notify: no ntfy topic on this machine' >&2; return %d; }
notify() {%s}
notify "a headline" "a detail"
printf 'rc=%%s' "$?"
""" % (notify_exit, macab_func("notify"))
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

    def test_the_exclusions_actually_exclude(self):
        """Word splitting does not remove quotes: `--exclude '.git'` names a
        file that does not exist, and the transport carries the history it was
        meant to leave behind -- while the two digests still match, because the
        one on the far side is built for a shell, which does remove them."""
        with scratch_dir() as tmp:
            src, dst = tmp / "src", tmp / "dst"
            (src / ".git").mkdir(parents=True)
            (src / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
            (src / "__pycache__").mkdir()
            (src / "__pycache__" / "x.pyc").write_text("x")
            (src / "wk").write_text("#!/bin/sh\n")
            dst.mkdir()
            cp = bash('. "$WK_ROOT/lib/common.sh"; . "$WK_ROOT/lib/store.sh"\n'
                      '. "$WK_ROOT/lib/bench.sh"\n'
                      '# shellcheck disable=SC2046\n'
                      f'tar -cf - $(bench_put_excludes) -C {shlex.quote(str(src))} . '
                      f'| tar -xf - -C {shlex.quote(str(dst))}\n',
                      env={"WK_STORE": str(tmp / "store")})
            self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
            landed = sorted(p.name for p in dst.iterdir())
            self.assertEqual(["wk"], landed,
                             f"the excluded names crossed anyway: {landed}")

    def test_the_exclusion_list_holds_no_metacharacter(self):
        """What makes the unquoted list above safe, checked rather than said."""
        cp = bash('. "$WK_ROOT/lib/common.sh"; . "$WK_ROOT/lib/store.sh"\n'
                  '. "$WK_ROOT/lib/bench.sh"; printf "%s" "$BENCH_PUT_SKIP"')
        names = cp.stdout.split()
        self.assertTrue(names, cp.stdout + cp.stderr)
        for name in names:
            self.assertRegex(name, r"^[A-Za-z0-9_.-]+$", name)

    def test_both_sides_exclude_the_same_names(self):
        """One list, read twice. Two lists is two file sets and two digests of
        different things, which reads as a corrupted tree on every plant."""
        text = MACAB.read_text()
        self.assertIn("BENCH_PUT_SKIP=", (REPO / "lib" / "bench.sh").read_text())
        body = func_body(text, "put_tree")
        self.assertEqual(1, body.count("$BENCH_PUT_SKIP"),
                         "the far side's list is the driver's own, from the same variable")
        for driver in ("mac-volume", "mac-guest"):
            self.assertIn('${BENCH_PUT_SKIP:?', (REPO / "boot" / ("%s.sh" % driver)).read_text())

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
        fake, d = mac_board("mac-volume")
        d.probe()
        fake.script = lambda name, p, input: Result(0, self.SYSCTL + "\n")
        self.assertEqual("1788835009", d.boot_id())

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
