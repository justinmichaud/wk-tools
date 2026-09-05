#!/usr/bin/env python3
"""Structured-data operations for `wk bench`: the JSON records a run produces, merging
jsc-shell iteration logs into one result, and warning when two runs are not comparable. Stdlib only, for whatever python3 a macOS host or a bare-metal board has; every input arrives as argv or stdin, never spliced into source text."""

import argparse
import json
import math
import os
import re
import sys

# The axes a bench report groups variance by -- what shifted the stack layout and the shared cache between iterations -- filled in for every subfield a caller left alone, so an older record and an uncontrolled one read alike.
DEFAULT_CONFIGURATION = {"aslr": "unset", "path_len": 0, "shared_cache": None, "env_pad_bytes": 0}


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
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


def cmd_get(args):
    doc = _load(args.file)
    value = _get_nested(doc, args.key)
    print(value if value is not None else args.default)


# gpu by default, deliberately: guessing gpu fails as an easy refusal, while guessing cpu fails as a MotionMark score off llvmpipe that reads as a regression.
def cmd_bench_class(args):
    plan = args.plan
    cpu_prefixes = ("jetstream", "octane", "kraken", "sunspider", "ares6", "jsbench")
    print("cpu" if plan.startswith(cpu_prefixes) else "gpu")


# The one parser for `--cores <set>`'s cpu-list syntax; a set naming a cpu the machine does not have is left to taskset itself to refuse.
_CORES_TOKEN = re.compile(r"^[0-9]+(-[0-9]+)?$")


def cores_set_valid(spec):
    return bool(spec) and all(_CORES_TOKEN.match(tok) for tok in spec.split(","))


def cmd_cores_valid(args):
    sys.exit(0 if cores_set_valid(args.set) else 1)


def cmd_cores_wrap(args):
    if not cores_set_valid(args.set):
        sys.exit("cores-wrap: not a valid cpu list: %s" % args.set)
    sys.stdout.write("taskset -c %s " % args.set)


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


# A jsc-shell log holds the driver's resultsJSON() somewhere in its output, so the last line parsing as a JSON object wins (jsc's exit noise follows it); appending each iteration's "current" scores onto one tree is the shape run-benchmark's --count produces, which compare-results needs to have variance to work with.
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


# The one writer for a bench run's env.json. A field arrives as `key=value` (a dotted key nests), never spliced into source, so a value carrying a quote or backslash can reach the JSON only as that literal string. `--bool` treats empty as false and anything else as true, the rule a shell flag that is either unset or "1" already follows.
def cmd_env_record(args):
    # The axes are written before a run so the record survives its death; wall_time_s is known only afterwards, and a second plain write would drop what the first wrote.
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
    # Every record gets the whole block, so `wk bench report` never special-cases a missing key on an older or untouched record.
    cfg = doc.setdefault("configuration", {})
    for key, value in DEFAULT_CONFIGURATION.items():
        cfg.setdefault(key, value)
    with open(args.out, "w") as f:
        json.dump(doc, f, indent=2)


# Three axes are recorded with every result -- class (what is measured), runner (jsc shell or browser), host (a container or a booted bench image) -- and a mismatch on any means two measurements rather than one slower run, so this is asked, as a warning, before any statistic.
def cmd_axis_check(args):
    a, b = _load(args.a), _load(args.b)
    for line in _axis_check_lines(a, b):
        print(line)


def _axis_check_lines(a, b):
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


# Benjamini-Hochberg as compare-results spells it (computeMultipleHypothesesSignificance): ranked largest to smallest, a rank is significant once it or a larger one clears rank*0.05/n, and every smaller p-value inherits that.
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


# The one result walker, {name: {"Score": [floats], "Time": [floats]}}, because the shapes disagree about depth: a merged jsc log and run-benchmark's JetStream keep numbers one "tests" level down, while Speedometer-2 on a board keeps the total at the suite root and the numbers three down (<suite>/<test>/Sync|Async), with bare descriptor lists between.
# So a node is recorded only where a metric has a "current" array, and named by its path below the suite.
def _subtest_metrics(doc):
    out = {}

    def metric_vals(metrics):
        entry = {}
        if isinstance(metrics, dict):
            for key in ("Score", "Time"):
                if key in metrics:
                    vals = _flatten(_first_current(metrics[key]))
                    if vals:
                        entry[key] = vals
        return entry

    def walk(name, node):
        if not isinstance(node, dict):
            return
        entry = metric_vals(node.get("metrics"))
        if entry:
            out[name] = entry
        tests = node.get("tests")
        if isinstance(tests, dict):
            for child, cnode in tests.items():
                walk("%s/%s" % (name, child) if name else str(child), cnode)

    if isinstance(doc, dict):
        for suite, node in doc.items():
            if not isinstance(node, dict):
                continue
            entry = metric_vals(node.get("metrics"))
            if entry:
                out[str(suite)] = entry
            tests = node.get("tests")
            if isinstance(tests, dict):
                for child, cnode in tests.items():
                    walk(str(child), cnode)
    return out


def _mean(vals):
    return sum(vals) / len(vals) if vals else None


def _sd(vals):
    n = len(vals)
    if n < 2:
        return 0.0
    m = sum(vals) / n
    return (sum((v - m) ** 2 for v in vals) / (n - 1)) ** 0.5


# env.json comes from the result's own directory, empty where missing, so an older run reads as unknown rather than refusing the report.
def _side_runs(paths):
    return [(p, _load(p), _load(os.path.join(os.path.dirname(p), "env.json"))) for p in paths]


def _config_key(env):
    cfg = dict(DEFAULT_CONFIGURATION)
    cfg.update((env or {}).get("configuration") or {})
    return tuple(sorted(cfg.items()))


def _config_label(key):
    return ", ".join("%s=%s" % (k, v) for k, v in key)


# Two things a person kept re-deriving by hand, so the report derives them.
def _consistency_lines(rows):
    """A score and the subtest times it is built from must move opposite ways:
    less work, higher score. When they do not, one of the two is wrong and
    neither should be quoted."""
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


def _order_lines(a_runs, b_runs):
    """Alternating is not the same as counterbalanced: if one arm always goes
    first, monotonic drift lands on the other."""
    order = sorted([(os.path.basename(os.path.dirname(p)), "A") for p, _, _ in a_runs]
                   + [(os.path.basename(os.path.dirname(p)), "B") for p, _, _ in b_runs])
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


def _build_report(a_paths, b_paths, header=(), warmup=()):
    a_runs, b_runs = _side_runs(a_paths), _side_runs(b_paths)
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

    # One correction per metric: Score and Time share no null hypothesis.
    for key in ("Score", "Time"):
        pvals = {r["name"]: r[key]["p"] for r in rows if r[key]["p"] is not None}
        sig = _bh_significant(pvals)
        for r in rows:
            r[key]["significant"] = sig.get(r["name"], False)

    axis_lines = _axis_check_lines(a_runs[0][2], b_runs[0][2])
    axis_lines += _order_lines(a_runs, b_runs)

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
        a_vals = [v for d in a_by_cfg[cfg_key] for e in _subtest_metrics(d).values() for v in primary_vals(e)]
        b_vals = [v for d in b_by_cfg[cfg_key] for e in _subtest_metrics(d).values() for v in primary_vals(e)]
        asd, bsd = _sd(a_vals), _sd(b_vals)
        variance.append({
            "config": _config_label(cfg_key), "a_sd": asd, "b_sd": bsd,
            "a_n": len(a_vals), "b_n": len(b_vals), "flagged": asd > 0 and bsd > asd * 1.2,
        })

    axis_lines += _consistency_lines(rows)
    return {"rows": rows, "axis_lines": axis_lines, "variance": variance,
            "header": list(header), "warmup_lines": list(warmup)}


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


def _render_text(report):
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
</body></html>
""" % (
        _xml_escape(title), _xml_escape(title), header_html, warmup_html, axis_html,
        "".join(rows_html), "".join(hist_html), "".join(var_rows),
    )


# The warmup round's evidence, and the judgement on it. What counts as a problem
# within one arm is decided where it is measured (bench/wk_board_driver.py) and
# recorded in the file; this adds only what needs both arms side by side.
def warmup_load(taskdir, device):
    out = {}
    for arm in ("a", "b"):
        doc = _load(os.path.join(taskdir, "warmup",
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
    """One line per arm, for the report: what the arm actually was."""
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


def cmd_warmup_check(args):
    a, b = _load(args.a), _load(args.b)
    problems = []
    for arm, rec, path in (("A", a, args.a), ("B", b, args.b)):
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
        problems.extend(warmup_cross_problems(a, b, args.same_width))
    for line in problems:
        print(line)
    sys.exit(1 if problems else 0)


def cmd_subtests(args):
    """The subtests a run should ask for: the plan's own list minus the
    exclusions, so both arms of an A/B cover the same set."""
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



def _split_paths(spec):
    return [p.strip() for p in spec.split(",") if p.strip()]


def cmd_report(args):
    a, b = _split_paths(args.a), _split_paths(args.b)
    if not a:
        sys.exit("report: no result files for A")
    if not b:
        sys.exit("report: no result files for B")
    report = _build_report(a, b)
    want_text = args.text or not args.html
    if args.html:
        with open(args.html, "w") as f:
            f.write(_render_html(report))
        print("wrote %s" % args.html)
    if want_text:
        sys.stdout.write(_render_text(report))



# A task is what one benchmarking command produced: a directory holding task.json (the request), runs/<run>/ (the evidence), the command's logs and the reports. Nothing about its state is stored -- planned, ended, usable and complete are recomputed on every read, and "running" is the task's lock, which the caller checks and passes in.
# A run's state comes from its files: `ok` has a result.json; `failed` has none but an env.json carrying wall_time_s, written when run-benchmark returns; `running` has neither, and is either live or a driver that died -- the task's lock tells which.

def _list_field(value):
    return [v.strip() for v in value.split(",") if v.strip()]


def cmd_task_write(args):
    doc = {"commands": list(args.commands)}
    for field in args.fields:
        key, sep, value = field.partition("=")
        if not sep:
            sys.exit("task-write: not a key=value: %s" % field)
        if key == "devices":
            # rpi3=<profile>,rpi4=<profile>: the device and the image it is measured on.
            devs = []
            for item in _list_field(value):
                dev, _, profile = item.partition("=")
                devs.append({"device": dev, "profile": profile})
            doc["devices"] = devs
        elif key in ("plans", "slots"):
            doc[key] = _list_field(value)
        elif key == "rounds":
            doc[key] = int(value)
        else:
            _set_nested(doc, key, value)
    for key in ("task", "requested", "devices", "plans", "slots", "rounds"):
        if key not in doc:
            sys.exit("task-write: %s is required" % key)
    if not doc["devices"] or not doc["plans"] or not doc["slots"]:
        sys.exit("task-write: devices, plans and slots each need at least one entry")
    os.makedirs(os.path.join(args.dir, "runs"), exist_ok=True)
    out = os.path.join(args.dir, "task.json")
    tmp = out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, out)


def _task_doc(taskdir):
    doc = _load(os.path.join(taskdir, "task.json"))
    if not doc:
        sys.exit("%s is not a task: no task.json (wk bench ls lists the tasks)" % taskdir)
    return doc


def _run_state(rundir, env):
    result = os.path.join(rundir, "result.json")
    if os.path.isfile(result) and os.path.getsize(result) > 0:
        return "ok"
    if "wall_time_s" in env:
        return "failed"
    return "running"


def _task_runs(taskdir):
    runs = []
    root = os.path.join(taskdir, "runs")
    if not os.path.isdir(root):
        return runs
    for name in sorted(os.listdir(root)):
        rundir = os.path.join(root, name)
        env = _load(os.path.join(rundir, "env.json"))
        if not env or env.get("warmup"):
            continue
        runs.append({"id": name, "dir": rundir, "env": env, "state": _run_state(rundir, env)})
    return runs


def _kv_file(path):
    out = {}
    try:
        for line in open(path):
            k, sep, v = line.rstrip("\n").partition("=")
            if sep:
                out[k] = v
    except OSError:
        pass
    return out


def _task_arms(doc):
    """The two things a task compares and what to call them: a slot A/B has two slots, while a systems A/B has one slot in two images and compares the systems."""
    subj = doc.get("subject", {})
    slots = doc.get("slots", [])
    if subj.get("kind") == "systems":
        spec = [x for x in subj.get("spec", "").split(",") if x]
        if len(spec) == 2:
            return spec, "system"
    return slots, "slot"


def _subject_line(doc):
    subj = doc.get("subject", {})
    kind = subj.get("kind", "")
    devices = ", ".join(d["device"] for d in doc.get("devices", []))
    plans = ", ".join(doc.get("plans", []))
    slots = doc.get("slots", [])
    if kind in ("pull", "commit"):
        what = "A/B %s: %s vs base %s" % (subj.get("spec", "?"), (subj.get("head") or "?")[:10], (subj.get("base") or "?")[:10])
    elif kind == "workspace":
        what = "%s %s" % (subj.get("spec", "?"), doc["devices"][0].get("profile", ""))
    elif kind == "systems":
        arms, _ = _task_arms(doc)
        what = "%s vs %s" % (arms[0], arms[1]) if len(arms) == 2 else "systems"
    elif len(slots) == 2:
        what = "%s vs %s" % (slots[0], slots[1])
    else:
        what = "slot %s" % "/".join(slots)
    parts = [what]
    if kind != "workspace":
        parts.append(devices)
    parts.append(plans)
    rounds = doc.get("rounds", 1)
    if len(slots) == 2 or rounds > 1:
        parts.append("%d round%s" % (rounds, "" if rounds == 1 else "s"))
    return " · ".join(p for p in parts if p)


# {(device, plan): {round: {arm: run}}}, paired by the ab.round each run recorded.
def _task_rounds(doc, runs):
    out = {}
    for d in doc.get("devices", []):
        for plan in doc.get("plans", []):
            out[(d["device"], plan)] = {}
    for r in runs:
        env = r["env"]
        key = (env.get("machine") or env.get("workspace") or "?", env.get("plan", "?"))
        ab = env.get("ab") or {}
        if "round" not in ab or "arm" not in ab:
            continue
        out.setdefault(key, {}).setdefault(int(ab["round"]), {})[ab["arm"]] = r
    return out


def task_state(taskdir, running):
    doc = _task_doc(taskdir)
    runs = _task_runs(taskdir)
    arm_names, _ = _task_arms(doc)
    planned = len(doc.get("devices", [])) * len(doc.get("plans", [])) * doc.get("rounds", 1) * len(arm_names)
    ok = [r for r in runs if r["state"] == "ok"]
    failed = [r for r in runs if r["state"] == "failed"]
    live = [r for r in runs if r["state"] == "running"]
    ended = len(ok) + len(failed)
    if running:
        state = "running"
    elif ended >= planned:
        state = "complete"
    else:
        state = "incomplete"
    usable = 0
    for byround in _task_rounds(doc, runs).values():
        for byarm in byround.values():
            if len(arm_names) == 2 and all(byarm.get(a, {}).get("state") == "ok" for a in ("a", "b")):
                usable += 1
    status = _kv_file(os.path.join(taskdir, "status"))
    current = live[0] if (running and live) else None
    summary = "%d/%d runs ended, %d ok, %d failed" % (ended, planned, len(ok), len(failed))
    if len(arm_names) == 2:
        summary += ", %d round%s usable" % (usable, "" if usable == 1 else "s")
    if current:
        env = current["env"]
        summary += "; now %s %s %s" % (env.get("plan", "?"), env.get("machine", "?"), env.get("build_slot", "?"))
        progress = _progress_line(os.path.join(current["dir"], "run.log"))
        if progress:
            summary += " (%s)" % progress
    elif running and status.get("stage"):
        summary += "; " + status["stage"]
    elif state == "incomplete" and live:
        summary += "; %d run(s) died with their driver" % len(live)
    return {"doc": doc, "runs": runs, "state": state, "planned": planned, "ended": ended,
            "ok": len(ok), "failed": len(failed), "usable": usable, "current": current,
            "stage": status.get("stage", ""), "summary": summary}


def _progress_line(log):
    try:
        text = open(log, errors="replace").read()
    except OSError:
        return ""
    m = None
    for m in re.finditer(r"Start the iteration (\d+) of (\d+)", text):
        pass
    return "iteration %s/%s" % (m.group(1), m.group(2)) if m else ""


def cmd_task_status(args):
    st = task_state(args.dir, args.running)
    for key in ("state", "planned", "ended", "ok", "failed", "usable", "summary"):
        print("%s=%s" % (key, st[key]))
    print("subject=%s" % _subject_line(st["doc"]))
    print("current=%s" % (st["current"]["id"] if st["current"] else ""))


def cmd_ls(args):
    running = set(_list_field(args.running or ""))
    tasks = sorted(d for d in os.listdir(args.bench_dir)
                   if os.path.isfile(os.path.join(args.bench_dir, d, "task.json")))
    if not tasks:
        print("(no tasks yet)")
        return
    for name in tasks:
        taskdir = os.path.join(args.bench_dir, name)
        st = task_state(taskdir, name in running)
        print("%s  %s" % (name, _subject_line(st["doc"])))
        print("    %s  %s" % (st["state"], st["summary"]))
        print("    %s" % taskdir)
        for r in st["runs"]:
            m = r["env"]
            axes = m.get("runner", "browser")
            if m.get("arch", "native") != "native":
                axes += "/" + m["arch"]
            if m.get("bench_host", "container") != "container":
                axes += "/" + m["bench_host"]
            print("      %s  %s %s %s %s %s%s" % (
                r["dir"], m.get("plan", "?"), m.get("config", "?"), axes,
                (m.get("webkit_sha") or "?")[:10], r["state"],
                "  [FORCED]" if m.get("forced") else ""))


# Partial data is reported as partial, naming the rounds that are missing.
def cmd_task_report(args):
    taskdir = args.dir.rstrip("/")
    st = task_state(taskdir, args.running)
    doc = st["doc"]
    arm_names, arm_kind = _task_arms(doc)
    name = doc.get("task", os.path.basename(taskdir))
    lines = ["task      %s" % name,
             "measures  %s" % _subject_line(doc),
             "state     %s -- %s" % (st["state"], st["summary"]),
             "data      %s" % taskdir]
    print("\n".join(lines))
    if len(arm_names) != 2:
        print("\nnot an A/B (one arm): nothing to compare. Runs:")
        for r in st["runs"]:
            print("  %s  %s" % (r["state"], r["dir"]))
        return
    rounds = _task_rounds(doc, st["runs"])
    want_text = args.text or not args.html
    for (device, plan), byround in sorted(rounds.items()):
        a_paths, b_paths, dropped = [], [], []
        for rnd in sorted(byround):
            arms = byround[rnd]
            if all(arms.get(x, {}).get("state") == "ok" for x in ("a", "b")):
                a_paths.append(os.path.join(arms["a"]["dir"], "result.json"))
                b_paths.append(os.path.join(arms["b"]["dir"], "result.json"))
            else:
                why = ", ".join("%s: %s" % (arm_names[0] if x == "a" else arm_names[1],
                                            arms[x]["state"] if x in arms else "not run")
                                for x in ("a", "b") if arms.get(x, {}).get("state") != "ok")
                dropped.append("round %d (%s)" % (rnd, why))
        header = ["%s on %s" % (plan, device),
                  "A = %s %s, B = %s %s" % (arm_kind, arm_names[0], arm_kind, arm_names[1]),
                  "rounds: %d usable of %d attempted (%d planned)%s" % (
                      len(a_paths), len(byround), doc.get("rounds", 1),
                      ("; dropped " + ", ".join(dropped)) if dropped else "")]
        print("\n" + "=" * 72)
        print("\n".join(header))
        if not a_paths:
            print("no round has both arms yet; nothing to compare")
            continue
        report = _build_report(a_paths, b_paths, header=lines + [""] + header,
                               warmup=warmup_lines(warmup_load(taskdir, device)))
        if args.html:
            out = os.path.join(taskdir, "report-%s-%s.html" % (device, plan))
            with open(out, "w") as f:
                f.write(_render_html(report, title="%s: %s on %s" % (name, plan, device)))
            print("wrote %s" % out)
        if want_text:
            print()
            sys.stdout.write(_render_text({**report, "header": []}))


def main(argv):
    parser = argparse.ArgumentParser(prog="wkdata.py", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("get", help="print one dotted field out of a JSON file")
    p.add_argument("file")
    p.add_argument("key")
    p.add_argument("--default", default="")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("ls", help="every task in the store, with its runs' paths and states")
    p.add_argument("bench_dir")
    p.add_argument("--running", help="comma-separated task names whose lock is held")
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("task-write", help="write a task's task.json")
    p.add_argument("dir")
    p.add_argument("fields", nargs="*", metavar="key=value")
    p.add_argument("--command", dest="commands", action="append", default=[], metavar="CMD")
    p.set_defaults(func=cmd_task_write)

    p = sub.add_parser("task-status", help="a task's state, recomputed from its runs, as key=value lines")
    p.add_argument("dir")
    p.add_argument("--running", action="store_true", help="the task's lock is held")
    p.set_defaults(func=cmd_task_status)

    p = sub.add_parser("task-report", help="a report per device x plan out of the rounds a task has so far")
    p.add_argument("dir")
    p.add_argument("--running", action="store_true", help="the task's lock is held")
    p.add_argument("--html", action="store_true", help="also write report-<device>-<plan>.html into the task")
    p.add_argument("--text", action="store_true", help="print the text tables (default when --html is not given)")
    p.set_defaults(func=cmd_task_report)

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

    p = sub.add_parser("plan-spec", help="a plan's fetchable source, read from stdin")
    p.set_defaults(func=cmd_plan_spec)

    p = sub.add_parser("subtests", help="the plan's subtests minus --exclude, read from stdin")
    p.add_argument("--exclude", default="", help="comma-separated subtests to drop")
    p.set_defaults(func=cmd_subtests)

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

    # argparse fills an `nargs="*"` positional from one unbroken run of words, so `env-record OUT --update wall_time_s=42` -- how every caller writing a field after a run spells it -- leaves the field over as unrecognized.
    # A leftover is a field for a subcommand that takes fields, and an error for one that does not.
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
