# Which profiler a machine can run, and getting it there.
#
# Two tools, because neither covers the fleet: samply publishes binaries for
# x86_64 and aarch64 only, and an armv7 userspace has to use whatever it ships.
# A caller asks profiler_resolve and gets one answer or a refusal.
#
# The arch to ask about is the *measured process's*, never `uname -m`: a lib32
# image runs a 64-bit kernel over a 32-bit userspace with no 64-bit loader.

SAMPLY_VER=0.13.1

samply_triple() { # <uname -m> -- the published release triple, or nothing
    case "$1" in
        x86_64)  printf 'x86_64-unknown-linux-gnu' ;;
        aarch64) printf 'aarch64-unknown-linux-gnu' ;;
    esac
}

samply_sha256() { # <triple>
    case "$1" in
        x86_64-unknown-linux-gnu)  printf '61875daad67888798690dea3cb2748279df6ac299c5c6a857d67eed7642473d9' ;;
        aarch64-unknown-linux-gnu) printf 'aa465162b62830168775b7ff4804bc35049436dcbc29bb3d1ea9f580380ea06a' ;;
    esac
}

samply_url() { # <triple>
    printf 'https://github.com/mstange/samply/releases/download/samply-v%s/samply-%s.tar.xz' "$SAMPLY_VER" "$1"
}

# Keyed by version and triple: a downloaded artifact, not a recomputable fact.
samply_store_dir() { printf '%s/cache/samply/%s-%s' "$WK_STORE" "$SAMPLY_VER" "$1"; }

samply_fetch() { # <uname -m> -- prints the binary's path on this host
    local machine="$1" triple sum dir tmp got
    triple=$(samply_triple "$machine") || true
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
    got=$(sha256sum "$tmp/samply.tar.xz" | awk '{print $1}')
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

# `profiler_resolve <uname -m> <sysprof-present: yes|no>` prints `<tool> <reason>`
# and returns 0, or prints the reason alone and returns 1. It decides; getting
# the tool to the machine is the caller's, because only the caller knows the
# channel.
profiler_resolve() { # <uname -m> <yes|no>
    local machine="$1" have_sysprof="${2:-no}"
    if [ -n "$(samply_triple "$machine")" ]; then
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
