#!/usr/bin/env python3
"""Split a perf profile into per-GC-section hardware-counter breakdowns.

Run as a perf script (NOT standalone), so parsing of perf.data is done by perf's own
Python scripting API rather than by hand:

    TRACE_AUX=/tmp/jsc-trace-aux perf script -i perf.data \
        -s split-perf.py

perf calls process_event() once per sample with a structured dict (event name, period,
CLOCK_MONOTONIC timestamp, comm/tid/pid, resolved symbol, callchain). This script pools
every sample that (a) belongs to a GC thread of the marker-emitting process and (b) falls
inside a "GC <phase>" text-marker span, across all GCs -- the same split+stitch as
split-trace.py, but summing PMU event periods instead of counting wall-clock samples.

For each phase it reports instructions-per-cycle and cache behaviour, and per-function
tables so you can see where memory stalls concentrate (e.g. during sweeping). It also
writes per-phase folded stacks weighted by cache misses for flamegraphs.

Env:
  TRACE_AUX     marker directory (default /tmp/jsc-trace-aux)
  GC_PREFIX     marker-name prefix identifying sections (default "GC ")
  PERF_OUTDIR   where folded-stack files are written (default $TRACE_AUX)
  ALL_THREADS   if set, keep every thread's samples (default: GC threads only)
"""
import os
import re
import sys
from collections import defaultdict

AUX = os.environ.get("TRACE_AUX", "/tmp/jsc-trace-aux")
GC_PREFIX = os.environ.get("GC_PREFIX", "GC ")
OUTDIR = os.environ.get("PERF_OUTDIR", AUX)
ALL_THREADS = bool(os.environ.get("ALL_THREADS"))

# Linux truncates thread names to 15 chars (prctl PR_SET_NAME); match by shared prefix.
GC_THREAD_NAMES = ("Heap Helper Thread", "JSC Heap Collector Thread")

# ---- marker spans (start_ns end_ns name), from the JSC text-marker files ----

def load_markers():
    """Return {phase_name: merged [ [start,end], ... ]}, set(marker pids)."""
    spans = defaultdict(list)
    pids = set()
    import glob
    for path in glob.glob(os.path.join(AUX, "marker-*.txt")):
        m = re.match(r"marker-(\d+)-(\d+)\.txt$", os.path.basename(path))
        if m:
            pids.add(int(m.group(2)))
        with open(path) as f:
            for line in f:
                parts = line.split(None, 2)
                if len(parts) < 3:
                    continue
                start, end, name = parts
                name = name.rstrip("\n")
                if not name.startswith(GC_PREFIX):
                    continue
                try:
                    spans[name].append([int(start), int(end)])
                except ValueError:
                    continue
    merged = {}
    for name, iv in spans.items():
        iv.sort()
        out = []
        for s, e in iv:
            if out and s <= out[-1][1]:
                out[-1][1] = max(out[-1][1], e)
            else:
                out.append([s, e])
        merged[name] = out
    return merged, pids


SECTIONS = {}
MARKER_PIDS = set()
# per phase: event -> summed period; and (phase) -> {symbol: {event: period}}
phase_events = defaultdict(lambda: defaultdict(int))
phase_func = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
phase_samples = defaultdict(int)
# folded stacks: (phase, metric_event) -> {stack_string: weight}
folded = defaultdict(lambda: defaultdict(int))
MISS_EVENTS = ("l1d_cache_refill", "ll_cache_miss_rd")


def _phase_for(ts):
    """Yield every phase whose merged spans contain ts (independent, like split-trace.py)."""
    for name, iv in SECTIONS.items():
        # linear scan is fine: a handful of spans per phase per GC, tens of GCs
        for s, e in iv:
            if s <= ts <= e:
                yield name
                break


def _is_gc_thread(pid, tid, comm):
    if pid not in MARKER_PIDS:
        return False
    if tid == pid:
        return True  # main thread
    if not comm:
        return False
    return len(comm) >= 8 and any(full.startswith(comm) or comm.startswith(full)
                                  for full in GC_THREAD_NAMES)


def _frame_name(fr):
    sym = fr.get("sym")
    if isinstance(sym, dict) and sym.get("name"):
        return sym["name"]
    ip = fr.get("ip")
    return ("0x%x" % ip) if isinstance(ip, int) else str(ip)


def trace_begin():
    global SECTIONS, MARKER_PIDS
    SECTIONS, MARKER_PIDS = load_markers()
    if not SECTIONS:
        sys.stderr.write("split-perf: no %r markers under %s\n" % (GC_PREFIX, AUX))


def process_event(param_dict):
    s = param_dict.get("sample", {})
    ts = s.get("time")
    if ts is None:
        return
    pid, tid = s.get("pid"), s.get("tid")
    comm = param_dict.get("comm")
    if not ALL_THREADS and not _is_gc_thread(pid, tid, comm):
        return
    ev = param_dict.get("ev_name", "")
    # perf appends ":" or a modifier suffix on some builds; normalise to the base name.
    ev = ev.split(":")[0].strip()
    period = s.get("period") or 0
    sym = param_dict.get("symbol") or "[unknown]"

    for phase in _phase_for(ts):
        phase_events[phase][ev] += period
        phase_func[phase][sym][ev] += period
        phase_samples[phase] += 1
        if ev in MISS_EVENTS:
            cc = param_dict.get("callchain") or []
            names = [_frame_name(fr) for fr in cc]
            if not names:
                names = [sym]
            stack = ";".join(reversed(names))  # root->leaf for flamegraphs
            folded[(phase, ev)][stack] += period


def _fmt(n):
    return f"{n:,}"


def _phase_report(phase):
    ev = phase_events[phase]
    cyc = ev.get("cycles", 0)
    ins = ev.get("instructions", 0)
    l1d = ev.get("l1d_cache_refill", 0)
    llm = ev.get("ll_cache_miss_rd", 0)
    stall = ev.get("stall_backend", 0)
    ipc = ins / cyc if cyc else 0.0
    l1_mpki = 1000.0 * l1d / ins if ins else 0.0
    ll_mpki = 1000.0 * llm / ins if ins else 0.0
    stall_frac = 100.0 * stall / cyc if cyc else 0.0
    print(f"\n=== {phase} ===  ({phase_samples[phase]} samples)")
    print(f"  cycles={_fmt(cyc)} instructions={_fmt(ins)}")
    print(f"  IPC                 {ipc:6.3f}")
    print(f"  L1D-refill MPKI     {l1_mpki:6.2f}   (l1d_cache_refill={_fmt(l1d)})")
    print(f"  LL-read-miss MPKI   {ll_mpki:6.2f}   (ll_cache_miss_rd={_fmt(llm)})")
    print(f"  backend-stall %cyc  {stall_frac:6.1f}   (stall_backend={_fmt(stall)})")
    # per-function tables by each miss event
    for metric, total in (("ll_cache_miss_rd", llm), ("l1d_cache_refill", l1d)):
        if not total:
            continue
        rows = []
        for fn, evs in phase_func[phase].items():
            m = evs.get(metric, 0)
            if not m:
                continue
            f_ipc = evs.get("instructions", 0) / evs["cycles"] if evs.get("cycles") else 0.0
            rows.append((m, fn, f_ipc))
        rows.sort(reverse=True)
        print(f"  -- top functions by {metric} (share of phase; local IPC) --")
        for m, fn, f_ipc in rows[:15]:
            print(f"     {100.0*m/total:5.1f}%  IPC={f_ipc:5.2f}  {fn}")


def _write_folded():
    os.makedirs(OUTDIR, exist_ok=True)
    for (phase, ev), stacks in folded.items():
        slug = re.sub(r"[^a-z0-9]+", "-", phase.lower()).strip("-")
        path = os.path.join(OUTDIR, f"perf-{slug}-{ev}.folded")
        with open(path, "w") as f:
            for stack, w in sorted(stacks.items(), key=lambda kv: -kv[1]):
                f.write(f"{stack} {w}\n")
        print(f"  folded stacks -> {path}")


def trace_end():
    print(f"marker pids: {sorted(MARKER_PIDS)}   sections: {', '.join(sorted(SECTIONS))}")
    print(f"thread scope: {'all threads' if ALL_THREADS else 'GC threads only'}")
    for phase in sorted(phase_events):
        _phase_report(phase)
    print()
    _write_folded()
