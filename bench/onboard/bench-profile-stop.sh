[ -f $WK_OUT.pid ] || { echo "nothing was started for $WK_OUT" >&2; exit 1; }
p=$(cat $WK_OUT.pid)
kill -INT "$p" 2>/dev/null || true
n=0
while [ "$n" -lt 60 ] && kill -0 "$p" 2>/dev/null; do n=$((n + 1)); sleep 1; done
kill -9 "$p" 2>/dev/null || true
rm -f $WK_OUT.pid
[ -s $WK_OUT ] || { echo "no capture at $WK_OUT:" >&2; sed "s/^/  /" $WK_OUT.log >&2; exit 1; }
echo "capture=$WK_OUT bytes=$(wc -c < $WK_OUT | tr -d " ")"
