r=$(sed -n "/root=PARTUUID=/{s/.*root=PARTUUID=//;s/ .*//;p;}" /proc/cmdline); \
[ -n "$r" ] || { echo "wk-self-disarm: root is not named by PARTUUID; cannot find the boot partition"; exit 0; }; \
d=$(readlink -f /dev/disk/by-partuuid/$r); b=$(echo "$d" | sed "s/p[0-9]*$/p1/"); \
m=$(mktemp -d); if mount -t vfat "$b" "$m"; then \
[ -f "$m/config.txt.rescue" ] && mv -f "$m/config.txt.rescue" "$m/config.txt" && echo "wk-self-disarm: this boot was the one; the rescue boots next"; \
sync; umount "$m"; fi; rmdir "$m"
