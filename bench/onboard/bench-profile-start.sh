n=0
while [ "$n" -lt 60 ]; do
    p=$(pidof $WK_PROCESS | tr ' ' '\n' | head -1)
    [ -n "$p" ] && break
    n=$((n + 1)); sleep 1
done
[ -n "$p" ] || { echo "no $WK_PROCESS appeared within 60s" >&2; exit 1; }
rm -f $WK_OUT $WK_OUT.log
case $WK_TOOL in
    samply)  $WK_DIR/samply record --save-only -o $WK_OUT -p "$p" </dev/null >$WK_OUT.log 2>&1 & ;;
    sysprof) sysprof-cli --force --pid "$p" $WK_OUT </dev/null >$WK_OUT.log 2>&1 & ;;
    *) echo "no such profiler: $WK_TOOL" >&2; exit 1 ;;
esac
echo $! > $WK_OUT.pid
sleep 3
kill -0 "$(cat $WK_OUT.pid)" 2>/dev/null || { echo "$WK_TOOL died immediately:" >&2; sed "s/^/  /" $WK_OUT.log >&2; exit 1; }
echo "attached=$WK_TOOL pid=$p"
