"""One lock per mutated resource (CLAUDE.md rule 4): concurrent takers of
`hold_lock` (lib/common.sh) serialise; a lock left by a holder that is gone
is reclaimed, not waited out forever; and a purely reporting command
(`wk status`, `wk ls` -- rule 6, read-only is read-only) takes no lock at
all. Every test here points WK_LOCK_DIR at a scratch directory
(`wk_lock_dir` honours the env override), so nothing touches this machine's
real lock state.

Run: python3 -m unittest tests.test_concurrency -v
"""
import unittest

from tests.support import REPO, WkTest, bash, run, scratch_dir


class TestHoldLockSerializes(WkTest):
    def test_two_takers_of_one_resource_do_not_overlap(self):
        with scratch_dir(prefix="wk-test-lock-") as d:
            lockdir = d / "locks"
            order = d / "order.log"
            script = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
export WK_LOCK_DIR="{lockdir}"

hold_and_report() {{
    local tag="$1" hold="$2"
    ( hold_lock demo-resource -w 20
      echo "$tag enter $(date +%s.%N)" >> "{order}"
      sleep "$hold"
      echo "$tag exit  $(date +%s.%N)" >> "{order}"
    )
}}

hold_and_report A 1 &
pidA=$!
sleep 0.3
hold_and_report B 0.2 &
pidB=$!
wait "$pidA" "$pidB"
'''
            cp = bash(script, timeout=30)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

            lines = order.read_text().splitlines()
            events = {}
            for ln in lines:
                tag, kind, ts = ln.split()
                events.setdefault(tag, {})[kind] = float(ts)

            self.assertIn("A", events)
            self.assertIn("B", events)
            # B (which asked for the lock second) must not enter its
            # critical section before A has left its own -- that is what
            # "one lock per resource" means.
            self.assertGreaterEqual(
                events["B"]["enter"], events["A"]["exit"],
                f"the two takers overlapped: {events}",
            )


class TestDeadHoldersLockIsReclaimed(WkTest):
    def test_a_lock_left_by_a_dead_pid_is_broken_rather_than_waited_out(self):
        with scratch_dir(prefix="wk-test-lock-") as d:
            lockdir = d / "locks"
            lockdir.mkdir()
            script = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
export WK_LOCK_DIR="{lockdir}"
h=$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo local)
# A pid above every default pid_max on both platforms: dead by construction
# (the same convention tests/test_state.py's status-file test uses).
ln -s "pid=4194304 tok=deadbeef at=2020-01-01T00:00:00Z cmd=stale" \
    "{lockdir}/demo-resource@$h.lock"

hold_lock demo-resource -w 5
echo "took it: $(readlink "{lockdir}/demo-resource@$h.lock")"
'''
            cp = bash(script, timeout=15)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("took it:", cp.stdout, cp.stdout)
            self.assertNotIn(
                "pid=4194304", cp.stdout,
                f"hold_lock did not replace the dead holder's payload: {cp.stdout}",
            )
            self.assertIn(
                f"pid={cp.stdout.split('pid=')[-1].split()[0]}",
                cp.stdout,
                "the reclaimed lock should carry the new holder's own pid",
            )


class TestReadOnlyCommandsTakeNoLock(WkTest):
    def test_status_and_ls_take_no_lock(self):
        # --no-fleet because the question here is whether a reporting command
        # takes a lock, not how fast it is: a bare `wk status` walks the fleet
        # over ssh, and how long five boards take to not answer is network
        # weather, not a property of this test. The lock path is the same one.
        with scratch_dir(prefix="wk-test-lock-") as d:
            lockdir = d / "locks"
            run("status", "--no-fleet", env={"WK_LOCK_DIR": str(lockdir)}, timeout=60)
            run("ls", env={"WK_LOCK_DIR": str(lockdir)}, timeout=60)
            left = list(lockdir.glob("*")) if lockdir.exists() else []
            self.assertEqual(
                left, [],
                f"'wk status'/'wk ls' left lock file(s) behind: {left} "
                "-- a reporting command must take no lock (rule 6)",
            )


if __name__ == "__main__":
    unittest.main()
