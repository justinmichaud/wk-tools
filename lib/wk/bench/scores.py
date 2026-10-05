"""The tree's one score reader, its significance test and the A/B's stopping rule and warmup check."""

import math
import os
import statistics
import sys

from wk.bench import record


# In stdlib: compare-results needs scipy and computes one metric per type. A t's two-tailed p is the regularized incomplete beta.
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
    out = [mean(_flatten(item)) for item in cur] if isinstance(cur, list) else []
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


def mean(vals):
    return statistics.fmean(vals) if vals else None


def sd(vals):
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


def evidence_paths(taskdir, device):
    return [os.path.join(taskdir, "warmup", "%s-%s.evidence.json" % (device, arm)) for arm in "ab"]


def warmup_check(a_path, b_path, same_width):
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
    return mean(vals) if vals else None


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
    sides = []
    for dirs, side in ((a_dirs, "A"), (b_dirs, "B")):
        scores, empty = _headline_scores(dirs)
        if not scores:
            raise ValueError("no scores on side %s:\n%s" % (side, "\n".join("  " + l for l in empty) or "  no run directories given"))
        for line in empty:
            (warn or (lambda m: print(m, file=sys.stderr)))("warning: side %s: %s" % (side, line))
        sides.append(scores)
    a, b = sides
    mde, p, mean_a, mean_b = mde_pct(a, b), welch_p(a, b), mean(a), mean(b)
    delta = (mean_b - mean_a) / mean_a * 100.0 if mean_a else None

    def f(fmt, v):
        return fmt % v if v is not None else ""
    need = "%d" % math.ceil(len(a) * (mde / goal) ** 2) if mde is not None and goal and mde > goal else ""
    return ["n_a=%d" % len(a), "n_b=%d" % len(b), "mean_a=%.4f" % mean_a, "mean_b=%.4f" % mean_b, "delta_pct=" + f("%.4f", delta),
            "mde_pct=" + f("%.4f", mde), "goal_pct=%.4f" % goal, "met=%s" % ("yes" if mde is not None and mde <= goal else "no"),
            "rounds_needed=" + need, "p=" + f("%.6f", p),
            "sd_a_pct=" + f("%.4f", sd(a) / mean_a * 100.0 if mean_a else None), "sd_b_pct=" + f("%.4f", sd(b) / mean_b * 100.0 if mean_b else None),
            "delta_vs_mde=" + f("%.1f", mde / abs(delta) if mde and delta else None)]


def resolved(a_dirs, b_dirs, goal):
    try:
        return "met=yes" in precision_lines(a_dirs, b_dirs, goal, warn=lambda m: None)
    except (ValueError, SystemExit):
        return False
