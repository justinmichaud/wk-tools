#!/usr/bin/env python3
"""A plan as a graph. A step declares one command, the machine it runs on, the
steps it needs, what it holds exclusively and how to ask whether it is done;
the scheduler starts every ready step whose resources are free."""

import argparse
import concurrent.futures as futures
import os
import subprocess
import sys

FIELDS = ("id", "machine", "needs", "holds", "done", "command")
RETRY_EXIT = 75


def _list(value):
    return tuple(w for w in value.replace(",", " ").split() if w)


class Step:
    def __init__(self, id, machine, needs, holds, done, command):
        self.id = id
        self.machine = machine
        self.needs = _list(needs)
        self.holds = _list(holds)
        self.done = done.strip()
        self.command = command.strip()

    def __repr__(self):
        return "Step(%s)" % self.id


def parse_steps(text):
    steps = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != len(FIELDS):
            sys.exit("step record %d has %d fields, not %d (%s)"
                     % (n, len(fields), len(FIELDS), "<tab>".join(FIELDS)))
        steps.append(Step(*fields))
    validate(steps)
    return steps


def validate(steps):
    ids = [s.id for s in steps]
    for s in steps:
        if not s.id:
            sys.exit("a step declared no id")
        if not s.command:
            sys.exit("step '%s' declares no command" % s.id)
        if ids.count(s.id) > 1:
            sys.exit("step '%s' is declared %d times; an id names one step"
                     % (s.id, ids.count(s.id)))
        for need in s.needs:
            if need not in ids:
                sys.exit("step '%s' needs '%s', which no step declares" % (s.id, need))
    _refuse_cycle(steps)


def _refuse_cycle(steps):
    by_id = {s.id: s for s in steps}
    state = {}

    def walk(step, path):
        if state.get(step.id) == "done":
            return
        if state.get(step.id) == "open":
            here = path[path.index(step.id):] + [step.id]
            sys.exit("these steps need each other, so none of them can ever run: "
                     + " -> ".join(here))
        state[step.id] = "open"
        for need in step.needs:
            walk(by_id[need], path + [step.id])
        state[step.id] = "done"

    for s in steps:
        walk(s, [])


def waves(steps, done=()):
    """What runs at once, in order, if every step took the same time; the
    scheduler itself is event-driven."""
    finished, left, out = set(done), [s for s in steps if s.id not in done], []
    while left:
        wave, held = [], set()
        for s in left:
            if not set(s.needs) <= finished or held & set(s.holds):
                continue
            held |= set(s.holds)
            wave.append(s)
        if not wave:
            sys.exit("no step is ready and none is running: " + ", ".join(s.id for s in left))
        out.append(wave)
        finished |= {s.id for s in wave}
        left = [s for s in left if s not in wave]
    return out


def plan_order(steps):
    return [s for wave in waves(steps) for s in wave]


def needed(steps, done=()):
    """What is left: a step is needed when it is not done and something needed
    still needs it -- a phase feeding a slot that already holds the commit is
    not run again."""
    done = set(done)
    dependents = {s.id: [] for s in steps}
    for s in steps:
        for n in s.needs:
            dependents[n].append(s.id)
    out = set()
    for s in reversed(plan_order(steps)):
        if s.id in done:
            continue
        if not dependents[s.id] or any(d in out for d in dependents[s.id]):
            out.add(s.id)
    return out


class Scheduler:
    """`run(step)` returns its exit status and `is_done(step)` whether it need
    not run; both are injected, so the schedule is testable against fakes."""

    def __init__(self, steps, run, is_done, retry_exit=RETRY_EXIT, announce=None):
        self.steps = plan_order(steps)
        self.run = run
        self.is_done = is_done
        self.retry_exit = retry_exit
        self.announce = announce or (lambda event, step, rc=0: None)
        self.ran, self.already, self.unneeded = [], [], []
        self.failed, self.skipped, self.left = [], [], []

    def run_all(self):
        # Asked once, before anything starts: what is done decides what is worth running.
        done = {s.id for s in self.steps if s.done and self.is_done(s)}
        want = needed(self.steps, done)
        self.already = [s for s in self.steps if s.id in done]
        self.unneeded = [s for s in self.steps if s.id not in done and s.id not in want]
        for s in self.already + self.unneeded:
            self.announce("already" if s.id in done else "unneeded", s)
        pending = {s.id for s in self.steps if s.id in want}
        done |= {s.id for s in self.unneeded}
        bad, refused, holding, live = set(), set(), set(), {}
        pool = futures.ThreadPoolExecutor(max_workers=max(1, len(self.steps)))
        try:
            while pending or live:
                self._start_ready(pending, done, bad, refused, holding, live, pool)
                if not live:
                    break
                self._collect(pending, done, bad, refused, holding, live)
        finally:
            pool.shutdown(wait=True)
        for s in self.steps:
            if s.id in pending:
                self.left.append(s)
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
                live[pool.submit(self.run, s)] = s
                progress = True

    def _collect(self, pending, done, bad, refused, holding, live):
        finished, _ = futures.wait(live, return_when=futures.FIRST_COMPLETED)
        settled = False
        for f in finished:
            s = live.pop(f)
            rc = f.result()
            holding -= set(s.holds)
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


def _shell(prelude, snippet, **kw):
    script = "".join('. %s\n' % _quote(p) for p in prelude) + snippet
    return subprocess.run(["bash", "-c", script], **kw).returncode


def _quote(word):
    return "'%s'" % word.replace("'", "'\\''")


def _log_name(step):
    """One log per resource: a board's deploys and benchmarks read in order,
    and a step holding nothing logs under its own id."""
    name = step.holds[0].split(":", 1)[-1] if step.holds else step.id
    return "".join(c if c.isalnum() or c in "._-" else "-" for c in name) + ".log"


def _render_plan(steps, done_ids, out):
    want = needed(steps, done_ids)
    skip = set(done_ids) | {s.id for s in steps if s.id not in want}
    print("graph -- %d step(s), each one command:" % len(steps), file=out)
    for s in steps:
        if s.id in done_ids:
            state = "already done"
        elif s.id not in want:
            state = "not needed"
        else:
            state = "to run"
        print("  %s  [%s]  on %s%s%s" % (
            s.id, state, s.machine or "here",
            ", holds " + " ".join(s.holds) if s.holds else "",
            ", needs " + " ".join(s.needs) if s.needs else ""), file=out)
        print("      %s" % s.command, file=out)
    print("", file=out)
    print("schedule -- what runs at once, in order:", file=out)
    schedule = waves(steps, skip)
    for n, wave in enumerate(schedule, 1):
        print("  %d. %s" % (n, ", ".join(s.id for s in wave)), file=out)
    if not schedule:
        print("  nothing: every step is already done", file=out)
    print("", file=out)
    print("a step starts as soon as its needs are done and the resources it holds", file=out)
    print("are free, so a wave above is the shape of the graph rather than a barrier.", file=out)


def _done_ids(steps, prelude):
    return {s.id for s in steps
            if s.done and _shell(prelude, s.done,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0}


def cmd_plan(args, steps):
    _render_plan(steps, _done_ids(steps, args.prelude), sys.stdout)
    return 0


def cmd_steps(args, steps):
    for s in plan_order(steps):
        print(s.command)
    return 0


def cmd_run(args, steps):
    order = plan_order(steps)
    index = {s.id: n for n, s in enumerate(order, 1)}

    def is_done(step):
        return _shell(args.prelude, step.done,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0

    def run(step):
        if args.log_dir:
            os.makedirs(args.log_dir, exist_ok=True)
            with open(os.path.join(args.log_dir, _log_name(step)), "a") as log:
                return _shell(args.prelude, step.command, stdout=log, stderr=log)
        return _shell(args.prelude, step.command)

    def announce(event, step, rc=0):
        n = index[step.id]
        if event == "start":
            where = os.path.join(args.log_dir, _log_name(step)) if args.log_dir else "here"
            say("[%d/%d] %s: %s  (log: %s)" % (n, len(order), step.id, step.command, where))
            if args.on_start:
                _shell(args.prelude,
                       args.on_start.replace("{step}", str(n)).replace("{id}", step.id))
        elif event == "already":
            say("[%d/%d] %s: already done" % (n, len(order), step.id))
        elif event == "unneeded":
            say("[%d/%d] %s: not needed -- what it feeds is already done"
                % (n, len(order), step.id))
        elif event == "ok":
            say("[%d/%d] %s: done" % (n, len(order), step.id))
        elif event == "refused":
            say("[%d/%d] %s: refused for now (exit %d); it is tried again when "
                "something else ends" % (n, len(order), step.id, rc))
        elif event == "failed":
            say("[%d/%d] %s: FAILED (exit %d)" % (n, len(order), step.id, rc))
        elif event == "skipped":
            say("[%d/%d] %s: not run -- a step it needs did not finish" % (n, len(order), step.id))

    sched = Scheduler(steps, run, is_done, retry_exit=args.retry_exit, announce=announce)
    rc = sched.run_all()
    for step, code in sched.failed:
        say("failed: %s (exit %d): %s" % (step.id, code, step.command))
    for step in sched.skipped + sched.left:
        say("not run: %s: %s" % (step.id, step.command))
    return rc


def say(message):
    print(message, file=sys.stderr, flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="sched.py",
        description="Schedule a graph of steps read as tab-separated records "
                    "(%s), one per line." % "<tab>".join(FIELDS))
    sub = ap.add_subparsers(dest="mode", required=True)
    for name, help_text in (("plan", "print the graph and the schedule; run nothing"),
                            ("steps", "the commands in schedule order, one per line"),
                            ("run", "run the graph")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--file", help="the step records (default: stdin)")
        p.add_argument("--prelude", action="append", default=[],
                       help="a shell file sourced before every command and predicate; repeatable")
    run = sub.choices["run"]
    run.add_argument("--log-dir", help="one log per step under this directory")
    run.add_argument("--on-start", help="a shell snippet run as each step starts; "
                                        "{step} is its 1-based place in the schedule, {id} its id")
    run.add_argument("--retry-exit", type=int, default=RETRY_EXIT,
                     help="the exit status that means 'not now' rather than failed: the step "
                          "keeps its place and is tried again once another step ends "
                          "(default: %(default)s)")
    args = ap.parse_args(argv)
    text = open(args.file).read() if args.file else sys.stdin.read()
    steps = parse_steps(text)
    return {"plan": cmd_plan, "steps": cmd_steps, "run": cmd_run}[args.mode](args, steps)


if __name__ == "__main__":
    sys.exit(main())
