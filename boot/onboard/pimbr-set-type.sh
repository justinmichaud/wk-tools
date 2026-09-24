printf "\\$WK_OCT" | dd of="$WK_DEV" bs=1 seek=450 count=1 conv=notrunc status=none && sync
