while read -r id parent mm root mp rest; do [ "$mp" = / ] && break; done < /proc/self/mountinfo; \
p=$(readlink -f /sys/dev/block/$mm) && \
d=/dev/$(basename "$(dirname "$p")") && \
printf "\203" | dd of="$d" bs=1 seek=450 count=1 conv=notrunc status=none && sync
