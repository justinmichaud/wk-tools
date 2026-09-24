#!/usr/bin/env python3
"""Structured-data operations the bash half of `wk bench` still calls: a plan's class, a cpu list, a Mac A/B's legs, and the record, report and stopping rule of lib/wk/bench as a CLI. Stdlib only, for whatever python3 a macOS host or a bare-metal board has."""

import argparse
import json
import os
import re
import sys


def _bench():
    """lib/wk/bench, imported on use: bench/mac-ab.sh pipes this file to a Mac's `python3 -` for ab-legs, with no lib/ beside it."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from wk.bench import record, report
    return record, report


# TODO: lib/wk/status.py imports wk.bench.record for task_state and subject_line, and these two go.
def task_state(taskdir, running):
    return _bench()[0].task_state(taskdir, running)


def _subject_line(doc):
    return _bench()[0].subject_line(doc)


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def cmd_get(args):
    record = _bench()[0]
    value = record.get_nested(record.load(args.file), args.key)
    print(value if value is not None else args.default)


def _pipeline():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from wk.bench import pipeline
    return pipeline


def cmd_bench_class(args):
    print(_pipeline().bench_class(args.plan))


def cmd_cores_valid(args):
    sys.exit(0 if _pipeline().cores_valid(args.set) else 1)


def cmd_cores_wrap(args):
    if not _pipeline().cores_valid(args.set):
        sys.exit("cores-wrap: not a valid cpu list: %s" % args.set)
    sys.stdout.write("taskset -c %s " % args.set)


def _state_lines(path):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                if "=" in line:
                    k, v = line.rstrip("\n").split("=", 1)
                    out[k] = v
    except OSError:
        pass
    return out


def _elapsed(start, end=None):
    import calendar, time as _time
    def _epoch(t):
        return calendar.timegm(_time.strptime(t, "%Y-%m-%dT%H:%M:%SZ"))
    try:
        secs = (_epoch(end) if end else _time.time()) - _epoch(start)
    except (ValueError, TypeError):
        return "for an unreadable span"
    return "%dm%02ds" % (int(secs) // 60, int(secs) % 60)


def cmd_ab_legs(args):
    root = args.root
    job = _load(os.path.join(root, "job.json")) or {}
    state = _state_lines(os.path.join(root, "autorun.state"))
    plans = job.get("plans") or []
    arms = job.get("arms") or []
    rounds = int(job.get("rounds") or 0)
    # The warmup round runs the first plan only, one leg per arm (mac-bench-autorun.sh).
    planned = len(arms) + rounds * len(plans) * len(arms)
    done = sum(1 for k in state if k.startswith("ok_"))
    print("%d of %d planned -- warmup %d, then %d round(s) x %d plan(s) x %d arm(s)"
          % (done, planned, len(arms), rounds, len(plans), len(arms)))
    began, ended = state.get("started_at"), state.get("finished_at")
    if began:
        span = _elapsed(began, ended)
        print("started %s, %s" % (began, ("ran %s" % span) if ended else ("running %s so far" % span)))

    stamp = state.get("job_stamp") or job.get("stamp") or ""
    # The warmup's rows are dropped from the map once it completes, so a leg the map does not name is a warmup leg.
    named = {}
    try:
        with open(os.path.join(root, "ab", stamp, "runs.tsv")) as f:
            for line in f:
                cols = line.rstrip("\n").split("\t")
                if len(cols) >= 6:
                    named[cols[3]] = (cols[0], cols[1], cols[4])
    except OSError:
        pass

    # The volume keeps every result it ever produced and a run directory is named for the instant its leg began, so this job's are the ones at or after the moment its autorun started; with no such moment it has run none, and listing the directory would report an older experiment as this one.
    since = re.sub(r"[-:]", "", state.get("started_at") or "")
    results = os.path.join(root, "results")
    rids = []
    if since:
        try:
            rids = sorted(d for d in os.listdir(results) if d.split("-", 1)[0] >= since)
        except OSError:
            rids = []
    if not rids:
        print("(no leg of this job has produced a result yet)")
        return
    for idx, rid in enumerate(rids):
        env = _load(os.path.join(results, rid, "env.json")) or {}
        if rid in named:
            rnd, label, clean = named[rid]
        else:  # a leg reaches the map when it ends, so the one in flight is never in it; the warmup is what runs before any measured round, one leg per arm
            rnd, label, clean = ("warmup" if idx < len(arms) else "-", "", "-")
        if not label:
            for arm in arms:
                if arm.get("id") and rid.endswith(arm["id"]):
                    label = arm.get("label", "")
                    break
        wall = env.get("wall_time_s")
        print("%-7s %-3s %-14s %6s  %s" % (
            rnd, label or "?", env.get("plan") or "?",
            (str(wall) + "s") if wall else "running", clean))

    warmup = os.path.join(root, "ab", stamp, "warmup")
    try:
        caps = sorted(f for f in os.listdir(warmup) if f.endswith(".json.gz"))
    except OSError:
        caps = []
    if caps:
        print("warmup captures: %s" % ", ".join(caps))
    else:  # the round exists to carry a profile the measured rounds cannot take, so an empty directory is the whole round wasted
        print("warmup captures: none in %s" % warmup)


def cmd_subtests(args):
    """The plan's own subtests minus the exclusions, so both arms of an A/B cover the same set."""
    plan = json.load(sys.stdin)
    listed = []
    for group in (plan.get("subtests") or {}).values():
        listed.extend(group)
    drop = {x for x in (args.exclude or "").split(",") if x}
    unknown = drop - set(listed)
    if unknown:
        sys.exit("subtests: %s names no subtest of this plan" % ", ".join(sorted(unknown)))
    keep = [s for s in listed if s not in drop]
    if not keep:
        sys.exit("subtests: every subtest of this plan is excluded")
    print(" ".join(keep))


def cmd_env_record(args):
    _bench()[0].write_env(args.out, args.fields, args.bool_fields, args.update)


def cmd_task_write(args):
    _bench()[0].task_write(args.dir, args.fields, args.commands)


def cmd_task_status(args):
    record = _bench()[0]
    print("\n".join(record.status_lines(record.task_state(args.dir, args.running))))


def cmd_warmup_check(args):
    problems = _bench()[1].warmup_check(args.a, args.b, args.same_width)
    for line in problems:
        print(line)
    sys.exit(1 if problems else 0)


def cmd_ab_precision(args):
    _bench()[1].precision(args.a, args.b, args.target)


def main(argv):
    parser = argparse.ArgumentParser(prog="wkdata.py", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("get", help="print one dotted field out of a JSON file")
    p.add_argument("file")
    p.add_argument("key")
    p.add_argument("--default", default="")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("task-write", help="write a task's task.json")
    p.add_argument("dir")
    p.add_argument("fields", nargs="*", metavar="key=value")
    p.add_argument("--command", dest="commands", action="append", default=[], metavar="CMD")
    p.set_defaults(func=cmd_task_write)

    p = sub.add_parser("task-status", help="a task's state, recomputed from its runs, as key=value lines")
    p.add_argument("dir")
    p.add_argument("--running", action="store_true", help="the task's lock is held")
    p.set_defaults(func=cmd_task_status)

    p = sub.add_parser("warmup-check", help="judge a warmup round's two arms; exit 1 and print why if they refuse the A/B")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--same-width", action="store_true",
                   help="the two arms are meant to be the same word size (a slot A/B, not an image one)")
    p.set_defaults(func=cmd_warmup_check)

    p = sub.add_parser("cores-valid", help="exit 0 if <set> is a valid taskset -c cpu list, 1 otherwise")
    p.add_argument("set")
    p.set_defaults(func=cmd_cores_valid)

    p = sub.add_parser("cores-wrap", help="print the 'taskset -c <set> ' prefix for a valid cpu list")
    p.add_argument("set")
    p.set_defaults(func=cmd_cores_wrap)

    p = sub.add_parser("ab-precision", help="how fine a difference the rounds so far resolve, and whether that meets --target")
    p.add_argument("--a", required=True, help="comma-separated run directories for arm A")
    p.add_argument("--b", required=True, help="comma-separated run directories for arm B")
    p.add_argument("--target", type=float, default=0.3, help="the effect the A/B has to be able to detect, in percent (default 0.3)")
    p.set_defaults(func=cmd_ab_precision)

    p = sub.add_parser("subtests", help="the plan's subtests minus --exclude, read from stdin")
    p.add_argument("--exclude", default="", help="comma-separated subtests to drop")
    p.set_defaults(func=cmd_subtests)

    p = sub.add_parser("bench-class", help="cpu or gpu -- what a plan measures")
    p.add_argument("plan")
    p.set_defaults(func=cmd_bench_class)

    p = sub.add_parser("env-record", help="write a run's env.json")
    p.add_argument("out")
    p.add_argument("fields", nargs="*", metavar="key=value")
    p.add_argument("--bool", dest="bool_fields", action="append", default=[], metavar="key=value")
    p.add_argument("--update", action="store_true",
                    help="merge onto the existing file instead of overwriting it (e.g. wall_time_s, after the run)")
    p.set_defaults(func=cmd_env_record)

    p = sub.add_parser("ab-legs", help="every leg an A/B has run so far, against what its job planned")
    p.add_argument("root", help="the bench root holding job.json, autorun.state, ab/ and results/")
    p.set_defaults(func=cmd_ab_legs)

    # argparse fills `nargs="*"` from one unbroken run of words, so a field after `--update` is a leftover: a field where the subcommand takes fields, an error elsewhere.
    args, extra = parser.parse_known_args(argv)
    if extra:
        if hasattr(args, "fields"):
            args.fields = list(args.fields) + extra
        else:
            parser.error("unrecognized arguments: %s" % " ".join(extra))
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
