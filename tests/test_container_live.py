"""Live, on the container place: a fresh workspace's wiring verdict and a shell entered in it, then Zed refused once its
container is gone; a build followed as it runs, lldb and the profilers on it once it is done; and the commit wall as an
agent session meets it. Each class makes one workspace and removes it.

    python3 tests/run.py --live -v -k TestFreshWorkspace -k TestBuiltWorkspace -k TestAgentCommitWall
"""
import os
import pty
import re
import select
import shlex
import subprocess
import sys
import time
import unittest

from tests.support import (REPO, WK, clean_env, container_side, container_store, container_target_missing, rand_suffix,
                           requires_container_place, run)
from tests.test_dev_integration import clean, last_build, ls_rows, real_env, tail, wk, workspace_record

sys.path.insert(0, str(REPO / "lib"))
from wk import secrets  # noqa: E402

SRC = "/src/WebKit"
JS = 'let s = 0; for (let i = 0; i < 3000000; ++i) s += i * i % 7; print("WK-PROFILE " + s);'
WALL_NOTE = "wk: git history is read-only in this session. An agent runs under bwrap"
PARANOID_REMEDY = r"echo 1 \| sudo tee /proc/sys/kernel/perf_event_paranoid|Remedy:  \./setup --stage quiesce"
BABYSIT_PROBE = "Source/JavaScriptCore/runtime/JSObject.cpp"


def new_workspace(ws):
    cp = run("new", ws, "--on", "container", timeout=1200)
    assert cp.returncode == 0, "wk new failed: " + cp.stdout
    run("status", ws, "--wait", "--timeout", "900", timeout=960)


def remove_workspace(ws):
    cp = run("rm", ws, env={"WK_YES": "1"}, timeout=900)
    if cp.returncode != 0:
        print("[teardown] 'wk rm %s' exited %d: %s" % (ws, cp.returncode, cp.stdout))


def inside(ws, script, timeout=300):
    return clean(run("enter", ws, "--", "sh", "-c", script, timeout=timeout).stdout)


class Pty:
    """`wk <args>` on a terminal of its own, what `ssh -t`, a login shell and lldb need; `out` is all it printed."""

    def __init__(self, *args):
        self.master, slave = pty.openpty()
        self.proc = subprocess.Popen([str(WK), *args], cwd=str(REPO), env=clean_env(wk_root=False), stdin=slave,
                                     stdout=slave, stderr=slave, close_fds=True, start_new_session=True)
        os.close(slave)
        self.raw = b""

    @property
    def out(self):
        return clean(self.raw.decode(errors="replace"))

    def read(self, wait):
        ready, _, _ = select.select([self.master], [], [], max(wait, 0))
        if not ready:
            return False
        try:
            chunk = os.read(self.master, 65536)
        except OSError:
            return False
        self.raw += chunk
        return bool(chunk)

    def expect(self, pattern, timeout):
        """The first match of `pattern` in the output, read until it appears, the process ends or `timeout` passes."""
        deadline = time.monotonic() + timeout
        while True:
            m = re.search(pattern, self.out, re.M)
            if m or time.monotonic() >= deadline:
                return m
            if not self.read(min(5, deadline - time.monotonic())) and self.proc.poll() is not None:
                return re.search(pattern, self.out, re.M)

    def send(self, text):
        os.write(self.master, text.encode())

    def wait(self, timeout):
        """The exit status once the process ends within `timeout`, else None; its output is read all the while."""
        deadline = time.monotonic() + timeout
        while self.proc.poll() is None and time.monotonic() < deadline:
            self.read(1)
        while self.read(0.5):
            pass
        return self.proc.returncode

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        os.close(self.master)


def on_a_pty(*args, timeout):
    """(exit status or None, output) of `wk <args>` run to its end on a pty."""
    p = Pty(*args)
    rc = p.wait(timeout)
    p.close()
    return rc, p.out


@requires_container_place()
class TestFreshWorkspace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ws = "wk-test-%s" % rand_suffix()
        new_workspace(cls.ws)

    @classmethod
    def tearDownClass(cls):
        remove_workspace(cls.ws)

    def test_1_sync_reports_it_wired(self):
        """`live sync.fresh_workspace_wired`: `wk sync <ws>` fetches from the mirror with no wiring finding; each fork
        pushes to its deploy key's alias, and the workspace's ssh hands every host to the push service's proxy."""
        cp = run("sync", self.ws, timeout=900)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertRegex(cp.stdout, r"(?m)^  %s\s+ok  \(" % re.escape(self.ws))
        self.assertNotIn("wired wrong", cp.stdout)
        self.assertNotIn("problem:", cp.stdout)
        for remote, repo, alias in secrets.forks():
            with self.subTest(remote=remote):
                self.assertEqual(inside(self.ws, "git -C %s remote get-url --push %s" % (SRC, remote)).strip(),
                                 "git@%s:%s.git" % (alias, repo))
        config = inside(self.ws, 'cat "$HOME/.ssh/config"')
        self.assertEqual(re.findall(r"(?m)^Host .*$", config), ["Host *"], config)
        self.assertIn("ProxyCommand /opt/wk-tools/container/proxy/ssh-proxy.py", config)

    def test_2_enter_lands_in_a_shell_and_zed_is_refused_once_the_container_is_gone(self):
        """`live enter.shell`: `wk enter <ws>` is a shell in the workspace; with its container removed behind wk's back,
        `wk enter <ws> --zed` refuses naming the repair."""
        shell = Pty("enter", self.ws)
        self.assertTrue(shell.expect(r"\S", 300), "nothing printed within 300s of 'wk enter %s'" % self.ws)
        shell.send("echo WK-SHELL-$(uname -n); exit\n")
        rc = shell.wait(120)
        shell.close()
        self.assertEqual(rc, 0, tail(shell.out))
        self.assertRegex(shell.out, r"WK-SHELL-[A-Za-z0-9][\w.-]*", tail(shell.out))

        gone = container_side("podman rm -f wk-%s" % self.ws, timeout=120)
        self.assertEqual(gone.returncode, 0, gone.stdout + gone.stderr)
        cp = run("enter", self.ws, "--zed", timeout=300)
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("'%s' is a record whose container environment is gone -- something outside wk removed it."
                      % self.ws, cp.stdout)
        self.assertIn("Repair:  wk rm %s    (then 'wk new %s')" % (self.ws, self.ws), cp.stdout)


@requires_container_place()
class TestBuiltWorkspace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ws = "wk-test-%s" % rand_suffix()
        try:
            new_workspace(cls.ws)
            cp = run("build", cls.ws, "jsc-debug", "--detach", timeout=600)
            assert cp.returncode == 0, cp.stdout
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        run("build", cls.ws, "--kill", timeout=600)
        remove_workspace(cls.ws)

    @classmethod
    def build(cls):
        """(state, preset) of the build on the books, once there is one (the detached run takes seconds to start)."""
        deadline = time.monotonic() + 180
        while True:
            state = last_build(workspace_record(run("status", cls.ws, "--records", timeout=300).stdout, cls.ws))
            if state != (None, None) or time.monotonic() > deadline:
                return state
            time.sleep(3)

    def built(self):
        self.assertNotEqual(self.build(), (None, None), "the detached build never reached the books")
        run("status", self.ws, "--wait", "--timeout", "1500", timeout=1560)
        self.assertEqual(self.build(), ("ok", "jsc-debug"), tail(run("status", self.ws, "--log", timeout=300).stdout))

    def test_1_status_log_follows_the_build(self):
        """`live logs.follow[container]`: `wk status <ws> --log -f` streams the running build's log, ninja's progress
        lines in it, until ^C."""
        self.assertNotEqual(self.build(), (None, None), "the detached build never reached the books")
        deadline = time.monotonic() + 300
        while "showing " in run("status", self.ws, "--log", timeout=300).stdout and time.monotonic() < deadline:
            time.sleep(3)   # the newest log is the creation's until the build writes its own
        follow = Pty("status", self.ws, "--log", "-f")
        seen = follow.expect(r"\[[0-9]+/[0-9]+\]", 900)
        follow.send("\x03")
        rc = follow.wait(60)
        follow.close()
        self.assertTrue(seen, "no ninja progress line in 900s of following:\n" + tail(follow.out))
        self.assertIsNotNone(rc, "the follow did not end on ^C:\n" + tail(follow.out))

    def test_2_lldb_gets_a_terminal(self):
        """`live run.lldb_tty`: `wk run <ws> --lldb` lands at lldb's prompt with jsc stopped at entry, and continued,
        jsc prints."""
        self.built()
        lldb = Pty("run", self.ws, "--lldb", "--", "-e", 'print("WK-LLDB-" + 1)')
        self.assertTrue(lldb.expect(r"Process [0-9]+ launched: .*/jsc", 600), "lldb did not launch jsc:\n" + tail(lldb.out))
        self.assertTrue(lldb.expect(r"(?m)^\(lldb\) $", 300), "no lldb prompt after the launch:\n" + tail(lldb.out))
        lldb.send("continue\n")
        self.assertTrue(lldb.expect(r"WK-LLDB-1", 600), "jsc did not print under lldb:\n" + tail(lldb.out))
        self.assertTrue(lldb.expect(r"Process [0-9]+ exited with status = 0", 120), "jsc did not exit 0 under lldb:\n" + tail(lldb.out))
        lldb.send("quit\n")   # with jsc gone, lldb asks nothing and ends
        rc = lldb.wait(120)
        lldb.close()
        self.assertEqual(rc, 0, tail(lldb.out))

    def test_3_profile_modes(self):
        """`live profile.modes[sampling]` prints the tier breakdown; `live profile.modes[bytecode]` leaves one JSCProfile
        json in the workspace and none in the checkout; `live profile.modes[samply]` records, or refuses naming the
        perf_event_paranoid remedy; `live profile.modes[sysprof]` leaves the capture and a JIT dump beside it, or refuses
        the same way -- the capture and the dump are the fact measured, Sysprof's own view of it is not opened here."""
        self.built()
        js = "/tmp/wk-live.js"
        self.assertEqual(inside(self.ws, "printf '%%s' %s > %s && cat %s" % (shlex.quote(JS), js, js)).strip(), JS)
        with self.subTest(mode="sampling"):
            rc, out = on_a_pty("run", self.ws, "--profile=sampling", "--", js, timeout=900)
            self.assertEqual(rc, 0, tail(out))
            self.assertIn("WK-PROFILE ", out)
            self.assertIn("Tier breakdown:", out)
            self.assertRegex(out, r"recorded in '%s': \S+/report\.txt" % self.ws)
        with self.subTest(mode="bytecode"):
            rc, out = on_a_pty("run", self.ws, "--profile=bytecode", "--", js, timeout=900)
            self.assertEqual(rc, 0, tail(out))
            m = re.search(r"recorded in '%s': ((\S+/wk-profile)/\S+/JSCProfile\.json)" % self.ws, out)
            self.assertTrue(m, "no 'recorded in' line naming a JSCProfile.json under wk-profile:\n" + out)
            found = inside(self.ws, "find %s -name 'JSCProfile*.json'" % shlex.quote(m.group(2))).split()
            self.assertEqual(len(found), 1, "not one JSCProfile json under %s but %r; the run said:\n%s" % (m.group(2), found, out))
            self.assertEqual(found[0], m.group(1), "the run named another file than it left:\n" + out)
            stray = inside(self.ws, "find %s -maxdepth 1 -name 'JSCProfile*.json'" % SRC).split()
            self.assertEqual(stray, [], "JSC's own JSCProfile-<pid>-<n>.json was left in the checkout; the run said:\n" + out)
        with self.subTest(mode="samply"):
            rc, out = on_a_pty("run", self.ws, "--profile=samply", "--", js, timeout=900)
            if rc == 0:
                self.assertRegex(out, r"recorded in '%s': \S+/profile\.json" % self.ws)
            else:
                self.assertRegex(out, r"perf_event_paranoid is -?[0-9]+ in '%s', and samply needs 1 or less\." % self.ws, tail(out))
                self.assertRegex(out, PARANOID_REMEDY, tail(out))
        with self.subTest(mode="sysprof"):
            rc, out = on_a_pty("run", self.ws, "--profile=sysprof", "--", js, timeout=900)
            if rc == 0:
                m = re.search(r"recorded in '%s': ((\S+)/capture\.syscap)" % self.ws, out)
                self.assertTrue(m, "no 'recorded in' line naming capture.syscap:\n" + out)
                listed = inside(self.ws, "find %s -maxdepth 1 -type f -size +0" % shlex.quote(m.group(2))).split()
                self.assertIn(m.group(1), listed, "no capture under %s but %r; the run said:\n%s" % (m.group(2), listed, out))
                self.assertTrue([f for f in listed if re.search(r"/jit-.*\.dump$", f)],
                                "no JIT dump beside the capture, only %r; the run said:\n%s" % (listed, out))
            else:
                self.assertRegex(out, r"perf_event_paranoid is -?[0-9]+ in '%s', and sysprof needs 1 or less\." % self.ws, out)
                self.assertRegex(out, PARANOID_REMEDY, out)

    def babysit_exit(self):
        """The newest babysit record's exit word for this workspace: "" while it runs, None with no record."""
        said = container_side('for d in %s/task/babysit-*/; do [ "$(cat "$d/name" 2>/dev/null)" = %s ] || continue; '
                              'echo "$(cat "$d/started" 2>/dev/null) $(cat "$d/exit" 2>/dev/null)"; done | sort | tail -1'
                              % (container_store(), self.ws), timeout=120).stdout.split()
        return None if not said else (said[1] if len(said) > 1 else "")

    def babysit_until(self, want, timeout):
        deadline = time.monotonic() + timeout
        while self.babysit_exit() not in want and time.monotonic() < deadline:
            time.sleep(5)
        return self.babysit_exit()

    def test_4_babysit_is_one_task_refused_beside_itself_cancelled_by_kill_and_ends_by_name(self):
        """`live build.babysit_e2e`: with the checkout broken, `wk build --babysit` is a task of its own: a second one is
        refused naming the pid and the kill, `--kill` leaves its record cancelled, and one run to its end reads ok (the
        one fix took) or gave-up, its report naming the attempt."""
        self.built()
        self.addCleanup(inside, self.ws, "git -C %s checkout -- %s" % (SRC, BABYSIT_PROBE))
        inside(self.ws, "echo '#error wk-babysit-probe' >> %s/%s" % (SRC, BABYSIT_PROBE))
        env = dict(real_env(), WK_BABYSIT_ATTEMPTS="1", WK_BABYSIT_MODEL="haiku")
        r = wk("build", self.ws, "jsc-debug", "--babysit", env=env, timeout=600)
        self.assertEqual(0, r.rc, tail(r.out))
        self.assertIn("babysitter started for jsc-debug in '%s'" % self.ws, r.out)
        self.assertEqual(self.babysit_until(("",), 60), "", "no running babysit record within 60s")
        second = wk("build", self.ws, "jsc-debug", "--babysit", env=env, timeout=600)
        self.assertNotEqual(0, second.rc, "a second babysitter started beside the first:\n" + tail(second.out))
        self.assertRegex(second.out, r"a babysitter is already running for '%s' \(pid [0-9]+\)" % self.ws)
        self.assertIn("Stop it:    wk build %s --kill" % self.ws, second.out)
        killed = run("build", self.ws, "--kill", timeout=600)
        self.assertEqual(killed.returncode, 0, killed.stdout)
        self.assertEqual(self.babysit_until(("cancelled",), 60), "cancelled")

        r = wk("build", self.ws, "jsc-debug", "--babysit", env=env, timeout=600)
        self.assertEqual(0, r.rc, tail(r.out))
        ended = self.babysit_until(("0", "ok", "gave-up", "error", "stalled"), 900)
        report = container_side("cat %s/ws/%s/babysit.report" % (container_store(), self.ws), timeout=120).stdout
        self.assertIn(ended, ("0", "ok", "gave-up"), "the babysitter ended '%s'; its report:\n%s" % (ended, report))
        self.assertRegex(report, r"=== fix attempt 1 \(exit [0-9]+\)", report)


@requires_container_place()
class TestAgentCommitWall(unittest.TestCase):
    """The session is a person's own `wk ai claude`, with this machine's credentials (test_dev_integration's `wk`)."""

    @classmethod
    def setUpClass(cls):
        cls.ws = "wk-test-%s" % rand_suffix()
        new_workspace(cls.ws)

    @classmethod
    def tearDownClass(cls):
        remove_workspace(cls.ws)

    def test_commit_and_push_name_the_wall_after_gits_own_error(self):
        """`live ai.commit_wall`: in an agent session `git commit` and `git push` each fail with git's own error, and the
        wall's note follows it."""
        script = 'cd %s && git commit --allow-empty -m x 2>&1; echo WK-S""EP; git push --dry-run origin HEAD 2>&1' % SRC
        r = wk("ai", "claude", self.ws, "-p", "Use your Bash tool to run exactly this command, then reply with its complete "
               "output verbatim and nothing else:\n" + script, timeout=900)
        self.assertEqual(0, r.rc, "'wk ai claude %s' exited %d:\n%s" % (self.ws, r.rc, tail(r.out)))
        commit, sep, push = r.out.partition("WK-SEP")
        self.assertTrue(sep, "the agent's reply holds no WK-SEP:\n" + tail(r.out))
        for verb, said in (("commit", commit), ("push", push)):
            with self.subTest(verb=verb):
                err = re.search(r"(?m)^\s*(fatal|error): ", said)
                self.assertTrue(err, "git %s gave no error of its own:\n%s" % (verb, tail(said)))
                self.assertIn(WALL_NOTE, said, "no wall note after git %s:\n%s" % (verb, tail(said)))
                self.assertLess(err.start(), said.index(WALL_NOTE), "the wall's note came before git's own error:\n" + tail(said))


@requires_container_place()
class TestStopKeepVmThenStart(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ws = "wk-test-%s" % rand_suffix()
        new_workspace(cls.ws)

    @classmethod
    def tearDownClass(cls):
        remove_workspace(cls.ws)

    def running(self):
        return sorted(n for n, place, state in ls_rows(run("ls", timeout=300).stdout) if place == "container" and state == "running")

    def test_stop_keep_vm_then_start_returns_every_container_to_running(self):
        """`live start.roundtrip`: `wk stop --keep-vm` stops every container (and a running macOS guest) and leaves the
        podman machine answering, and a bare `wk start` has each of those containers running again. The machine's own
        stop and start are not round-tripped here: minutes, and the guests compete with it for memory."""
        before = self.running()
        self.assertIn(self.ws, before, "the fresh workspace is not listed running:\n" + run("ls", timeout=300).stdout)
        cp = run("stop", "--keep-vm", timeout=900)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("leaving the machine running (--keep-vm)", cp.stdout)
        self.assertIsNone(container_target_missing(), "the podman machine went down under --keep-vm:\n" + cp.stdout)
        self.assertEqual([n for n in self.running() if n in before], [], "still running after 'wk stop --keep-vm':\n" + cp.stdout)
        cp = run("start", timeout=900)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual([n for n in before if n not in self.running()], [], "not running again after 'wk start':\n" + cp.stdout)


if __name__ == "__main__":
    unittest.main()
