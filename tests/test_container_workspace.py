"""Real integration tests against one container workspace: create it,
exercise the read-only surface, cancel a real build in it, then destroy it.
Gated on a running podman `wk` VM (this repo's own container target) -- it
never starts one itself.

Run: python3 -m unittest tests.test_container_workspace -v
"""
import json
import os
import pty
import select
import subprocess
import time
import unittest

from tests.support import (REPO, WK, WkTest, rand_suffix, requires_podman_vm,
                           run, shell_files)


@requires_podman_vm()
class TestContainerWorkspaceLifecycle(WkTest):
    """`wk new` (container target) -> `wk ls`/`wk status`/`wk build --dry-run`
    -> `wk rm`, cleaning up in tearDown even if an assertion fails midway."""

    def setUp(self):
        super().setUp()
        self.name = f"wk-test-{rand_suffix()}"
        self._created = False

    def tearDown(self):
        if self._created:
            # cmd/rm's confirm() only skips the prompt with WK_YES=1 (no
            # terminal here declines by default, which would leave the
            # workspace behind).
            cp = run("rm", self.name, env={"WK_YES": "1"})
            # Best-effort: report but do not mask the real test failure.
            if cp.returncode != 0:
                print(f"[teardown] 'wk rm {self.name}' exited {cp.returncode}: {cp.stdout + cp.stderr}")
        super().tearDown()

    def _in_ws(self, *args, timeout=180):
        """One command in the workspace's checkout, the way a person reaches
        it: `wk enter <ws> -- ...` in /src/WebKit."""
        return run("enter", self.name, "--", "git", "-C", "/src/WebKit", *args,
                   timeout=timeout)

    def _config(self, *args):
        return self._in_ws("config", *args).stdout.strip()

    def _assert_the_checkout_is_what_wk_new_promises(self):
        """The four defects, asked of a real workspace: it is on main tracking
        origin/main (not detached), every remote fetches the machine's mirror
        for the refs that mirror carries, a bare `git fetch origin` is a local
        read rather than half a minute of github.com, and `git-webkit setup`
        has already run."""
        self.assertEqual(self._in_ws("symbolic-ref", "--short", "HEAD").stdout.strip(),
                         "main", "a fresh workspace is on main, not detached")
        self.assertEqual(
            self._in_ws("rev-parse", "--abbrev-ref", "--symbolic-full-name",
                        "@{u}").stdout.strip(),
            "origin/main", "main tracks origin/main, so `git pull` has an upstream")

        self.assertEqual(self._config("--get-all", "remote.origin.fetch"),
                         "+refs/heads/main:refs/remotes/origin/main")
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
                    "/mirror/WebKit.git",
                    self._config("--get-regexp", r"^url\..*\.insteadof$"),
                    "the four remotes are rewritten to the mirror bind-mounted in")

        t0 = time.time()
        cp = self._in_ws("fetch", "origin")
        took = time.time() - t0
        self.assertEqual(cp.returncode, 0, f"'git fetch origin' failed: {cp.stdout}")
        print(f"[timing] git fetch origin in the workspace: {took:.1f}s")
        self.assertLess(took, 25, f"'git fetch origin' took {took:.1f}s -- it reads "
                                  "the mirror bind-mounted at /mirror, so it is a "
                                  "local read of a handful of refs")

        self.assertEqual(self._config("webkitscmpy.setup"), "true",
                         "`git-webkit setup --defaults` runs at first start "
                         "(container/firstrun.sh), so `git-webkit pr` and the "
                         "commit hooks work without being asked for")

    def test_create_list_status_build_dry_run_remove(self):
        """wk new -> wk ls -> wk status --text --no-fleet -> wk build --dry-run -> wk rm"""
        t0 = time.time()
        cp = run("new", self.name, "--target", "container", timeout=600)
        self._created = cp.returncode == 0
        self.assertEqual(cp.returncode, 0, f"wk new failed: {cp.stdout + cp.stderr}")
        created_s = time.time() - t0

        # It may still be finishing in the background; wait for it to settle.
        run("status", self.name, "--wait", "--timeout", "300")

        self._assert_the_checkout_is_what_wk_new_promises()

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


@requires_podman_vm()
class TestCancellingARealBuild(WkTest):
    """^C reaches the driver and nothing else -- a container build is `podman
    exec` with no signal proxy -- so the driver has to tell the machine that
    builds. This drives the real thing: a real JSC build in a real container,
    interrupted once ninja is running, then refused while one runs, then
    stopped by name.

    The interrupt is a ^C byte into a pty, which is what a person does and the
    only thing that reaches the far side: on a macOS host the command is
    forwarded into the podman VM, and with a terminal that hop is `ssh -t`, so
    the remote pty raises the signal there (measured: a SIGINT to the local
    `podman machine ssh` is ignored and the build carries on).
    """

    def setUp(self):
        super().setUp()
        self.name = f"wk-test-{rand_suffix()}"
        cp = run("new", self.name, "--target", "container", timeout=1200)
        self._created = cp.returncode == 0
        self.assertEqual(cp.returncode, 0, f"wk new failed: {cp.stdout}")
        run("status", self.name, "--wait", "--timeout", "300")

    def tearDown(self):
        run("build", self.name, "--kill", timeout=600)
        if getattr(self, "_created", False):
            cp = run("rm", self.name, env={"WK_YES": "1"}, timeout=900)
            if cp.returncode != 0:
                print(f"[teardown] 'wk rm {self.name}' exited {cp.returncode}: {cp.stdout}")
        super().tearDown()

    def _build_task(self):
        """The build task record `wk status --records` reports, and the walk's
        own exit code."""
        cp = run("status", self.name, "--records", "--no-fleet", timeout=300)
        last = None
        for line in cp.stdout.splitlines():
            if not line.startswith("{"):
                continue
            rec = json.loads(line)
            if rec.get("kind") == "task" and rec.get("task_kind") == "build" \
                    and rec.get("name") == self.name:
                last = rec
        return cp, last

    def _ninja_started(self):
        """Whether the log has a ninja progress line yet: the build's output
        goes to its log, not to the driver's stdout."""
        cp = run("logs", self.name, timeout=300)
        return any(l.strip().startswith("[") and "/" in l[:16]
                   for l in cp.stdout.splitlines())

    def test_interrupt_then_refuse_then_kill(self):
        """One workspace, three questions in the order a person meets them:
        ^C on a foreground build, a second build while one runs, and --kill on
        a detached one. One `wk new` for all three -- creating the workspace is
        the expensive part."""
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
        for _ in range(30):   # the detached driver takes the lock as it starts
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
    """`_hpodman` is how this driver reaches podman, everywhere. A second,
    bare wrapper works wherever the daemon is local and reaches the *rootful*
    podman from a macOS host -- and the commands a person types outside the VM
    (`wk scp`, `wk stop`) are exactly where that shows up."""

    def test_the_bare_wrapper_is_gone(self):
        text = (REPO / "targets" / "container.sh").read_text()
        self.assertNotIn("_podman() {", text)
        self.assertIn("_hpodman() {", text)

    def test_nothing_in_the_tree_still_calls_it(self):
        for f in shell_files():
            with self.subTest(script=str(f.relative_to(REPO))):
                for line in f.read_text().splitlines():
                    self.assertNotRegex(line, r"(?<![_A-Za-z])_podman ")


if __name__ == "__main__":
    unittest.main()
