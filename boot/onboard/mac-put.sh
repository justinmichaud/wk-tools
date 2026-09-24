case "$WK_DO" in (tree) rm -rf "$WK_DEST" && mkdir -p "$WK_DEST" && tar -xf "$WK_TMP" -C "$WK_DEST" ;; (file) mv -f "$WK_TMP" "$WK_DEST" ;; esac
rc=$?; rm -f "$WK_TMP"; exit "$rc"
