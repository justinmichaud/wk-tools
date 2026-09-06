# samply publishes x86_64 and aarch64 only; an armv7 userspace uses whatever the
# image ships. Ask about the measured process's arch, never `uname -m`: a lib32
# image runs a 64-bit kernel over a 32-bit userspace with no 64-bit loader.

command -v warn >/dev/null 2>&1 || . "$(dirname "${BASH_SOURCE[0]}")/common.sh"
SAMPLY_VER=0.13.1

samply_triple() { # <uname -m> [uname -s, default Linux] -- the published release triple, or nothing. The OS is the *measured* machine's, never this one's: a Mac driving a board asks about the board.
    case "${2:-Linux}:$1" in
        Darwin:arm64)  printf 'aarch64-apple-darwin' ;;
        Darwin:x86_64) printf 'x86_64-apple-darwin' ;;
        *:x86_64)      printf 'x86_64-unknown-linux-gnu' ;;
        *:aarch64)     printf 'aarch64-unknown-linux-gnu' ;;
    esac
}

samply_sha256() { # <triple>
    case "$1" in
        x86_64-unknown-linux-gnu)  printf '61875daad67888798690dea3cb2748279df6ac299c5c6a857d67eed7642473d9' ;;
        aarch64-unknown-linux-gnu) printf 'aa465162b62830168775b7ff4804bc35049436dcbc29bb3d1ea9f580380ea06a' ;;
        aarch64-apple-darwin)      printf '7597239aa3769e75058be5ed359dbfe067f5e7714a5f052c45dd81d509aec17f' ;;
        x86_64-apple-darwin)       printf 'a57f05f9162d06c36df6d51425d976ac774a8cd6dbd84050e6303fa8bf813998' ;;
    esac
}

samply_url() { # <triple>
    printf 'https://github.com/mstange/samply/releases/download/samply-v%s/samply-%s.tar.xz' "$SAMPLY_VER" "$1"
}

_samply_sha256_of() {   # macOS ships shasum, Linux ships sha256sum, and this fetch runs on both
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

samply_store_dir() { printf '%s/samply/%s-%s' "$(wk_artifact_dir)" "$SAMPLY_VER" "$1"; }

samply_fetch() { # <uname -m> [uname -s] -- prints the binary's path on this host
    local machine="$1" triple sum dir tmp got
    triple=$(samply_triple "$machine" "${2:-Linux}") || true
    [ -n "$triple" ] || return 1
    dir=$(samply_store_dir "$triple")
    if [ -x "$dir/samply" ]; then printf '%s/samply' "$dir"; return 0; fi

    sum=$(samply_sha256 "$triple")
    tmp=$(mktemp -d) || return 1
    if ! curl -fsSL -o "$tmp/samply.tar.xz" "$(samply_url "$triple")"; then
        rm -rf "$tmp"
        warn "samply $SAMPLY_VER download failed for $triple -- check egress"
        return 1
    fi
    got=$(_samply_sha256_of "$tmp/samply.tar.xz")
    if [ "$got" != "$sum" ]; then
        rm -rf "$tmp"
        warn "samply $SAMPLY_VER for $triple did not verify (sha256 $got, expected $sum)"
        return 1
    fi
    if ! tar -xJf "$tmp/samply.tar.xz" -C "$tmp"; then
        rm -rf "$tmp"; warn "samply $SAMPLY_VER for $triple would not unpack"; return 1
    fi
    ensure_dir "$dir" >/dev/null
    install -m 0755 "$tmp/samply-$triple/samply" "$dir/samply" || { rm -rf "$tmp"; return 1; }
    rm -rf "$tmp"
    printf '%s/samply' "$dir"
}

profiler_resolve() { # <uname -m> <yes|no> [uname -s]
    local machine="$1" have_sysprof="${2:-no}" os="${3:-Linux}"
    if [ -n "$(samply_triple "$machine" "$os")" ]; then
        printf 'samply upstream publishes samply %s for %s\n' "$SAMPLY_VER" "$machine"
        return 0
    fi
    if [ "$have_sysprof" = yes ]; then
        printf 'sysprof no samply release for %s; the image ships sysprof-cli\n' "$machine"
        return 0
    fi
    printf 'neither samply nor sysprof can profile a %s userspace: upstream publishes
    no samply binary for it (x86_64 and aarch64 only), and the image has no
    sysprof-cli. Add sysprof-cli to the image, or build samply %s for it.\n' \
        "$machine" "$SAMPLY_VER"
    return 1
}
