# The pmos builder: a postmarketOS system for a phone, built over ssh because pmbootstrap is Linux-only and needs
# root (loop devices, chroots, kpartx). The host must be aarch64, as both phones are, so qemu never starts.

pmos_host() {  # PMO_BUILD_HOST (image/profiles.sh), or WK_PMOS_HOST
    [ -n "${PMO_BUILD_HOST:-}" ] || die "this pmos profile sets no PMO_BUILD_HOST (image/profiles.sh)"
    echo "${WK_PMOS_HOST:-$PMO_BUILD_HOST}"
}
pmos_root()      { echo "${WK_PMOS_ROOT:-\$HOME/wk-pmos}"; }
pmos_out()       { echo "$(pmos_root)/out/$1"; }
pmos_log()       { echo "$(pmos_root)/out/$1/build.log"; }
pmos_rc()        { echo "$(pmos_root)/out/$1/build.rc"; }

pmos_ssh() {  # a fleet machine's ssh destination when the name is one, else the name itself
    local h; h=$(pmos_host)
    if machine_load "$h" 2>/dev/null && [ -n "${NODE_SSH:-}" ]; then
        printf '%s' "$NODE_SSH"
    else
        printf '%s' "$h"
    fi
}

# Keepalives: the build host roams on WiFi and ConnectTimeout covers only the handshake. machine_load runs here so NODE_ROLE is set before `m_ssh_opts` reads it.
pmos_ssh_opts() {
    machine_load "$(pmos_host)" >/dev/null 2>&1
    printf '%s' "-o BatchMode=yes -o ConnectTimeout=$(wk_ssh_timeout) $(m_ssh_opts) \
-o ServerAliveInterval=15 -o ServerAliveCountMax=4"
}

_pmos_sh() {
    # shellcheck disable=SC2046
    ssh $(pmos_ssh_opts) "$(pmos_ssh)" "$@"
}

# pipefail would take ssh's non-zero status and set -e kill the caller silently, so the remote side is made to succeed: an empty answer is not an error.
_pmos_ask() {
    _pmos_sh "$* 2>/dev/null || true" 2>/dev/null | tr -d '\r' || true
}

# Under ~/wk-pmos on the build host: work/ is ~8 GB of chroots and apk cache (`wk gc --purge-pmos`), out/ half a gigabyte per build (`wk gc`).

pmos_build_hosts() {  # a subshell per profile: image_profile_load sets globals no caller should keep
    local p
    for p in $(image_profile_list | awk '/^[a-z0-9-]+$/ { print $1 }'); do
        ( image_profile_load "$p" >/dev/null 2>&1 || exit 0
          [ "${IMG_BUILDER:-}" = pmos ] || exit 0
          pmos_host )
    done | sed '/^$/d' | sort -u
}

pmos_cache_probe() { # <host> -> kb<TAB>label<TAB>note; '??' when unreachable, since "0" would be a measurement
    local out
    out=$(WK_PMOS_HOST="$1" _pmos_ask "du -sk \$HOME/wk-pmos/work \$HOME/wk-pmos/out")
    if [ -z "$out" ]; then
        if WK_PMOS_HOST="$1" _pmos_sh true >/dev/null 2>&1; then return 0; fi
        printf '%s\t%s\t%s\n' '??' "$1" "did not answer; nothing measured"
        return 0
    fi
    printf '%s\n' "$out" | while read -r kb path; do
        [ -n "${kb:-}" ] || continue
        case "$path" in
            */work) printf '%s\t%s\t%s\n' "$kb" "$1: pmbootstrap chroots" \
                        "rebuildable; wk gc --purge-pmos" ;;
            */out)  printf '%s\t%s\t%s\n' "$kb" "$1: finished and in-flight builds" \
                        "wk gc keeps the newest per profile" ;;
        esac
    done
}

pmos_purge_work() { # <host>
    local h="$1" kb
    kb=$(WK_PMOS_HOST="$h" _pmos_ask "du -sk \$HOME/wk-pmos/work | cut -f1" | tr -d ' \n')
    case "$kb" in ''|*[!0-9]*) kb=0 ;; esac
    [ "$kb" -gt 0 ] || { debug "no pmbootstrap work folder on $h"; return 0; }

    # Unmount first, or the chroots' bind mounts follow the rm into the host's own /proc and /dev -- and pmbootstrap's shutdown is the only thing that knows what it mounted.
    log "  $h: $(human_bytes $((kb * 1024))) of pmbootstrap chroots and package cache"
    confirm "erase it on $h? the next build there refetches (minutes, not hours)" \
        || { info "kept the chroots on $h"; return 0; }
    WK_PMOS_HOST="$h" _pmos_sh "for c in \$HOME/wk-pmos/*.cfg; do
            [ -f \"\$c\" ] || continue
            \$HOME/wk-pmos/venv/bin/pmbootstrap -c \"\$c\" -y shutdown >/dev/null 2>&1 || true
        done
        rm -rf \$HOME/wk-pmos/work" \
        || { warn "could not erase the work folder on $h"; return 0; }
    changed "erased $(human_bytes $((kb * 1024))) of pmbootstrap chroots on $h"
}

pmos_dry_run() {
    local id="$1" host; host=$(pmos_host)
    cat >&2 <<EOF
would build image $id
  profile     $IMG_PROFILE (pmos builder)
  device      $PMO_DEVICE ($IMG_ARCH), pmOS channel $PMO_CHANNEL, UI $PMO_UI
  for bridge  ${PMO_BRIDGE:-none} -- 'wk bridge setup ${PMO_BRIDGE:-<name>}' provisions the role
  hostname    $IMG_HOSTNAME
  build on    $host ($(pmos_ssh)), pmbootstrap $PMO_PMB_VERSION in a venv there
  packages    ${PMO_PACKAGES:-none} (on top of postmarketos-base and the UI)
  ssh key     ${WK_IMAGE_KEY:-$HOME/.ssh/id_ed25519.pub}
  uplink      $host's own WiFi credential, copied on $host (the PSK never leaves it)
  radio       $(printf '%s' "${PMO_WIFI_BANDS:-unknown}" | tr ' ' '/') GHz -- the build refuses if that SSID is not on the air in a
              band this phone has, because such an image boots into isolation
  console     user '$PMO_USER', password '$PMO_PASSWORD' (the phone's screen; ssh is key-only)
  into        $(pmos_out "$id") on $host
  writes to   a card -- 'wk bridge provision' finds and copies it automatically,
              or by hand once it is local: wk sysimage write --from <path> --disk <machine>:<device>
EOF
    log "dry run -- nothing was built."
}

pmos_newest_out() { # <profile>; "finished" is a result block -- --resume's own build has an rc but no result
    local d
    for d in $(_pmos_ask "ls -1t $(pmos_root)/out" | grep "^$1-"); do
        _pmos_sh "test -f $(pmos_out "$d")/result" >/dev/null 2>&1 \
            && { printf '%s' "$d"; return 0; }
    done
    return 1
}

# The hash is the one the build host computed: the only form of that check that works from a Mac, with no sfdisk.
pmos_fetch_out() { # <id> <local dest path>
    local id="$1" dest="$2" out result there here
    out=$(pmos_out "$id")

    _pmos_sh "test -f $out/result" \
        || die "the build on $(pmos_host) left no result block at $out.
    It did not get as far as producing an image. The log is at $(pmos_log "$id")
    on that machine."

    info "copying $id off $(pmos_host)"
    _pmos_sh "cat $out/disk.wic.xz" | xz -dc > "$dest" \
        || die "could not copy the image out of $(pmos_host)"

    result=$(_pmos_sh "cat $out/result") \
        || die "could not read the build's result block from $(pmos_host)"
    there=$(printf '%s\n' "$result" | sed -n 's/^raw_sha256=//p' | head -1)
    here=$(sha256sum "$dest" | cut -d' ' -f1)
    [ "$there" = "$here" ] || die "the image does not survive the trip:
    on $(pmos_host): $there
    here:            $here"
    printf '%s' "$dest"
}

# Under nohup on the build host: a foreground ssh build dies with the connection, leaving a chroot mounted.
pmos_spawn() {
    local id="$1" out; out=$(pmos_out "$id")

    pmos_prune

    info "copying the builder to $(pmos_host)"
    _pmos_sh "mkdir -p $out" || die "cannot write $out on $(pmos_host)"
    _pmos_sh "cat > $(pmos_root)/pmos-build.sh && chmod +x $(pmos_root)/pmos-build.sh" \
        < "$WK_ROOT/image/pmos-build.sh" \
        || die "could not copy image/pmos-build.sh to $(pmos_host)"

    local key; key="${WK_IMAGE_KEY:-$HOME/.ssh/id_ed25519.pub}"
    [ -r "$key" ] || die "no public key at $key
    The image has to accept an ssh key on first boot. Set WK_IMAGE_KEY."
    _pmos_sh "cat > $(pmos_root)/driving-key.pub" < "$key" \
        || die "could not copy the public key to $(pmos_host)"

    pmos_ensure_packages

    # A build's first act is `pmbootstrap shutdown`, unmounting the chroots under any other build; the pattern is bracketed because `pgrep -f` would match the ssh carrying this check.
    local running
    running=$(_pmos_ask "pgrep -f 'pmos-build[.]sh' >/dev/null && echo yes" | tr -d ' \n')
    [ "$running" = yes ] && die "a pmos build is already running on $(pmos_host).
    Two at once would fight over the same chroots and loop devices. Wait for it,
    or watch it:  ssh $(pmos_ssh) tail -f \$HOME/wk-pmos/out/*/build.log"

    info "starting the build on $(pmos_host) (it survives this connection)"
    command -v detach_remote >/dev/null 2>&1 || . "$WK_ROOT/lib/detach.sh"
    detach_remote _pmos_sh "$(pmos_log "$id")" "$(pmos_rc "$id")" -- \
        env \
        PMO_ID="$id" \
        PMO_DEVICE="$PMO_DEVICE" \
        PMO_UI="$PMO_UI" \
        PMO_CHANNEL="$PMO_CHANNEL" \
        PMO_PMB_VERSION="$PMO_PMB_VERSION" \
        PMO_USER="$PMO_USER" \
        PMO_PASSWORD="$PMO_PASSWORD" \
        PMO_PACKAGES="$PMO_PACKAGES" \
        PMO_EXTRA_SPACE="$PMO_EXTRA_SPACE" \
        PMO_KERNEL_APORT="${PMO_KERNEL_APORT:-}" \
        PMO_KCONFIG="${PMO_KCONFIG:-}" \
        PMO_HOSTNAME="$IMG_HOSTNAME" \
        PMO_KEYFILE="$(pmos_root)/driving-key.pub" \
        PMO_ROOT="$(pmos_root)" \
        sh "$(pmos_root)/pmos-build.sh" \
        || die "could not start the build on $(pmos_host)"
    sleep 3
}

pmos_ensure_packages() {  # the build host's own prerequisites, for a host `./setup` has not run against
    local missing="" pkgs=""
    # pmbootstrap names the program it cannot find and apt wants the package carrying it; kpartx is the one whose names differ.
    _pmos_need() {
        _pmos_sh "command -v $1 >/dev/null 2>&1" >/dev/null 2>&1 && return 0
        missing="$missing $1"; pkgs="$pkgs $2"
    }
    _pmos_need kpartx multipath-tools
    _pmos_need xz xz-utils
    _pmos_sh "python3 -c 'import ensurepip'" >/dev/null 2>&1 \
        || { missing="$missing python3-venv"; pkgs="$pkgs python3-venv"; }
    _pmos_sh "python3 -c 'import yaml'" >/dev/null 2>&1 \
        || { missing="$missing python3-yaml"; pkgs="$pkgs python3-yaml"; }
    unset -f _pmos_need
    [ -n "$missing" ] || return 0

    die "$(pmos_host) is missing what pmbootstrap needs:$missing
    './setup' on that machine installs them (host/linux/apt.txt); by hand:
        ssh $(pmos_ssh) sudo apt install -y$pkgs"
}

# No rc file is an interrupted build that cannot be resumed, so it goes; a finished one is kept, newest per profile.
pmos_prune() {
    local d rc profile seen_profiles="" keep=1
    for d in $(_pmos_ask "ls -1t $(pmos_root)/out"); do
        case "$d" in ''|probe-*) continue ;; esac
        rc=$(_pmos_ask "cat $(pmos_root)/out/$d/build.rc" | tr -d ' \n')
        if [ -z "$rc" ]; then
            info "removing an interrupted build on $(pmos_host): $d"
            _pmos_sh "rm -rf $(pmos_root)/out/$d" || true
            continue
        fi
        profile="${d%-*}"  # newest per profile, not overall: a host can hold builds for more than one device
        case " $seen_profiles " in
            *" $profile "*) ;;
            *) seen_profiles="$seen_profiles $profile"; continue ;;
        esac
        info "removing an old build on $(pmos_host): $d ($(_pmos_ask "du -sh $(pmos_root)/out/$d | cut -f1" | tr -d ' \n'))"
        _pmos_sh "rm -rf $(pmos_root)/out/$d" || true
    done
}

pmos_running() {
    local id="$1"
    _pmos_sh "test -f $(pmos_rc "$id")" >/dev/null 2>&1 && return 1
    return 0
}

# A poll, not `ssh tail -f`, which cannot know when the build is over: it ends when the rc file appears.
pmos_follow() {
    local id="$1" rc
    command -v detach_wait_remote >/dev/null 2>&1 || . "$WK_ROOT/lib/detach.sh"
    info "following the build on $(pmos_host) -- ^C stops watching, not building"
    rc=$(detach_wait_remote _pmos_sh "$(pmos_log "$id")" "$(pmos_rc "$id")" 5 1)
    [ -n "$rc" ] || die "lost track of the build on $(pmos_host).
    It may still be running. 'wk sysimage build $IMG_PROFILE --resume' picks it
    back up; the log is $(pmos_log "$id")."
    [ "$rc" = 0 ] || die "the build failed on $(pmos_host) (exit $rc).
    Its own output is above, and the whole log is $(pmos_log "$id") there."
}

pmos_find_build() {  # the newest build of this profile, finished or not: what `--resume` attaches to
    _pmos_ask "ls -1t $(pmos_root)/out" | grep "^$IMG_PROFILE-" | head -1 || true
}

# The credential is copied from the build host's own association, so it carries that band implicitly: one for a network the phone's radio cannot see boots it into isolation.

pmos_uplink_ssid() {  # read the way image/pmos-build.sh reads the whole credential, so the two cannot disagree
    _pmos_ask "sudo -n cat /etc/netplan/*.yaml 2>/dev/null \
        | python3 -c \"
import sys, yaml
for doc in (d for d in yaml.safe_load_all(sys.stdin) if d):
    for _, dev in (doc.get('network', {}).get('wifis') or {}).items():
        for ssid, ap in (dev.get('access-points') or {}).items():
            psk = ((ap.get('auth') or {}).get('password') or ap.get('password') or '')
            if psk:
                print(ssid); sys.exit(0)
\""
}

# A fresh scan unioned with the cached `scan dump`, so a stale cache can only be over-generous.
pmos_ssid_freqs() { # <ssid> -> every frequency in MHz that SSID is broadcast on
    local ifs i how out all=""
    ifs=$(_pmos_ask "iw dev 2>/dev/null | awk '/Interface/ { print \$2 }'")
    for i in $ifs; do
        for how in scan "scan dump"; do
            out=$(_pmos_sh "sudo -n iw dev $i $how 2>/dev/null" < /dev/null 2>/dev/null \
                  | awk -v want="$1" '
                      /^BSS/            { f = "" }
                      /freq:/           { f = $2 }
                      /^[ \t]*SSID: /   { sub(/^[ \t]*SSID: /, "")
                                          if ($0 == want && f != "") print f }') || out=""
            all="$all
$out"
        done
    done
    printf '%s\n' "$all" | sed '/^$/d' | sort -u
}

pmos_check_uplink_band() {
    local bands="${PMO_WIFI_BANDS:-}" ssid freqs f reach="" seen="" want24="" want5=""
    [ -n "$bands" ] || { debug "profile declares no PMO_WIFI_BANDS -- not checking the uplink band"; return 0; }
    case " $bands " in *" 2.4 "*) want24=1 ;; esac
    case " $bands " in *" 5 "*)   want5=1  ;; esac

    ssid=$(pmos_uplink_ssid)
    [ -n "$ssid" ] || { debug "could not read the uplink SSID from $(pmos_host) -- leaving the band unchecked"; return 0; }

    freqs=$(pmos_ssid_freqs "$ssid")
    if [ -z "$freqs" ]; then  # not proof: a scan can miss an AP, and the phone will be elsewhere anyway
        warn "$(pmos_host) cannot currently see '$ssid' on the air, so which bands it"
        warn "  offers could not be checked. $PMO_DEVICE's radio is ${bands} GHz."
        return 0
    fi

    for f in $freqs; do
        # `iw` prints "freq: 2412.0", and `[ 2412.0 -lt 3000 ]` is a syntax error `[` reports as false.
        f=${f%%.*}
        case "$f" in
            ''|*[!0-9]*) continue ;;
        esac
        if [ "$f" -lt 3000 ]; then
            seen="${seen:+$seen }2.4"; [ -n "$want24" ] && reach=1
        else
            seen="${seen:+$seen }5";   [ -n "$want5" ]  && reach=1
        fi
    done
    [ -n "$reach" ] && {
        debug "'$ssid' is on the air in a band $PMO_DEVICE supports"
        return 0
    }

    die "'$ssid' is only being broadcast on $(printf '%s' "$seen" | tr ' ' '\n' | sort -u | tr '\n' '/' | sed 's:/$::') GHz, and $PMO_DEVICE's radio is $(printf '%s' "$bands" | tr ' ' '/') GHz only.
    The image copies its WiFi credential from $(pmos_host)'s own association, so it
    would be built with a valid PSK for a network the phone's hardware cannot
    see -- and a phone with no uplink has no way in at all. That failure looks
    exactly like a bad card: everything else verifies and the phone simply never
    appears.
    The fix is on the access point, not here: broadcast '$ssid' on $(printf '%s' "$bands" | tr ' ' '/') GHz as well.
    Same SSID and same PSK, so nothing needs rebuilding once the band exists --
    and re-running this build will then stop refusing."
}

pmos_build() {
    local profile="$1"; shift
    local dry="" detach="" resume=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --dry-run) dry=1 ;;
            --detach)  detach=1 ;;
            --resume)  resume=1 ;;
            *) die "usage: wk sysimage build $profile [--dry-run|--detach|--resume]; unknown option: $1" ;;
        esac
        shift
    done

    local id="$profile-$(date -u +%Y%m%dT%H%M%SZ)"

    [ -n "$dry" ] && { pmos_dry_run "$id"; return 0; }

    _pmos_sh true >/dev/null 2>&1 \
        || die "cannot ssh to $(pmos_ssh) -- that is the build host for this profile.
    pmbootstrap is Linux-only and needs root, so the build happens there.
    Another machine: WK_PMOS_HOST=<name> wk sysimage build $profile"

    pmos_check_uplink_band

    if [ -n "$resume" ]; then
        id=$(pmos_find_build) || true
        [ -n "$id" ] || die "no build to resume on $(pmos_host) for '$profile'.
    'wk sysimage build $profile' starts one."
        info "resuming $id on $(pmos_host)"
        if pmos_running "$id"; then
            pmos_follow "$id"
        else
            local rc; rc=$(_pmos_ask "cat $(pmos_rc "$id")" | tr -d ' \n')
            [ "$rc" = 0 ] || die "that build failed (exit $rc); its log is $(pmos_log "$id") on $(pmos_host)"
            info "it has already finished"
        fi
    else
        pmos_spawn "$id"
        if [ -n "$detach" ]; then
            info "building $id on $(pmos_host); this connection is not involved"
            log  "  follow:  ssh $(pmos_ssh) tail -f $(pmos_log "$id")"
            log  "  finish:  wk sysimage build $profile --resume   (waits for it, then reports where it is)"
            return 0
        fi
        pmos_follow "$id"
    fi

    _pmos_sh "test -f $(pmos_out "$id")/result" \
        || die "the build on $(pmos_host) left no result block at $(pmos_out "$id").
    It did not get as far as producing an image. The log is at $(pmos_log "$id")
    on that machine."

    info "built $id on $(pmos_host) -- $(pmos_out "$id")/disk.wic.xz"
    log  "  the rest of the way:  wk bridge provision ${PMO_BRIDGE:-<bridge>}"
    log  "  ...or by hand: copy disk.wic.xz off $(pmos_host), then"
    log  "             wk sysimage write --from <path> --disk <machine>:<device>"
    log  "             ('wk sysimage disks <machine>' lists what is attached where)"
    log  "             then the card goes into the phone, and:"
    log  "             wk bridge setup ${PMO_BRIDGE:-<bridge>}"
}
