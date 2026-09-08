"""What `wk status` may claim about a build that has gone quiet.

A full-LTO link writes nothing to the log for many minutes, so silence is not
death, and this machine's compiler count cannot say otherwise: a container
build's compilers reach the process table through `podman exec` and name their
workspace only in their cgroup, and a macOS guest's run in another kernel. So
`build_live` (lib/detach.sh) reads the log and nothing else, cmd/status reports
`silent` rather than `stalled` for a quiet `running` record, and `stalled`
stays what cmd/build and cmd/test write when a watchdog killed the job.

The exit codes that follow: `silent` is busy (2), so `wk status --wait` waits
through it; `stalled` is 3, and `--wait` returns.

Run: python3 -m unittest tests.test_build_liveness -v
"""
import json
import os
import shutil
import time
import unittest

from tests.support import REPO, WkTest, bash, rand_suffix, run, stub_path

STATUS_VIEW = REPO / "lib" / "status-view.py"

# The stub `ssh` tests/test_fleet_walk.py uses for a fleet walk with no fleet:
# it runs the probe locally instead of reaching a machine.
ANSWERING_SSH = '''#!/bin/sh
for last; do :; done
exec bash -c "$last"
'''

REAL_PS = shutil.which("ps") or "/usr/bin/ps"

# Busy on the one reading lib/detach.sh takes, the real `ps` for anything else.
BUSY_PS = f'''#!/bin/sh
if [ "$*" = "-A -o pcpu=,comm=" ]; then
    printf ' 99.5 cc1plus\\n 98.0 cc1plus\\n 97.0 ld\\n  0.1 bash\\n'
    exit 0
fi
exec {REAL_PS} "$@"
'''

STUB_PS = '''ps() {
cat <<'PSOUT'
 99.5 cc1plus
 98.0 cc1plus
 97.0 ld
  0.1 bash
PSOUT
}
'''


class TestBuildLiveReadsTheLogAndNotTheProcessTable(WkTest):
    """lib/detach.sh's `build_live`: `state=running` plus a log that moved
    within WK_STALL_SECONDS. A machine mid-link is the case that made
    someone want the process count in here, so it is stubbed present in
    every one of these and changes no answer."""

    def _live(self, state, stale, after="", stall=""):
        sf = self.tmp / "build.status"
        log = self.tmp / "build.log"
        stamp = 'touch -t 202001010000 "%s"' % log if stale else ""
        cp = bash(f'''
. "{REPO}/lib/detach.sh"
{STUB_PS}
status_write "{sf}" state={state}
: > "{log}"
{stamp}
{after}
{stall}build_live "{sf}" "{log}" && echo LIVE || echo NOTLIVE
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout.strip()

    def test_the_reading_is_available_and_says_three(self):
        cp = bash(f'. "{REPO}/lib/detach.sh"\n{STUB_PS}\nbuild_processes')
        self.assertEqual(cp.stdout.strip(), "3", cp.stdout + cp.stderr)

    def test_a_fresh_log_is_live(self):
        self.assertEqual(self._live("running", False), "LIVE")

    def test_a_stale_log_is_not_live_even_with_three_compilers_running(self):
        self.assertEqual(self._live("running", True), "NOTLIVE")

    def test_a_finished_record_is_not_live_even_with_a_fresh_log(self):
        self.assertEqual(self._live("ok", False), "NOTLIVE")

    def test_the_silence_threshold_is_wk_stall_seconds(self):
        # Two seconds of silence: past a threshold of one, not past the default.
        self.assertEqual(
            self._live("running", False, after="sleep 2",
                       stall="WK_STALL_SECONDS=1 "), "NOTLIVE")
        self.assertEqual(self._live("running", False, after="sleep 2"), "LIVE")


class _FakeWalk(WkTest):
    """One `wk status` walk over faked workspaces, with no hardware: the
    scaffolding tests/test_owed_cli_exit_code.py uses -- WK_TARGET=remote
    with an answering stub `ssh`, an empty WK_MACHINES_DIR so the device
    walk finds nothing, a `.wk-ready` marker per workspace under a scratch
    WK_REMOTE_ROOT so each reads as `present` (a `creating` workspace bumps
    the exit code to 4 and would swamp every build state), and a
    build.status under the scratch XDG_STATE_HOME where targets/remote.sh's
    per-target store resolves."""

    def setUp(self):
        super().setUp()
        self.xdg = self.tmp / "xdg"
        self.remote_root = self.tmp / "remote-root"
        self.machdir = self.tmp / "machines"
        self.machdir.mkdir(parents=True)

    def workspace(self, fields, log=None, log_age=0):
        """A faked workspace named for its state; returns its name."""
        name = f"wsl-{fields.get('state', 'none')}-{rand_suffix()}"
        wsdir = self.xdg / "wk" / "remote" / "remote" / "ws" / name
        wsdir.mkdir(parents=True)
        if log == "missing":
            fields["log"] = str(wsdir / "build.log")
        elif log is not None:
            logf = wsdir / "build.log"
            logf.write_text(log)
            if log_age:
                past = time.time() - log_age
                os.utime(logf, (past, past))
            fields["log"] = str(logf)
        (wsdir / "build.status").write_text(
            "".join(f"{k}={v}\n" for k, v in fields.items()))
        (self.remote_root / "ws" / name).mkdir(parents=True)
        (self.remote_root / "ws" / name / ".wk-ready").touch()
        return name

    def walk(self, *args, ps=None, timeout=90, env=None):
        stubs = {"ssh": ANSWERING_SSH}
        if ps:
            stubs["ps"] = ps
        with stub_path(stubs) as binp:
            e = {
                "XDG_STATE_HOME": str(self.xdg),
                "WK_REMOTE_ROOT": str(self.remote_root),
                "WK_MACHINES_DIR": str(self.machdir),
                "WK_TARGET": "remote",
                "WK_REMOTE_HOST": "fake-reachable-machine",
                "PATH": f"{binp}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            }
            e.update(env or {})
            return run("status", *args, env=e, timeout=timeout)

    def build_sub(self, cp, name):
        """The build record `wk status --records` wrote for one workspace."""
        for line in cp.stdout.splitlines():
            if not line.startswith("{"):
                continue
            rec = json.loads(line)
            if rec.get("kind") == "workspace" and rec.get("name") == name:
                for sub in rec.get("subs", []):
                    if sub.get("kind") == "build":
                        return rec, sub
        raise AssertionError(f"no build record for {name}:\n{cp.stdout}")

    def notes(self, rec):
        return "\n".join(n["text"] for n in rec.get("notes", []))


class TestAQuietRunningBuildIsSilentNotStalled(_FakeWalk):
    def test_a_quiet_running_record_reads_silent_and_busy(self):
        name = self.workspace({"state": "running", "config": "jsc-release"},
                              log="[1/4200] cc\n", log_age=4000)
        cp = self.walk("--records")
        rec, sub = self.build_sub(cp, name)
        self.assertEqual(sub["state"], "silent", cp.stdout)
        self.assertEqual(cp.returncode, 2, cp.stdout)
        self.assertIn("no log output for", self.notes(rec))

    def test_the_note_says_it_keeps_waiting_and_whose_count_it_quoted(self):
        name = self.workspace({"state": "running", "config": "jsc-release"},
                              log="[1/4200] cc\n", log_age=4000)
        rec, _ = self.build_sub(self.walk("--records"), name)
        text = self.notes(rec)
        self.assertIn("--wait", text)
        self.assertIn("this machine's, not this", text)

    def test_a_moving_log_reads_running(self):
        name = self.workspace({"state": "running", "config": "jsc-release"},
                              log="[9/4200] cc\n")
        cp = self.walk("--records")
        _, sub = self.build_sub(cp, name)
        self.assertEqual(sub["state"], "running", cp.stdout)
        self.assertEqual(cp.returncode, 2, cp.stdout)

    def test_three_compilers_running_change_the_note_and_not_the_state(self):
        """The count is reported where a person is deciding, and is not the
        verdict: with `ps` answering as a machine mid-link, the state is the
        same `silent` the log alone gives."""
        name = self.workspace({"state": "running", "config": "jsc-release"},
                              log="[1/4200] cc\n", log_age=4000)
        cp = self.walk("--records", ps=BUSY_PS)
        rec, sub = self.build_sub(cp, name)
        self.assertEqual(sub["state"], "silent", cp.stdout)
        self.assertEqual(cp.returncode, 2, cp.stdout)
        self.assertIn("compiler/linker process(es)", self.notes(rec))

    def test_no_compilers_at_all_is_still_silent_not_stalled(self):
        idle_ps = f'''#!/bin/sh
if [ "$*" = "-A -o pcpu=,comm=" ]; then printf '  0.1 bash\\n'; exit 0; fi
exec {REAL_PS} "$@"
'''
        name = self.workspace({"state": "running", "config": "jsc-release"},
                              log="[1/4200] cc\n", log_age=4000)
        cp = self.walk("--records", ps=idle_ps)
        rec, sub = self.build_sub(cp, name)
        self.assertEqual(sub["state"], "silent", cp.stdout)
        self.assertEqual(cp.returncode, 2, cp.stdout)
        self.assertIn("nothing here is compiling or linking", self.notes(rec))

    def test_a_running_record_whose_log_is_gone_is_silent_and_says_so(self):
        name = self.workspace({"state": "running", "config": "jsc-release"},
                              log="missing")
        cp = self.walk("--records")
        rec, sub = self.build_sub(cp, name)
        self.assertEqual(sub["state"], "silent", cp.stdout)
        self.assertEqual(cp.returncode, 2, cp.stdout)
        self.assertIn("there is no log at", self.notes(rec))


class TestTheStalledStateIsOnlyAKill(_FakeWalk):
    def test_a_written_stalled_record_stays_stalled_and_exits_3(self):
        name = self.workspace({"state": "stalled", "exit": "124"},
                              log="[1/4200] cc\n", log_age=4000)
        cp = self.walk("--records")
        rec, sub = self.build_sub(cp, name)
        self.assertEqual(sub["state"], "stalled", cp.stdout)
        self.assertEqual(cp.returncode, 3, cp.stdout)
        self.assertIn("killed after no output", self.notes(rec))

    def test_nothing_in_cmd_status_manufactures_stalled(self):
        text = (REPO / "cmd" / "status").read_text()
        self.assertNotIn("state=stalled", text)
        self.assertIn("build_live", text)


class TestOneExitCodePerRecordedState(_FakeWalk):
    def _code(self, fields, **kw):
        self.workspace(fields, **kw)
        return self.walk("--records").returncode

    def test_ok_is_0(self):
        self.assertEqual(self._code({"state": "ok", "exit": "0"}), 0)

    def test_failed_is_1(self):
        self.assertEqual(
            self._code({"state": "failed", "exit": "1"}, log="error: no\n"), 1)

    def test_running_is_2(self):
        self.assertEqual(
            self._code({"state": "running"}, log="[9/9] cc\n"), 2)

    def test_silent_is_2(self):
        self.assertEqual(
            self._code({"state": "running"}, log="[1/9] cc\n", log_age=4000), 2)

    def test_stalled_is_3(self):
        self.assertEqual(
            self._code({"state": "stalled", "exit": "124"}, log="x\n"), 3)

    def test_oom_is_3(self):
        self.assertEqual(
            self._code({"state": "oom", "exit": "137", "peak_mb": "64000"},
                       log="x\n"), 3)


class TestWaitWaitsThroughSilence(_FakeWalk):
    """`--wait` waits on exit 2 and on nothing else (cmd/status's one
    `[ "$_rc" = 2 ] || break`), so this drives the states rather than the
    loop: a silent build blocks until --timeout, a killed one returns."""

    def _wait(self, fields, **kw):
        self.workspace(fields, **kw)
        t0 = time.time()
        cp = self.walk("--wait", "--timeout=2",
                       env={"WK_WAIT_INTERVAL": "1"}, timeout=180)
        return cp, time.time() - t0

    def test_a_silent_build_is_waited_through_until_the_timeout(self):
        cp, _ = self._wait({"state": "running"},
                           log="[1/4200] cc\n", log_age=4000)
        self.assertIn("wk status says busy", cp.stdout)
        self.assertIn("still busy after 2s", cp.stdout)
        self.assertEqual(cp.returncode, 2, cp.stdout)

    def test_a_stalled_build_ends_the_wait_at_once(self):
        cp, _ = self._wait({"state": "stalled", "exit": "124"}, log="x\n")
        self.assertNotIn("wk status says busy", cp.stdout)
        self.assertEqual(cp.returncode, 3, cp.stdout)

    def test_a_finished_build_ends_the_wait_at_once(self):
        cp, _ = self._wait({"state": "ok", "exit": "0"})
        self.assertNotIn("wk status says busy", cp.stdout)
        self.assertEqual(cp.returncode, 0, cp.stdout)


class TestTheViewCallsSilenceBusy(unittest.TestCase):
    def test_severity_of_silent_is_busy(self):
        cp = bash(f'''python3 - <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("sv", "{STATUS_VIEW}")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(m.severity("silent"), m.severity("stalled"))
PY''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "busy bad", cp.stdout + cp.stderr)

    def test_exit_2_is_explained_the_same_way_in_both_files(self):
        """The page prints the exit code in cmd/status's own words, so the
        two say the same thing about a silent build or one of them is
        wrong."""
        header = (REPO / "cmd" / "status").read_text()
        view = STATUS_VIEW.read_text()
        for text in ("a build is running or silent",
                     "a build stalled and was killed by its own watchdog"):
            self.assertIn(text, header, text)
            self.assertIn(text, view, text)


if __name__ == "__main__":
    unittest.main()
