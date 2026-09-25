"""`wk bench report`, `compare` and `precision`: the tree's one score reader, significance test and stopping rule."""

import math
import os
import sys

from wk.bench import record


def axis_check_lines(a, b):
    lines = []
    if not a or not b:
        return lines

    if a.get("plan") != b.get("plan"):
        lines.append("warning: different plans (%s vs %s)" % (a.get("plan"), b.get("plan")))
    if a.get("config") != b.get("config"):
        lines.append("warning: different build configs (%s vs %s)" % (a.get("config"), b.get("config")))

    # A runner or host mismatch is not a caveat: it is two machines doing different work, and the statistics below will still produce a p-value for them.
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

    # No default: a run predating the field says nothing, and absent is not different.
    if a.get("class") and b.get("class") and a["class"] != b["class"]:
        lines.append("warning: different benchmark classes (%s vs %s)" % (a["class"], b["class"]))

    # Only evidence for a gpu-class run: "no renderer" about a jsc-shell JetStream run is noise, and noise in front of real warnings is how those stop being read.
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
    # A restricted run is a different measurement from a whole one, and the number carries no mark of it otherwise.
    ex_a, ex_b = a.get("subtests_excluded") or "", b.get("subtests_excluded") or ""
    if ex_a != ex_b:
        lines.append("warning: the arms ran different subtest sets (%s vs %s)"
                     % (ex_a or "none excluded", ex_b or "none excluded"))
    elif ex_a:
        lines.append("note: %d subtest(s) excluded from both arms -- %s"
                     % (len(ex_a.split(",")), ex_a))
    if a.get("forced") or b.get("forced"):
        lines.append("warning: at least one run was taken with failing preflight checks (--force)")

    # Bench mode asserted by an override rather than by having booted the image: the number came off a workstation however it is labelled.
    if a.get("role_marker_overridden") or b.get("role_marker_overridden"):
        lines.append(
            "warning: at least one run only *claimed* bench mode "
            "(WK_IMAGE_MARKER was overridden) -- it was measured on a workstation"
        )
    if a.get("local_copy") != b.get("local_copy"):
        lines.append("warning: different benchmark payloads (%s vs %s)" % (a.get("local_copy"), b.get("local_copy")))

    cores_a = (a.get("cores") or {}).get("set") or ""
    cores_b = (b.get("cores") or {}).get("set") or ""
    if cores_a != cores_b:
        lines.append(
            "warning: different core pins (%s vs %s)"
            % (cores_a or "unpinned", cores_b or "unpinned")
        )

    # A warning where the kernel below is not: nobody sets out to compare two boards.
    if a.get("machine") and b.get("machine") and a["machine"] != b["machine"]:
        lines.append(
            "warning: different machines (%s vs %s) -- these are two computers, not "
            "two states of one" % (a["machine"], b["machine"])
        )

    # The kernel and system are reported, not warned about: for a kernel A/B their differing is the whole experiment. Width, which `arch` does not answer, is a warning -- that is two measurements.
    kaa = (a.get("host") or {}).get("kernel_arch")
    kab = (b.get("host") or {}).get("kernel_arch")
    if kaa and kab and kaa != kab:
        lines.append(
            "warning: different kernel widths (%s vs %s) -- a 32-bit system and a "
            "32-bit process on a 64-bit kernel are not the same measurement" % (kaa, kab)
        )

    # Cheap flash contributes variance rather than a subtractable bias, so a stick run and an SSD run are two series.
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


# The one place that turns two saved runs into a judgement, so this repo has one score reader and one significance test. Welch and Benjamini-Hochberg are pure stdlib rather than Tools/Scripts/compare-results, which needs scipy, is off the PYTHONPATH outside a workspace, and computes one metric per benchmark type rather than both per subtest.
# math.lgamma gives the regularized incomplete beta function, of which a t statistic's two-tailed p-value is a closed form: the same test, no dependency.
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


# Two-sided Welch's t-test: unequal variance, Welch-Satterthwaite degrees of freedom.
def welch_p(a, b):
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


# Benjamini-Hochberg as compare-results spells it (computeMultipleHypothesesSignificance): ranked largest to smallest, a rank is significant once it or a larger one clears rank*0.05/n, and every smaller p-value inherits that.
# A run stops when it can *resolve* the effect asked of it, not when it has found one: stopping on precision is a legitimate sequential design where stopping on a p-value is not. The t comes back out of the same incomplete beta the p-value goes into, by bisection, so this file holds one distribution.
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


# The smallest relative difference this many rounds of this much spread resolve, at two-sided 95% confidence and 80% power, as a percentage of A's mean.
def mde_pct(a, b):
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return None
    mean_a, mean_b = sum(a) / na, sum(b) / nb
    if mean_a <= 0:
        return None
    var_a = sum((x - mean_a) ** 2 for x in a) / (na - 1)
    var_b = sum((x - mean_b) ** 2 for x in b) / (nb - 1)
    se2 = var_a / na + var_b / nb
    if se2 <= 0:
        return 0.0
    df = se2 * se2 / ((var_a / na) ** 2 / (na - 1) + (var_b / nb) ** 2 / (nb - 1))
    t_alpha = t_crit(df, 0.05)
    t_beta = t_crit(df, 0.40)   # one-tailed 0.80 is the two-tailed 0.40 point
    if t_alpha is None or t_beta is None:
        return None
    return (t_alpha + t_beta) * math.sqrt(se2) / mean_a * 100.0


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


def _flatten(x, acc=None):
    if acc is None:
        acc = []
    if isinstance(x, list):
        for i in x:
            _flatten(i, acc)
    elif isinstance(x, (int, float)):
        acc.append(float(x))
    return acc


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
_AGGREGATORS = {
    "Arithmetic": lambda vals: sum(vals) / len(vals),
    "Geometric": lambda vals: math.exp(sum(math.log(v) for v in vals) / len(vals)),
    "Total": sum,
}


def _iteration_values(metric):
    """One value per iteration; Speedometer's entry is itself that iteration's internal repeats."""
    cur = _first_current(metric)
    if not isinstance(cur, list):
        return None
    out = []
    for item in cur:
        vals = _flatten(item)
        if not vals:
            return None
        out.append(sum(vals) / len(vals))
    return out or None


# A declaration is a list where values would be; any metric may carry one, at any depth.
def _declared_metric(node, key="Score"):
    metrics = node.get("metrics") if isinstance(node, dict) else None
    metric = metrics.get(key) if isinstance(metrics, dict) else None
    return metric if isinstance(metric, list) else None


# One aggregate per iteration, because pooling every subtest's every iteration mixes them into one number that is nobody's score. Returns the per-iteration aggregates, or the reason there are none -- a reader that refuses and one that reports both need it, and neither computes it twice.
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
# A node becomes a row where a metric has a "current" array, or where the suite declares an aggregate instead of writing one; either way it is named by its full path, so the suite is the only row whose name holds no "/".
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
    return sum(vals) / len(vals) if vals else None


def _sd(vals):
    n = len(vals)
    if n < 2:
        return 0.0
    m = sum(vals) / n
    return (sum((v - m) ** 2 for v in vals) / (n - 1)) ** 0.5


# A run is named by the directory a benchmark wrote; the files inside it are derived here and nowhere else.
def run_result(rundir):
    path = os.path.join(rundir, "result.json")
    if not os.path.isfile(path):
        return path, None, "%s: no result.json in this directory" % rundir
    doc = record.load(path)
    return path, doc, None if doc else "%s: empty, or not JSON" % path


# env.json is read from the same directory, empty where missing, so an older run reads as unknown rather than refusing the report.
def _side_runs(dirs, side):
    runs, missing = [], []
    for d in dirs:
        d = os.path.normpath(d)
        _path, doc, why = run_result(d)
        if why:
            missing.append(why)
            continue
        env = record.load(os.path.join(d, "env.json"))
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
    tops = [r for r in rows if r["Score"]["a_mean"] and r["Score"]["b_mean"]
            and "/" not in r["name"]]
    leaves = [r for r in rows if r["Time"]["a_mean"] and r["Time"]["b_mean"]
              and "/" in r["name"]]
    if len(tops) != 1 or len(leaves) < 8:
        return []
    sa, sb = tops[0]["Score"]["a_mean"], tops[0]["Score"]["b_mean"]
    ta = sum(r["Time"]["a_mean"] for r in leaves)
    tb = sum(r["Time"]["b_mean"] for r in leaves)
    if not (sa and ta):
        return []
    score_delta = (sb - sa) / sa * 100.0
    time_delta = (tb - ta) / ta * 100.0
    lines = ["note: B scores %+.2f%% on %+.2f%% subtest time (%d subtests, A %.0f ms, B %.0f ms)"
             % (score_delta, time_delta, len(leaves), ta, tb)]
    if abs(score_delta) < 0.2 and abs(time_delta) < 0.2:
        return lines
    if (score_delta > 0) == (time_delta > 0):
        lines.append(
            "warning: the score and the subtest times it is made of disagree in "
            "SIGN -- B does %+.2f%% work and scores %+.2f%%. One of the two is "
            "wrong; do not quote either until they are reconciled."
            % (time_delta, score_delta))
    elif abs(score_delta + time_delta) > 5.0:
        lines.append(
            "warning: the score moved %+.2f%% where the subtest times imply about "
            "%+.2f%% -- the aggregate weights subtests very differently from their "
            "cost, so the headline and the table answer different questions."
            % (score_delta, -time_delta))
    return lines


def order_lines(a_runs, b_runs):
    """Alternating is not counterbalanced: if one arm always goes first, monotonic drift lands on the other."""
    order = sorted([(os.path.basename(p), "A") for p, _, _ in a_runs]
                   + [(os.path.basename(p), "B") for p, _, _ in b_runs])
    if len(order) < 4:
        return []
    pos = {"A": [], "B": []}
    for i, (_, arm) in enumerate(order, 1):
        pos[arm].append(i)
    ma = sum(pos["A"]) / len(pos["A"])
    mb = sum(pos["B"]) / len(pos["B"])
    if abs(ma - mb) < 0.25:
        return []
    late, gap = ("B", mb - ma) if mb > ma else ("A", ma - mb)
    return ["warning: the arms alternate but are not counterbalanced -- %s runs "
            "%.1f position(s) later on average (A at %s, B at %s), so monotonic "
            "drift lands on %s rather than cancelling"
            % (late, gap, pos["A"], pos["B"], late)]


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

    # Speedometer has no Score at all: the same rule _row_primary applies per subtest.
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


def _xml_escape(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# Overlaid rather than side by side: the overlap is what shows two distributions occupying the same range or not. No plotting library; this is the whole chart.
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


def render_text(report):
    out = list(report.get("header", []))
    if out:
        out.append("")
    if report.get("warmup_lines"):
        out.append("warmup round (not measured):")
        out += ["  " + l for l in report["warmup_lines"]]
        out.append("")
    out.append("axis check:")
    out += ["  " + l for l in report["axis_lines"]] or ["  (no warnings)"]
    out.append("")
    fmt = "%%-%ds %%-6s %%14s %%14s %%10s %%9s %%5s" % max(
        [len("subtest")] + [len(r["name"]) for r in report["rows"]])
    header = fmt % ("subtest", "metric", "A mean+-sd", "B mean+-sd", "delta %", "p", "sig")
    out += ["subtests:", header, "-" * len(header)]
    for row in report["rows"]:
        for key in ("Score", "Time"):
            m = row[key]
            if m["a_mean"] is None and m["b_mean"] is None:
                continue
            out.append(
                fmt
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
    out.append("")
    out.append("spread, within a run and between runs:")
    out += ["  " + l for l in report["spread"]] or ["  (no suite row)"]
    return "\n".join(out) + "\n"


def render_html(report, title="wk bench report"):
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

    header_html = "".join("<li>%s</li>" % _xml_escape(l) for l in report.get("header", []))
    if header_html:
        header_html = "<ul>%s</ul>" % header_html

    warmup_html = ""
    if report.get("warmup_lines"):
        warmup_html = "<h2>warmup round (not measured)</h2><ul>%s</ul>" % "".join(
            "<li>%s</li>" % _xml_escape(l) for l in report["warmup_lines"])
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
        _xml_escape(title), _xml_escape(title), header_html, warmup_html, axis_html,
        "".join(rows_html), "".join(hist_html), "".join(var_rows),
        "".join("<li>%s</li>" % _xml_escape(l) for l in report["spread"]) or "<li>(no suite row)</li>",
    )


# The warmup round's evidence, and the judgement on it. What counts as a problem within one arm is decided where it is measured (lib/wk/bench/board_driver.py) and recorded in the file; this adds only what needs both arms side by side.
def warmup_load(taskdir, device):
    out = {}
    for arm in ("a", "b"):
        doc = record.load(os.path.join(taskdir, "warmup",
                                "%s-%s.evidence.json" % (device, arm)))
        if doc:
            out[arm] = doc
    return out


def warmup_cross_problems(a, b, same_width_expected):
    problems = []
    ga, gb = a.get("gl", {}), b.get("gl", {})
    if ga.get("driver") and gb.get("driver") and ga["driver"] != gb["driver"]:
        problems.append("the arms rendered through different drivers (%s vs %s)"
                        % (ga["driver"], gb["driver"]))
    ea, eb = a.get("elf", {}), b.get("elf", {})
    if same_width_expected and ea.get("bits") and eb.get("bits") and ea["bits"] != eb["bits"]:
        problems.append("the arms are %d-bit and %d-bit, and this A/B varies neither the "
                        "image nor the width" % (ea["bits"], eb["bits"]))
    return problems


def warmup_lines(evidence):
    lines = []
    for arm in ("a", "b"):
        rec = evidence.get(arm)
        if not rec:
            lines.append("%s: no warmup evidence" % arm.upper())
            continue
        elf, gl, jit = rec.get("elf", {}), rec.get("gl", {}), rec.get("jit", {})
        gpu = rec.get("gpu") or {}
        lines.append("%s: %s-bit %s, renderer %s%s" % (
            arm.upper(), elf.get("bits", "?"), elf.get("machine", "?"),
            os.path.basename(gl.get("driver") or "unknown"),
            " [SOFTWARE]" if gl.get("software") else ""))
        lines.append("   GPU busy %s ms on %s%s" % (
            gpu.get("busy_ms", "?"), gpu.get("driver") or "unknown",
            (" (" + ", ".join("%s %d ms" % kv for kv in
                              list((gpu.get("by_process_ms") or {}).items())[:3]) + ")")
            if gpu.get("by_process_ms") else ""))
        lines.append("   JIT %s, %s executable in %d mapping(s)%s" % (
            jit.get("verdict", "?"), _bytes_label(jit.get("exec_bytes", 0)),
            jit.get("exec_mappings", 0),
            ("; compiles " + ", ".join("%s=%d" % kv for kv in sorted(
                (jit.get("tiers") or {}).items()))) if jit.get("tiers") else ""))
        for note in rec.get("notes", []):
            lines.append("   %s" % note)
        if rec.get("problems"):
            lines.extend("   %s" % p for p in rec["problems"])
        if rec.get("profile"):
            lines.append("   profile: %s (%s)" % (rec["profile"].get("file", "?"),
                                                  rec["profile"].get("tool", "?")))
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
        if not rec:
            problems.append("arm %s produced no warmup evidence (%s)" % (arm, path))
            continue
        missing = [k for k in ("elf", "gl", "jit", "problems") if k not in rec]
        if missing:
            problems.append(
                "arm %s's evidence file is not warmup evidence -- it parses as JSON but "
                "has no %s (%s)" % (arm, "/".join(missing), path))
            continue
        problems.extend("arm %s: %s" % (arm, p) for p in rec.get("problems", []))
    if a and b:
        problems.extend(warmup_cross_problems(a, b, same_width))
    return problems


def headline_score(doc):
    roots = [(str(k), v) for k, v in doc.items()
             if isinstance(v, dict) and isinstance(v.get("metrics"), dict)
             and "Score" in v["metrics"]]
    if len(roots) != 1:
        return None
    suite, node = roots[0]
    declared = _declared_metric(node)
    if declared is None:
        vals = _iteration_values(node["metrics"]["Score"])
    else:
        vals, why = _declared_aggregate(suite, node, declared)
        if why:
            sys.exit("ab-precision: " + why)
    return sum(vals) / len(vals) if vals else None


def _headline_scores(dirs):
    scores, empty = [], []
    for d in dirs:
        path, doc, why = run_result(d)
        if why:
            empty.append(why)
            continue
        score = headline_score(doc)
        if score is None:
            empty.append("%s: no single suite carrying a Score metric" % path)
            continue
        scores.append(score)
    return scores, empty


def precision_lines(a_dirs, b_dirs, target, warn=None):
    """The stopping rule's verdict as key=value lines: how fine a difference these rounds resolve, against `target`
    percent. ValueError names a side with no scores."""
    a, a_empty = _headline_scores(a_dirs)
    b, b_empty = _headline_scores(b_dirs)
    for scores, empty, side in ((a, a_empty, "A"), (b, b_empty, "B")):
        if not scores:
            raise ValueError("no scores on side %s:\n%s" % (side, "\n".join("  " + l for l in empty) or "  no run directories given"))
        for line in empty:
            (warn or (lambda m: print(m, file=sys.stderr)))("warning: side %s: %s" % (side, line))
    mde = mde_pct(a, b)
    mean_a, mean_b = sum(a) / len(a), sum(b) / len(b)
    delta = (mean_b - mean_a) / mean_a * 100.0 if mean_a else None
    p = welch_p(a, b)
    # The half-width scales as 1/sqrt(n), so the rounds still owed at this spread is what the operator wants to know before committing the machine.
    need = "%d" % math.ceil(len(a) * (mde / target) ** 2) if mde is not None and target and mde > target else ""
    lines = ["n_a=%d" % len(a), "n_b=%d" % len(b), "mean_a=%.4f" % mean_a, "mean_b=%.4f" % mean_b,
             "delta_pct=%s" % ("%.4f" % delta if delta is not None else ""),
             "mde_pct=%s" % ("%.4f" % mde if mde is not None else ""),
             "target_pct=%.4f" % target,
             "met=%s" % ("yes" if mde is not None and mde <= target else "no"),
             "rounds_needed=%s" % need,
             "p=%s" % ("%.6f" % p if p is not None else "")]
    # Each arm's noise floor as a share of its own mean, and the delta read against what the rounds resolve: `met` answers --target, these answer the delta just measured.
    for side, vals, mean in (("a", a, mean_a), ("b", b, mean_b)):
        lines.append("sd_%s_pct=%s" % (side, "%.4f" % (_sd(vals) / mean * 100.0) if mean else ""))
    lines.append("delta_vs_mde=%s" % ("%.1f" % (mde / abs(delta)) if mde and delta else ""))
    return lines


def resolved(a_dirs, b_dirs, target):
    """--detect's stopping rule, asked between rounds on every system: these rounds resolve `target` percent."""
    try:
        return "met=yes" in precision_lines(a_dirs, b_dirs, target, warn=lambda m: None)
    except (ValueError, SystemExit):
        return False


def precision(a_spec, b_spec, target, out=None):
    try:
        lines = precision_lines(split_paths(a_spec), split_paths(b_spec), target)
    except ValueError as e:
        sys.exit("ab-precision: %s" % e)
    (out or sys.stdout).write("\n".join(lines) + "\n")


def map_row(line):
    """One line of runs.tsv, the autorun's record of which result is which round, arm and plan:
    (round, label, staged, run, clean, plan)."""
    r = (line.rstrip("\n").split("\t") + [""] * 6)[:6]
    return tuple(r[:5]) + (r[5] or "unnamed",)


def runs_map(path):
    with open(path) as f:
        return [map_row(l) for l in f if l.strip()]


def ab_summary(runs, root, now, out_path="", out=None):
    """A Mac A/B's verdict off its run map, per plan: precision, then arm A against arm B. The warmup round is left out;
    a leg a software-update scan ran across is kept and named."""
    out = out or sys.stdout
    rows = runs_map(runs)
    sink = open(out_path, "w") if out_path else None

    def emit(line=""):
        out.write(line + "\n")
        if sink:
            sink.write(line + "\n")

    try:
        labels = list(dict.fromkeys(r[1] for r in rows))
        emit("A/B summary -- %s" % now)
        emit("run map: %s" % runs)
        emit()
        scanned = ["    round %s arm %s" % (r[0], r[1]) for r in rows if r[4] == "scanned"]
        if scanned:
            for line in ["  WARNING: a software-update scan ran during these arms:"] + scanned + [
                    "  Their numbers are included below. Treat a difference that depends on", "  them as unproven.", ""]:
                emit(line)
        if len(labels) < 2:
            emit("only one arm ('%s') -- nothing to compare. Its runs are listed above." % (labels or [""])[0])
            return 0
        a, b = labels[:2]
        if len(labels) > 2:
            emit("note: %d arms; comparing '%s' against '%s' only" % (len(labels), a, b))
        if {r[2] for r in rows if r[1] == a} & {r[2] for r in rows if r[1] == b}:
            for line in ("  both arms ran the SAME staged build. This is an A/A control: what it",
                         "  measures is the noise floor of this lane, not a difference between",
                         "  builds. A significant result here means the lane is not yet quiet",
                         "  enough to trust a real A/B at that magnitude.", ""):
                emit(line)
        for plan in dict.fromkeys(r[5] for r in rows):
            dirs = {l: [os.path.join(root, "results", r[3]) for r in rows if r[5] == plan and r[1] == l and r[0] != "0"] for l in labels}
            emit("================ %s ================" % plan)
            for l in labels:
                emit("  arm %s: %d run(s)" % (l, len(dirs[l])))
            emit("  precision:")
            try:
                for line in precision_lines(dirs[a], dirs[b], 0.3, warn=lambda m: emit("    " + m)):
                    emit("    " + line)
            except (ValueError, SystemExit) as e:
                emit("    ab-precision: %s" % e)
            emit("    mde_pct is the smallest difference these rounds resolve; below it,")
            emit("    'not significant' means 'under this threshold', not 'absent'.")
            emit()
            emit("  comparing arm %s against arm %s" % (a, b))
            if dirs[a] and dirs[b]:
                report = build_report(dirs[a], dirs[b])
                if out_path:
                    html = "%s-%s.html" % (os.path.splitext(out_path)[0], plan)
                    _write_html(html, report, "A/B summary: %s" % plan)
                    emit("wrote %s" % html)
                for line in render_text(report).splitlines():
                    emit(line)
            emit()
        return 0
    finally:
        if sink:
            sink.close()


def split_paths(spec):
    return [p.strip() for p in spec.split(",") if p.strip()]


def _write_html(path, report, title):
    with open(path, "w") as f:
        f.write(render_html(report, title=title))


def two_runs(a_dirs, b_dirs, html="", text=False, out=None):
    out = out or sys.stdout
    report = build_report(a_dirs, b_dirs)
    if html:
        _write_html(html, report, "wk bench report")
        out.write("wrote %s\n" % html)
    if text or not html:
        out.write(render_text(report))


def task_report(taskdir, running, html=False, text=False, out=None):
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
             "data      %s" % taskdir]
    out.write("\n".join(lines) + "\n")
    if len(arm_names) != 2:
        out.write("\nnot an A/B (one arm): nothing to compare. Runs:\n")
        for r in st["runs"]:
            out.write("  %s  %s\n" % (r["state"], r["dir"]))
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
            path = os.path.join(taskdir, "report-%s-%s.html" % (device, plan))
            _write_html(path, report, "%s: %s on %s" % (name, plan, device))
            out.write("wrote %s\n" % path)
        if text or not html:
            out.write("\n" + render_text(dict(report, header=[])))
