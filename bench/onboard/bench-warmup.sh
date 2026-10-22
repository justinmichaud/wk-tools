p=$(pidof $WK_PROCESS | tr ' ' '\n' | head -1)
[ -n "$p" ] || { echo "pid=0"; exit 0; }
echo "pid=$p"
echo "exe=$(readlink /proc/$p/exe)"
echo "elf=$(od -An -tu1 -N20 /proc/$p/exe | tr -s ' ' | tr -d '\n')"
echo "dri=$(awk '$6 ~ /_dri\.so$/ { print $6 }' /proc/$p/maps | sort -u | tr '\n' ' ')"
echo "gl=$(awk '$6 ~ /lib(EGL|GLESv2|gbm)\./ { print $6 }' /proc/$p/maps | sort -u | tr '\n' ' ')"
for f in /proc/$p/fd/*; do
    t=$(readlink "$f" 2>/dev/null)
    case "$t" in /dev/dri/*) echo "drifd=$t" ;; esac
done
awk '$2 ~ /x/ && $5 == "0" && NF == 5 { print "execmap=" $1 }' /proc/$p/maps
