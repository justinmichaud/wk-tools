mkdir -p "$(dirname "$WK_RECORD")" && { cat; printf 'armed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"; } > "$WK_RECORD"
