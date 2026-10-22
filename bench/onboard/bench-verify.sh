p=$(pidof $WK_PROCESS | tr ' ' '\n' | head -1)
[ -n "$p" ] || { echo "pids=0"; exit 0; }
echo "pids=$(pidof $WK_PROCESS | wc -w)"
echo "exe=$(readlink /proc/$p/exe)"
want=$(ls -i $WK_LIB 2>/dev/null | awk '{print $1}')
echo "lib_inode=$want"
echo "mapped=$(awk -v ino="$want" '$5 == ino { print $6; exit }' /proc/$p/maps)"
echo "other_webkit=$(awk '$6 ~ /libWPEWebKit|libjavascriptcore|libwebkit2/ { print $6 }' /proc/$p/maps | sort -u | grep -v "^$WK_LIB$" | tr '\n' ' ')"
echo "lib_sha256=$(sha256sum $WK_LIB 2>/dev/null | awk '{print $1}')"
