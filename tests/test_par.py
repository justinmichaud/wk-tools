"""lib/par.sh: several jobs at once, each leaving its exit status as a
marker the moment it ends -- whatever way it ends. par_wait collects those
statuses in start order, so a job whose marker never lands is a caller that never finishes.

Run: python3 -m unittest tests.test_par -v
"""
import subprocess
import unittest

from tests.support import REPO, WkTest, bash


PRELUDE = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/lib/par.sh"
exec 3>"$OUT"
'''


class TestParMarkers(WkTest):
    def _run(self, script):
        out = self.tmp / "records"
        try:
            cp = bash(PRELUDE + script, env={"OUT": str(out)}, timeout=30)
        except subprocess.TimeoutExpired:
            self.fail("par_wait never finished: a job left no marker")
        self.assertEqual(cp.returncode, 0, f"script failed: {cp.stdout}{cp.stderr}")
        return cp.stdout

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
par_wait
for n in ok returns exits dies trips; do printf '%s=%s %s\\n' "$n" "$(cat "$d/$n.rc")" "$(par_record "$n")"; done
echo "status=$_par_status"
par_end
[ -d "$d" ] && echo "dir kept" || echo "dir removed"
'''
        stdout = self._run(script)
        for job, rc in (("ok", 0), ("returns", 3), ("exits", 2), ("dies", 1), ("trips", 1)):
            self.assertIn('%s=%d {"job":"%s"}' % (job, rc, job), stdout)
        self.assertIn("status= ok 0 returns 3 exits 2 dies 1 trips 1", stdout)
        self.assertIn("dir removed", stdout)
        self.assertNotIn("unreachable", stdout)

    def test_the_statuses_come_back_in_start_order(self):
        """a slow first job is still first, whatever finished first"""
        script = '''
a() { sleep 0.3; echo A >&3; return 2; }
b() { echo B >&3; return 0; }
par_begin; par_run a a; par_run b b; par_wait
echo "status=$_par_status"; par_record a; par_record b
par_end
'''
        stdout = self._run(script)
        self.assertIn("status= a 2 b 0", stdout)
        self.assertIn("A\nB\n", stdout)


if __name__ == "__main__":
    unittest.main()
