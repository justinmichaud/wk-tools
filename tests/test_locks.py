"""lib/common.sh's lock: it dies with its holder, admits one taker at a
time, and its release composes with a command's own exit handlers.

Run: python3 -m unittest tests.test_locks -v
"""
import unittest

from tests.support import REPO, WkTest, lock_bash


HOLDER = f"bash -c '. {REPO}/lib/common.sh; hold_lock %s; exec sleep 30' &"
TAKEN = 'i=0; until [ -L "$(_lock_path %s)" ] || [ $((i += 1)) -gt 50 ]; do sleep 0.1; done'


class TestLocks(WkTest):
    def test_lock_dies_with_its_holder(self):
        """a lock dies with its holder, and a lock naming nobody is not waited out"""
        script = f'''
{HOLDER % "k"}
p=$!; {TAKEN % "k"}; kill -9 $p 2>/dev/null; wait $p 2>/dev/null
s=$(date +%s); ( hold_lock k -w 20 >/dev/null 2>&1 )
[ $(( $(date +%s) - s )) -lt 3 ] || echo "killed holder: waited"

mkdir -p "$(_lock_path h)"
s=$(date +%s); ( hold_lock h -w 20 >/dev/null 2>&1 )
[ $(( $(date +%s) - s )) -lt 3 ] || echo "holder-less lock: waited it out"

{HOLDER % "l"}
q=$!; {TAKEN % "l"}
( hold_lock l -w 2 >/dev/null 2>&1 ) && echo "live holder: walked in"
kill $q 2>/dev/null; wait $q 2>/dev/null
'''
        cp = lock_bash(script, self.tmp / "locks", timeout=30)
        self.assertEqual(cp.stdout.strip(), "", cp.stdout + cp.stderr)

    def test_takers_of_one_lock_one_at_a_time(self):
        """four takers of one lock, one at a time, past a dead holder's lock"""
        script = f'''
c="$WK_LOCK_DIR/n"; mkdir -p "$WK_LOCK_DIR"; echo 0 > "$c"
ln -s "pid=99999998 tok=dead at=x cmd=x" "$(_lock_path r)"
for i in 1 2 3 4; do
    ( hold_lock r -w 60 >/dev/null 2>&1
      n=$(cat "$c"); sleep 0.2; echo $((n + 1)) > "$c" ) &
done
wait
[ "$(cat "$c")" = 4 ] || echo "four takers, $(cat "$c") critical sections"
[ -L "$(_lock_path r)" ] && echo "the lock was left behind"
true
'''
        cp = lock_bash(script, self.tmp / "locks", timeout=30)
        self.assertEqual(cp.stdout.strip(), "", cp.stdout + cp.stderr)

    def test_an_unreadable_holder_is_kept_not_cleared(self):
        """readlink failing is not evidence the holder is gone -- a transient
        read failure could hide a live hold, so the lock is kept, never
        cleared. This is `Lock.hold`'s own rule (lib/wk/lock.py); bash's
        `hold_lock` must make the same call rather than treating "cannot
        read" the same as "no holder in it"."""
        script = f'''
{HOLDER % "u"}
p=$!; {TAKEN % "u"}
mkdir -p "$WK_LOCK_DIR/bin"
printf '#!/bin/sh\\nexit 1\\n' > "$WK_LOCK_DIR/bin/readlink"
chmod +x "$WK_LOCK_DIR/bin/readlink"
PATH="$WK_LOCK_DIR/bin:$PATH" bash -c '. {REPO}/lib/common.sh
    hold_lock u -w 2' >"$WK_LOCK_DIR/out" 2>&1
rc=$?
[ "$rc" != 0 ] || echo "an unreadable holder was not waited out"
grep -q "cannot be read" "$WK_LOCK_DIR/out" || echo "no unreadable message: $(cat "$WK_LOCK_DIR/out")"
[ -L "$(_lock_path u)" ] || echo "an unreadable holder's lock was cleared"
kill $p 2>/dev/null; wait $p 2>/dev/null
'''
        cp = lock_bash(script, self.tmp / "locks", timeout=30)
        self.assertEqual(cp.stdout.strip(), "", cp.stdout + cp.stderr)

    def test_exit_handlers_compose(self):
        """a command's own end-of-run work does not disable the lock release"""
        script = f'''
r=$(bash -c '. {REPO}/lib/common.sh
             mine() {{ echo mine; }}
             hold_lock e; wk_atexit mine' 2>/dev/null)
[ "$r" = mine ] || echo "the command own handler did not run"
[ -L "$(_lock_path e)" ] && echo "the lock was not released"
bash -c '. {REPO}/lib/common.sh; hold_lock x; exit 7' >/dev/null 2>&1
[ $? = 7 ] || echo "the exit status did not survive the handlers"
true
'''
        cp = lock_bash(script, self.tmp / "locks", timeout=30)
        self.assertEqual(cp.stdout.strip(), "", cp.stdout + cp.stderr)


if __name__ == "__main__":
    unittest.main()
