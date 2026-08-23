#!/usr/bin/env bash
#
# wk bench ab-summary -- turn a run of `wk bench mac-ab` into a verdict.
#
#   wk bench ab-summary --runs <runs.tsv> [--root <dir>] [--out <file>]
#
# Reads the arm-to-result map the autorun wrote (round, label, staged id,
# result directory), prints each arm's score, and hands the two arms to
# `wk bench compare` -- which is where the axis warnings and the p-value come
# from, and which is deliberately not reimplemented here.
#
# Runs on the Mac in *either* mode: in bench mode it is the last thing the
# autorun does, and in host mode it is how the same verdict is re-read after
# the machine has come back. The only difference is the path to the results,
# which is why --root exists.
#
# Two arms with the same staged id is not a mistake -- it is the control. An A/A
# measures the lane's own noise, and there is no honest way to read an A/B
# without it: a 2% difference means nothing until you know whether the same
# build twice differs by 3%.

set -uo pipefail
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

arm_paths() {  # $1 = label -> comma-separated result.json paths
    awk -F'\t' -v l="$1" '$2==l {printf "%s%s/results/%s/result.json", sep, r, $4; sep=","}' \
        r="$ROOT" "$RUNS"
}

emit() {
    printf '%s\n' "$*"
    [ -n "$OUT" ] && printf '%s\n' "$*" >> "$OUT"
    return 0
}

[ -n "$OUT" ] && : > "$OUT"

emit "A/B summary -- $(date -u +%Y-%m-%dT%H:%M:%SZ)"
emit "run map: $RUNS"
emit ""

# Per-arm numbers, read out of the results themselves rather than out of the run
# log: the log is a transcript and the result is the record.
#
# One python per arm, handed every path at once. The first version put a heredoc
# inside a command substitution inside a `while read` fed by a pipe -- three
# things competing for one stdin -- and printed nothing at all for either arm
# while the comparison below worked perfectly. A summary that silently omits its
# own numbers is worse than one that fails.
arm_numbers() {  # $1 = comma-separated result.json paths
    printf '%s' "$1" | tr ',' '\n' | "$PY" -c '
import json, os, sys

def find(o, key):
    # metrics.<Score|Time>.current, wherever the suite puts it. Speedometer
    # nests it under the suite name; other plans nest it differently, and the
    # shape is the stable part rather than the path.
    if isinstance(o, dict):
        m = o.get("metrics")
        if isinstance(m, dict) and key in m:
            cur = (m[key] or {}).get("current")
            if cur is not None:
                return cur
        for v in o.values():
            r = find(v, key)
            if r is not None:
                return r
    return None

def flat(x, acc):
    if isinstance(x, list):
        for i in x:
            flat(i, acc)
    elif isinstance(x, (int, float)):
        acc.append(float(x))
    return acc

for line in sys.stdin:
    p = line.strip()
    if not p:
        continue
    name = os.path.basename(os.path.dirname(p))
    try:
        d = json.load(open(p))
    except Exception as e:
        print("  %-44s unreadable (%s)" % (name, e))
        continue
    for key in ("Score", "Time"):
        cur = find(d, key)
        if cur is None:
            continue
        vals = flat(cur, [])
        if not vals:
            continue
        n = len(vals)
        mean = sum(vals) / n
        sd = (sum((v - mean) ** 2 for v in vals) / (n - 1)) ** 0.5 if n > 1 else 0.0
        print("  %-44s %-5s %8.3f  sd %6.3f (%4.1f%%)  n=%d"
              % (name, key, mean, sd, (100.0 * sd / mean) if mean else 0.0, n))
        break
    else:
        print("  %-44s no Score or Time metric" % name)
'
}

for l in $labels; do
    emit "arm $l"
    while IFS= read -r line; do emit "$line"; done <<EOF
$(arm_numbers "$(arm_paths "$l")")
EOF
    emit ""
done

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

# `wk bench compare` rather than compare-results directly: it is the thing that
# checks the three axes and warns when two runs are not comparable, and that
# check is the point of having it.
if [ -n "$OUT" ]; then
    "$WK_ROOT/cmd/bench" compare "$(arm_paths "$A")" "$(arm_paths "$B")" 2>&1 | tee -a "$OUT"
else
    "$WK_ROOT/cmd/bench" compare "$(arm_paths "$A")" "$(arm_paths "$B")" 2>&1
fi
