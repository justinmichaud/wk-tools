#!/usr/bin/env bash
# The benchmark install's tailnet identity: build the daemon, stage it, join.

set -euo pipefail
WK_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"

# A Go toolchain, a module cache and a build cache: rebuildable artifacts keyed by
# content, so they belong in the artifact store beside ccache and yocto's sstate,
# not in the state directory -- which holds records, is expected to be small, and
# is walked file by file by whatever fingerprints it.
ts_artifacts() { printf '%s/cache/mac-tailnet' "$WK_STORE"; }

PIN="$WK_ROOT/bench/mac-tailnet-pin.inc"
REL="$WK_ROOT/image/yocto/meta-wk-tailnet/recipes-network/tailscale/tailscale-release.inc"

TS_BIN=/usr/local/bin
TS_STATE_DIR=/var/db/wk/tailscale
TS_SOCK=/var/run/tailscaled.socket
TS_KEY=/etc/wk/tailscale-authkey
TS_CONF=/etc/wk/tailnet.conf
DAEMON_LABEL=com.wk.tailscaled
JOIN_LABEL=com.wk.tailnet-join
LAUNCHD=/Library/LaunchDaemons
PAYLOAD_TOOLS=/usr/local/share/wk-bench/wk-tools
MACHINE="${WK_MAC_MACHINE:-mbp}"   # static; this lane runs on the one Mac

usage() {
    cat >&2 <<'EOF'
usage: mac-tailnet.sh build
       mac-tailnet.sh collect <dir>
       mac-tailnet.sh install <volume-root> <collected-dir> [privilege-prefix...]
       mac-tailnet.sh stage <volume-root> [privilege-prefix...]
       mac-tailnet.sh remember <volume-root> [privilege-prefix...]
       mac-tailnet.sh join

  build      fetch the pinned tailscale source and Go toolchain, verify both
             against bench/mac-tailnet-pin.inc, build tailscaled and tailscale
             for darwin/arm64, and print the directory holding them
  collect    gather everything a benchmark install needs into one directory --
             the binaries, this machine's auth key, the node name and tag, the
             LaunchDaemons and the remembered identity. Needs a network and
             credentials; needs no root
  install    lay a collected directory down on a volume root. Needs root; needs
             neither network nor credentials, so the benchmark install can run
             it against itself with its own passwordless sudo
  stage      collect and then install, for a caller standing on the host install
  remember   take a benchmark volume's tailnet node identity aside, so the next
             volume written rejoins as the same node rather than a new one
  join       (on the benchmark install, as root) start the daemon and join
EOF
    exit 1
}

pin_field() { sed -n "s/^$1 = \"\(.*\)\"\$/\1/p" "$PIN" | head -1; }
rel_field() { sed -n "s/^$1 = \"\(.*\)\"\$/\1/p" "$REL" | head -1; }

_sha256_check() { # <expected> <file>
    if command -v sha256sum >/dev/null 2>&1; then
        printf '%s  %s\n' "$1" "$2" | sha256sum -c - >/dev/null 2>&1
    elif command -v shasum >/dev/null 2>&1; then
        printf '%s  %s\n' "$1" "$2" | shasum -a 256 -c - >/dev/null 2>&1
    else
        die "no sha256sum and no shasum here, so neither the tailscale source nor
    the Go toolchain can be verified. Refusing to build a daemon out of
    unverified bytes and put it on a machine that measures."
    fi
}

_is_darwin_arm64() { # <file> -- Mach-O 64 magic and CPU_TYPE_ARM64, read as bytes
    python3 - "$1" <<'PY'
import struct, sys
try:
    with open(sys.argv[1], "rb") as fh:
        head = fh.read(8)
except OSError:
    raise SystemExit(1)
if len(head) < 8:
    raise SystemExit(1)
magic, cputype = struct.unpack("<II", head)
raise SystemExit(0 if magic == 0xfeedfacf and cputype == 0x0100000c else 1)
PY
}

ts_version() {
    local v s
    v=$(rel_field TS_VERSION); s=$(pin_field TS_SRC_FOR)
    [ -n "$v" ] || die "no TS_VERSION in $REL"
    [ -n "$s" ] || die "no TS_SRC_FOR in $PIN"
    [ "$v" = "$s" ] || die "$REL pins tailscale $v and $PIN carries the source
    checksum for $s. The Mac runs the same tailscale as every board, so these
    cannot differ.
    Remedy: set TS_SRC_FOR to $v in $PIN and replace TS_SRC_SHA256 with
      curl -fsSL https://proxy.golang.org/tailscale.com/@v/v$v.zip | sha256sum"
    printf '%s' "$v"
}

go_toolchain() { # prints the go binary, fetching and verifying it once
    local ver key sha host art tgz
    ver=$(pin_field GO_VERSION)
    [ -n "$ver" ] || die "no GO_VERSION in $PIN"
    host="$(uname -s | tr '[:upper:]' '[:lower:]')_$(uname -m)"
    case "$host" in
        darwin_arm64|linux_aarch64) host="${host%_aarch64}_arm64" ;;
    esac
    key="GO_SHA256_$host"
    sha=$(pin_field "$key")
    [ -n "$sha" ] || die "$PIN declares no $key, so this machine cannot build the
    darwin tailscaled the benchmark install needs.
    Remedy: add $key to $PIN with go.dev's published checksum for
      go$ver.${host%_*}-${host#*_}.tar.gz, or stage the volume from a host that has one."

    art=$(ts_artifacts)
    if [ -x "$art/go-$ver/go/bin/go" ]; then
        printf '%s' "$art/go-$ver/go/bin/go"; return 0
    fi
    ensure_dir "$art" 0755 >/dev/null
    tgz="$art/go$ver.${host%_*}-${host#*_}.tar.gz"
    if [ ! -f "$tgz" ] || ! _sha256_check "$sha" "$tgz"; then
        info "fetching the Go $ver toolchain (${host%_*}/${host#*_})"
        curl -fsSL -o "$tgz.part" "https://go.dev/dl/$(basename "$tgz")" \
            || die "could not fetch the Go toolchain"
        _sha256_check "$sha" "$tgz.part" \
            || { rm -f "$tgz.part"; die "$(basename "$tgz") does not match $key in $PIN.
    Refusing to build with an unverified toolchain."; }
        mv "$tgz.part" "$tgz"
    fi
    rm -rf "$art/go-$ver.part" "$art/go-$ver"
    mkdir -p "$art/go-$ver.part"
    tar xzf "$tgz" -C "$art/go-$ver.part" || die "could not unpack $tgz"
    mv "$art/go-$ver.part" "$art/go-$ver"
    printf '%s' "$art/go-$ver/go/bin/go"
}

ts_source() { # prints the extracted module directory
    local ver sha art zip src
    ver="$1"; sha=$(pin_field TS_SRC_SHA256)
    [ -n "$sha" ] || die "no TS_SRC_SHA256 in $PIN"
    art=$(ts_artifacts)
    src="$art/src-$ver/tailscale.com@v$ver"
    [ -f "$src/go.mod" ] && { printf '%s' "$src"; return 0; }

    ensure_dir "$art" 0755 >/dev/null
    zip="$art/tailscale-$ver.zip"
    if [ ! -f "$zip" ] || ! _sha256_check "$sha" "$zip"; then
        info "fetching the tailscale $ver source"
        curl -fsSL -o "$zip.part" "https://proxy.golang.org/tailscale.com/@v/v$ver.zip" \
            || die "could not fetch the tailscale source"
        _sha256_check "$sha" "$zip.part" \
            || { rm -f "$zip.part"; die "the tailscale $ver module zip does not match TS_SRC_SHA256.
    Refusing to build a daemon out of unverified bytes -- $PIN is what says
    which bytes are right."; }
        mv "$zip.part" "$zip"
    fi
    rm -rf "$art/src-$ver.part" "$art/src-$ver"
    python3 - "$zip" "$art/src-$ver.part" <<'PY'
import sys, zipfile
zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])
PY
    mv "$art/src-$ver.part" "$art/src-$ver"
    [ -f "$src/go.mod" ] || die "the tailscale module zip holds no $src/go.mod"
    printf '%s' "$src"
}

cmd_build() {
    local ver art out go src
    ver=$(ts_version)
    art=$(ts_artifacts)
    out="$art/darwin-arm64-$ver"
    if _is_darwin_arm64 "$out/tailscaled" && _is_darwin_arm64 "$out/tailscale"; then
        printf '%s\n' "$out"; return 0
    fi
    go=$(go_toolchain)
    src=$(ts_source "$ver")

    info "building tailscaled $ver for darwin/arm64"
    rm -rf "$out.part"; mkdir -p "$out.part"
    ( cd "$src"
      GOPATH="$art/gopath" GOMODCACHE="$art/gopath/pkg/mod" \
      GOCACHE="$art/gocache" GOTOOLCHAIN=local \
      CGO_ENABLED=0 GOOS=darwin GOARCH=arm64 \
      "$go" build -trimpath \
        -ldflags "-X tailscale.com/version.longStamp=$ver -X tailscale.com/version.shortStamp=$ver" \
        -o "$out.part/" ./cmd/tailscaled ./cmd/tailscale ) \
        || die "could not build tailscaled for darwin/arm64"

    _is_darwin_arm64 "$out.part/tailscaled" && _is_darwin_arm64 "$out.part/tailscale" \
        || die "the build produced something that is not a Mach-O arm64 executable.
    Refusing to stage it: a binary the benchmark install cannot run is a
    machine that comes back unreachable."
    chmod 0755 "$out.part/tailscaled" "$out.part/tailscale"
    rm -rf "$out"; mv "$out.part" "$out"
    info "  tailscaled $ver, darwin/arm64, $(du -sh "$out" | cut -f1)"
    printf '%s\n' "$out"
}

bench_node_name() {
    local n
    n=$( . "$WK_ROOT/boot/machines.sh" >/dev/null 2>&1
         machine_load "$MACHINE" >/dev/null 2>&1
         printf '%s' "${NODE_BENCH_SSH:-}" )
    [ -n "$n" ] || die "boot/machines/$MACHINE.conf declares no NODE_BENCH_SSH,
    so there is no name for the benchmark install to join the tailnet under.
    Every phase of this lane reaches it by that name; a node that joins under
    another one is a machine nothing here can find."
    printf '%s' "$n"
}

remembered_state() { printf '%s' "$(wk_state_dir)/mac-tailnet/$(bench_node_name).state"; }

# /private/etc and /private/var, never /etc and /var: those are symlinks on a macOS volume, and a package payload under a symlink is a payload the installer will not lay down.
volume_state() { printf '%s' "${1:-}/private$TS_STATE_DIR/tailscaled.state"; }

daemon_plist() { # <state file>
    cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>              <string>$DAEMON_LABEL</string>
  <key>ProgramArguments</key>   <array>
      <string>$TS_BIN/tailscaled</string>
      <string>--state=$1</string>
      <string>--tun=utun</string>
  </array>
  <key>RunAtLoad</key>          <true/>
  <key>KeepAlive</key>          <true/>
  <key>StandardOutPath</key>    <string>/var/log/wk-tailscaled.log</string>
  <key>StandardErrorPath</key>  <string>/var/log/wk-tailscaled.log</string>
</dict>
</plist>
PLIST
}

join_plist() {
    cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>              <string>$JOIN_LABEL</string>
  <key>ProgramArguments</key>   <array>
      <string>/bin/bash</string>
      <string>$PAYLOAD_TOOLS/bench/mac-tailnet.sh</string>
      <string>join</string>
  </array>
  <key>RunAtLoad</key>          <true/>
  <key>StandardOutPath</key>    <string>/var/log/wk-tailnet-join.log</string>
  <key>StandardErrorPath</key>  <string>/var/log/wk-tailnet-join.log</string>
</dict>
</plist>
PLIST
}

# Split where the privilege and the network split: `collect` needs a Go toolchain, a route to the module proxy, this machine's auth key and its remembered node identity, and no root; `install` needs root and nothing else. That is what lets the benchmark install stage itself -- it has passwordless root and neither network nor credentials -- and `stage` is still the two of them for a caller standing on the host install.
cmd_collect() {
    local dir="${1:-}"
    [ -n "$dir" ] || die "usage: mac-tailnet.sh collect <dir>"
    local name keyfile out kept
    name=$(bench_node_name)
    keyfile=$(wk_tailscale_authkey) \
        || die "there is no tailnet auth key on this machine, so the benchmark
    install staged here would come up with no tailnet identity -- unobservable
    from the moment it reboots until it powers itself off.
    Set one first:  wk key set tailnet"
    out=$(cmd_build)

    rm -rf "$dir.part"; mkdir -p "$dir.part" || die "could not make $dir.part"
    install -m 0755 "$out/tailscaled" "$out/tailscale" "$dir.part/"
    install -m 0600 "$keyfile" "$dir.part/authkey"
    printf 'hostname=%s\ntag=%s\n' "$name" "${WK_TAILNET_TAG:-tag:wk}" > "$dir.part/tailnet.conf"
    daemon_plist "$TS_STATE_DIR/tailscaled.state" > "$dir.part/$DAEMON_LABEL.plist"
    join_plist > "$dir.part/$JOIN_LABEL.plist"
    kept=$(remembered_state)
    if [ -s "$kept" ]; then
        install -m 0600 "$kept" "$dir.part/tailscaled.state"
        info "  tailnet: '$name' will rejoin as the node this machine remembers"
    else
        info "  tailnet: '$name' will join fresh; its identity is kept from then on"
    fi
    rm -rf "$dir"; mv "$dir.part" "$dir"
    printf '%s\n' "$dir"
}

cmd_install() {
    local root="${1:-}"; root="${root%/}"; shift || true
    local dir="${1:-}"; shift || true
    [ -d "$dir" ] || die "usage: mac-tailnet.sh install <volume-root> <collected-dir> [privilege-prefix...]"
    for f in tailscaled tailscale authkey tailnet.conf "$DAEMON_LABEL.plist" "$JOIN_LABEL.plist"; do
        [ -f "$dir/$f" ] || die "$dir carries no $f, so it is not a collected tailnet payload"
    done
    _is_darwin_arm64 "$dir/tailscaled" && _is_darwin_arm64 "$dir/tailscale" \
        || die "$dir holds something that is not a Mach-O arm64 executable.
    Refusing to install it: a binary the benchmark install cannot run is a
    machine that comes back unreachable."

    "$@" install -d -m 0755 "$root$TS_BIN" "$root$LAUNCHD"
    "$@" install -m 0755 "$dir/tailscaled" "$dir/tailscale" "$root$TS_BIN/"
    # BSD install -d applies -m to every directory it creates, so the parents are made at 0755 first: a package payload carrying /private/var/db at 0700 breaks the install it lands on.
    "$@" install -d -m 0755 "$root/private$(dirname "$TS_STATE_DIR")" "$root/private/etc/wk"
    "$@" install -d -m 0700 "$root/private$TS_STATE_DIR"
    "$@" install -m 0600 "$dir/authkey" "$root/private$TS_KEY"
    "$@" install -m 0644 "$dir/tailnet.conf" "$root/private$TS_CONF"
    "$@" install -m 0644 "$dir/$DAEMON_LABEL.plist" "$dir/$JOIN_LABEL.plist" "$root$LAUNCHD/"
    if [ -s "$dir/tailscaled.state" ]; then
        "$@" install -m 0600 "$dir/tailscaled.state" "$(volume_state "$root")"
    fi
    info "  tailnet: tailscaled installed on '${root:-/}'"
}

cmd_stage() {
    local root="${1:-}"; root="${root%/}"; shift || true
    [ -n "$root" ] && [ -d "$root" ] \
        || die "usage: mac-tailnet.sh stage <volume-root> [privilege-prefix...]"
    local dir; dir="$(ts_artifacts)/collected"
    dir=$(cmd_collect "$dir" | tail -1) || return 1
    cmd_install "$root" "$dir" "$@"
}

cmd_remember() {
    local root="${1:-}"; root="${root%/}"; shift || true
    [ -n "$root" ] && [ -d "$root" ] \
        || die "usage: mac-tailnet.sh remember <volume-root> [privilege-prefix...]"
    local state kept
    state=$(volume_state "$root")
    "$@" test -s "$state" || { info "  tailnet: '$root' holds no node identity to keep"; return 0; }
    kept=$(remembered_state)
    ensure_dir "$(dirname "$kept")" 0700 >/dev/null
    "$@" cat "$state" > "$kept.part" || die "could not read '$root's tailnet node identity"
    chmod 0600 "$kept.part"; mv "$kept.part" "$kept"
    info "  tailnet: kept '$(bench_node_name)' aside; the next volume rejoins as it"
}

cmd_join() {
    [ "$(id -u)" = 0 ] || die "tailscaled needs root to open a utun, so this needs root too"
    [ -x "$TS_BIN/tailscaled" ] || die "no $TS_BIN/tailscaled on this install: it was never staged.
    Remedy, from the host install:  wk boot mbp --repair"

    launchctl print "system/$DAEMON_LABEL" >/dev/null 2>&1 \
        || launchctl bootstrap system "$LAUNCHD/$DAEMON_LABEL.plist" \
        || die "launchd refused $LAUNCHD/$DAEMON_LABEL.plist, so there is no daemon to join with"

    local i=0
    while [ "$i" -lt 30 ] && [ ! -S "$TS_SOCK" ]; do i=$((i + 1)); sleep 1; done
    [ -S "$TS_SOCK" ] || die "tailscaled did not open $TS_SOCK within 30s, so there is nothing
    to join with. Its output is in /var/log/wk-tailscaled.log."

    local ip
    if ip=$("$TS_BIN/tailscale" ip -4 2>/dev/null | head -1) && [ -n "$ip" ]; then
        info "already on the tailnet as $ip"
        rm -f "$TS_KEY"
        return 0
    fi

    [ -r "$TS_KEY" ] || die "no auth key at $TS_KEY and no tailnet identity in the state file,
    so this install cannot join and cannot be watched while it measures.
    Remedy, from the host install:  wk boot mbp --repair"

    local name tag
    name=$(sed -n 's/^hostname=//p' "$TS_CONF" | head -1)
    tag=$(sed -n 's/^tag=//p' "$TS_CONF" | head -1)
    [ -n "$name" ] && [ -n "$tag" ] \
        || die "$TS_CONF names no hostname and tag, so this install does not know
    which node it is. Remedy, from the host install:  wk boot mbp --repair"

    info "joining the tailnet as $name ($tag)"
    # --timeout, because without one `up` waits for the backend to reach Running for as long as that takes, and the caller is an unattended boot with a benchmark to run: an install whose Wi-Fi did not come up would sit here rather than measure. Failing is fine -- the key is kept and the run goes on unobserved.
    "$TS_BIN/tailscale" up --timeout=90s --auth-key "file:$TS_KEY" --advertise-tags="$tag" \
        --hostname="$name" --accept-dns=false \
        || die "tailscale up did not reach Running inside 90s. The key is kept so the
    next boot retries; a key that is single-use, untagged or expired fails exactly
    here, and so does an install with no route out."
    ip=$("$TS_BIN/tailscale" ip -4 2>/dev/null | head -1) || ip=""
    [ -n "$ip" ] || die "tailscale up returned success and this node still has no address.
    The key is kept so the next boot retries."
    info "up as $name at $ip"
    rm -f "$TS_KEY"
}

case "${1:-}" in
    build)    cmd_build ;;
    collect)  shift; cmd_collect "$@" ;;
    install)  shift; cmd_install "$@" ;;
    stage)    shift; cmd_stage "$@" ;;
    remember) shift; cmd_remember "$@" ;;
    join)     cmd_join ;;
    *)        usage ;;
esac
