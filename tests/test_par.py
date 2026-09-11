"""lib/par.sh: several jobs at once, each leaving its exit status as a
marker the moment it ends -- whatever way it ends. `wk status` polls those
markers (par_join_stream), so a job whose marker never lands is a listing
that never finishes.

Run: python3 -m unittest tests.test_par -v
"""
import subprocess
import unittest

from tests.support import REPO, WkTest, bash


PRELUDE = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/lib/par.sh"
worst=0
bump() {{ [ "$1" -gt "$worst" ] && worst="$1" || true; }}
exec 3>"$OUT"
'''


class TestParMarkers(WkTest):
    def _run(self, script):
        out = self.tmp / "records"
        try:
            cp = bash(PRELUDE + script, env={"OUT": str(out)}, timeout=30)
        except subprocess.TimeoutExpired:
            self.fail("par_join_stream never finished: a job left no marker")
        self.assertEqual(cp.returncode, 0, f"script failed: {cp.stdout}{cp.stderr}")
        return cp.stdout, out.read_text()

    def test_every_way_a_job_can_end_leaves_its_status(self):
        """return N, exit N, die, a `set -e` trip and success all land a marker"""
        script = '''
ok()       { echo '{"job":"ok"}' >&3; return 0; }
returns()  { echo '{"job":"returns"}' >&3; return 3; }
exits()    { echo '{"job":"exits"}' >&3; exit 2; }
dies()     { echo '{"job":"dies"}' >&3; die "on purpose"; }
trips()    { echo '{"job":"trips"}' >&3; false; echo unreachable >&3; }
par_begin
d="$_par_dir"
par_run ok ok; par_run returns returns; par_run exits exits
par_run dies dies; par_run trips trips
sleep 0.5
for n in ok returns exits dies trips; do printf '%s=%s\\n' "$n" "$(cat "$d/$n.rc")"; done
par_join_stream
echo "worst=$worst"
[ -d "$d" ] && echo "dir kept" || echo "dir removed"
'''
        stdout, records = self._run(script)
        self.assertIn("ok=0\nreturns=3\nexits=2\ndies=1\ntrips=1\n", stdout)
        self.assertIn("worst=3", stdout)
        self.assertIn("dir removed", stdout)
        for job in ("ok", "returns", "exits", "dies", "trips"):
            self.assertIn(f'{{"job":"{job}"}}', records)
        self.assertNotIn("unreachable", records)
        for job in ("ok", "returns", "exits", "dies", "trips"):
            self.assertIn(f'{{"kind":"flush","job":"{job}"}}', records)
        self.assertEqual(records.count('"kind":"flush"'), 5)

    def test_par_join_reports_the_worst_status_in_start_order(self):
        """the non-streaming join keeps start order and raises the worst status"""
        script = '''
a() { sleep 0.3; echo A >&3; return 2; }
b() { echo B >&3; return 0; }
par_begin; par_run a a; par_run b b; par_join
echo "worst=$worst"
'''
        stdout, records = self._run(script)
        self.assertIn("worst=2", stdout)
        self.assertEqual(records, "A\nB\n")


class TestParRc(WkTest):
    """`par_rc` is how a caller that replays the records itself -- `wk key
    check`'s table, `wk sync`'s workspace list -- asks what one row's job
    made of it, in an order of its own rather than the order they finished."""

    def _run(self, script):
        out = self.tmp / "records"
        cp = bash(PRELUDE + script, env={"OUT": str(out)}, timeout=30)
        self.assertEqual(cp.returncode, 0, f"script failed: {cp.stdout}{cp.stderr}")
        return cp.stdout

    def test_each_jobs_status_is_readable_by_name(self):
        script = '''
slow_ok()  { sleep 0.3; echo SLOW >&3; return 0; }
quick_bad(){ echo QUICK >&3; return 2; }
par_begin; par_run slow slow_ok; par_run quick quick_bad; par_wait
for n in quick slow; do printf '%s=%s:%s\\n' "$n" "$(par_rc "$n")" "$(par_record "$n")"; done
par_end
'''
        stdout = self._run(script)
        self.assertIn("quick=2:QUICK", stdout)
        self.assertIn("slow=0:SLOW", stdout)

    def test_a_name_no_job_ran_under_is_refused(self):
        script = '''
par_begin; par_run only true; par_wait
rc=0; par_rc absent || rc=$?
echo "refused=$rc"
par_end
'''
        self.assertIn("refused=1", self._run(script))
