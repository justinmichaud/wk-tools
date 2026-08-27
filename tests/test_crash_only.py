"""Crash-only convergence (CLAUDE.md, rule 2): a killed mutating command
re-runs to the declared final state. Two real, podman-gated cases drive an
actual container workspace and kill its detached driver mid-creation
(`wk new --no-wait` prints the driver's own pid, running inside the podman
VM once `wk`'s forwarding execs the whole command there -- see
lib/target.sh's forward_to_vm; killing it there is what
support.podman_vm_ssh is for). A third case -- the orphaned creation record
that `wk gc` reaps once its driver is dead and no workspace exists anywhere
-- has no seam to exercise in isolation (see the skipped test below) and is
left for whoever adds one. The status-files-are-claims case
(`build_live` on a stale log) needs no hardware at all.

Run: python3 -m unittest tests.test_crash_only -v
"""
import re
import time
import unittest

from tests.support import (
    REPO,
    WkTest,
    bash,
    podman_vm_ssh,
    rand_suffix,
    requires_podman_vm,
    run,
    scratch_dir,
)


def _wait_dead(pid, timeout=60):
    """Poll until <pid>, inside the podman VM, is no longer alive."""
    waited = 0
    while waited < timeout:
        cp = podman_vm_ssh(f"kill -0 {pid} 2>/dev/null")
        if cp.returncode != 0:
            return True
        time.sleep(1)
        waited += 1
    return False


@requires_podman_vm()
class TestWkNewKilledMidway(WkTest):
    """`wk new` detaches its driver (cmd/new's `--_detached` half) and this
    end only follows the log -- so killing the *driver* (not the following
    `wk new` process) is the fair test of rule 2: nothing here waited for
    it, and a second `wk new` for the same name is the resume."""

    def setUp(self):
        super().setUp()
        self.name = f"wk-test-{rand_suffix()}"
        self._created = False

    def tearDown(self):
        if self._created:
            cp = run("rm", self.name, env={"WK_YES": "1"})
            if cp.returncode != 0:
                print(f"[teardown] 'wk rm {self.name}' exited {cp.returncode}: {cp.stdout}")
        super().tearDown()

    def test_killed_driver_then_rerun_converges(self):
        cp = run("new", self.name, "--target", "container", "--no-wait", timeout=120)
        self.assertEqual(cp.returncode, 0, f"'wk new --no-wait' failed: {cp.stdout}")

        m = re.search(r"detached as pid (\d+)", cp.stdout)
        self.assertIsNotNone(m, f"'wk new --no-wait' did not report a driver pid: {cp.stdout}")
        pid = m.group(1)

        # Kill it where it actually lives: forwarded into the podman VM.
        podman_vm_ssh(f"kill -9 {pid}")
        self.assertTrue(_wait_dead(pid), f"driver pid {pid} did not die")

        # Whatever it left behind, this end now owns cleanup either way.
        self._created = True

        st = run("status", self.name, "--json")
        # Not asserted further than "not present": the exact rubble state
        # (a live-record 'creating', or nothing at all if the kill landed
        # before the store directory existed) depends on exactly when the
        # kill landed, and both are legitimate half-made states rule 2
        # applies to. What matters is the workspace never reads as finished.
        self.assertNotIn(
            '"state":"present"', st.stdout.replace(" ", ""),
            f"'{self.name}' reads present after its driver was killed: {st.stdout}",
        )

        # The resume: re-running `wk new` must destroy the rubble and finish
        # the job, not refuse "already exists" (rule 2's own example).
        cp2 = run("new", self.name, "--target", "container", timeout=600)
        self.assertEqual(
            cp2.returncode, 0,
            f"'wk new {self.name}' did not converge after its driver was killed: {cp2.stdout}",
        )
        self.assertIn("ready", cp2.stdout, cp2.stdout)

        ls = run("ls")
        self.assertIn(self.name, ls.stdout, f"'{self.name}' is missing from 'wk ls' after converging: {ls.stdout}")


@requires_podman_vm()
class TestWkRmOfRubble(WkTest):
    """The same kill, but the recovery asked for is `wk rm` rather than a
    second `wk new`: rule 2 applies to destruction too -- a half-made
    workspace is exactly what `wk rm` promises to clear."""

    def setUp(self):
        super().setUp()
        self.name = f"wk-test-{rand_suffix()}"

    def test_rm_converges_on_a_killed_creation(self):
        cp = run("new", self.name, "--target", "container", "--no-wait", timeout=120)
        self.assertEqual(cp.returncode, 0, f"'wk new --no-wait' failed: {cp.stdout}")
        m = re.search(r"detached as pid (\d+)", cp.stdout)
        self.assertIsNotNone(m, cp.stdout)
        pid = m.group(1)

        podman_vm_ssh(f"kill -9 {pid}")
        self.assertTrue(_wait_dead(pid), f"driver pid {pid} did not die")

        cp2 = run("rm", self.name, env={"WK_YES": "1"}, timeout=180)
        self.assertEqual(
            cp2.returncode, 0,
            f"'wk rm {self.name}' did not converge on the rubble left by a killed 'wk new': {cp2.stdout}",
        )
        self.assertTrue(
            "forgotten" in cp2.stdout or "destroyed" in cp2.stdout,
            f"'wk rm' gave no sign of having cleared the rubble: {cp2.stdout}",
        )

        ls = run("ls")
        self.assertNotIn(self.name, ls.stdout, f"'wk rm' left '{self.name}' behind: {ls.stdout}")


class TestGcReapsDeadCreationRecord(unittest.TestCase):
    """cmd/gc's orphaned-creation-record reaping is a callable seam now
    (gc_creation_records, lib/target.sh, next to ws_target/ws_exists), so
    this drives it directly against a fake WK_STORE and a stubbed
    target_all/load_target -- the way test_state.py's TestWsStateWords
    drives ws_state -- rather than the real shared store cmd/gc otherwise
    unconditionally pipes itself into on macOS (cmd/gc's podman-VM half)."""

    def test_gc_reaps_a_dead_creation_record(self):
        with scratch_dir(prefix="wk-test-gc-creation-") as tmp:
            store = tmp / "store"
            create = store / "create"
            create.mkdir(parents=True)
            (store / "ws").mkdir()
            script = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/lib/target.sh"
. "{REPO}/lib/detach.sh"
WK_STORE="{store}"; export WK_STORE

# One fake target whose store is this same WK_STORE (load_target is a
# no-op): enough for gc_creation_records to walk without touching the real
# container/vm drivers.
target_all() {{ echo fake; }}
load_target() {{ :; }}

# dead: a pid nothing answers to, and no workspace directory anywhere -- reaped.
status_write "$WK_STORE/create/dead.status" state=creating pid=4194304 stage=create
: > "$WK_STORE/create/dead.log"

# alive: this very process's own pid -- kept, though its workspace directory
# does not exist yet.
status_write "$WK_STORE/create/alive.status" state=creating "pid=$$" stage=create
: > "$WK_STORE/create/alive.log"

# found: a dead pid, but a workspace directory exists on the (fake) target
# -- kept.
status_write "$WK_STORE/create/found.status" state=creating pid=4194304 stage=create
: > "$WK_STORE/create/found.log"
mkdir -p "$WK_STORE/ws/found"

gc_creation_records

for n in dead alive found; do
    if [ -f "$WK_STORE/create/$n.status" ]; then echo "$n:kept"; else echo "$n:reaped"; fi
done
'''
            cp = bash(script)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            want = "dead:reaped\nalive:kept\nfound:kept"
            self.assertEqual(
                cp.stdout.strip(), want,
                f"got:\n{cp.stdout}\nwant:\n{want}\nstderr:\n{cp.stderr}",
            )

            # The log beside a reaped record goes with it; a kept record's
            # log is untouched.
            self.assertFalse((create / "dead.log").exists(), "dead.log survived its reaped record")
            self.assertTrue((create / "alive.log").exists(), "alive.log was removed")
            self.assertTrue((create / "found.log").exists(), "found.log was removed")


class TestBuildLiveOnAStaleLog(unittest.TestCase):
    """Status files are claims, not evidence (CLAUDE.md rule 5's cousin):
    `state=running` alone does not mean a build is live -- lib/detach.sh's
    build_live also demands the log have moved within WK_STALL_SECONDS."""

    def _write_running(self, sf):
        return f'''
. "{REPO}/lib/detach.sh"
status_write "{sf}" state=running pid=$$ stage=build
'''

    def test_stale_log_is_not_live(self):
        with scratch_dir(prefix="wk-test-buildlive-") as tmp:
            sf = tmp / "build.status"
            log = tmp / "build.log"
            script = f'''
{self._write_running(sf)}
: > "{log}"
touch -t 202001010000 "{log}"
build_live "{sf}" "{log}" && echo LIVE || echo NOTLIVE
'''
            cp = bash(script)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertEqual(cp.stdout.strip(), "NOTLIVE", cp.stdout + cp.stderr)

    def test_fresh_log_is_live(self):
        with scratch_dir(prefix="wk-test-buildlive-") as tmp:
            sf = tmp / "build.status"
            log = tmp / "build.log"
            script = f'''
{self._write_running(sf)}
: > "{log}"
build_live "{sf}" "{log}" && echo LIVE || echo NOTLIVE
'''
            cp = bash(script)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertEqual(cp.stdout.strip(), "LIVE", cp.stdout + cp.stderr)

    def test_non_running_state_is_never_live_even_with_a_fresh_log(self):
        with scratch_dir(prefix="wk-test-buildlive-") as tmp:
            sf = tmp / "build.status"
            log = tmp / "build.log"
            script = f'''
. "{REPO}/lib/detach.sh"
status_write "{sf}" state=ok pid=$$ stage=build
: > "{log}"
build_live "{sf}" "{log}" && echo LIVE || echo NOTLIVE
'''
            cp = bash(script)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertEqual(cp.stdout.strip(), "NOTLIVE", cp.stdout + cp.stderr)

    def test_missing_status_file_is_not_live(self):
        script = f'''
. "{REPO}/lib/detach.sh"
build_live "/nonexistent/build.status" "/nonexistent/build.log" && echo LIVE || echo NOTLIVE
'''
        cp = bash(script)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "NOTLIVE", cp.stdout + cp.stderr)


if __name__ == "__main__":
    unittest.main()
