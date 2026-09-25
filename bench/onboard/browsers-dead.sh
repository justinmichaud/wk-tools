b="cog MiniBrowser WPEWebProcess WPENetworkProcess"
[ -x /etc/init.d/S90cog ] && /etc/init.d/S90cog stop >/dev/null 2>&1
killall $b 2>/dev/null
n=0
while [ "$n" -lt 5 ] && pidof $b >/dev/null 2>&1; do sleep 1; n=$((n+1)); done
if pidof $b >/dev/null 2>&1; then killall -9 $b 2>/dev/null; sleep 1; fi
! pidof $b >/dev/null 2>&1
