"""`wk bench report`, `compare` and `precision`: the tree's one score reader, significance test and stopping rule."""

import math
import os
import statistics
import sys
from html import escape

from wk import pgo, presets
from wk.bench import record
from wk.machine import Local


AXES = (("plan", "plans", None, ""), ("preset", "build presets", None, ""),
        ("runner", "runners", "browser", " -- the jsc shell and MiniBrowser are not the same measurement"),
        ("bench_host", "benchmark hosts", "container", " -- a container shares a kernel and a desktop with everything else on the machine; an image does not"),
        ("arch", "architectures", "native", ""), ("local_copy", "benchmark payloads", None, ""))
BOTH = (("class", "benchmark classes", ""), ("machine", "machines", " -- these are two computers, not two states of one"),
        ("host.kernel_arch", "kernel widths", " -- a 32-bit system and a 32-bit process on a 64-bit kernel are not the same measurement"),
        ("host.root_device", "root storage", " -- cheap flash contributes variance, not a bias that can be subtracted afterwards"))


def axis_check_lines(a, b):
    if not a or not b:
        return []
    lines = ["warning: different %s (%s vs %s)%s" % (label, a.get(k, d), b.get(k, d), why)
             for k, label, d, why in AXES[:5] if a.get(k, d) != b.get(k, d)]
    got = [(record.get_nested(a, k), record.get_nested(b, k), label, why) for k, label, why in BOTH]
    lines += ["warning: different %s (%s vs %s)%s" % (label, x, y, why) for x, y, label, why in got[:1] if x and y and x != y]
    # Only evidence for a gpu-class run: "no renderer" about a jsc-shell JetStream run is noise in front of real warnings.
    if a.get("class") != "cpu" and b.get("class") != "cpu":
        if a.get("gpu_renderer") != b.get("gpu_renderer"):
            lines.append("warning: different renderers (%s vs %s)" % (a.get("gpu_renderer"), b.get("gpu_renderer")))
        if a.get("session_mode") != b.get("session_mode"):
            lines.append("warning: different session modes (%s vs %s) -- only 'gpu' is a measurable display path"
                         % (a.get("session_mode"), b.get("session_mode")))
    if bool(a.get("software")) != bool(b.get("software")):
        lines.append("warning: one run is software-rendered and the other is not -- these are not comparable")
    ex_a, ex_b = a.get("subtests_excluded") or "", b.get("subtests_excluded") or ""
    if ex_a != ex_b:
        lines.append("warning: the arms ran different subtest sets (%s vs %s)" % (ex_a or "none excluded", ex_b or "none excluded"))
    elif ex_a:
        lines.append("note: %d subtest(s) excluded from both arms -- %s" % (len(ex_a.split(",")), ex_a))
    if a.get("forced") or b.get("forced"):
        lines.append("warning: at least one run was taken with failing preflight checks (--force)")
    if a.get("role_marker_overridden") or b.get("role_marker_overridden"):
        lines.append("warning: at least one run only *claimed* bench mode (WK_IMAGE_MARKER was overridden) -- it was measured on a workstation")
    k, label, d, why = AXES[5]
    if a.get(k, d) != b.get(k, d):
        lines.append("warning: different %s (%s vs %s)%s" % (label, a.get(k, d), b.get(k, d), why))
    cores = [(r.get("cores") or {}).get("set") or "unpinned" for r in (a, b)]
    if cores[0] != cores[1]:
        lines.append("warning: different core pins (%s vs %s)" % tuple(cores))
    lines += ["warning: different %s (%s vs %s)%s" % (label, x, y, why) for x, y, label, why in got[1:] if x and y and x != y]
    # The kernel and system are reported, not warned about: for a kernel A/B their differing is the whole A/B.
    lines += ["note: %s differs -- %s vs %s" % (k, a[k], b[k]) for k in ("system", "profile") if a.get(k) and b.get(k) and a[k] != b[k]]
    ka, kb = record.get_nested(a, "host.kernel"), record.get_nested(b, "host.kernel")
    if ka and kb and ka != kb:
        lines.append("note: kernel differs -- %s vs %s" % (ka, kb))
    elif ka and kb and a.get("system") != b.get("system"):
        lines.append("note: same kernel release (%s) on both sides. If this was meant to be a kernel A/B, the patched build needs its own "
                     "LOCALVERSION -- otherwise the two are indistinguishable here and their modules collide on disk." % ka)
    return lines


# Welch and Benjamini-Hochberg in stdlib: Tools/Scripts/compare-results needs scipy, is off the PYTHONPATH outside a workspace, and
# computes one metric per benchmark type. A t's two-tailed p is the regularized incomplete beta (Lentz's continued fraction).
def _betacf(a, b, x):
    def clamp(v):
        return v if abs(v) >= 1e-300 else 1e-300
    c, d = 1.0, 1.0 / clamp(1.0 - (a + b) * x / (a + 1.0))
    h = d
    for m in range(1, 201):
        for aa in (m * (b - m) * x / ((a - 1.0 + 2 * m) * (a + 2 * m)), -(a + m) * (a + b + m) * x / ((a + 2 * m) * (a + 1.0 + 2 * m))):
            d, c = 1.0 / clamp(1.0 + aa * d), clamp(1.0 + aa / c)
            h *= d * c
        if abs(d * c - 1.0) < 3e-12:
            break
    return h


def _betai(a, b, x):
    if x <= 0.0 or x >= 1.0:
        return max(0.0, min(1.0, x))
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def _welch(a, b):
    """(mean_a, mean_b, se2, Welch-Satterthwaite df), or None under two values a side."""
    if len(a) < 2 or len(b) < 2:
        return None
    va, vb = statistics.variance(a) / len(a), statistics.variance(b) / len(b)
    se2 = va + vb
    return statistics.fmean(a), statistics.fmean(b), se2, (se2 * se2 / (va * va / (len(a) - 1) + vb * vb / (len(b) - 1)) if se2 > 0 else 0)


def welch_p(a, b):
    w = _welch(a, b)
    if w is None:
        return None
    mean_a, mean_b, se2, df = w
    if se2 <= 0:
        return None if mean_a == mean_b else 0.0
    t = (mean_a - mean_b) / math.sqrt(se2)
    return _betai(df / 2.0, 0.5, df / (df + t * t))


# Stopping on precision is a sequential design where stopping on a p-value is not; t comes back out of the same beta by bisection.
def t_crit(df, two_tailed_area):
    if df <= 0:
        return None
    lo, hi = 0.0, 1000.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if _betai(df / 2.0, 0.5, df / (df + mid * mid)) > two_tailed_area:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


# The smallest relative difference these rounds resolve at two-sided 95% confidence and 80% power, as a percentage of A's mean.
def mde_pct(a, b):
    w = _welch(a, b)
    if w is None or w[0] <= 0:
        return None
    mean_a, _, se2, df = w
    if se2 <= 0:
        return 0.0
    t_alpha, t_beta = t_crit(df, 0.05), t_crit(df, 0.40)   # one-tailed 0.80 is the two-tailed 0.40 point
    return (t_alpha + t_beta) * math.sqrt(se2) / mean_a * 100.0


# Benjamini-Hochberg as compare-results spells it: a rank is significant once it or a larger one clears rank*0.05/n.
def bh_significant(pvalues):
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


def _flatten(x):
    if isinstance(x, list):
        return [v for i in x for v in _flatten(i)]
    return [float(x)] if isinstance(x, (int, float)) else []


# However many modifier levels sit above it: none in a merged jsc log, a null one in run-benchmark's JetStream output, "Total" in Speedometer's Time. One rule, not a name per shape.
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


# A metric holds either its values or a declaration of how to aggregate its subtests' -- ["Geometric"] for JetStream3 and MotionMark, whose overall score is never written into the file at all -- and the list's first name is the primary one.
_AGGREGATORS = {"Arithmetic": statistics.fmean, "Geometric": lambda vals: math.exp(sum(math.log(v) for v in vals) / len(vals)), "Total": sum}


def _iteration_values(metric):
    """One value per iteration; Speedometer's entry is itself that iteration's internal repeats."""
    cur = _first_current(metric)
    out = [_mean(_flatten(item)) for item in cur] if isinstance(cur, list) else []
    return out if out and None not in out else None


def _declared_metric(node, key="Score"):
    metrics = node.get("metrics") if isinstance(node, dict) else None
    metric = metrics.get(key) if isinstance(metrics, dict) else None
    return metric if isinstance(metric, list) else None


# A child that declares its own aggregate rather than writing one is resolved first: Speedometer's Time is a Total of Totals three levels deep, and a level that answered "no Time" would make every level above it silent.
def _declared_aggregate(suite, node, metric, key="Score"):
    name = metric[0] if metric else ""
    if name not in _AGGREGATORS:
        return None, ("%s declares its %s as '%s', which this file does not "
                      "aggregate. Implemented: %s." % (suite, key, name, ", ".join(sorted(_AGGREGATORS))))
    children = (node.get("tests") or {}) if isinstance(node.get("tests"), dict) else {}
    per_child, silent = {}, []
    for child, cnode in children.items():
        vals = None
        if isinstance(cnode, dict):
            declared = _declared_metric(cnode, key)
            if declared is None:
                vals = _iteration_values((cnode.get("metrics") or {}).get(key))
            else:
                vals = _declared_aggregate("%s/%s" % (suite, child), cnode, declared, key)[0]
        if vals:
            per_child[str(child)] = vals
        else:
            silent.append(str(child))
    if silent or not per_child:
        return None, ("%s's %s is the %s of its subtests' %ss, and %d of "
                      "%d first-level tests report no %s (%s). Re-run the plan; a partial suite "
                      "has no headline score."
                      % (suite, key, name, key, len(silent), len(children), key,
                         ", ".join(sorted(silent)) or "none ran"))
    counts = sorted({len(v) for v in per_child.values()})
    if len(counts) != 1:
        return None, ("%s's subtests report %s iterations -- one aggregate per "
                      "iteration needs the same count from every subtest."
                      % (suite, "/".join(str(c) for c in counts)))
    fn = _AGGREGATORS[name]
    try:
        return [fn([v[i] for v in per_child.values()]) for i in range(counts[0])], None
    except ValueError:
        return None, ("%s reports a subtest %s of zero or less, and its %s mean "
                      "is undefined." % (suite, key, name))


# The one result walker, ({name: {"Score": [floats], "Time": [floats]}}, [why a row is absent]), because the shapes disagree about depth: a merged jsc log and run-benchmark's JetStream keep numbers one "tests" level down, while Speedometer-2 on a board keeps the total at the suite root and the numbers three down, with bare descriptor lists between.
def subtest_metrics(doc):
    out, absent = {}, []

    def metric_vals(metrics):
        entry = {}
        if isinstance(metrics, dict):
            for key in ("Score", "Time"):
                if key in metrics:
                    vals = _flatten(_first_current(metrics[key]))
                    if vals:
                        entry[key] = vals
        return entry

    def walk(name, node, resolve):
        if not isinstance(node, dict):
            return
        entry = metric_vals(node.get("metrics"))
        # Only the topmost declaration that cannot be resolved is reported: the levels above a silent subtest are silent for the same one reason, and each would say so again.
        for key in ("Score", "Time"):
            declared = _declared_metric(node, key)
            if declared is None or not resolve:
                continue
            vals, why = _declared_aggregate(name, node, declared, key)
            if why:
                absent.append(why)
                resolve = False
            else:
                entry[key] = vals
        if entry:
            out[name] = entry
        tests = node.get("tests")
        if isinstance(tests, dict):
            for child, cnode in tests.items():
                walk("%s/%s" % (name, child), cnode, resolve)

    if isinstance(doc, dict):
        for suite, node in doc.items():
            walk(str(suite), node, True)
    return out, absent


def _mean(vals):
    return statistics.fmean(vals) if vals else None


def _sd(vals):
    return statistics.stdev(vals) if len(vals) > 1 else 0.0


def run_result(rundir):
    path = os.path.join(rundir, "result.json")
    if not os.path.isfile(path):
        return path, None, "%s: no result.json in this directory" % rundir
    try:
        doc = record.load(path)
    except ValueError as e:
        return path, None, "%s: not valid JSON (%s)" % (path, e)
    return path, doc, None if doc else "%s: empty" % path


# env.json is read from the same directory, empty where missing, so an older run reads as unknown rather than refusing the report.
def _side_runs(dirs, side):
    runs, missing = [], []
    for d in dirs:
        d = os.path.normpath(d)
        _path, doc, why = run_result(d)
        if why:
            missing.append(why)
            continue
        try:
            env = record.load(os.path.join(d, "env.json"))
        except ValueError as e:
            sys.exit("report: side %s: %s/env.json is not valid JSON (%s)" % (side, d, e))
        if record.not_a_measurement(env):
            sys.exit("report: side %s: %s -- %s" % (side, d, record.not_a_measurement(env)))
        runs.append((d, doc, env))
    if not runs:
        sys.exit("report: no results on side %s:\n%s"
                 % (side, "\n".join("  " + l for l in missing)
                    or "  no run directories given"))
    for line in missing:
        print("warning: side %s: %s" % (side, line), file=sys.stderr)
    return runs


def _config_key(env):
    cfg = dict(record.DEFAULT_CONFIGURATION)
    cfg.update((env or {}).get("configuration") or {})
    return tuple(sorted(cfg.items()))


def _config_label(key):
    return ", ".join("%s=%s" % (k, v) for k, v in key)


def consistency_lines(rows):
    """A score and the subtest times it is built from move opposite ways; when they do not, neither is to be quoted."""
    tops = [r for r in rows if r["Score"]["a_mean"] and r["Score"]["b_mean"] and "/" not in r["name"]]
    leaves = [r for r in rows if r["Time"]["a_mean"] and r["Time"]["b_mean"] and "/" in r["name"]]
    if len(tops) != 1 or len(leaves) < 8:
        return []
    sa, sb = tops[0]["Score"]["a_mean"], tops[0]["Score"]["b_mean"]
    ta, tb = sum(r["Time"]["a_mean"] for r in leaves), sum(r["Time"]["b_mean"] for r in leaves)
    score_delta, time_delta = (sb - sa) / sa * 100.0, (tb - ta) / ta * 100.0
    lines = ["note: B scores %+.2f%% on %+.2f%% subtest time (%d subtests, A %.0f ms, B %.0f ms)" % (score_delta, time_delta, len(leaves), ta, tb)]
    if abs(score_delta) < 0.2 and abs(time_delta) < 0.2:
        return lines
    if (score_delta > 0) == (time_delta > 0):
        lines.append("warning: the score and the subtest times it is made of disagree in SIGN -- B does %+.2f%% work and scores %+.2f%%. "
                     "One of the two is wrong; do not quote either until they are reconciled." % (time_delta, score_delta))
    elif abs(score_delta + time_delta) > 5.0:
        lines.append("warning: the score moved %+.2f%% where the subtest times imply about %+.2f%% -- the aggregate weights subtests very "
                     "differently from their cost, so the headline and the table answer different questions." % (score_delta, -time_delta))
    return lines


def order_lines(a_runs, b_runs):
    """Alternating is not counterbalanced: if one arm always goes first, monotonic drift lands on the other."""
    order = sorted([(os.path.basename(p), "A") for p, _, _ in a_runs] + [(os.path.basename(p), "B") for p, _, _ in b_runs])
    if len(order) < 4:
        return []
    pos = {"A": [i for i, (_, arm) in enumerate(order, 1) if arm == "A"], "B": [i for i, (_, arm) in enumerate(order, 1) if arm == "B"]}
    ma, mb = statistics.fmean(pos["A"]), statistics.fmean(pos["B"])
    if abs(ma - mb) < 0.25:
        return []
    late = "B" if mb > ma else "A"
    return ["warning: the arms alternate but are not counterbalanced -- %s runs %.1f position(s) later on average (A at %s, B at %s), "
            "so monotonic drift lands on %s rather than cancelling" % (late, abs(mb - ma), pos["A"], pos["B"], late)]


def build_report(a_dirs, b_dirs, header=(), warmup=()):
    a_runs, b_runs = _side_runs(a_dirs, "A"), _side_runs(b_dirs, "B")

    def merged_subtests(runs, side):
        merged, per_run, absent = {}, {}, []
        for _, doc, _env in runs:
            rows, why = subtest_metrics(doc)
            absent += ["warning: side %s: %s" % (side, w) for w in why]
            for name, entry in rows.items():
                dest = merged.setdefault(name, {})
                for key, vals in entry.items():
                    dest.setdefault(key, []).extend(vals)
                    per_run.setdefault((name, key), []).append(vals)
        return merged, per_run, absent

    a_sub, a_per_run, a_absent = merged_subtests(a_runs, "A")
    b_sub, b_per_run, b_absent = merged_subtests(b_runs, "B")
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
                "delta_pct": delta, "p": welch_p(av, bv) if av and bv else None,
                "a_vals": av, "b_vals": bv,
            }
        rows.append(row)

    # One correction per metric: Score and Time share no null hypothesis.
    for key in ("Score", "Time"):
        pvals = {r["name"]: r[key]["p"] for r in rows if r[key]["p"] is not None}
        sig = bh_significant(pvals)
        for r in rows:
            r[key]["significant"] = sig.get(r["name"], False)

    axis_lines = axis_check_lines(a_runs[0][2], b_runs[0][2])
    axis_lines += order_lines(a_runs, b_runs)
    axis_lines += sorted(set(a_absent + b_absent))

    # A patch that makes a machine noisier under one configuration is a regression even where the mean does not move, so B's spread exceeding A's by 20% is flagged.
    def by_config(runs):
        out = {}
        for _, doc, env in runs:
            out.setdefault(_config_key(env), []).append(doc)
        return out

    def primary_vals(entry):
        return entry["Score"] if "Score" in entry else entry.get("Time", [])

    a_by_cfg, b_by_cfg = by_config(a_runs), by_config(b_runs)
    variance = []
    for cfg_key in sorted(set(a_by_cfg) & set(b_by_cfg), key=_config_label):
        a_vals = [v for d in a_by_cfg[cfg_key] for e in subtest_metrics(d)[0].values() for v in primary_vals(e)]
        b_vals = [v for d in b_by_cfg[cfg_key] for e in subtest_metrics(d)[0].values() for v in primary_vals(e)]
        asd, bsd = _sd(a_vals), _sd(b_vals)
        variance.append({
            "config": _config_label(cfg_key), "a_sd": asd, "b_sd": bsd,
            "a_n": len(a_vals), "b_n": len(b_vals), "flagged": asd > 0 and bsd > asd * 1.2,
        })

    axis_lines += consistency_lines(rows)
    spread = [spread_line(r["name"], key, a_per_run.get((r["name"], key), []), b_per_run.get((r["name"], key), []))
              for r in rows if "/" not in r["name"] for key in ("Score", "Time")
              if (r["name"], key) in a_per_run or (r["name"], key) in b_per_run]
    return {"rows": rows, "axis_lines": axis_lines, "variance": variance, "spread": spread,
            "header": list(header), "warmup_lines": list(warmup)}


def spread(per_run):
    """(sd pooled within each run, sd of the run means, runs): the first is what --count averages down, the second only more rounds do."""
    within = [(len(v) - 1, _sd(v) ** 2) for v in per_run if len(v) >= 2]
    dof = sum(n for n, _ in within)
    pooled = math.sqrt(sum(n * var for n, var in within) / dof) if dof else None
    means = [_mean(v) for v in per_run if v]
    return pooled, (_sd(means) if len(means) >= 2 else None), len(means)


def spread_line(name, key, a_per_run, b_per_run):
    def side(label, per_run):
        within, between, n = spread(per_run)
        return "%s within-run sd=%s, run-to-run sd=%s over %d run(s)" % (
            label, "-" if within is None else "%.4f" % within, "-" if between is None else "%.4f" % between, n)
    return "%s %s: %s; %s" % (name, key, side("A", a_per_run), side("B", b_per_run))


def _row_primary(row):
    if row["Score"]["a_vals"] or row["Score"]["b_vals"]:
        return "Score", row["Score"]["a_vals"], row["Score"]["b_vals"]
    return "Time", row["Time"]["a_vals"], row["Time"]["b_vals"]


# Overlaid rather than side by side: the overlap is what shows two distributions occupying the same range or not.
def _svg_histogram(name, a_vals, b_vals, width=420, height=140, buckets=12):
    vals, svg = a_vals + b_vals, '<svg viewBox="0 0 %d %d" width="%d" height="%d"' % (width, height, width, height)
    if not vals:
        return svg + "></svg>"
    lo, hi = min(vals), max(vals)
    lo, hi = (lo - 0.5, hi + 0.5) if lo == hi else (lo, hi)
    counts = [[0] * buckets, [0] * buckets]
    for side, vs in enumerate((a_vals, b_vals)):
        for v in vs:
            counts[side][min(buckets - 1, max(0, int((v - lo) / (hi - lo) * buckets)))] += 1
    peak, margin, plot_h = max(max(counts[0]), max(counts[1]), 1), 24, height - 40
    bw = (width - 2 * margin) / buckets
    bars = ['<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" fill="%s" fill-opacity="0.55" />'
            % (margin + i * bw, margin + plot_h - c[i] / peak * plot_h, bw * 0.9, c[i] / peak * plot_h, colour)
            for i in range(buckets) for c, colour in zip(counts, ("#4c78a8", "#e45756"))]
    axis = '<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="currentColor" stroke-opacity="0.35" />' % (margin, margin + plot_h, width - margin, margin + plot_h)
    return svg + ' role="img" aria-label="%s histogram"><title>%s</title>%s%s</svg>' % (escape(name), escape(name), "".join(bars), axis)


def _cells(m, pm=" +- "):
    return (("%.3f" + pm + "%.3f") % (m["a_mean"], m["a_sd"]) if m["a_mean"] is not None else "-",
            ("%.3f" + pm + "%.3f") % (m["b_mean"], m["b_sd"]) if m["b_mean"] is not None else "-",
            "%+.2f%%" % m["delta_pct"] if m["delta_pct"] is not None else "-", "%.4f" % m["p"] if m["p"] is not None else "-",
            "yes" if m.get("significant") else ("no" if m["p"] is not None else "-"))


def _metric_rows(report):
    return [(row["name"], key, row[key]) for row in report["rows"] for key in ("Score", "Time")
            if row[key]["a_mean"] is not None or row[key]["b_mean"] is not None]


def render_text(report):
    out = list(report.get("header", [])) + ([""] if report.get("header") else [])
    if report.get("warmup_lines"):
        out += ["warmup round (not measured):"] + ["  " + l for l in report["warmup_lines"]] + [""]
    out += ["axis check:"] + (["  " + l for l in report["axis_lines"]] or ["  (no warnings)"]) + [""]
    fmt = "%%-%ds %%-6s %%14s %%14s %%10s %%9s %%5s" % max([len("subtest")] + [len(r["name"]) for r in report["rows"]])
    header = fmt % ("subtest", "metric", "A mean+-sd", "B mean+-sd", "delta %", "p", "sig")
    out += ["subtests:", header, "-" * len(header)] + [fmt % ((name, key) + _cells(m, "+-")) for name, key, m in _metric_rows(report)]
    out += ["", "variance by configuration:"] + (["  %s: A sd=%.4f (n=%d), B sd=%.4f (n=%d)%s" % (
        v["config"], v["a_sd"], v["a_n"], v["b_sd"], v["b_n"], "  ** B sd exceeds A sd by >20% **" if v["flagged"] else "")
        for v in report["variance"]] or ["  (A and B share no configuration group)"])
    out += ["", "spread, within a run and between runs:"] + (["  " + l for l in report["spread"]] or ["  (no suite row)"])
    return "\n".join(out) + "\n"


def render_html(report, title="wk bench report"):
    def ul(lines, empty=""):
        return "".join("<li>%s</li>" % escape(l) for l in lines) or (empty and "<li>%s</li>" % empty)

    rows_html = ["<tr>%s</tr>" % "".join("<td>%s</td>" % c for c in (escape(name), key) + _cells(m, " &plusmn; "))
                 for name, key, m in _metric_rows(report)]
    hist_html = ['<div class="hist"><h3>%s <span class="metric">(%s)</span></h3>%s</div>'
                 % (escape(row["name"]), metric, _svg_histogram(row["name"], av, bv)) for row in report["rows"] for metric, av, bv in [_row_primary(row)]]
    var_rows = ['<tr%s><td>%s</td><td>%.4f (n=%d)</td><td>%.4f (n=%d)</td><td>%s</td></tr>'
                % (' class="flag"' if v["flagged"] else "", escape(v["config"]), v["a_sd"], v["a_n"], v["b_sd"], v["b_n"],
                   "B sd &gt; A sd by &gt;20%" if v["flagged"] else "") for v in report["variance"]]
    var_rows = var_rows or ['<tr><td colspan="4">A and B share no configuration group</td></tr>']
    header_html = "<ul>%s</ul>" % ul(report["header"]) if report.get("header") else ""
    warmup_html = "<h2>warmup round (not measured)</h2><ul>%s</ul>" % ul(report["warmup_lines"]) if report.get("warmup_lines") else ""
    axis_html = ul(report["axis_lines"], "(no warnings)")
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
%s
%s
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
<h2>spread, within a run and between runs</h2>
<ul>%s</ul>
</body></html>
""" % (
        escape(title), escape(title), header_html, warmup_html, axis_html, "".join(rows_html), "".join(hist_html), "".join(var_rows),
        ul(report["spread"], "(no suite row)"))


# What counts as a problem within one arm is decided where it is measured (board_driver.py); this adds what needs both arms.
def evidence_paths(taskdir, device):
    return [os.path.join(taskdir, "warmup", "%s-%s.evidence.json" % (device, arm)) for arm in "ab"]


def warmup_load(taskdir, device):
    return {arm: doc for arm, doc in zip("ab", map(record.load, evidence_paths(taskdir, device))) if doc}


def warmup_lines(evidence):
    lines = []
    for arm in "ab":
        rec = evidence.get(arm)
        if not rec:
            lines.append("%s: no warmup evidence" % arm.upper())
            continue
        elf, gl, jit, gpu = rec.get("elf", {}), rec.get("gl", {}), rec.get("jit", {}), rec.get("gpu") or {}
        by_process = list((gpu.get("by_process_ms") or {}).items())[:3]
        lines += ["%s: %s-bit %s, renderer %s%s" % (arm.upper(), elf.get("bits", "?"), elf.get("machine", "?"),
                                                    os.path.basename(gl.get("driver") or "unknown"), " [SOFTWARE]" if gl.get("software") else ""),
                  "   GPU busy %s ms on %s%s" % (gpu.get("busy_ms", "?"), gpu.get("driver") or "unknown",
                                                 " (%s)" % ", ".join("%s %d ms" % kv for kv in by_process) if by_process else ""),
                  "   JIT %s, %s executable in %d mapping(s)%s" % (jit.get("verdict", "?"), _bytes_label(jit.get("exec_bytes", 0)), jit.get("exec_mappings", 0),
                                                                   "; compiles " + ", ".join("%s=%d" % kv for kv in sorted(jit["tiers"].items())) if jit.get("tiers") else "")]
        lines += ["   %s" % n for n in rec.get("notes", []) + rec.get("problems", [])]
        if rec.get("profile"):
            lines.append("   profile: %s (%s)" % (rec["profile"].get("file", "?"), rec["profile"].get("tool", "?")))
    return lines


def _bytes_label(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%d %s" % (n, unit)
        n //= 1024


def warmup_check(a_path, b_path, same_width):
    """Why a warmup round's two arms refuse the A/B, one reason per line; none when they do not."""
    a, b = record.load(a_path), record.load(b_path)
    problems = []
    for arm, rec, path in (("A", a, a_path), ("B", b, b_path)):
        missing = [k for k in ("elf", "gl", "jit", "problems") if k not in rec]
        if not rec:
            problems.append("arm %s produced no warmup evidence (%s)" % (arm, path))
        elif missing:
            problems.append("arm %s's evidence file is not warmup evidence -- it parses as JSON but has no %s (%s)" % (arm, "/".join(missing), path))
        else:
            problems += ["arm %s: %s" % (arm, p) for p in rec["problems"]]
    if a and b:
        ga, gb, ea, eb = a.get("gl", {}), b.get("gl", {}), a.get("elf", {}), b.get("elf", {})
        if ga.get("driver") and gb.get("driver") and ga["driver"] != gb["driver"]:
            problems.append("the arms rendered through different drivers (%s vs %s)" % (ga["driver"], gb["driver"]))
        if same_width and ea.get("bits") and eb.get("bits") and ea["bits"] != eb["bits"]:
            problems.append("the arms are %d-bit and %d-bit, and this A/B varies neither the image nor the width" % (ea["bits"], eb["bits"]))
    return problems


def headline_score(doc):
    roots = [(str(k), v) for k, v in doc.items() if isinstance(v, dict) and isinstance(v.get("metrics"), dict) and "Score" in v["metrics"]]
    if len(roots) != 1:
        return None
    suite, node = roots[0]
    declared = _declared_metric(node)
    vals, why = (_iteration_values(node["metrics"]["Score"]), None) if declared is None else _declared_aggregate(suite, node, declared)
    if why:
        sys.exit("precision: " + why)
    return _mean(vals) if vals else None


def _headline_scores(dirs):
    scores, empty = [], []
    for d in dirs:
        path, doc, why = run_result(d)
        score = None if why else headline_score(doc)
        if score is None:
            empty.append(why or "%s: no single suite carrying a Score metric" % path)
        else:
            scores.append(score)
    return scores, empty


def precision_lines(a_dirs, b_dirs, goal, warn=None):
    """The stopping rule's verdict as key=value lines: how fine a difference these rounds resolve, against `goal` percent."""
    sides = []
    for dirs, side in ((a_dirs, "A"), (b_dirs, "B")):
        scores, empty = _headline_scores(dirs)
        if not scores:
            raise ValueError("no scores on side %s:\n%s" % (side, "\n".join("  " + l for l in empty) or "  no run directories given"))
        for line in empty:
            (warn or (lambda m: print(m, file=sys.stderr)))("warning: side %s: %s" % (side, line))
        sides.append(scores)
    a, b = sides
    mde, p, mean_a, mean_b = mde_pct(a, b), welch_p(a, b), _mean(a), _mean(b)
    delta = (mean_b - mean_a) / mean_a * 100.0 if mean_a else None

    def f(fmt, v):
        return fmt % v if v is not None else ""
    # The half-width scales as 1/sqrt(n), so the rounds still owed at this spread is what the operator wants to know first.
    need = "%d" % math.ceil(len(a) * (mde / goal) ** 2) if mde is not None and goal and mde > goal else ""
    return ["n_a=%d" % len(a), "n_b=%d" % len(b), "mean_a=%.4f" % mean_a, "mean_b=%.4f" % mean_b, "delta_pct=" + f("%.4f", delta),
            "mde_pct=" + f("%.4f", mde), "goal_pct=%.4f" % goal, "met=%s" % ("yes" if mde is not None and mde <= goal else "no"),
            "rounds_needed=" + need, "p=" + f("%.6f", p),
            # Each arm's noise floor against its own mean, and the delta against what the rounds resolve.
            "sd_a_pct=" + f("%.4f", _sd(a) / mean_a * 100.0 if mean_a else None), "sd_b_pct=" + f("%.4f", _sd(b) / mean_b * 100.0 if mean_b else None),
            "delta_vs_mde=" + f("%.1f", mde / abs(delta) if mde and delta else None)]


def resolved(a_dirs, b_dirs, goal):
    """--detect's stopping rule, asked between rounds on every system: these rounds resolve `goal` percent."""
    try:
        return "met=yes" in precision_lines(a_dirs, b_dirs, goal, warn=lambda m: None)
    except (ValueError, SystemExit):
        return False


def precision(a_spec, b_spec, goal, out=None):
    try:
        lines = precision_lines(split_paths(a_spec), split_paths(b_spec), goal)
    except ValueError as e:
        sys.exit("precision: %s" % e)
    (out or sys.stdout).write("\n".join(lines) + "\n")


def map_row(line):
    """One line of the autorun's runs.tsv: (round, label, staged, run, clean, plan)."""
    r = (line.rstrip("\n").split("\t") + [""] * 6)[:6]
    return tuple(r[:5]) + (r[5] or "unnamed",)


def runs_map(path):
    with open(path) as f:
        return [map_row(l) for l in f if l.strip()]


def ab_summary(runs, root, now, out_path="", out=None, machine=None):
    """A Mac A/B's verdict per plan off its run map, warmup left out; a leg a software-update scan crossed is kept and named."""
    out, machine = out or sys.stdout, machine or Local()
    rows, sink = runs_map(runs), []

    def emit(text=""):
        out.write(text + "\n")
        sink.append(text + "\n")

    try:
        labels = list(dict.fromkeys(r[1] for r in rows))
        emit("A/B summary -- %s\nrun map: %s\n" % (now, runs))
        scanned = ["    round %s arm %s" % (r[0], r[1]) for r in rows if r[4] == "scanned"]
        if scanned:
            emit("\n".join(["  WARNING: a software-update scan ran during these arms:"] + scanned)
                 + "\n  Their numbers are included below. Treat a difference that depends on\n  them as unproven.\n")
        if len(labels) < 2:
            emit("only one arm ('%s') -- nothing to compare. Its runs are listed above." % (labels or [""])[0])
            return 0
        a, b = labels[:2]
        if len(labels) > 2:
            emit("note: %d arms; comparing '%s' against '%s' only" % (len(labels), a, b))
        if {r[2] for r in rows if r[1] == a} & {r[2] for r in rows if r[1] == b}:
            emit("  both arms ran the SAME staged build. This is an A/A control: what it\n  measures is the noise floor of this bench path, "
                 "not a difference between\n  builds. A significant result here means the bench path is not yet quiet\n"
                 "  enough to trust a real A/B at that magnitude.\n")
        for plan in dict.fromkeys(r[5] for r in rows):
            dirs = {l: [os.path.join(root, "results", r[3]) for r in rows if r[5] == plan and r[1] == l and r[0] != "0"] for l in labels}
            emit("================ %s ================" % plan)
            emit("\n".join("  arm %s: %d run(s)" % (l, len(dirs[l])) for l in labels) + "\n  precision:")
            try:
                for line in precision_lines(dirs[a], dirs[b], 0.3, warn=lambda m: emit("    " + m)):
                    emit("    " + line)
            except (ValueError, SystemExit) as e:
                emit("    precision: %s" % e)
            emit("    mde_pct is the smallest difference these rounds resolve; below it,\n"
                 "    'not significant' means 'under this threshold', not 'absent'.\n\n  comparing arm %s against arm %s" % (a, b))
            if dirs[a] and dirs[b]:
                report = build_report(dirs[a], dirs[b])
                if out_path:
                    html = "%s-%s.html" % (os.path.splitext(out_path)[0], plan)
                    machine.write(html, render_html(report, title="A/B summary: %s" % plan))
                    emit("wrote %s" % html)
                emit(render_text(report).rstrip("\n"))
            emit()
        return 0
    finally:
        if out_path:
            machine.write(out_path, "".join(sink))


def split_paths(spec):
    return [p.strip() for p in spec.split(",") if p.strip()]


def two_runs(a_dirs, b_dirs, html="", text=False, out=None, machine=None):
    out = out or sys.stdout
    report = build_report(a_dirs, b_dirs)
    if html:
        (machine or Local()).write(html, render_html(report, title="wk bench report"))
        out.write("wrote %s\n" % html)
    if text or not html:
        out.write(render_text(report))


def built_lines(doc, runs, arm_names, arm_kind):
    """The commit each arm's runs measured, from their env.json, against the one the task names for it."""
    subj = doc.get("subject", {})
    named = {"a": subj.get("base") or "", "b": subj.get("head") or ""} if subj.get("kind") in ("pull", "commit") else {}
    shas = {}
    for r in runs:
        shas.setdefault((r["env"].get("ab") or {}).get("arm", ""), set()).add(r["env"].get("webkit_sha") or "")
    out = []
    for arm, found in sorted(shas.items()):
        label = "%s %s" % (arm_kind, arm_names["ab".index(arm)]) if arm in ("a", "b") and len(arm_names) == 2 else "the runs"
        known, want = sorted(x for x in found if x), named.get(arm, "")
        if not known:
            verdict = "unknown -- no run recorded the commit it measured"
        elif len(known) > 1:
            verdict = "FAIL -- its runs measured %d different commits" % len(known)
        elif want and not (want.startswith(known[0]) or known[0].startswith(want)):
            verdict = "FAIL -- the task names %s" % want[:12]
        else:
            verdict = "ok" + (", the task's %s" % ("base" if arm == "a" else "head") if want else "")
        out.append("%s: %s  %s" % (label, ",".join(k[:12] for k in known) or "?", verdict))
    return out or ["no runs yet"]


def pgo_built(env):
    return bool(presets.PRESETS.get(env.get("preset") or "", {}).get("pgo")) or env.get("build_preset") == pgo.USE


def pgo_faults(reading):
    if not all(k in reading for k in ("benchmarks", "combined", "compressed", "missing")):
        return ["a profile-check.json that is not a profile-check reading"]
    return pgo.faults(reading)


def checks(taskdir, doc, runs, arm_kind):
    """(verdict, check, evidence) for each check a run records: its preflight, the warmup round's, the PGO profile's reading."""
    out = []
    forced = [r for r in runs if r["env"].get("forced")]
    if runs:
        out.append(("FAIL", "preflight", "%d of %d runs forced past failing checks: %s" % (len(forced), len(runs), forced[0]["env"].get("preflight_notes") or "?"))
                   if forced else ("ok", "preflight", "every run passed it"))
        said = [(r["env"].get("preflight_notes", "") + " " + r["env"].get("profiler", "")).strip() for r in runs]
        unread = [s for s in said if record.UNMEASURED in s]
        if unread:
            out.append(("unknown", "preflight", "%d of %d runs could not measure every check: %s" % (len(unread), len(runs), unread[0])))
    rehearsed = [r for r in runs if r["state"] == "rehearsal"]
    if rehearsed:
        out.append(("FAIL", "measured", "%d of %d runs are rehearsals, not measurements" % (len(rehearsed), len(runs))))
    for d in doc.get("devices", []):
        paths = evidence_paths(taskdir, d["device"])
        if any(os.path.isfile(p) for p in paths):
            problems = warmup_check(paths[0], paths[1], arm_kind != "system")
            out.append(("FAIL", "warmup", "%s: %s" % (d["device"], "; ".join(problems))) if problems else ("ok", "warmup", "%s: both arms are what the A/B claims" % d["device"]))
    built = [r for r in runs if pgo_built(r["env"])]
    readings = [record.load(os.path.join(r["dir"], "profile-check.json")) for r in built]
    faults = sorted({f for reading in readings if reading for f in pgo_faults(reading)})
    missing = sum(1 for reading in readings if not reading)
    if faults:
        out.append(("FAIL", "PGO profile", "; ".join(faults)))
    elif missing:
        out.append(("unknown", "PGO profile", "%d of %d PGO runs carry no profile-check.json reading" % (missing, len(built))))
    elif built:
        out.append(("ok", "PGO profile", "every one of %d PGO runs' readings passes" % len(built)))
    return out


def task_report(taskdir, running, html=False, text=False, out=None, shown=None):
    """A task's A/B, one report per device x plan out of its paired rounds; partial data is reported as partial, naming what is missing."""
    out = out or sys.stdout
    taskdir = taskdir.rstrip("/")
    st = record.task_state(taskdir, running)
    doc = st["doc"]
    arm_names, arm_kind = record.task_arms(doc)
    name = doc.get("task", os.path.basename(taskdir))
    lines = ["task      %s" % name,
             "measures  %s" % record.subject_line(doc),
             "state     %s -- %s" % (st["state"], st["summary"]),
             "data      %s" % (shown or taskdir)]
    if st["state"] == "incomplete" and doc.get("restart"):
        lines.append("restart   %s" % doc["restart"])
    lines += ["built:"] + ["  " + l for l in built_lines(doc, st["runs"], arm_names, arm_kind)]
    lines += ["checks:"] + ["  %-8s %-13s %s" % c for c in checks(taskdir, doc, st["runs"], arm_kind)]
    out.write("\n".join(lines) + "\n")
    if len(arm_names) != 2:
        out.write("\nnot an A/B (one arm): nothing to compare. Runs:\n")
        for r in st["runs"]:
            out.write("  %s  %s\n" % (r["state"], os.path.join(shown, os.path.relpath(r["dir"], taskdir)) if shown else r["dir"]))
        return
    for (device, plan), byround in sorted(record.task_rounds(doc, st["runs"]).items()):
        a_dirs, b_dirs, dropped = record.paired(byround, arm_names)
        header = ["%s on %s" % (plan, device),
                  "A = %s %s, B = %s %s" % (arm_kind, arm_names[0], arm_kind, arm_names[1]),
                  "rounds: %d usable of %d attempted (%d planned)%s" % (
                      len(a_dirs), len(byround), doc.get("rounds", 1),
                      ("; dropped " + ", ".join(dropped)) if dropped else "")]
        out.write("\n" + "=" * 72 + "\n" + "\n".join(header) + "\n")
        if not a_dirs:
            out.write("no round has both arms yet; nothing to compare\n")
            continue
        report = build_report(a_dirs, b_dirs, header=lines + [""] + header,
                              warmup=warmup_lines(warmup_load(taskdir, device)))
        if html:
            path = os.path.join(taskdir, "report-%s-%s.html" % (device, plan))   # taskdir is the caller's staged copy
            Local().write_own(path, render_html(report, title="%s: %s on %s" % (name, plan, device)))
            out.write("wrote %s\n" % os.path.join(shown or taskdir, os.path.basename(path)))
        if text or not html:
            out.write("\n" + render_text(dict(report, header=[])))
