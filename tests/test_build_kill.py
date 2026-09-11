"""Stopping a build, and refusing to start a second one.

^C at the terminal reaches the driver and nothing else: the container path is
`podman exec` behind an `ssh -t` with no signal proxy, the remote path an ssh
the far side notices only when it writes. So the build announces its pid down
its log (`wk: build pid <n>`, build/build-in-target.sh), the driver records it
in the task record, and one implementation -- `job_kill` (lib/watchdog.sh) --
signals it through `t_exec`, whichever machine that is. Descendants first,
because ninja's children reparent to init the moment their parent is gone, and
no pattern kill: a wkdev container shares the host's PID namespace, so
`pkill -f <build dir>` in one would match another workspace's build. That log
is bind-mounted read-write into the workspace, so the pid down it is the
workspace's own claim: it is adopted, and later signalled, only while its
command line inside the target matches the pattern the job declared
(`pid_match`, lib/watchdog.sh job_pid_adopt).

`wk build <ws> --kill` and `wk test <ws> --kill` are that, from the outside,
and every refusal names one of them: a second build is refused at once (the
lock is not waited on for an hour) and so is a build in a workspace that
already has a job holding its checkout.

Run: python3 -m unittest tests.test_build_kill -v
"""
import os
import re
import shlex
import signal
import socket
import subprocess
import time
import unittest
from pathlib import Path

from tests.support import REPO, WkTest, bash, fake_workspace, rand_suffix, run

PRELUDE = f'''set -uo pipefail
WK_ROOT="{REPO}"
. "{REPO}/lib/common.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/lib/target.sh"
. "{REPO}/lib/watchdog.sh"
'''

# The record the fake workspace's own `local` target resolves to.
def store_of(ws):
    return Path(ws.state_dir) / "wk"


# What the pid's command line inside the target must match, recorded by
# job_pid_adopt with the pid itself: nothing signals a pid without it.
TARGET_PID_MATCH = "*build-in-target.sh*"
TARGET_PID_ARGS = "bash /opt/wk-tools/build/build-in-target.sh --release"


def begin(store, kind="build", name="selftest-ws", pid=None, log="/dev/null",
          where="here", kill=None, plan="compile jsc-release",
          pid_match=TARGET_PID_MATCH):
    """One record through lib/task.sh, as a command writes it."""
    kill = kill or f"wk {kind} {name} --kill"
    script = PRELUDE + f'''
d=$(task_begin {kind} {where} {name} '{kill}' '{log}' '{plan}')
task_step "$d" 1
''' + (f"task_set \"$d\" pid_match '{pid_match}'\n" if where == "target" else "") \
       + (f'task_pid "$d" {pid}\n' if pid else "") + 'printf "%s" "$d"\n'
    cp = bash(script, env={"WK_STORE": str(store)})
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return cp.stdout.strip()


def field(d, name):
    p = Path(d) / name
    return p.read_text().strip() if p.exists() else ""


def spawn_orphan(marker=None):
    """A process nothing is waiting on -- `nohup ... &` from a shell that then
    exits, so it is reparented and reaped the moment it dies. A pid whose
    parent never reaps it stays in the table as a zombie and `kill -0` still
    answers yes, which is a property of the test and not of the job. Returns
    its pid; with <marker> it leaves a child of its own, whose pid goes there.
    """
    inner = "exec sleep 300"
    if marker:
        inner = "sleep 300 & echo $! > %s; exec sleep 300" % marker
    cp = subprocess.run(
        ["bash", "-c", "nohup bash -c %s >/dev/null 2>&1 & echo $!" % shlex.quote(inner)],
        capture_output=True, text=True, check=True)
    pid = int(cp.stdout.strip())
    if marker:
        for _ in range(50):
            if pathlib_exists(marker):
                break
            time.sleep(0.1)
    return pid


def pathlib_exists(p):
    return Path(p).exists()


def reap(pid):
    if alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# A target whose pid never stops answering, and whose command line is the job's.
STUB_ALIVE_EXEC = ('t_exec() { shift; case "$*" in "ps -o args="*)'
                   ' printf \'%s\\n\' "' + TARGET_PID_ARGS + '" ;; esac; return 0; }\n')


def stub_ps(args):
    """A t_exec that answers `ps -o args=` with <args>, as the adopt check asks."""
    return ('t_exec() { shift; case "$*" in "ps -o args="*)'
            " printf '%%s\\n' '%s' ;; esac; return 0; }\n" % args)


def stub_target(execs, answer="dead", args=TARGET_PID_ARGS):
    """A t_exec that records its argv in <execs>, answers `kill -0` as asked,
    and reports <args> as the pid's command line inside the target."""
    return f'''
t_exec() {{
    local name="$1"; shift
    printf '%s\\n' "$*" >> "{execs}"
    case "$*" in
        "kill -0 "*) [ "{answer}" = alive ] ;;
        "ps -o args="*) printf '%s\\n' "{args}" ;;
        *) return 0 ;;
    esac
}}
'''


class TestJobKillStopsWhatTheJobStarted(WkTest):
    """`job_kill` with the record's `where` at `here`: the local process tree,
    the same walk `run_watched`'s interrupt hook uses."""

    def test_a_recorded_pid_and_its_children_are_gone_and_the_record_converges(self):
        store = self.tmp / "store"
        marker = self.tmp / "kid"
        pid = spawn_orphan(marker=marker)
        kid = int(marker.read_text().strip())
        try:
            d = begin(store, pid=pid)
            cp = bash(PRELUDE + f'job_kill selftest-ws "{d}" cancelled && echo GONE || echo LEFT',
                      env={"WK_STORE": str(store)}, timeout=60)
            self.assertEqual(cp.stdout.strip().splitlines()[-1], "GONE",
                             cp.stdout + cp.stderr)
            self.assertEqual(field(d, "exit"), "cancelled")
            self.assertFalse(alive(kid), "the child outlived the job it belonged to")
            self.assertFalse(alive(pid))
        finally:
            reap(kid)
            reap(pid)

    def test_the_drivers_own_pid_is_converged_and_not_waited_for(self):
        """Until the job announces its pid the record holds the driver's own
        (task_begin), and ^C arrives exactly then: signalling this shell would
        only wait out the kill bound on the process that is already leaving."""
        store = self.tmp / "store"
        cp = bash(PRELUDE + '\nd=$(task_begin build here selftest-ws \'wk build selftest-ws --kill\' /dev/null compile)\njob_kill selftest-ws "$d" cancelled && echo GONE || echo LEFT\nprintf \'exit=%s\\n\' "$(task_field "$d" exit)"\n',
                  env={"WK_STORE": str(store)}, timeout=30)
        self.assertIn("GONE", cp.stdout, cp.stdout + cp.stderr)
        self.assertIn("exit=cancelled", cp.stdout, cp.stdout + cp.stderr)

    def test_a_record_with_no_pid_yet_is_just_converged(self):
        store = self.tmp / "store"
        d = begin(store)
        # task_begin records this shell's own pid for a `here` job, and that
        # shell is gone: nothing to signal, and the record still ends.
        cp = bash(PRELUDE + f'job_kill selftest-ws "{d}" cancelled && echo GONE || echo LEFT',
                  env={"WK_STORE": str(store)}, timeout=60)
        self.assertEqual(cp.stdout.strip().splitlines()[-1], "GONE", cp.stdout)
        self.assertEqual(field(d, "exit"), "cancelled")


class TestJobKillReachesTheMachineThatRuns(WkTest):
    """`where=target`: the pid belongs to the container or the far machine, so
    every signal and every liveness question goes through `t_exec`."""

    def test_the_term_goes_through_t_exec_with_the_descendant_walk(self):
        store = self.tmp / "store"
        d = begin(store, where="target", pid=4242)
        cp = bash(PRELUDE + stub_target(self.tmp / "execs") +
                  f'job_kill selftest-ws "{d}" cancelled && echo GONE || echo LEFT',
                  env={"WK_STORE": str(store)}, timeout=60)
        self.assertEqual(cp.stdout.strip().splitlines()[-1], "GONE", cp.stdout + cp.stderr)
        execs = (self.tmp / "execs").read_text()
        self.assertIn("_watched_descendants 4242", execs,
                      "the far side gets the same descendants-first walk")
        self.assertIn("kill -TERM", execs)
        self.assertNotIn("pkill", execs,
                         "a container shares the host's PID namespace, so no pattern kill")
        self.assertEqual(field(d, "exit"), "cancelled")

    def test_one_that_outlives_term_and_kill_is_reported_not_claimed_stopped(self):
        store = self.tmp / "store"
        d = begin(store, where="target", pid=4242)
        cp = bash(PRELUDE + stub_target(self.tmp / "execs", answer="alive") +
                  f'job_kill selftest-ws "{d}" cancelled && echo GONE || echo LEFT',
                  env={"WK_STORE": str(store), "WK_KILL_WAIT": "1"}, timeout=60)
        self.assertEqual(cp.stdout.strip().splitlines()[-1], "LEFT", cp.stdout + cp.stderr)
        self.assertIn("kill -KILL", (self.tmp / "execs").read_text())
        self.assertEqual(field(d, "exit"), "cancelled",
                         "the record still converges: nothing is left saying running")


    def test_a_kill_stays_cancelled_when_the_driver_it_stopped_ends_too(self):
        """The driver survives the job it started: its `run_watched` returns
        non-zero and it ends the record with that failure. The kill got there
        first, and the first verdict is the one the record keeps."""
        store = self.tmp / "store"
        d = begin(store, where="target", pid=4242)
        cp = bash(PRELUDE + stub_target(self.tmp / "execs") + f'''
( while [ "$(task_field "{d}" exit)" != cancelled ]; do sleep 0.2; done
  task_end "{d}" 1 ) &
job_kill selftest-ws "{d}" cancelled
wait
''', env={"WK_STORE": str(store)}, timeout=60)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(field(d, "exit"), "cancelled")


class TestJobStopHasOneExitCodePerOutcome(WkTest):
    def _stop(self, store, kind="build", env=None):
        e = {"WK_STORE": str(store)}
        e.update(env or {})
        return bash(PRELUDE + f'''
t_exec() {{ shift; case "$*" in
    "kill -0 "*) return 1 ;;
    "ps -o args="*) printf '%s\\n' "{TARGET_PID_ARGS}" ;;
esac; return 0; }}
job_stop selftest-ws {kind}
''', env=e, timeout=60)

    def test_nothing_running_is_2_and_says_so(self):
        cp = self._stop(self.tmp / "store")
        self.assertEqual(cp.returncode, 2, cp.stdout + cp.stderr)
        self.assertIn("no build is running", cp.stdout + cp.stderr)

    def test_a_stopped_job_is_0_and_names_what_it_stopped(self):
        store = self.tmp / "store"
        pid = spawn_orphan()
        try:
            begin(store, pid=pid)
            cp = self._stop(store)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("stopping the build in 'selftest-ws'", cp.stdout + cp.stderr)
            self.assertIn("cancelled", cp.stdout + cp.stderr)
        finally:
            reap(pid)

    def test_one_that_outlives_the_kill_is_1(self):
        """A far side that keeps answering `kill -0`: the record still ends,
        and the caller is told rather than shown a success."""
        store = self.tmp / "store"
        begin(store, where="target", pid=4242)
        cp = bash(PRELUDE + STUB_ALIVE_EXEC + "job_stop selftest-ws build\n",
                  env={"WK_STORE": str(store), "WK_KILL_WAIT": "1"}, timeout=60)
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)


class TestThePidComesBackDownTheLog(WkTest):
    """The one channel that reaches the driver from every target kind."""

    def test_job_pid_watch_records_the_announced_pid_and_flips_where(self):
        store = self.tmp / "store"
        log = self.tmp / "build.log"
        log.write_text("cmake -G Ninja\nwk: build pid 8123\n[1/9] cc\n")
        d = begin(store, log=str(log))
        cp = bash(PRELUDE + stub_ps(TARGET_PID_ARGS) +
                  f'job_pid_watch "{d}" "{log}" build "{TARGET_PID_MATCH}"',
                  env={"WK_STORE": str(store)}, timeout=60)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(field(d, "pid"), "8123")
        self.assertEqual(field(d, "pid_match"), TARGET_PID_MATCH,
                         "the pattern is recorded with the pid, or nothing can check it later")
        self.assertEqual(field(d, "where"), "target",
                         "until the pid is known the record's pid is the driver's own")

    def test_a_pid_whose_command_line_is_not_the_job_is_not_adopted(self):
        """The log is bind-mounted read-write into the workspace, so the pid
        down it is the workspace's claim: a container shares the host's PID
        namespace, and an unchecked pid is a signal at another workspace's
        build. The record keeps the driver's own pid, so nothing signals it."""
        store = self.tmp / "store"
        log = self.tmp / "build.log"
        log.write_text("wk: build pid 1\n")
        d = begin(store, log=str(log))
        cp = bash(PRELUDE + stub_ps("/sbin/init") +
                  f'job_pid_watch "{d}" "{log}" build "{TARGET_PID_MATCH}" && echo TOOK || echo REFUSED',
                  env={"WK_STORE": str(store)}, timeout=60)
        self.assertEqual(cp.stdout.strip().splitlines()[-1], "REFUSED", cp.stdout + cp.stderr)
        self.assertIn("/sbin/init", cp.stdout + cp.stderr)
        self.assertNotEqual(field(d, "pid"), "1")
        self.assertEqual(field(d, "pid_match"), "")

    def test_nothing_is_signalled_at_a_pid_that_is_not_the_job(self):
        """The check is at the signal too, not only at adoption: a pid can be
        recycled between the two."""
        store = self.tmp / "store"
        d = begin(store, where="target", pid=4242)
        cp = bash(PRELUDE + stub_target(self.tmp / "execs", answer="alive", args="/usr/bin/sshd -D") +
                  f'job_kill selftest-ws "{d}" cancelled && echo GONE || echo LEFT',
                  env={"WK_STORE": str(store), "WK_KILL_WAIT": "1"}, timeout=60)
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("refusing to send", cp.stdout + cp.stderr)
        self.assertNotIn("kill -TERM", (self.tmp / "execs").read_text())

    def test_a_target_record_with_no_pattern_at_all_is_a_refusal(self):
        store = self.tmp / "store"
        d = begin(store, where="target", pid=4242)
        (Path(d) / "pid_match").unlink()
        cp = bash(PRELUDE + stub_target(self.tmp / "execs", answer="alive") +
                  f'job_kill selftest-ws "{d}" cancelled',
                  env={"WK_STORE": str(store), "WK_KILL_WAIT": "1"}, timeout=60)
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("job_pid_adopt", cp.stdout + cp.stderr)
        self.assertFalse((self.tmp / "execs").exists(),
                         "it refused before asking the target anything")

    def test_build_in_target_announces_it_before_the_exec(self):
        text = (REPO / "build" / "build-in-target.sh").read_text()
        pid_line = text.index('echo "wk: build pid $$"')
        self.assertLess(pid_line, text.index("guard_exec"),
                        "the pid has to be out before the exec that replaces this shell")
        self.assertIn("WK_DRY_RUN", text[:pid_line],
                      "a dry run exits above it, so it announces nothing")


class TestThePatternsCoverEveryShapeTheJobTakes(WkTest):
    """The pid's command line is matched against the patterns its job declared
    (`pid_match`). They are a space-separated list matched one at a time,
    because `|` inside an expansion is alternation to neither `case` nor
    `[[`; and each call site's list has to cover every shape its own pid
    takes -- build-in-target.sh execs the port's build script through
    guard_exec, so that one pid is the driver before the exec and
    `Tools/Scripts/build-*` after it."""

    def _declared(self, path, label):
        m = re.search(r"job_pid_watch [^\n]*? %s '([^'\n]*)'" % label,
                      (REPO / path).read_text())
        self.assertIsNotNone(m, f"no job_pid_watch for {label} in {path}")
        return m.group(1)

    def _matches(self, args, want):
        cp = bash(PRELUDE + "_job_pid_matches %s %s && echo MATCH || echo NOPE"
                  % (shlex.quote(args), shlex.quote(want)), timeout=30)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout.strip().splitlines()[-1] == "MATCH"

    def test_a_builds_two_shapes_both_match_and_nothing_else_does(self):
        want = self._declared("cmd/build", "build")
        for args in ("env WK_JOBS=8 /opt/wk-tools/build/build-in-target.sh --release",
                     "Tools/Scripts/build-webkit --release --export-compile-commands",
                     "linux32 Tools/Scripts/build-jsc --release"):
            self.assertTrue(self._matches(args, want), args)
        for args in ("/sbin/init", "/usr/bin/sshd -D", "bash -lc sleep 300"):
            self.assertFalse(self._matches(args, want), args)

    def test_a_test_runs_suite_matches_and_another_shell_does_not(self):
        want = self._declared("cmd/test", "test")
        self.assertTrue(self._matches(
            'bash -lc echo "wk: test pid $$" >&2 cd /src/WebKit && nice -n 10 '
            'Tools/Scripts/run-javascriptcore-tests --release wpe', want))
        self.assertTrue(self._matches(
            "bash -lc cd /src/WebKit && Tools/Scripts/run-webkit-tests --release", want))
        self.assertFalse(self._matches("bash -lc sleep 300", want))

    def test_the_image_builds_wrapper_matches_and_a_bare_bitbake_does_not(self):
        """The adopted pid is the wrapper the driver spawned, not a bitbake it
        started: a bare bitbake in the same PID namespace is another job."""
        m = re.search(r"job_pid_adopt [^\n]*?'(\*[^'\n]*)'",
                      (REPO / "image" / "yocto.sh").read_text())
        self.assertIsNotNone(m, "no job_pid_adopt in image/yocto.sh")
        self.assertTrue(self._matches(
            "/opt/wk-tools/image/yocto-build.sh --target rpi5 --stage image", m.group(1)))
        self.assertFalse(self._matches("bitbake core-image-weston", m.group(1)))


class TestASecondBuildIsRefusedAtOnceAndNamesTheRemedy(WkTest):
    def _lockfile(self, lockdir, res):
        host = subprocess.run(["hostname", "-s"], capture_output=True,
                              text=True).stdout.strip() or socket.gethostname()
        lockdir.mkdir(parents=True, exist_ok=True)
        f = lockdir / f"{res}@{host}.lock"
        os.symlink(f"pid={os.getpid()} tok={rand_suffix()} at=now cmd=test", f)
        return f

    def test_a_held_ws_lock_refuses_in_seconds_naming_kill(self):
        with fake_workspace() as ws:
            lockdir = Path(ws.tmp) / "locks"
            self._lockfile(lockdir, "ws-selftest-ws")
            t0 = time.time()
            cp = ws.run("build", "jsc-release", env={"WK_LOCK_DIR": str(lockdir)},
                        timeout=60)
            took = time.time() - t0
            self.assertNotEqual(cp.returncode, 0, cp.stdout)
            self.assertIn("already building", cp.stdout)
            self.assertIn("wk build --kill", cp.stdout)
            self.assertLess(took, 30, f"it waited {took:.0f}s instead of refusing")

    def test_a_job_holding_the_checkout_refuses_and_names_its_own_stop_command(self):
        with fake_workspace() as ws:
            begin(store_of(ws), kind="yocto", pid=os.getpid(),
                  kill="wk sysimage build rpi5-64 --stage image --stop",
                  plan="image")
            cp = ws.run("build", "jsc-release", timeout=120)
            self.assertNotEqual(cp.returncode, 0, cp.stdout)
            self.assertIn("already has a job running in it", cp.stdout)
            self.assertIn("wk sysimage build rpi5-64 --stage image --stop", cp.stdout)

    def test_an_agent_session_in_the_workspace_is_not_a_reason_to_refuse(self):
        """`rc` (wk ai claude --rc) does not hold the checkout, so a build
        beside it is fine -- only the kinds that write it are exclusive. The
        build itself then fails: a fake workspace has no Tools/Scripts, which
        is past the refusal this asks about."""
        with fake_workspace() as ws:
            begin(store_of(ws), kind="rc", where="target", pid=os.getpid(),
                  kill="wk ai claude selftest-ws --rc --stop", plan="claude")
            cp = ws.run("build", "jsc-release", timeout=180)
            self.assertNotIn("already has a job running", cp.stdout)

    def test_the_job_that_started_this_build_is_not_counted_against_it(self):
        """build/babysit.sh runs `wk build` itself, and exports the record it
        holds so its own build is not refused as a second job."""
        with fake_workspace() as ws:
            d = begin(store_of(ws), kind="babysit", pid=os.getpid(),
                      kill="wk build selftest-ws --kill", plan="build jsc-release")
            cp = ws.run("build", "jsc-release", env={"WK_TASK_PARENT": d},
                        timeout=180)
            self.assertNotIn("already has a job running", cp.stdout)
            cp = ws.run("build", "jsc-release", timeout=180)
            self.assertIn("already has a job running in it", cp.stdout)


class TestKillFromTheOutside(WkTest):
    def test_kill_with_nothing_running_says_so_and_exits_0(self):
        with fake_workspace() as ws:
            cp = ws.run("build", "--kill", timeout=60)
            self.assertEqual(cp.returncode, 0, cp.stdout)
            self.assertIn("no build is running", cp.stdout)

    def test_kill_takes_no_config_and_says_which_form_it_is(self):
        with fake_workspace() as ws:
            cp = ws.run("build", "--kill", "jsc-release", timeout=60)
            self.assertNotEqual(cp.returncode, 0, cp.stdout)
            self.assertIn("takes nothing with it", cp.stdout)
            self.assertIn("jsc-release", cp.stdout)

    def test_kill_stops_a_recorded_build_and_records_it_cancelled(self):
        with fake_workspace() as ws:
            pid = spawn_orphan()
            try:
                d = begin(store_of(ws), pid=pid)
                cp = ws.run("build", "--kill", timeout=120)
                self.assertEqual(cp.returncode, 0, cp.stdout)
                self.assertIn("stopping the build", cp.stdout)
                self.assertIn("resumes rather than starts over", cp.stdout)
                self.assertEqual(field(d, "exit"), "cancelled")
                self.assertFalse(alive(pid))
            finally:
                reap(pid)

    def test_a_test_run_is_stopped_the_same_way(self):
        with fake_workspace() as ws:
            pid = spawn_orphan()
            try:
                d = begin(store_of(ws), kind="test", pid=pid,
                          kill="wk test --kill", plan="jsc/jsc-release")
                cp = ws.run("test", "--kill", timeout=120)
                self.assertEqual(cp.returncode, 0, cp.stdout)
                self.assertIn("stopping the test", cp.stdout)
                self.assertEqual(field(d, "exit"), "cancelled")
                self.assertFalse(alive(pid))
            finally:
                reap(pid)

    def test_the_record_a_running_build_writes_names_the_kill_command(self):
        text = (REPO / "cmd" / "build").read_text()
        self.assertIn('_KILL_CMD="wk build$(in_workspace || printf \' %s\' "$NAME") --kill"',
                      text)
        self.assertIn('task_begin build here "$NAME" "$_KILL_CMD"', text)


class TestTheBabysitterIsOneOfTheseJobsToo(unittest.TestCase):
    """build/babysit.sh writes the same record: its plan is every build it may
    run, and `wk build <ws> --kill` stops it before the build it drives, or it
    would start the next one."""

    def test_it_declares_a_record_with_a_real_kill_command(self):
        text = (REPO / "build" / "babysit.sh").read_text()
        self.assertIn('task_begin babysit here "$NAME" "wk build $NAME --kill"', text)
        self.assertNotIn("babysit.status", text)
        self.assertNotIn("bs_status", text)

    def test_every_way_out_ends_the_record(self):
        text = (REPO / "build" / "babysit.sh").read_text()
        for word in ("0", "stalled", "gave-up", "error", "cancelled"):
            self.assertIn(f'task_end "$TASK" {word}', text, word)

    def test_kill_stops_the_babysitter_before_the_build(self):
        text = (REPO / "cmd" / "build").read_text()
        self.assertIn("for _kind in babysit build; do", text)

    def test_cmd_status_renders_it_through_the_one_task_renderer(self):
        text = (REPO / "cmd" / "status").read_text()
        self.assertNotIn("report_babysit", text)
        self.assertNotIn("babysit.status", text)


class TestTheRecordFollowsTheBuildToTheMachineThatRunsIt(WkTest):
    """A remote target builds on another machine, and `wk status` asks that
    machine for its own workspaces (t_has_wk delegates), so the record is
    copied there after every write -- with the log path and machine name that
    machine's own, or its `wk status` reads the liveness of a path it does not
    have. The driver's `_rsh` is stubbed: this asks what is shipped, not
    whether ssh works."""

    def test_the_shipped_record_carries_the_far_log_and_host(self):
        store = self.tmp / "store"
        d = begin(store, pid=4242, log="/here/build.log")
        lifted = subprocess.run(
            ["sed", "-n", "/^t_task_put() {/,/^}/p", str(REPO / "targets" / "remote.sh")],
            capture_output=True, text=True).stdout
        self.assertIn("tar", lifted, "t_task_put not found in targets/remote.sh")
        cp = bash(PRELUDE + lifted + f"""
_remote_is_local() {{ return 1; }}
_remote_ws() {{ printf '%s' "/far/ws/$1"; }}
_remote_root() {{ printf '%s' "/far"; }}
WK_REMOTE_HOST=farbox
_rsh() {{ printf '%s\n' "$*" > "{self.tmp}/cmd"; cat > "{self.tmp}/sent.tar"; }}
t_task_put selftest-ws "{d}"
""", env={"WK_STORE": str(store)}, timeout=60)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        sent = (self.tmp / "cmd").read_text()
        self.assertIn("/far/task/" + Path(d).name, sent)
        self.assertIn("/far/ws/selftest-ws/build.log", sent,
                      "the far side's own log path, or it loses the liveness check")
        self.assertIn("farbox", sent, "and the machine that actually builds")
        names = subprocess.run(["tar", "-tf", str(self.tmp / "sent.tar")],
                               capture_output=True, text=True).stdout
        for field in ("plan", "kind", "pid", "kill"):
            self.assertIn(field, names, f"{field} was not shipped: {names}")

    def test_a_target_on_this_machine_ships_nothing(self):
        """Every other driver's record already sits in the store the machine
        that builds reports from, so the default is a no-op."""
        text = (REPO / "lib" / "target.sh").read_text()
        self.assertIn("t_task_put()  { :; }", text)
        callers = (REPO / "cmd" / "build").read_text()
        self.assertIn('t_task_put "$NAME" "$TASK"', callers)


if __name__ == "__main__":
    unittest.main()
