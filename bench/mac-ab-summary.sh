#!/usr/bin/env bash
#
# wk bench ab-summary -- turn a run of `wk bench mac-ab` into a verdict.
#
#   wk bench ab-summary --runs <runs.tsv> [--root <dir>] [--out <file>]
#                                          (--out also writes <file-without-
#                                          extension>.html, a self-contained
#                                          report with histograms)
#
# Reads the arm-to-result map the autorun wrote (round, label, staged id,
# result directory) and hands the two arms to `wk bench report` -- which is
# where the per-subtest Score and Time, the axis warnings, the Welch/FDR
# p-value and the histograms come from, and which is deliberately not
# reimplemented here.
#
# Runs on the Mac in *either* mode: in bench mode it is the last thing the
# autorun does, and in host mode it is how the same verdict is re-read after
# the machine has come back. The only difference is the path to the results,
# which is why --root exists.
#
# Two arms with the same staged id is not a mistake -- it is the A/A control:
# the noise floor a real A/B needs to be read against (below).

set -euo pipefail
WK_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$WK_ROOT/lib/common.sh"

ROOT="/var/wk"
RUNS=""
OUT=""

while [ $# -gt 0 ]; do
    case "$1" in
        --root) ROOT="${2:-}"; shift 2 ;;
        --runs) RUNS="${2:-}"; shift 2 ;;
        --out)  OUT="${2:-}"; shift 2 ;;
        -h|--help) sed -n '3,6p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2 ;;
        *) die "unknown option: $1" ;;
    esac
done

[ -n "$RUNS" ] || die "which run map? --runs <runs.tsv>"
[ -f "$RUNS" ] || die "no run map at $RUNS -- the A/B recorded nothing"

PY=/usr/bin/python3
[ -x "$PY" ] || PY=python3

# The arms, and the paths that make each one. Everything downstream reads these
# two variables, so the grouping happens once.
labels=$(awk -F'\t' '{print $2}' "$RUNS" | awk '!seen[$0]++')
[ -n "$labels" ] || die "no arms in $RUNS"

arm_paths() {  # $1 = label -> comma-separated run directories
    awk -F'\t' -v l="$1" '$2==l {printf "%s%s/results/%s", sep, r, $4; sep=","}' \
        r="$ROOT" "$RUNS"
}

emit() {
    printf '%s\n' "$*"
    [ -n "$OUT" ] || return 0
    printf '%s\n' "$*" >> "$OUT" || die "cannot write to $OUT"
    return 0
}

[ -n "$OUT" ] && { : > "$OUT" || die "cannot write to $OUT"; }

emit "A/B summary -- $(date -u +%Y-%m-%dT%H:%M:%SZ)"
emit "run map: $RUNS"
emit ""

# Contaminated arms, named before the numbers rather than after them (fifth
# column in $RUNS; docs/HANDOFF-mac-perf-mode.md). Still averaged in below --
# dropping data on a heuristic is its own way to get a wrong answer -- but
# said first: a difference that lives entirely in a scanned arm is not a
# difference between builds. Runs from before the column existed carry no
# accusation.
scanned=$(awk -F'\t' '$5 == "scanned" { printf "    round %s arm %s\n", $1, $2 }' "$RUNS")
if [ -n "$scanned" ]; then
    emit "  WARNING: a software-update scan ran during these arms:"
    while IFS= read -r line; do emit "$line"; done <<EOF
$scanned
EOF
    emit "  Their numbers are included below. Treat a difference that depends on"
    emit "  them as unproven."
    emit ""
fi

# Per-arm numbers are not computed here: `wk bench report` below reads every
# arm's result.json directly (lib/wkdata.py, via cmd/bench) and shows A and B
# side by side, per subtest -- more than one arm's mean alone would say. This
# just counts rounds per arm, so an empty arm is visible before the report
# tries to compare it against nothing.
for l in $labels; do
    emit "arm $l: $(arm_paths "$l" | tr ',' '\n' | grep -c .) run(s)"
done
emit ""

# A null result means nothing on its own: it is only informative next to the
# smallest difference this run had the power to find (docs/HANDOFF-mac-perf-
# mode.md has the noise-floor analysis and the count/rounds tradeoff).
mde_note() {
    "$PY" - "$RUNS" "$ROOT" <<'MDEPY'
import json, os, statistics as st, sys
runs, root = sys.argv[1], sys.argv[2]
arms = {}
for line in open(runs):
    f = line.rstrip("\n").split("\t")
    if len(f) < 4: continue
    arms.setdefault(f[1], []).append(os.path.join(root, "results", f[3], "result.json"))
def coll(o, out):
    if isinstance(o, dict):
        for k, v in o.items():
            if k == "Score": out.append(v)
            coll(v, out)
    elif isinstance(o, list):
        for i in o: coll(i, out)
    return out
def nums(x, a):
    if isinstance(x, list):
        for i in x: nums(i, a)
    elif isinstance(x, dict):
        for v in x.values(): nums(v, a)
    elif isinstance(x, (int, float)): a.append(float(x))
    return a
def score(p):
    try: d = json.load(open(p))
    except Exception: return None
    v = nums(coll(d, []), [])
    return st.mean(v) if v else None
groups = {}
for a, ps in arms.items():
    vs = [x for x in (score(p) for p in ps) if x is not None]
    if len(vs) >= 2: groups[a] = vs
if len(groups) < 2:
    print("  (too few rounds per arm to state a detectable effect size)")
    raise SystemExit
num = sum((len(v) - 1) * st.variance(v) for v in groups.values())
den = sum(len(v) - 1 for v in groups.values())
sp = (num / den) ** 0.5
grand = st.mean([x for v in groups.values() for x in v])
ns = [len(v) for v in groups.values()][:2]
se = sp * (1.0 / ns[0] + 1.0 / ns[1]) ** 0.5
tt = {1:12.71,2:4.303,3:3.182,4:2.776,5:2.571,6:2.447,8:2.306,10:2.228,
      12:2.179,14:2.145,18:2.101,22:2.074,30:2.042,60:2.000}
df = ns[0] + ns[1] - 2
t = tt.get(df) or tt[min(tt, key=lambda k: abs(k - df))]
if grand:
    print("  run-to-run sd (pooled, this experiment): %.4f  (%.3f%% of score)" % (sp, 100*sp/grand))
    print("  smallest difference it could have found: %.2f%%  (p<0.05, %d+%d rounds)"
          % (100*t*se/grand, ns[0], ns[1]))
    print("  below that, 'not significant' means 'under this threshold', not 'absent'.")
    for r in (8, 12):
        se_r = sp * (2.0 / r) ** 0.5
        t_r = tt.get(2*(r-1), 2.05)
        print("    with %2d rounds a side it would be %.2f%%" % (r, 100*t_r*se_r/grand))
MDEPY
}

emit "power"
while IFS= read -r line; do emit "$line"; done <<MDEEOF
$(mde_note 2>/dev/null)
MDEEOF
emit ""

# The comparison proper. Two arms only -- compare-results takes -a and -b, and
# a three-arm A/B/C is two comparisons rather than one, which the caller can ask
# for by naming the pairs.
set -- $labels
if [ $# -lt 2 ]; then
    emit "only one arm ('$1') -- nothing to compare. Its numbers are above."
    exit 0
fi
A="$1"; B="$2"
[ $# -gt 2 ] && emit "note: $# arms; comparing '$A' against '$B' only"

emit "comparing arm $A against arm $B"
emit ""
same=$(awk -F'\t' -v a="$A" -v b="$B" '
    $2==a {ia[$3]=1} $2==b {ib[$3]=1}
    END { for (k in ia) if (k in ib) { print "yes"; exit } }' "$RUNS")
if [ -n "$same" ]; then
    emit "  both arms ran the SAME staged build. This is an A/A control: what it"
    emit "  measures is the noise floor of this lane, not a difference between"
    emit "  builds. A significant result here means the lane is not yet quiet"
    emit "  enough to trust a real A/B at that magnitude."
    emit ""
fi

# `wk bench report` rather than a script of its own: it checks the three
# axes, computes the per-subtest Welch/FDR table and draws the histograms, so
# nothing here re-derives any of that. --out also writes html next to the
# text summary, same basename (docs/Urgent/"Benchmarking variance.md" asks
# for one command producing a report with no one in the room).
if [ -n "$OUT" ]; then
    "$WK_ROOT/cmd/bench" report "$(arm_paths "$A")" "$(arm_paths "$B")" \
        --html "${OUT%.*}.html" --text 2>&1 | tee -a "$OUT"
else
    "$WK_ROOT/cmd/bench" report "$(arm_paths "$A")" "$(arm_paths "$B")" 2>&1
fi
