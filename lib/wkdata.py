#!/usr/bin/env python3
"""Structured-data operations for `wk bench`: reading and writing the JSON
records a run produces, merging jsc-shell iteration logs into one result, and
warning when two runs are not comparable. Stdlib only -- it has to run on
whatever python3 is on macOS and on a bare-metal Linux board, with no pip
step. Every operation takes its input as argv or stdin, never spliced into
source text, so a value from the shell cannot break out of a string literal.
"""

import argparse
import json
import math
import os
import re
import sys

# The configuration axes a bench report groups variance by (docs/Urgent/
# "Benchmarking variance.md"): what shifted the stack layout and the shared
# cache between iterations. Every existing env.json predates this field, and
# every future one that does not deliberately vary one of these axes should
# read the same way a predating record does -- "not controlled" -- so these
# are the values cmd_env_record fills in for whichever subfields a caller did
# not set, rather than a caller having to restate them on every run.
DEFAULT_CONFIGURATION = {"aslr": "unset", "path_len": 0, "shared_cache": None, "env_pad_bytes": 0}


def _load(path):
    try:
        return json.load(open(path))
    except Exception:
        return {}


def _get_nested(doc, dotted_key):
    node = doc
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _set_nested(doc, dotted_key, value):
    parts = dotted_key.split(".")
    node = doc
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


# --- get ---------------------------------------------------------------------
# One field out of a JSON file, by dotted path. Never fails: a missing file, a
# parse error or a missing key all print the default, because every call site
# that reads a bench record already treats "unknown" and "absent" the same.
def cmd_get(args):
    doc = _load(args.file)
    value = _get_nested(doc, args.key)
    print(value if value is not None else args.default)


# --- ls-summary ----------------------------------------------------------------
# One line of `wk bench ls`: the axes a saved run was taken on. Left to raise
# on a malformed env.json -- a run whose own record does not parse is not one
# `wk bench ls` can summarize, and printing a wrong answer would be worse.
def cmd_ls_summary(args):
    m = json.load(open(args.file))
    axes = m.get("runner", "browser")
    if m.get("arch", "native") != "native":
        axes += "/" + m["arch"]
    if m.get("bench_host", "container") != "container":
        axes += "/" + m["bench_host"]
    print(
        "%-14s %-12s %-12s %s%s"
        % (
            m.get("plan", "?"),
            m.get("config", "?"),
            axes,
            (m.get("webkit_sha") or "?")[:10],
            "  [FORCED]" if m.get("forced") else "",
        )
    )


# --- bench-class ---------------------------------------------------------------
# What a plan measures: cpu-class (JetStream and the other JS benchmarks) needs
# no GPU or compositor; everything else is gpu-class by default, deliberately
# -- a plan nobody has classified is more likely to be a rendering benchmark
# than not, and guessing gpu fails as an easy refusal while guessing cpu fails
# as a MotionMark score from llvmpipe that looks like a regression. The one
# classifier: cmd/bench's own bench_class() calls this rather than keeping a
# second copy of the plan-name list, and so does `wk pi bench`.
def cmd_bench_class(args):
    plan = args.plan
    cpu_prefixes = ("jetstream", "octane", "kraken", "sunspider", "ares6", "jsbench")
    print("cpu" if plan.startswith(cpu_prefixes) else "gpu")


# --- cores-valid / cores-wrap ---------------------------------------------------
# `--cores <set>`'s cpu-list syntax (0-3, 2,3, 0-1,4, 7) and the `taskset -c
# <set> ` prefix built from it, the one parser cmd/bench's bench_cores_valid
# and bench_cores_wrap both call through. A set naming a cpu the machine does
# not have is left to taskset itself to refuse.
_CORES_TOKEN = re.compile(r"^[0-9]+(-[0-9]+)?$")


def cores_set_valid(spec):
    return bool(spec) and all(_CORES_TOKEN.match(tok) for tok in spec.split(","))


def cmd_cores_valid(args):
    sys.exit(0 if cores_set_valid(args.set) else 1)


# Printed only for a set cores-valid already accepted, so its charset
# (digits, commas, dashes) needs no shell quoting at the call site.
def cmd_cores_wrap(args):
    if not cores_set_valid(args.set):
        sys.exit("cores-wrap: not a valid cpu list: %s" % args.set)
    sys.stdout.write("taskset -c %s " % args.set)


# --- plan-spec -----------------------------------------------------------------
# Where a plan's payload comes from, read from the plan JSON on stdin.
# Prints "<kind> <url> <ref> <subdir>"; a plan with no fetchable source, or an
# unparseable github_source, exits nonzero with the reason on stderr.
def cmd_plan_spec(_args):
    p = json.load(sys.stdin)
    if "git_repository" in p:
        g = p["git_repository"]
        print("git", g["url"], g.get("branch", "main"), ".")
    elif "github_source" in p:
        m = re.match(
            r"https://github.com/([^/]+/[^/]+)/tree/([0-9a-f]+)/?(.*)",
            p["github_source"],
        )
        if not m:
            sys.exit("unparseable github_source: " + p["github_source"])
        print("git", "https://github.com/%s.git" % m.group(1), m.group(2), m.group(3) or ".")
    elif "local_copy" in p:
        print("local", p["local_copy"], "", "")
    else:
        sys.exit("plan has no fetchable source; run-benchmark will handle it")


# --- merge-jsc-logs --------------------------------------------------------------
# One result.json out of N per-iteration run logs from the jsc-shell runner.
# Each log holds the driver's resultsJSON() somewhere in its output; this
# takes the last line in each that parses as a JSON object (jsc's own exit
# noise follows it) and appends each iteration's "current" scores onto one
# merged tree, which is what compare-results needs to have variance to work
# with -- the same shape run-benchmark's own --count produces.
def _extract(path):
    with open(path, errors="replace") as f:
        lines = f.read().splitlines()
    for line in reversed(lines):
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _merge(into, other):
    for key, value in other.items():
        if key not in into:
            into[key] = value
        elif isinstance(value, dict) and isinstance(into[key], dict):
            _merge(into[key], value)
        elif key == "current" and isinstance(value, list) and isinstance(into[key], list):
            into[key].extend(value)
    return into


def cmd_merge_jsc_logs(args):
    merged = None
    missing = []
    for path in args.logs:
        one = _extract(path)
        if one is None:
            missing.append(path)
            continue
        merged = one if merged is None else _merge(merged, one)

    if merged is None:
        sys.exit(
            "no results in any iteration log -- the suite printed no JSON. "
            "Check the run-*.log files: a payload whose cli.js does not accept "
            "--dump-json-results will have run the whole suite and reported "
            "only in its own text format."
        )
    if missing:
        print("warning: no results in %s" % ", ".join(missing), file=sys.stderr)

    with open(args.out, "w") as f:
        json.dump(merged, f, indent=2)


# --- env-record ----------------------------------------------------------------
# The one writer for a bench run's env.json, for every runner and every host:
# a container run, a staged macOS run, and (by hand-off) `wk pi bench`. Fields
# arrive as `key=value` (a dotted key nests, e.g. `host.kernel=...`), never
# spliced into source, so a value with a quote or a backslash in it cannot
# reach the JSON any way but as that literal string. `--bool key=value` is for
# the handful of fields that are booleans in the record (forced, software,
# role_marker_overridden, ...): empty string is false, anything else is true --
# the same rule `bool()` applies to a shell flag that is either unset or "1".
def cmd_env_record(args):
    # --update loads what is already there instead of starting from {}: the
    # three axes are known before a run starts and written then so the record
    # exists even if the run dies, but wall_time_s is only known once the run
    # has finished, and a second plain write would silently drop everything
    # the first one wrote.
    doc = _load(args.out) if args.update else {}
    for field in args.fields:
        key, sep, value = field.partition("=")
        if not sep:
            sys.exit("env-record: not a key=value: %s" % field)
        _set_nested(doc, key, value)
    for field in args.bool_fields:
        key, sep, value = field.partition("=")
        if not sep:
            sys.exit("env-record: not a key=value: %s" % field)
        _set_nested(doc, key, bool(value))
    # Every record gets a `configuration` block, whether or not this run
    # touched any of its axes: a caller that never passed
    # configuration.aslr et al. still gets DEFAULT_CONFIGURATION's "not
    # controlled" values rather than a missing key, so `wk bench report`
    # never has to special-case an older or untouched record.
    cfg = doc.setdefault("configuration", {})
    for key, value in DEFAULT_CONFIGURATION.items():
        cfg.setdefault(key, value)
    with open(args.out, "w") as f:
        json.dump(doc, f, indent=2)


# --- axis-check ----------------------------------------------------------------
# Whether two saved runs are comparable, printed as warnings (never fatal --
# the caller decides what to do with a mismatch). Three axes decide what a run
# needs and what it may be compared with, and are recorded with every result:
#
#   class     what the benchmark measures (cpu-class or gpu-class).
#   runner    what executed it (the jsc shell or a real browser).
#   host      where it ran (a container, or a booted bench image).
#
# A mismatch on any of them means the two runs are not the same measurement,
# not that one is slower -- so this is the first thing `wk bench compare`
# checks, before any statistic is computed from the numbers themselves.
def cmd_axis_check(args):
    a, b = _load(args.a), _load(args.b)
    for line in _axis_check_lines(a, b):
        print(line)


# The warnings themselves, as a list of strings, so `wk bench report` can put
# them in a report next to the numbers instead of only on stderr next to a
# `wk bench compare` invocation. One implementation of "are these two runs
# comparable" rather than two: cmd_axis_check below is now the thinnest
# possible caller of this.
def _axis_check_lines(a, b):
    lines = []
    if not a or not b:
        return lines

    if a.get("plan") != b.get("plan"):
        lines.append("warning: different plans (%s vs %s)" % (a.get("plan"), b.get("plan")))
    if a.get("config") != b.get("config"):
        lines.append("warning: different build configs (%s vs %s)" % (a.get("config"), b.get("config")))

    # The three axes. A runner or host mismatch is not a caveat on a comparison
    # -- it means the two runs measured different machines doing different
    # work, and the statistics below will still happily produce a p-value for
    # them.
    if a.get("runner", "browser") != b.get("runner", "browser"):
        lines.append(
            "warning: different runners (%s vs %s) -- the jsc shell and MiniBrowser "
            "are not the same measurement" % (a.get("runner", "browser"), b.get("runner", "browser"))
        )
    if a.get("bench_host", "container") != b.get("bench_host", "container"):
        lines.append(
            "warning: different benchmark hosts (%s vs %s) -- a container shares a kernel "
            "and a desktop with everything else on the machine; an image does not"
            % (a.get("bench_host", "container"), b.get("bench_host", "container"))
        )
    if a.get("arch", "native") != b.get("arch", "native"):
        lines.append("warning: different architectures (%s vs %s)" % (a.get("arch", "native"), b.get("arch", "native")))

    # No default for class: runs predating the field say nothing about what
    # they measured, and "absent" is not "different".
    if a.get("class") and b.get("class") and a["class"] != b["class"]:
        lines.append("warning: different benchmark classes (%s vs %s)" % (a["class"], b["class"]))

    # The renderer and the session are only evidence for a gpu-class run.
    # Warning that a JetStream run in the jsc shell had no renderer is noise,
    # and noise in front of real warnings is how the real ones stop being
    # read.
    gpu_class = a.get("class") != "cpu" and b.get("class") != "cpu"
    if gpu_class and a.get("gpu_renderer") != b.get("gpu_renderer"):
        lines.append("warning: different renderers (%s vs %s)" % (a.get("gpu_renderer"), b.get("gpu_renderer")))
    if gpu_class and a.get("session_mode") != b.get("session_mode"):
        lines.append(
            "warning: different session modes (%s vs %s) -- only 'gpu' is a measurable display path"
            % (a.get("session_mode"), b.get("session_mode"))
        )
    if bool(a.get("software")) != bool(b.get("software")):
        lines.append("warning: one run is software-rendered and the other is not -- these are not comparable")
    if a.get("forced") or b.get("forced"):
        lines.append("warning: at least one run was taken with failing preflight checks (--force)")

    # A rehearsal: bench mode was asserted by an override rather than by the
    # machine having booted the image, so the number came off a workstation
    # however it is labelled (cmd_staged, WK_IMAGE_MARKER).
    if a.get("role_marker_overridden") or b.get("role_marker_overridden"):
        lines.append(
            "warning: at least one run only *claimed* bench mode "
            "(WK_IMAGE_MARKER was overridden) -- it was measured on a workstation"
        )
    if a.get("local_copy") != b.get("local_copy"):
        lines.append("warning: different benchmark payloads (%s vs %s)" % (a.get("local_copy"), b.get("local_copy")))

    # The core pin (`--cores`, taskset -c); unpinned reads as "" the same as
    # a record from before the field existed.
    cores_a = (a.get("cores") or {}).get("set") or ""
    cores_b = (b.get("cores") or {}).get("set") or ""
    if cores_a != cores_b:
        lines.append(
            "warning: different core pins (%s vs %s)"
            % (cores_a or "unpinned", cores_b or "unpinned")
        )

    # The machine, for on-board runs. Two boards are two different computers,
    # and an rpi3 score against an rpi4 score is not a comparison however
    # similar the axes look -- different SoC, different width, different
    # memory. This is a warning where the kernel below is not, because nobody
    # sets out to compare two boards to each other.
    if a.get("machine") and b.get("machine") and a["machine"] != b["machine"]:
        lines.append(
            "warning: different machines (%s vs %s) -- these are two computers, not "
            "two states of one" % (a["machine"], b["machine"])
        )

    # The kernel and the system, *reported* rather than warned about. For most
    # comparisons these being equal is what you want; for a kernel A/B their
    # differing is the entire experiment, so a warning would fire on the
    # intended case and train the eye to skip it. Printing the delta serves
    # both readings.
    #
    # The width of the kernel, which `arch` above does not answer. Two runs
    # whose kernels differ in width are two measurements however identical
    # their builds.
    kaa = (a.get("host") or {}).get("kernel_arch")
    kab = (b.get("host") or {}).get("kernel_arch")
    if kaa and kab and kaa != kab:
        lines.append(
            "warning: different kernel widths (%s vs %s) -- a 32-bit system and a "
            "32-bit process on a 64-bit kernel are not the same measurement" % (kaa, kab)
        )

    # Storage. Cheap flash contributes variance rather than a subtractable
    # bias, so a stick run and an SSD run are two series. Reported rather than
    # warned about when only the model differs on the same transport.
    ra = (a.get("host") or {}).get("root_device")
    rb = (b.get("host") or {}).get("root_device")
    if ra and rb and ra != rb:
        lines.append(
            "warning: different root storage (%s vs %s) -- cheap flash contributes "
            "variance, not a bias that can be subtracted afterwards" % (ra, rb)
        )

    for key, label in (("system", "system"), ("profile", "profile")):
        if a.get(key) and b.get(key) and a[key] != b[key]:
            lines.append("note: %s differs -- %s vs %s" % (label, a[key], b[key]))

    ka = (a.get("host") or {}).get("kernel")
    kb = (b.get("host") or {}).get("kernel")
    if ka and kb and ka != kb:
        lines.append("note: kernel differs -- %s vs %s" % (ka, kb))
    elif ka and kb and ka == kb and a.get("system") != b.get("system"):
        lines.append(
            "note: same kernel release (%s) on both sides. If this was meant to be a "
            "kernel A/B, the patched build needs its own LOCALVERSION -- otherwise "
            "the two are indistinguishable here and their modules collide on disk." % ka
        )

    return lines


# --- report --------------------------------------------------------------------
# `wk bench report <run-a> <run-b> [--html out.html] [--text]`: the one place
# that turns two saved runs into a judgement -- per-subtest Score and Time
# with a Welch/FDR p-value, a histogram of each side's distribution, the axis
# warnings above, and variance grouped by `configuration`. `wk bench compare`
# is a thin wrapper around this in text mode, and bench/mac-ab-summary.sh
# calls it directly, so there is exactly one score reader and one
# significance test in this repo.
#
# The Welch t-test and the Benjamini-Hochberg FDR correction are implemented
# here in pure stdlib rather than by shelling out to WebKit's
# Tools/Scripts/compare-results: that script needs scipy, is not on this
# machine's PYTHONPATH outside a workspace or a staged tree, and computes one
# metric (Score for JetStream/MotionMark, Time for Speedometer) per benchmark
# type rather than both for every subtest. math.lgamma gives the regularized
# incomplete beta function, and the two-tailed p-value of a t statistic is a
# closed form of it -- the same test, no second dependency.
def _betacf(a, b, x):
    maxit, eps, fpmin = 200, 3e-12, 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, maxit + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _betai(a, b, x):
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    bt = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1.0 - x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


# Two-sided Welch's t-test p-value -- unequal variance, Welch-Satterthwaite
# degrees of freedom. None where a p-value has no meaning: fewer than two
# samples on either side.
def _welch_p(a, b):
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return None
    mean_a, mean_b = sum(a) / na, sum(b) / nb
    var_a = sum((x - mean_a) ** 2 for x in a) / (na - 1)
    var_b = sum((x - mean_b) ** 2 for x in b) / (nb - 1)
    se2 = var_a / na + var_b / nb
    if se2 <= 0:
        return None if mean_a == mean_b else 0.0
    t = (mean_a - mean_b) / math.sqrt(se2)
    df = se2 * se2 / ((var_a / na) ** 2 / (na - 1) + (var_b / nb) ** 2 / (nb - 1))
    return _betai(df / 2.0, 0.5, df / (df + t * t))


# The Benjamini-Hochberg procedure Tools/Scripts/compare-results uses
# (computeMultipleHypothesesSignificance): p-values ranked largest to
# smallest, a rank is significant once it or any larger rank clears
# rank*0.05/n, and every smaller p-value inherits that significance. A
# missing p-value is never significant.
def _bh_significant(pvalues):
    result = {k: False for k in pvalues}
    keys = sorted((k for k, p in pvalues.items() if p is not None), key=lambda k: pvalues[k])
    n = len(keys)
    is_sig = False
    for rank in range(n, 0, -1):
        k = keys[rank - 1]
        if pvalues[k] <= (rank * 0.05) / n:
            is_sig = True
        result[k] = is_sig
    return result


def _flatten(x, acc=None):
    if acc is None:
        acc = []
    if isinstance(x, list):
        for i in x:
            _flatten(i, acc)
    elif isinstance(x, (int, float)):
        acc.append(float(x))
    return acc


# The first "current" array found under a metrics value, however many
# modifier levels sit above it: none for a jsc-shell merged log
# (metrics.Score.current), a null modifier for run-benchmark's own JetStream
# output (metrics.Score.None.current), "Total" for Speedometer's Time
# (metrics.Time.Total.current). One rule instead of one name per shape.
def _first_current(node):
    if isinstance(node, dict):
        cur = node.get("current")
        if isinstance(cur, list):
            return cur
        for v in node.values():
            r = _first_current(v)
            if r is not None:
                return r
    return None


# The one result walker: {subtest: {"Score": [floats], "Time": [floats]}} for
# every named test directly under a suite's "tests" mapping -- the
# granularity compare-results' --breakdown reports at. Handles a browser run
# through run-benchmark, a jsc-shell log merged by merge-jsc-logs above, and
# an on-board `wk pi bench` result: whatever produced the JSON, this is the
# only place that reads it apart.
def _subtest_metrics(doc):
    out = {}
    if not isinstance(doc, dict):
        return out
    for suite in doc.values():
        if not isinstance(suite, dict):
            continue
        tests = suite.get("tests")
        if not isinstance(tests, dict):
            continue
        for name, node in tests.items():
            if not isinstance(node, dict):
                continue
            metrics = node.get("metrics")
            if not isinstance(metrics, dict):
                continue
            entry = {}
            for key in ("Score", "Time"):
                if key in metrics:
                    vals = _flatten(_first_current(metrics[key]))
                    if vals:
                        entry[key] = vals
            if entry:
                out[name] = entry
    return out


def _mean(vals):
    return sum(vals) / len(vals) if vals else None


def _sd(vals):
    n = len(vals)
    if n < 2:
        return 0.0
    m = sum(vals) / n
    return (sum((v - m) ** 2 for v in vals) / (n - 1)) ** 0.5


# A side of a comparison: a comma-separated list of result.json paths, the
# same shape `wk bench compare`'s aspec/bspec already use for an interleaved
# arm of several runs. env.json is read from the same directory, empty where
# missing so an older run reads as "unknown" rather than refusing the report.
def _side_runs(spec):
    runs = []
    for p in spec.split(","):
        p = p.strip()
        if not p:
            continue
        runs.append((p, _load(p), _load(os.path.join(os.path.dirname(p), "env.json"))))
    return runs


def _config_key(env):
    cfg = dict(DEFAULT_CONFIGURATION)
    cfg.update((env or {}).get("configuration") or {})
    return tuple(sorted(cfg.items()))


def _config_label(key):
    return ", ".join("%s=%s" % (k, v) for k, v in key)


def _build_report(a_spec, b_spec):
    a_runs, b_runs = _side_runs(a_spec), _side_runs(b_spec)
    if not a_runs:
        sys.exit("report: no result files for A")
    if not b_runs:
        sys.exit("report: no result files for B")

    def merged_subtests(runs):
        merged = {}
        for _, doc, _env in runs:
            for name, entry in _subtest_metrics(doc).items():
                dest = merged.setdefault(name, {})
                for key, vals in entry.items():
                    dest.setdefault(key, []).extend(vals)
        return merged

    a_sub, b_sub = merged_subtests(a_runs), merged_subtests(b_runs)
    rows = []
    for name in sorted(set(a_sub) | set(b_sub)):
        row = {"name": name}
        for key in ("Score", "Time"):
            av = a_sub.get(name, {}).get(key, [])
            bv = b_sub.get(name, {}).get(key, [])
            am, bm = _mean(av), _mean(bv)
            delta = ((bm - am) / am * 100.0) if (am and bv and av) else None
            row[key] = {
                "a_mean": am, "a_sd": _sd(av), "b_mean": bm, "b_sd": _sd(bv),
                "delta_pct": delta, "p": _welch_p(av, bv) if av and bv else None,
                "a_vals": av, "b_vals": bv,
            }
        rows.append(row)

    # One FDR correction per metric: Score and Time are different
    # measurements with no shared null hypothesis, so they are not one
    # multiple-hypothesis family.
    for key in ("Score", "Time"):
        pvals = {r["name"]: r[key]["p"] for r in rows if r[key]["p"] is not None}
        sig = _bh_significant(pvals)
        for r in rows:
            r[key]["significant"] = sig.get(r["name"], False)

    axis_lines = _axis_check_lines(a_runs[0][2], b_runs[0][2])

    # Variance per configuration group (docs/Urgent/"Benchmarking
    # variance.md"): pair up the A and B runs that share an identical
    # `configuration` tuple, and flag where B's spread exceeds A's by more
    # than 20% -- a patch that makes a machine noisier under one
    # configuration is a regression this cares about even where the mean
    # does not move.
    def by_config(runs):
        out = {}
        for _, doc, env in runs:
            out.setdefault(_config_key(env), []).append(doc)
        return out

    # Score where the benchmark has one, Time otherwise (Speedometer has no
    # Score at all) -- the same "prefer Score" rule _row_primary uses per
    # subtest, applied per metric entry here.
    def primary_vals(entry):
        return entry["Score"] if "Score" in entry else entry.get("Time", [])

    a_by_cfg, b_by_cfg = by_config(a_runs), by_config(b_runs)
    variance = []
    for cfg_key in sorted(set(a_by_cfg) & set(b_by_cfg), key=_config_label):
        a_vals = [v for d in a_by_cfg[cfg_key] for e in _subtest_metrics(d).values() for v in primary_vals(e)]
        b_vals = [v for d in b_by_cfg[cfg_key] for e in _subtest_metrics(d).values() for v in primary_vals(e)]
        asd, bsd = _sd(a_vals), _sd(b_vals)
        variance.append({
            "config": _config_label(cfg_key), "a_sd": asd, "b_sd": bsd,
            "a_n": len(a_vals), "b_n": len(b_vals), "flagged": asd > 0 and bsd > asd * 1.2,
        })

    return {"rows": rows, "axis_lines": axis_lines, "variance": variance}


def _row_primary(row):
    if row["Score"]["a_vals"] or row["Score"]["b_vals"]:
        return "Score", row["Score"]["a_vals"], row["Score"]["b_vals"]
    return "Time", row["Time"]["a_vals"], row["Time"]["b_vals"]


def _xml_escape(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# A bucketed bar histogram for one subtest, A and B overlaid (same buckets,
# same x, semi-transparent fills) rather than side by side -- overlap is the
# point, it's what shows two distributions occupying the same range or not.
# No plotting library: this is the entire chart, in inline SVG.
def _svg_histogram(name, a_vals, b_vals, width=420, height=140, buckets=12):
    vals = a_vals + b_vals
    if not vals:
        return '<svg viewBox="0 0 %d %d" width="%d" height="%d"></svg>' % (width, height, width, height)
    lo, hi = min(vals), max(vals)
    if lo == hi:
        lo, hi = lo - 0.5, hi + 0.5
    span = hi - lo

    def bucket_counts(vs):
        counts = [0] * buckets
        for v in vs:
            idx = min(buckets - 1, max(0, int((v - lo) / span * buckets)))
            counts[idx] += 1
        return counts

    ca, cb = bucket_counts(a_vals), bucket_counts(b_vals)
    peak = max(max(ca, default=0), max(cb, default=0), 1)
    margin, plot_w = 24, width - 48
    plot_h = height - 40
    bw = plot_w / buckets
    bars = []
    for i in range(buckets):
        x = margin + i * bw
        ha, hb = (ca[i] / peak) * plot_h, (cb[i] / peak) * plot_h
        bars.append(
            '<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" fill="#4c78a8" fill-opacity="0.55" />'
            % (x, margin + plot_h - ha, bw * 0.9, ha)
        )
        bars.append(
            '<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" fill="#e45756" fill-opacity="0.55" />'
            % (x, margin + plot_h - hb, bw * 0.9, hb)
        )
    title = _xml_escape(name)
    axis = '<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="currentColor" stroke-opacity="0.35" />' % (
        margin, margin + plot_h, width - margin, margin + plot_h,
    )
    return (
        '<svg viewBox="0 0 %d %d" width="%d" height="%d" role="img" aria-label="%s histogram">'
        "<title>%s</title>%s%s</svg>"
    ) % (width, height, width, height, title, title, "".join(bars), axis)


def _render_text(report):
    out = ["axis check:"]
    out += ["  " + l for l in report["axis_lines"]] or ["  (no warnings)"]
    out.append("")
    header = "%-28s %-6s %14s %14s %10s %9s %5s" % ("subtest", "metric", "A mean+-sd", "B mean+-sd", "delta %", "p", "sig")
    out += ["subtests:", header, "-" * len(header)]
    for row in report["rows"]:
        for key in ("Score", "Time"):
            m = row[key]
            if m["a_mean"] is None and m["b_mean"] is None:
                continue
            out.append(
                "%-28s %-6s %14s %14s %10s %9s %5s"
                % (
                    row["name"], key,
                    ("%.3f+-%.3f" % (m["a_mean"], m["a_sd"])) if m["a_mean"] is not None else "-",
                    ("%.3f+-%.3f" % (m["b_mean"], m["b_sd"])) if m["b_mean"] is not None else "-",
                    ("%+.2f%%" % m["delta_pct"]) if m["delta_pct"] is not None else "-",
                    ("%.4f" % m["p"]) if m["p"] is not None else "-",
                    "yes" if m.get("significant") else ("no" if m["p"] is not None else "-"),
                )
            )
    out.append("")
    out.append("variance by configuration:")
    if not report["variance"]:
        out.append("  (A and B share no configuration group)")
    for v in report["variance"]:
        flag = "  ** B sd exceeds A sd by >20% **" if v["flagged"] else ""
        out.append(
            "  %s: A sd=%.4f (n=%d), B sd=%.4f (n=%d)%s"
            % (v["config"], v["a_sd"], v["a_n"], v["b_sd"], v["b_n"], flag)
        )
    return "\n".join(out) + "\n"


def _render_html(report, title="wk bench report"):
    rows_html = []
    for row in report["rows"]:
        for key in ("Score", "Time"):
            m = row[key]
            if m["a_mean"] is None and m["b_mean"] is None:
                continue
            rows_html.append(
                "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                % (
                    _xml_escape(row["name"]), key,
                    ("%.3f &plusmn; %.3f" % (m["a_mean"], m["a_sd"])) if m["a_mean"] is not None else "-",
                    ("%.3f &plusmn; %.3f" % (m["b_mean"], m["b_sd"])) if m["b_mean"] is not None else "-",
                    ("%+.2f%%" % m["delta_pct"]) if m["delta_pct"] is not None else "-",
                    ("%.4f" % m["p"]) if m["p"] is not None else "-",
                    "yes" if m.get("significant") else ("no" if m["p"] is not None else "-"),
                )
            )

    hist_html = []
    for row in report["rows"]:
        metric, av, bv = _row_primary(row)
        hist_html.append(
            '<div class="hist"><h3>%s <span class="metric">(%s)</span></h3>%s</div>'
            % (_xml_escape(row["name"]), metric, _svg_histogram(row["name"], av, bv))
        )

    axis_html = "".join("<li>%s</li>" % _xml_escape(l) for l in report["axis_lines"]) or "<li>(no warnings)</li>"

    var_rows = []
    for v in report["variance"]:
        var_rows.append(
            '<tr%s><td>%s</td><td>%.4f (n=%d)</td><td>%.4f (n=%d)</td><td>%s</td></tr>'
            % (
                ' class="flag"' if v["flagged"] else "",
                _xml_escape(v["config"]), v["a_sd"], v["a_n"], v["b_sd"], v["b_n"],
                "B sd &gt; A sd by &gt;20%" if v["flagged"] else "",
            )
        )
    if not var_rows:
        var_rows.append('<tr><td colspan="4">A and B share no configuration group</td></tr>')

    return """<!doctype html>
<html><head><meta charset="utf-8"><title>%s</title>
<style>
  body { font: 14px/1.4 -apple-system, system-ui, sans-serif; margin: 2em; color: #1b1f23; background: #fff; }
  table { border-collapse: collapse; margin: 1em 0; }
  td, th { border: 1px solid #ccc; padding: 4px 8px; text-align: right; }
  th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) { text-align: left; }
  tr.flag { background: #fde2e1; }
  .hist { display: inline-block; margin: 8px; vertical-align: top; }
  .hist h3 { font-size: 13px; margin: 0 0 2px; font-weight: 600; }
  .metric { font-weight: 400; color: #666; }
  ul { margin: 0.5em 0; }
</style></head><body>
<h1>%s</h1>
<h2>axis check</h2>
<ul>%s</ul>
<h2>subtests</h2>
<table><thead><tr><th>subtest</th><th>metric</th><th>A mean &plusmn; sd</th><th>B mean &plusmn; sd</th>
<th>delta %%</th><th>p</th><th>significant (FDR)</th></tr></thead>
<tbody>%s</tbody></table>
<h2>histograms</h2>
%s
<h2>variance by configuration</h2>
<table><thead><tr><th>configuration</th><th>A sd</th><th>B sd</th><th></th></tr></thead>
<tbody>%s</tbody></table>
</body></html>
""" % (
        _xml_escape(title), _xml_escape(title), axis_html,
        "".join(rows_html), "".join(hist_html), "".join(var_rows),
    )


def cmd_report(args):
    report = _build_report(args.a, args.b)
    want_text = args.text or not args.html
    if args.html:
        with open(args.html, "w") as f:
            f.write(_render_html(report))
        print("wrote %s" % args.html)
    if want_text:
        sys.stdout.write(_render_text(report))


def main(argv):
    parser = argparse.ArgumentParser(prog="wkdata.py", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("get", help="print one dotted field out of a JSON file")
    p.add_argument("file")
    p.add_argument("key")
    p.add_argument("--default", default="")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("ls-summary", help="one `wk bench ls` line for a saved run's env.json")
    p.add_argument("file")
    p.set_defaults(func=cmd_ls_summary)

    p = sub.add_parser("cores-valid", help="exit 0 if <set> is a valid taskset -c cpu list, 1 otherwise")
    p.add_argument("set")
    p.set_defaults(func=cmd_cores_valid)

    p = sub.add_parser("cores-wrap", help="print the 'taskset -c <set> ' prefix for a valid cpu list")
    p.add_argument("set")
    p.set_defaults(func=cmd_cores_wrap)

    p = sub.add_parser("plan-spec", help="a plan's fetchable source, read from stdin")
    p.set_defaults(func=cmd_plan_spec)

    p = sub.add_parser("bench-class", help="cpu or gpu -- what a plan measures")
    p.add_argument("plan")
    p.set_defaults(func=cmd_bench_class)

    p = sub.add_parser("merge-jsc-logs", help="merge jsc-shell iteration logs into one result.json")
    p.add_argument("out")
    p.add_argument("logs", nargs="+")
    p.set_defaults(func=cmd_merge_jsc_logs)

    p = sub.add_parser("env-record", help="write a run's env.json")
    p.add_argument("out")
    p.add_argument("fields", nargs="*", metavar="key=value")
    p.add_argument("--bool", dest="bool_fields", action="append", default=[], metavar="key=value")
    p.add_argument("--update", action="store_true",
                    help="merge onto the existing file instead of overwriting it (e.g. wall_time_s, after the run)")
    p.set_defaults(func=cmd_env_record)

    p = sub.add_parser("axis-check", help="warn where two runs are not comparable")
    p.add_argument("a")
    p.add_argument("b")
    p.set_defaults(func=cmd_axis_check)

    p = sub.add_parser("report", help="a score+time+variance report for two saved runs, as text or one self-contained html file")
    p.add_argument("a", help="comma-separated result.json paths for the A side")
    p.add_argument("b", help="comma-separated result.json paths for the B side")
    p.add_argument("--html", metavar="FILE", help="write a self-contained html report to FILE")
    p.add_argument("--text", action="store_true", help="print the text table (default when --html is not given)")
    p.set_defaults(func=cmd_report)

    # parse_known_args, not parse_args: argparse fills an `nargs="*"`
    # positional from one unbroken run of words, so `env-record OUT --update
    # wall_time_s=42` leaves the trailing field over as unrecognized -- and
    # that is how every caller writing a field after a run spells it
    # (cmd/bench, four call sites). The leftovers are fields for a subcommand
    # that takes fields, and a refusal for one that does not; cmd_env_record
    # refuses anything that is not key=value, so a mistyped flag still stops.
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
