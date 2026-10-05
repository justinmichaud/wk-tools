"""Live: one real container workspace created, read, a real build cancelled in it, and destroyed."""
import json
import os
import pty
import select
import subprocess
import sys
import time
import unittest
from unittest import mock

from tests.support import REPO, WK, WkTest, rand_suffix, requires_container_place, run

sys.path.insert(0, str(REPO / "lib"))


def firstrun_lines(log):
    return "\n".join(l for l in log.replace("\r", "").splitlines() if l.startswith("[firstrun]"))


def container_log(ws):
    """`podman logs` where the container is: inside the podman machine on macOS."""
    argv = ["podman", "logs", "wk-" + ws]
    if sys.platform == "darwin":
        argv = ["podman", "machine", "ssh", "wk", "--"] + argv
    cp = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)
    return cp.stdout


@requires_container_place()
class TestContainerWorkspaceLifecycle(WkTest):
    def setUp(self):
        super().setUp()
        self.name = f"wk-test-{rand_suffix()}"
        self._created = False

    def tearDown(self):
        if self._created:
            cp = run("rm", self.name, env={"WK_YES": "1"})
            if cp.returncode != 0:
                print(f"[teardown] 'wk rm {self.name}' exited {cp.returncode}: {cp.stdout + cp.stderr}")
        super().tearDown()

    def _in_ws(self, *args, timeout=180):
        return run("enter", self.name, "--", "git", "-C", "/src/WebKit", *args,
                   timeout=timeout)

    def _config(self, *args):
        return self._in_ws("config", *args).stdout.strip()

    def _assert_the_checkout_is_what_wk_new_promises(self):
        self.assertEqual(self._in_ws("symbolic-ref", "--short", "HEAD").stdout.strip(),
                         "main", "a fresh workspace is on main, not detached")
        self.assertEqual(
            self._in_ws("rev-parse", "--abbrev-ref", "--symbolic-full-name",
                        "@{u}").stdout.strip(),
            "origin/main", "main tracks origin/main, so `git pull` has an upstream")

        from wk.images import mirror_branches
        self.assertEqual(
            self._config("--get-all", "remote.origin.fetch").split("\n"),
            ["+refs/heads/%s:refs/remotes/origin/%s" % (b, b)
             for b in mirror_branches()])
        self.assertEqual(self._config("remote.origin.url"),
                         "https://github.com/WebKit/WebKit.git",
                         "git-webkit reads this to find the project")
        for remote in ("wpe", "fork", "forkwpe"):
            with self.subTest(remote=remote):
                self.assertEqual(
                    self._config("--get-all", f"remote.{remote}.fetch"),
                    f"+refs/remotes/{remote}/*:refs/remotes/{remote}/*")
        for remote in ("origin", "wpe", "fork", "forkwpe"):
            with self.subTest(remote=remote):
                self.assertEqual(self._config(f"remote.{remote}.tagOpt"), "--no-tags")
                self.assertIn(
                    "/git/WebKit.git",
                    self._config("--get-regexp", r"^url\..*\.insteadof$"),
                    "the four remotes are rewritten to the mirror bind-mounted in")

        t0 = time.time()
        cp = self._in_ws("fetch", "origin")
        took = time.time() - t0
        self.assertEqual(cp.returncode, 0, f"'git fetch origin' failed: {cp.stdout}")
        print(f"[timing] git fetch origin in the workspace: {took:.1f}s")
        self.assertLess(took, 25, f"'git fetch origin' took {took:.1f}s -- it reads "
                                  "the mirror bind-mounted in, so it is a "
                                  "local read of a handful of refs")

        if self._config("webkitscmpy.setup") != "true":
            self.fail("`git-webkit setup --defaults` runs at first start (container/firstrun.sh), so `git-webkit pr` "
                      "and the commit hooks work without being asked for; it did not finish here. firstrun said:\n"
                      + firstrun_lines(container_log(self.name)))

    def _assert_a_session_can_run_in_it(self):
        cp = run("enter", self.name, "--", "bash", "-c",
                 'find "$HOME" -xdev -maxdepth 4 ! -user "$(id -un)" -printf "%u %m %p\\n"',
                 timeout=300)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual(cp.stdout.strip(), "",
                         "these are in the workspace user's home and are not theirs, "
                         "so what installs there cannot")

        cp = run("enter", self.name, "--", "bash", "-c", "command -v claude", timeout=300)
        self.assertEqual(cp.returncode, 0,
                         f"no 'claude' on $PATH in the workspace: {cp.stdout}")
        self.assertTrue(cp.stdout.strip().endswith("claude"), cp.stdout)

    def test_create_list_status_build_dry_run_remove(self):
        t0 = time.time()
        cp = run("new", self.name, "--on", "container", timeout=600)
        self._created = cp.returncode == 0
        self.assertEqual(cp.returncode, 0, f"wk new failed: {cp.stdout + cp.stderr}")
        created_s = time.time() - t0

        run("status", self.name, "--wait", "--timeout", "900", timeout=960)

        self._assert_the_checkout_is_what_wk_new_promises()
        self._assert_a_session_can_run_in_it()

        cp = run("ls")
        self.assertIn(self.name, cp.stdout, f"'wk ls' does not list {self.name}: {cp.stdout}")

        cp = run("status", self.name, "--text", "--no-fleet")
        self.assertIn(cp.returncode, (0, 2), f"'wk status {self.name} --text --no-fleet' exited {cp.returncode}: {cp.stdout + cp.stderr}")

        cp = run("build", self.name, "jsc-release", "--dry-run")
        self.assertEqual(cp.returncode, 0, f"'wk build {self.name} jsc-release --dry-run' failed: {cp.stdout + cp.stderr}")
        self.assertTrue(cp.stdout.strip(), "the dry run printed no command")

        cp = run("rm", self.name, env={"WK_YES": "1"})
        self.assertEqual(cp.returncode, 0, f"'wk rm {self.name}' failed: {cp.stdout + cp.stderr}")
        self._created = False

        cp = run("ls")
        self.assertNotIn(self.name, cp.stdout, f"'wk ls' still lists {self.name} after rm")

        total_s = time.time() - t0
        print(f"[timing] wk new: {created_s:.1f}s, total lifecycle: {total_s:.1f}s")


@requires_container_place()
class TestCancellingARealBuild(WkTest):
    """A ^C byte into a pty (the only interrupt that crosses `ssh -t` into the podman VM), a refused second
    build, and --kill, on a real JSC build."""

    def setUp(self):
        super().setUp()
        self.name = f"wk-test-{rand_suffix()}"
        cp = run("new", self.name, "--on", "container", timeout=1200)
        self._created = cp.returncode == 0
        self.assertEqual(cp.returncode, 0, f"wk new failed: {cp.stdout}")
        run("status", self.name, "--wait", "--timeout", "900", timeout=960)

    def tearDown(self):
        run("build", self.name, "--kill", timeout=600)
        if getattr(self, "_created", False):
            cp = run("rm", self.name, env={"WK_YES": "1"}, timeout=900)
            if cp.returncode != 0:
                print(f"[teardown] 'wk rm {self.name}' exited {cp.returncode}: {cp.stdout}")
        super().tearDown()

    def _build_task(self):
        """(`wk status --records` result, the build's task record or the workspace row's build sub)."""
        cp = run("status", self.name, "--records", "--no-fleet", timeout=300)
        task, sub = None, None
        for line in cp.stdout.splitlines():
            if not line.startswith("{"):
                continue
            rec = json.loads(line)
            if rec.get("kind") == "task" and rec.get("task_kind") == "build" \
                    and rec.get("name") == self.name:
                task = rec
            if rec.get("kind") == "workspace" and rec.get("name") == self.name:
                sub = next((x for x in rec.get("subs", []) if x.get("kind") == "build"), sub)
        return cp, task or sub

    def _ninja_started(self):
        cp = run("status", self.name, "--log", timeout=300)
        return any(l.strip().startswith("[") and "/" in l[:16]
                   for l in cp.stdout.splitlines())

    def test_interrupt_then_refuse_then_kill(self):
        self._interrupt_stops_the_build_inside_the_container()
        self._a_second_build_is_refused_and_kill_converges_the_detached_one()

    def _interrupt_stops_the_build_inside_the_container(self):
        master, slave = pty.openpty()
        proc = subprocess.Popen(
            [str(WK), "build", self.name, "jsc-release"], cwd=str(REPO),
            stdin=slave, stdout=slave, stderr=slave, close_fds=True,
            start_new_session=True)
        os.close(slave)
        out = b""
        sent = False
        deadline = time.time() + 1800
        try:
            while time.time() < deadline:
                r, _, _ = select.select([master], [], [], 5)
                if r:
                    try:
                        out += os.read(master, 65536)
                    except OSError:
                        break
                if not sent and self._ninja_started():
                    os.write(master, b"\x03")
                    sent = True
                    print("[timing] ninja running; ^C sent")
                if proc.poll() is not None:
                    break
            rc = proc.wait(timeout=600)
        finally:
            if proc.poll() is None:
                proc.kill()
            os.close(master)
        text = out.decode(errors="replace")
        self.assertTrue(sent, f"the build never reached a ninja line:\n{text[-2000:]}")
        self.assertEqual(rc, 130, f"exit was {rc}:\n{text[-2000:]}")
        self.assertIn("interrupted -- stopping the build", text)

        cp, task = self._build_task()
        self.assertIsNotNone(task, f"no build record after the interrupt:\n{cp.stdout}")
        self.assertEqual(task["state"], "cancelled", cp.stdout)
        self.assertNotEqual(cp.returncode, 2, f"'wk status' still reads busy:\n{cp.stdout}")
        # -x, not -f: `pgrep -f ninja` matches the `wk enter ... pgrep -f
        # ninja` pipeline asking the question.
        left = run("enter", self.name, "--", "pgrep", "-x", "ninja", timeout=300)
        self.assertNotEqual(left.returncode, 0,
                            f"ninja is still running in '{self.name}': {left.stdout}")

    def _a_second_build_is_refused_and_kill_converges_the_detached_one(self):
        cp = run("build", self.name, "jsc-debug", "--detach", timeout=600)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("--kill", cp.stdout, "the detach names how to stop it")

        second = None
        for _ in range(30):   # the detached run takes the lock as it starts
            second = run("build", self.name, "jsc-debug", timeout=300)
            if second.returncode != 0 and "already building" in second.stdout:
                break
            time.sleep(2)
        self.assertNotEqual(second.returncode, 0, second.stdout)
        self.assertIn("already building", second.stdout)
        self.assertIn("--kill", second.stdout)

        killed = run("build", self.name, "--kill", timeout=600)
        self.assertEqual(killed.returncode, 0, killed.stdout)
        cp, task = self._build_task()
        self.assertIsNotNone(task, f"no build record to converge:\n{cp.stdout}")
        self.assertIn(task["state"], ("cancelled", "ok"), cp.stdout)
        self.assertNotEqual(cp.returncode, 2, f"still busy after --kill:\n{cp.stdout}")


class TestOnePodmanWrapper(unittest.TestCase):
    """`Container.podman()` names the machine's connection from a macOS host."""

    def container(self, env, system):
        from wk import places
        from wk.machine import Fake
        with mock.patch("wk.places.os.uname", return_value=mock.Mock(sysname=system)):
            return places.Container("container", str(REPO), env, Fake("here")).podman()

    def test_a_macos_host_names_the_machines_connection_and_a_local_daemon_is_plain(self):
        for env, system, want in (({}, "Darwin", ["podman", "-c", "wk"]),
                                  ({"WK_MACHINE": "other"}, "Darwin", ["podman", "-c", "other"]),
                                  ({"WK_IN_VM": "1"}, "Darwin", ["podman"]), ({}, "Linux", ["podman"])):
            self.assertEqual(want, self.container(env, system), (env, system))

if __name__ == "__main__":
    unittest.main()
