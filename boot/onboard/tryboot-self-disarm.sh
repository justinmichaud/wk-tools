b=$(grep -m1 "^$WK_SD " /proc/mounts | cut -d" " -f2); o=; \
if [ -z "$b" ]; then b=$(mktemp -d); mount -t vfat "$WK_SD" "$b" || { rmdir "$b"; exit 0; }; o=1; fi; \
if [ -f "$b/tryboot.txt" ]; then rm -f "$b/tryboot.txt"; rm -rf "$b/second"; \
echo "wk-self-disarm: this boot spent the tryboot staging; the rescue boots next"; fi; \
if [ -n "$o" ]; then sync; umount "$b"; rmdir "$b"; fi
