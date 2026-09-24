at=$(awk -v p="$WK_PART" '$1 == p { print $2; exit }' /proc/mounts)
if [ -n "$at" ]; then cat "$at/$WK_NAME" 2>/dev/null; exit 0; fi
mkdir -p /mnt/wk-read
mount -o ro "$WK_PART" /mnt/wk-read 2>/dev/null || exit 0
cat "/mnt/wk-read/$WK_NAME" 2>/dev/null
umount /mnt/wk-read
