r=${WK_RECORD:-${XDG_STATE_HOME:-$HOME/.local/state}/wk/boot-armed}
case "$WK_DO" in
    read) cat "$r" 2>/dev/null || true ;;
    write) mkdir -p "$(dirname "$r")" && { cat; printf 'armed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"; } > "$r" ;;
    clear) rm -f "$r" ;;
esac
