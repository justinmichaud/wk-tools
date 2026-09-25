"""A plan as a graph. A step is an id, the machine it runs on, the steps it needs, what it holds exclusively, a
`done()` asking whether it need not run and a `run()` returning its exit status; the scheduler starts every
ready step whose holds are free."""

import concurrent.futures as futures

from wk import act
from wk.act import die

LOGGED = 'l=$1; shift; exec "$@" >>"$l" 2>&1'


def _list(value):
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return tuple(w for w in value.replace(",", " ").split() if w)


class Step:
    def __init__(self, id, machine, needs=(), holds=(), done=None, run=None, command=""):
        self.id, self.machine, self.command = id, machine, command
        self.needs, self.holds = _list(needs), _list(holds)
        self.done, self.run = done, run

    def __repr__(self):
        return "Step(%s)" % self.id


def validate(steps):
    ids = [s.id for s in steps]
    for s in steps:
        if not s.id:
            die("a step declared no id")
        if s.run is None:
            die("step '%s' declares no command" % s.id)
        if ids.count(s.id) > 1:
            die("step '%s' is declared %d times; an id names one step" % (s.id, ids.count(s.id)))
        for need in s.needs:
            if need not in ids:
                die("step '%s' needs '%s', which no step declares" % (s.id, need))
    by_id, state = {s.id: s for s in steps}, {}

    def walk(step, path):
        if state.get(step.id) == "done":
            return
        if state.get(step.id) == "open":
            die("these steps need each other, so none of them can ever run: "
                + " -> ".join(path[path.index(step.id):] + [step.id]))
        state[step.id] = "open"
        for need in step.needs:
            walk(by_id[need], path + [step.id])
        state[step.id] = "done"

    for s in steps:
        walk(s, [])
    return steps


def waves(steps, done=()):
    """What runs at once, in order, if every step took the same time; the scheduler itself is event-driven."""
    finished, left, out = set(done), [s for s in steps if s.id not in done], []
    while left:
        wave, held = [], set()
        for s in left:
            if not set(s.needs) <= finished or held & set(s.holds):
                continue
            held |= set(s.holds)
            wave.append(s)
        if not wave:
            die("no step is ready and none is running: " + ", ".join(s.id for s in left))
        out.append(wave)
        finished |= {s.id for s in wave}
        left = [s for s in left if s not in wave]
    return out


def plan_order(steps):
    return [s for wave in waves(steps) for s in wave]


def needed(steps, done=()):
    """Steps not done that a needed step still needs: a phase feeding a slot that already holds the commit is not run again."""
    done = set(done)
    dependents = {s.id: [] for s in steps}
    for s in steps:
        for n in s.needs:
            dependents[n].append(s.id)
    out = set()
    for s in reversed(plan_order(steps)):
        if s.id not in done and (not dependents[s.id] or any(d in out for d in dependents[s.id])):
            out.add(s.id)
    return out


def log_name(step):
    """One log per resource, so a board's deploys and benchmarks read in order; a step holding nothing logs under its id."""
    name = step.holds[0].split(":", 1)[-1] if step.holds else step.id
    return "".join(c if c.isalnum() or c in "._-" else "-" for c in name) + ".log"


def done_ids(steps, pool=futures.ThreadPoolExecutor):
    """Asked all at once: a question forwarded into the podman machine costs 0.8-1.1 s (measured), so a plan waits for the slowest."""
    asked = [s for s in steps if s.done is not None]
    if not asked:
        return set()
    with pool(max_workers=len(asked)) as p:
        answers = list(p.map(lambda s: s.done(), asked))
    return {s.id for s, yes in zip(asked, answers) if yes}


def render(steps, done, out):
    want = needed(steps, done)
    out.write("graph -- %d step(s), each one command:\n" % len(steps))
    for s in steps:
        state = "already done" if s.id in done else "not needed" if s.id not in want else "to run"
        out.write("  %s  [%s]  on %s%s%s\n      %s\n" % (
            s.id, state, s.machine or "here", ", holds " + " ".join(s.holds) if s.holds else "",
            ", needs " + " ".join(s.needs) if s.needs else "", s.command))
    out.write("\nschedule -- what runs at once, in order:\n")
    schedule = waves(steps, set(done) | {s.id for s in steps if s.id not in want})
    for n, wave in enumerate(schedule, 1):
        out.write("  %d. %s\n" % (n, ", ".join(s.id for s in wave)))
    if not schedule:
        out.write("  nothing: every step is already done\n")
    out.write("\na step starts as soon as its needs are done and the resources it holds\n"
              "are free, so a wave above is the shape of the graph rather than a barrier.\n")


class Scheduler:
    """`announce(event, step, rc)` hears start, ok, already, unneeded, refused, failed and skipped; `pool` runs the steps."""

    def __init__(self, steps, announce=None, retry_exit=act.RETRY_EXIT, pool=futures.ThreadPoolExecutor):
        self.steps = plan_order(validate(list(steps)))
        self.retry_exit, self.pool = retry_exit, pool
        self.announce = announce or (lambda event, step, rc=0: None)
        self.ran, self.already, self.unneeded, self.failed, self.skipped, self.left = [], [], [], [], [], []

    def run_all(self):
        done = done_ids(self.steps, self.pool)
        want = needed(self.steps, done)
        self.already = [s for s in self.steps if s.id in done]
        self.unneeded = [s for s in self.steps if s.id not in done and s.id not in want]
        for s in self.already + self.unneeded:
            self.announce("already" if s.id in done else "unneeded", s)
        pending = {s.id for s in self.steps if s.id in want}
        done |= {s.id for s in self.unneeded}
        bad, refused, holding, live = set(), set(), set(), {}
        pool = self.pool(max_workers=max(1, len(self.steps)))
        try:
            while pending or live:
                self._start_ready(pending, done, bad, refused, holding, live, pool)
                if not live:
                    break
                self._collect(done, bad, refused, holding, live, pending)
        finally:
            pool.shutdown(wait=True)
        self.left = [s for s in self.steps if s.id in pending]
        return 0 if not (self.failed or self.skipped or self.left) else 1

    def _start_ready(self, pending, done, bad, refused, holding, live, pool):
        progress = True
        while progress:
            progress = False
            for s in self.steps:
                if s.id not in pending:
                    continue
                if set(s.needs) & bad:
                    pending.discard(s.id)
                    bad.add(s.id)
                    self.skipped.append(s)
                    self.announce("skipped", s)
                    progress = True
                    continue
                if not set(s.needs) <= done or s.id in refused or set(s.holds) & holding:
                    continue
                pending.discard(s.id)
                holding |= set(s.holds)
                self.announce("start", s)
                live[pool.submit(s.run)] = s
                progress = True

    def _collect(self, done, bad, refused, holding, live, pending):
        finished, _ = futures.wait(list(live), return_when=futures.FIRST_COMPLETED)
        settled = False
        for f in finished:
            s = live.pop(f)
            holding -= set(s.holds)
            rc = f.result()
            if rc == 0:
                done.add(s.id)
                self.ran.append(s)
                settled = True
                self.announce("ok", s, rc)
            elif rc == self.retry_exit:
                pending.add(s.id)
                refused.add(s.id)
                self.announce("refused", s, rc)
            else:
                bad.add(s.id)
                self.failed.append((s, rc))
                settled = True
                self.announce("failed", s, rc)
        if settled:
            refused.clear()


def say_event(order, event, step, rc=0, where=""):
    n = "[%d/%d] %s" % (order.index(step) + 1, len(order), step.id)
    return {"start": "%s: %s  (log: %s)" % (n, step.command, where or "here"),
            "already": n + ": already done",
            "unneeded": n + ": not needed -- what it feeds is already done",
            "ok": n + ": done",
            "refused": "%s: refused for now (exit %d); it is tried again when something else ends" % (n, rc),
            "failed": "%s: FAILED (exit %d)" % (n, rc),
            "skipped": n + ": not run -- a step it needs did not finish"}[event]


def summary(sched):
    return (["failed: %s (exit %d): %s" % (s.id, rc, s.command) for s, rc in sched.failed]
            + ["not run: %s: %s" % (s.id, s.command) for s in sched.skipped + sched.left])


def wk_step(machine, wk, log_of, sid, on, needs, holds, done, words, target=""):
    argv = (["env", "WK_TARGET=" + target] if target else []) + [wk] + list(words)
    s = Step(sid, on, needs, holds, done, command=("WK_TARGET=%s " % target if target else "") + " ".join(["wk"] + list(words)))
    s.run = lambda: machine.act_run(["sh", "-c", LOGGED, "sh", log_of(s)] + argv).rc
    return s


def wk_yes(machine, argv):
    """A readonly `wk` command's verdict is its last line: one forwarded to a stopped podman machine exits 0 saying so."""
    return lambda: machine.run(argv).out.strip().splitlines()[-1:] == ["yes"]
