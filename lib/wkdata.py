#!/usr/bin/env python3
"""A Mac A/B's legs against its job, stdlib only: `MacAB.read_status` hands this file alone to the bench install's python3."""

import json
import os
import re
import sys


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


def ab_legs(root):
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


def main(argv):
    if len(argv) != 2 or argv[0] != "ab-legs":
        sys.stderr.write("usage: python3 wkdata.py ab-legs <bench root holding job.json, autorun.state, ab/ and results/>\n")
        return 2
    ab_legs(argv[1])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
