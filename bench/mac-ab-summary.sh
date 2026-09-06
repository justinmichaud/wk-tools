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

plans=$(awk -F'\t' '{print ($6 == "" ? "unnamed" : $6)}' "$RUNS" | awk '!seen[$0]++')

arm_paths() {  # $1 = plan, $2 = label -> comma-separated run directories
    awk -F'\t' -v p="$1" -v l="$2" '($6 == p || ($6 == "" && p == "unnamed")) && $2==l {printf "%s%s/results/%s", sep, r, $4; sep=","}' \
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

set -- $labels
if [ $# -lt 2 ]; then
    emit "only one arm ('$1') -- nothing to compare. Its runs are listed above."
    exit 0
fi
A="$1"; B="$2"   # compare-results takes -a and -b, so a third arm is a second comparison
[ $# -gt 2 ] && emit "note: $# arms; comparing '$A' against '$B' only"

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

for plan in $plans; do
    emit "================ $plan ================"
    for l in $labels; do
        emit "  arm $l: $(arm_paths "$plan" "$l" | tr ',' '\n' | grep -c .) run(s)"
    done

    emit "  precision:"   # the statistic the run stopped on, so the two cannot disagree
    while IFS= read -r line; do emit "    $line"; done <<PRECEOF
$("$PY" "$WK_ROOT/lib/wkdata.py" ab-precision \
      --a "$(arm_paths "$plan" "$A" | sed 's#\([^,]*\)#\1/result.json#g')" \
      --b "$(arm_paths "$plan" "$B" | sed 's#\([^,]*\)#\1/result.json#g')" 2>&1)
PRECEOF
    emit "    mde_pct is the smallest difference these rounds resolve; below it,"
    emit "    'not significant' means 'under this threshold', not 'absent'."
    emit ""

    emit "  comparing arm $A against arm $B"
    if [ -n "$OUT" ]; then
        "$WK_ROOT/cmd/bench" report "$(arm_paths "$plan" "$A")" "$(arm_paths "$plan" "$B")" \
            --html "${OUT%.*}-$plan.html" --text 2>&1 | tee -a "$OUT"
    else
        "$WK_ROOT/cmd/bench" report "$(arm_paths "$plan" "$A")" "$(arm_paths "$plan" "$B")" 2>&1
    fi
    emit ""
done
