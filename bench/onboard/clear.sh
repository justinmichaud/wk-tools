# Foreign: a process another ssh login started (its SSH_CONNECTION names that login's address and port), or a browser.
WK_PROC=${WK_PROC:-/proc}
[ -n "$SSH_CONNECTION" ] || { echo "not run over ssh, so no login can be told from this one" >&2; exit 2; }
b="cog MiniBrowser WPEWebProcess WPENetworkProcess"
[ -x /etc/init.d/S90cog ] && /etc/init.d/S90cog stop >/dev/null 2>&1
command -v systemctl >/dev/null 2>&1 && systemctl stop netdata 2>/dev/null
foreign() {
  for e in "$WK_PROC"/[0-9]*/environ; do
    c=$(tr '\0' '\n' < "$e" 2>/dev/null | sed -n 's/^SSH_CONNECTION=//p')
    [ -n "$c" ] && [ "$c" != "$SSH_CONNECTION" ] && { e=${e%/environ}; echo "${e##*/}"; }
  done
}
f=$(foreign)
[ -z "$f" ] || { echo "stopping foreign: $(for p in $f; do cat "$WK_PROC/$p/comm" 2>/dev/null; done | tr '\n' ' ')"; kill $f 2>/dev/null; }
killall $b 2>/dev/null
n=0
while [ "$n" -lt 5 ] && { pidof $b >/dev/null 2>&1 || [ -n "$(foreign)" ]; }; do sleep 1; n=$((n+1)); done
f=$(foreign)
pidof $b >/dev/null 2>&1 || [ -n "$f" ] && { kill -9 $f 2>/dev/null; killall -9 $b 2>/dev/null; sleep 1; }
! pidof $b >/dev/null 2>&1 && [ -z "$(foreign)" ]
