# Sourced, never run: the files a benchmark install needs and the one writer that lays them down, because its two callers have different privilege and different reach -- mac-bench-volume.sh with sudo over a mounted volume, mac-bench-autorun.sh with passwordless root over itself -- and a payload file must not land one way and not the other. The caller provides `run` and WK_ROOT. Not here: the tailnet payload, whose collection needs a network this install has not got.

bench_payload_files() {   # <source, repo-relative> <dest under the root> <mode>
    cat <<'ROWS'
bench/mac-bench-firstboot.sh usr/local/libexec/wk-bench-firstboot.sh 0755
bench/mac-quiet-hosts.sh     usr/local/libexec/wk-bench-quiet-hosts.sh 0644
bench/mac-quiet-desktop.sh   usr/local/libexec/wk-bench-quiet-desktop.sh 0644
bench/mac-pyobjc.sh          usr/local/libexec/wk-bench-pyobjc.sh 0644
ROWS
}

stage_payload() {   # <root> [privilege prefix...]
    local root="$1"; shift
    local src dest mode
    run "$@" install -d -m 0755 "$root/usr/local/libexec" "$root/usr/local/share/wk-bench"
    while read -r src dest mode; do
        [ -n "$src" ] || continue
        run "$@" install -m "$mode" "$WK_ROOT/$src" "$root/$dest"
    done <<ROWS
$(bench_payload_files)
ROWS

    if [ -f "$HOME/.ssh/authorized_keys" ]; then
        run "$@" install -m 0644 "$HOME/.ssh/authorized_keys" \
            "$root/usr/local/share/wk-bench/authorized_keys"
        log "  authorized_keys: $(grep -c . "$HOME/.ssh/authorized_keys" 2>/dev/null || echo 0) key(s) from this install"
    else
        warn "  no ~/.ssh/authorized_keys here, so the bench install will have none"
        warn "  -- it will boot, and nothing will be able to drive it"
    fi

    run "$@" rsync -a --chmod=go-w --delete --exclude '.git/' --exclude '__pycache__/' --exclude '*.pyc' \
        "$WK_ROOT/" "$root/usr/local/share/wk-bench/wk-tools/" \
        || die "could not stage wk-tools onto '$root'"
}
