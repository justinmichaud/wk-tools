# HANDOFF — status while a command runs

Owed: every long-running command reports its progress the same way, and
`wk status` reads it.

- [ ] one progress record per running command (`wk pi bench`, `wk ab`, `wk sysimage build`, `wk sysimage webkit`, `wk build`, `wk bench`): what step it is on, of how many, since when, and the log to follow -- written where `wk status` reads a build's `alive: [n/m] (last output Xs ago)` today, and dying with the command (a lock is not state) [decision: one shape for every command, in lib/]
- [ ] `wk pi bench` prints a line per iteration and a heartbeat while a run is silent (run-benchmark logs "Start the iteration i of n"; today the round line is the last word for up to the plan's timeout), and `wk ab` prints which board/round it is on -- from the same record, not a second print [needs the record above]
- [ ] `wk status` shows every running command's record in the fleet/bench block (today it names only the latest bench result directory, and a detached buildroot or yocto image stage shows as `build=none`, so `wk status <ws> --wait` returns at once) [needs the record above]
