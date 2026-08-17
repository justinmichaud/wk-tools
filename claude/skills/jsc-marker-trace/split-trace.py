#!/usr/bin/env python3
"""
Split a samply profile into one profile per section, keyed by JSC text markers.

The instrumented build emits interval text markers (JSC_useTextMarkers=1), each a
span covering the wall-clock time a named section ran. This tool selects markers
whose name starts with --prefix (default "GC "), and for each distinct name writes
a new profile keeping only the samples whose timestamp falls inside one of that
name's spans. Because the section repeats (e.g. once per GC), the per-section
profile pools samples from every occurrence, so its call tree is built from far
more samples than any single occurrence provides -- trading wall-clock spread for
statistical depth.

The worked example is the GC ("GC ParallelMarking" / "GC Sweeping" /
"GC Finalizers" from Heap::recordGCPhaseMarker), but any prefix works. The shared
stack/frame/func/string tables (which carry the JIT-symbolicated frames from
JSC_useJITDump) are kept verbatim, so the split profiles retain full symbolication.

Usage:
  split-trace.py PROFILE.json.gz [-o OUTDIR] [--prefix "GC "] [--summary] [--top N]
                                 [--all-threads] [--drop-idle]
"""

import argparse
import gzip
import itertools
import json
import os
import re
import sys


def load_profile(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt") as f:
        return json.load(f)


def dump_profile(profile, path):
    with gzip.open(path, "wt") as f:
        json.dump(profile, f, separators=(",", ":"))


def string_array_for(profile, thread):
    # Newer samply keeps the string array in the shared block; older keeps it per thread.
    shared = profile.get("shared")
    if shared and "stringArray" in shared:
        return shared["stringArray"]
    return thread["stringArray"]


def marker_name(markers, j, strings):
    # samply writes these as SimpleMarker, whose display name is in data.name (a
    # string index); the top-level name column is just the marker type "SimpleMarker".
    data = markers.get("data")
    if isinstance(data, list):
        d = data[j]
        if isinstance(d, dict) and "name" in d:
            v = d["name"]
            return strings[v] if isinstance(v, int) else v
    return strings[markers["name"][j]]


def merge_intervals(intervals):
    intervals.sort()
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def collect_sections(profile, prefix):
    """Return ({section_name: merged [start,end] list}, {pids that emitted them})."""
    sections = {}
    marker_pids = set()
    for thread in profile["threads"]:
        markers = thread["markers"]
        strings = string_array_for(profile, thread)
        starts = markers["startTime"]
        ends = markers["endTime"]
        for j in range(markers["length"]):
            name = marker_name(markers, j, strings)
            if not name.startswith(prefix):
                continue
            start, end = starts[j], ends[j]
            if start is None or end is None:
                continue  # only interval markers delimit a section
            sections.setdefault(name, []).append((start, end))
            marker_pids.add(thread["pid"])
    return {name: merge_intervals(iv) for name, iv in sections.items()}, marker_pids


# Threads that actually run GC work; everything else in the GC window (audio render,
# web workers, unrelated processes) is just noise sampled during the pause. Linux
# truncates thread names to 15 chars (prctl PR_SET_NAME), so match by shared prefix.
GC_THREAD_NAMES = ("Heap Helper Thread", "JSC Heap Collector Thread")


def is_gc_thread(thread, marker_pids):
    if thread["pid"] not in marker_pids:
        return False
    if thread.get("isMainThread"):
        return True
    nm = thread["name"]
    return len(nm) >= 8 and any(full.startswith(nm) or nm.startswith(full) for full in GC_THREAD_NAMES)


def empty_samples(samples):
    out = dict(samples)
    out["length"] = 0
    for k in ("stack", "timeDeltas", "weight", "threadCPUDelta", "eventDelay"):
        if isinstance(samples.get(k), list):
            out[k] = []
    return out


def filter_samples(samples, merged, hit_spans, time_range, is_idle=None):
    """Keep samples whose absolute time is inside a merged interval; recompute deltas.

    hit_spans: set updated with the merged-interval indices that received a sample.
    time_range: [min, max] of all sample absolute times, updated in place.
    is_idle: optional predicate on a stack index; matching samples (parked threads)
             are dropped so the split profile shows only active GC work.
    """
    n = samples["length"]
    deltas = samples["timeDeltas"]
    stack = samples["stack"]
    per_sample_keys = [k for k in ("stack", "weight", "threadCPUDelta", "eventDelay")
                       if isinstance(samples.get(k), list) and len(samples[k]) == n]

    keep = []
    kept_abs = []
    t = 0.0
    ptr = 0
    k = len(merged)
    for i in range(n):
        t += deltas[i]
        while ptr < k and merged[ptr][1] < t:
            ptr += 1
        if ptr < k and merged[ptr][0] <= t <= merged[ptr][1]:
            if is_idle is not None and is_idle(stack[i]):
                continue
            keep.append(i)
            kept_abs.append(t)
            hit_spans.add(ptr)
    if n:
        time_range[0] = min(time_range[0], deltas[0])
        time_range[1] = max(time_range[1], t)

    new_samples = dict(samples)
    new_samples["length"] = len(keep)
    new_deltas = []
    prev = 0.0
    for a in kept_abs:
        new_deltas.append(a - prev)
        prev = a
    new_samples["timeDeltas"] = new_deltas
    for key in per_sample_keys:
        col = samples[key]
        new_samples[key] = [col[i] for i in keep]
    return new_samples, len(keep)


def filter_markers(markers, keep_names, strings):
    n = markers["length"]
    keep = [j for j in range(n) if marker_name(markers, j, strings) in keep_names]
    new_markers = dict(markers)
    new_markers["length"] = len(keep)
    for key, col in markers.items():
        if isinstance(col, list) and len(col) == n:
            new_markers[key] = [col[j] for j in keep]
    return new_markers


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def leaf_name(profile, stack_index):
    shared = profile["shared"]
    st, fr, fu = shared["stackTable"], shared["frameTable"], shared["funcTable"]
    frame = st["frame"][stack_index]
    func = fr["func"][frame]
    return shared["stringArray"][fu["name"][func]]


def leaf_name_line(profile, stack_index):
    """Leaf function name plus the source line of that frame's address (from DWARF)."""
    shared = profile["shared"]
    st, fr, fu = shared["stackTable"], shared["frameTable"], shared["funcTable"]
    frame = st["frame"][stack_index]
    name = shared["stringArray"][fu["name"][fr["func"][frame]]]
    line = fr["line"][frame] if isinstance(fr.get("line"), list) else None
    return name, line


# Leaf frames that mean "this thread was parked/idle", not doing GC work.
# Covers both macOS (mach/psynch) and Linux (futex/poll/nanosleep) wait primitives.
IDLE_LEAVES = {
    # macOS
    "__psynch_cvwait", "__psynch_cvsignal", "__psynch_mutexwait", "semaphore_wait_trap",
    "semaphore_wait_signal_trap", "semaphore_timedwait_trap", "syscall_thread_switch",
    "__workq_kernreturn", "mach_msg2_trap", "mach_msg_trap", "mach_msg2_internal",
    "__semwait_signal", "thread_switch", "start_wqthread", "_pthread_wqthread",
    "read", "__read_nocancel", "kevent", "kevent_id",
    # Linux
    "futex", "__futex_abstimed_wait_common", "__futex_abstimed_wait_common64",
    "futex_wait", "futex_abstimed_wait", "do_futex_wait", "__pthread_cond_wait",
    "pthread_cond_wait", "pthread_cond_timedwait", "__pthread_cond_timedwait",
    "syscall", "__poll", "poll", "ppoll", "epoll_wait", "__epoll_wait_nocancel",
    "nanosleep", "__nanosleep", "clock_nanosleep", "g_main_context_iteration",
    "g_poll", "__GI___libc_read", "syscall_cancel",
}


def summarize(profile, section, new_threads, top):
    threads_samples = [t["samples"] for t in new_threads]
    total = sum(s["length"] for s in threads_samples)
    counts = {}
    by_thread = {}
    idle = 0
    for thread in new_threads:
        samples = thread["samples"]
        if samples["length"]:
            by_thread[thread["name"]] = by_thread.get(thread["name"], 0) + samples["length"]
        for stack_index in samples["stack"]:
            if stack_index is None:
                continue
            name = leaf_name(profile, stack_index)
            if name in IDLE_LEAVES:
                idle += 1
                continue
            counts[name] = counts.get(name, 0) + 1
    active = total - idle
    idle_pct = 100.0 * idle / total if total else 0.0
    # Line-level self time: key by (function, source line) so hot lines are visible.
    line_counts = {}
    for thread in new_threads:
        for stack_index in thread["samples"]["stack"]:
            if stack_index is None:
                continue
            name, line = leaf_name_line(profile, stack_index)
            if name in IDLE_LEAVES:
                continue
            key = f"{name}:{line}" if line is not None else name
            line_counts[key] = line_counts.get(key, 0) + 1
    print(f"  [{section}] {total} samples ({active} active, {idle_pct:.0f}% parked) across {len(by_thread)} thread(s)")
    for tname, count in sorted(by_thread.items(), key=lambda kv: -kv[1])[:6]:
        print(f"     thread {count:7d}  {tname}")
    print(f"     -- top active self-time by function:line (parked frames excluded) --")
    for key, count in sorted(line_counts.items(), key=lambda kv: -kv[1])[:top]:
        pct = 100.0 * count / active if active else 0.0
        print(f"     {pct:5.1f}%  {count:7d}  {key}")


def main():
    ap = argparse.ArgumentParser(description="Split a samply profile per GC section marker.")
    ap.add_argument("profile")
    ap.add_argument("-o", "--outdir", default=None, help="output directory (default: alongside input)")
    ap.add_argument("--prefix", default="GC ", help="marker-name prefix identifying sections")
    ap.add_argument("--summary", action="store_true", help="print top self-time functions per section")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--all-threads", action="store_true",
                    help="keep every thread's samples; default keeps only GC threads "
                         "(main + Heap Helper + Collector) of the marker-bearing process")
    ap.add_argument("--drop-idle", action="store_true",
                    help="drop samples whose leaf is a park/wait syscall, so the split "
                         "profile shows only active GC work")
    args = ap.parse_args()

    profile = load_profile(args.profile)
    if "shared" not in profile:
        print("warning: profile has no 'shared' block; assuming per-thread tables", file=sys.stderr)

    sections, marker_pids = collect_sections(profile, args.prefix)
    if not sections:
        print(f"No markers found with prefix {args.prefix!r}. Was JSC_useTextMarkers=1 set?", file=sys.stderr)
        return 1

    total_samples = sum(t["samples"]["length"] for t in profile["threads"])
    base = os.path.splitext(os.path.splitext(os.path.basename(args.profile))[0])[0]
    outdir = args.outdir or os.path.dirname(os.path.abspath(args.profile))
    os.makedirs(outdir, exist_ok=True)

    idle_pred = None
    if args.drop_idle and "shared" in profile:
        sh = profile["shared"]
        st, fr, fu, strs = sh["stackTable"], sh["frameTable"], sh["funcTable"], sh["stringArray"]

        def idle_pred(si):
            return strs[fu["name"][fr["func"][st["frame"][si]]]] in IDLE_LEAVES

    scope = "all threads" if args.all_threads else "GC threads only"
    print(f"input: {args.profile}")
    print(f"total samples (all threads): {total_samples}")
    print(f"marker process pid(s): {', '.join(sorted(marker_pids))}   thread scope: {scope}")
    print(f"sections found: {', '.join(sorted(sections))}\n")

    for section, merged in sorted(sections.items()):
        span_ms = sum(e - s for s, e in merged)
        new_threads = []
        kept = 0
        hit_spans = set()
        time_range = [float("inf"), float("-inf")]
        for thread in profile["threads"]:
            strings = string_array_for(profile, thread)
            new_thread = dict(thread)
            if not args.all_threads and not is_gc_thread(thread, marker_pids):
                new_thread["samples"] = empty_samples(thread["samples"])
            else:
                new_thread["samples"], k = filter_samples(thread["samples"], merged, hit_spans, time_range, idle_pred)
                kept += k
            new_thread["markers"] = filter_markers(thread["markers"], {section}, strings)
            new_threads.append(new_thread)

        out_profile = dict(profile)
        out_profile["threads"] = new_threads
        meta = dict(profile["meta"])
        meta["product"] = f"{profile['meta'].get('product', 'profile')} [{section}]"
        out_profile["meta"] = meta

        out_path = os.path.join(outdir, f"{base}-{slug(section)}.json.gz")
        dump_profile(out_profile, out_path)
        pct = 100.0 * kept / total_samples if total_samples else 0.0
        per_span = kept / len(hit_spans) if hit_spans else 0.0
        print(f"{section:24s} spans={len(merged):5d} hit={len(hit_spans):5d} "
              f"span_time={span_ms/1000:8.2f}s samples={kept:8d} ({pct:5.2f}% of total) "
              f"~{per_span:5.1f}/span  -> {os.path.basename(out_path)}")
        # Sanity: markers must fall inside the sampled time window, else the time
        # bases disagree and the split is meaningless.
        m_lo = min(s for s, _ in merged)
        m_hi = max(e for _, e in merged)
        if time_range[0] != float("inf") and (m_hi < time_range[0] or m_lo > time_range[1]):
            print(f"  WARNING: marker window [{m_lo/1000:.1f},{m_hi/1000:.1f}]s "
                  f"is outside sample window [{time_range[0]/1000:.1f},{time_range[1]/1000:.1f}]s")
        if args.summary:
            summarize(out_profile, section, new_threads, args.top)

    return 0


if __name__ == "__main__":
    sys.exit(main())
