#!/usr/bin/env bash
# wk bench ab-summary -- turn a run of `wk bench mac-ab` into a verdict.
#
#   wk bench ab-summary --runs <runs.tsv> [--root <dir>] [--out <file>]
#                                          (--out also writes <file-without-

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
        -h|--help) usage_block "$0" >&2; exit 2 ;;
        *) die "unknown option: $1" ;;
    esac
done

[ -n "$RUNS" ] || die "which run map? --runs <runs.tsv>"
[ -f "$RUNS" ] || die "no run map at $RUNS -- the A/B recorded nothing"

PY=/usr/bin/python3
[ -x "$PY" ] || PY=python3

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

for l in $labels; do
    emit "arm $l: $(arm_paths "$l" | tr ',' '\n' | grep -c .) run(s)"
done
emit ""

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

set -- $labels
if [ $# -lt 2 ]; then
    emit "only one arm ('$1') -- nothing to compare. Its numbers are above."
    exit 0
fi
A="$1"; B="$2"   # compare-results takes -a and -b, so a third arm is a second comparison
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

if [ -n "$OUT" ]; then
    "$WK_ROOT/cmd/bench" report "$(arm_paths "$A")" "$(arm_paths "$B")" \
        --html "${OUT%.*}.html" --text 2>&1 | tee -a "$OUT"
else
    "$WK_ROOT/cmd/bench" report "$(arm_paths "$A")" "$(arm_paths "$B")" 2>&1
fi
