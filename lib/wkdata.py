#!/usr/bin/env python3
"""Structured-data reads for `wk bench`, stdlib only, for whatever python3 a macOS host or a bare-metal board has: a job's legs against its plan, and the A/B stopping rule as a CLI."""

import argparse
import json
import os
import re
import sys


def _bench():
    # lib/wk/bench, imported on use: bench/mac.py's Remote.py hands this file alone to a Mac's `python3 -c` for `ab-legs`, where __file__ does not even exist, so nothing above this line may import wk.*; only `ab-precision`, always run as a file, needs it.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from wk.bench import report
    return report


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


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
    # The warmup round runs the first plan only, one leg per arm (lib/wk/bench/autorun.py).
    planned = len(arms) + rounds * len(plans) * len(arms)
    done = sum(1 for k in state if k.startswith("ok_"))
    print("%d of %d planned -- warmup %d, then %d round(s) x %d plan(s) x %d arm(s)"
          % (done, planned, len(arms), rounds, len(plans), len(arms)))
    began, ended = state.get("started_at"), state.get("finished_at")
    if began:
        span = _elapsed(began, ended)
        print("started %s, %s" % (began, ("ran %s" % span) if ended else ("running %s so far" % span)))

    stamp = state.get("job_stamp") or job.get("stamp") or ""
    named = {}
    try:
        with open(os.path.join(root, "ab", stamp, "runs.tsv")) as f:
            for line in f:
                cols = line.rstrip("\n").split("\t")
                if len(cols) >= 6:
                    named[cols[3]] = (cols[0], cols[1], cols[4])
    except OSError:
        pass

    # The volume keeps every result it ever produced and a run directory is named for the instant its leg began, so this job's are the ones at or after the moment its autorun started; with no such moment it has run none, and listing the directory would report an older A/B task as this one.
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
    else:
        print("warmup captures: none in %s" % warmup)


def cmd_ab_precision(args):
    _bench().precision(args.a, args.b, args.target)


def main(argv):
    parser = argparse.ArgumentParser(prog="wkdata.py", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ab-precision", help="how fine a difference the rounds so far resolve, and whether that meets --target")
    p.add_argument("--a", required=True, help="comma-separated run directories for arm A")
    p.add_argument("--b", required=True, help="comma-separated run directories for arm B")
    p.add_argument("--target", type=float, default=0.3, help="the effect the A/B has to be able to detect, in percent (default 0.3)")
    p.set_defaults(func=cmd_ab_precision)

    p = sub.add_parser("ab-legs", help="every leg an A/B has run so far, against what its job planned")
    p.add_argument("root", help="the bench root holding job.json, autorun.state, ab/ and results/")
    p.set_defaults(func=cmd_ab_legs)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
