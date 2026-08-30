# The pmos builder: a postmarketOS system for a phone, built by pmbootstrap
# on a Linux aarch64 machine. The build's output stays on that machine, and
# `wk bridge provision` copies it down when it is actually needed for a
# write. Sourced by cmd/sysimage.
#
# Over ssh because pmbootstrap is Linux-only and needs root (loop devices,
# chroots, kpartx) while the workstation driving this is a Mac. The build
# host must be aarch64, because both phones are: on the target's own
# architecture pmbootstrap never starts qemu, and image/pmos-build.sh
# refuses anything else rather than quietly taking ten times as long.
#
# For the two tailnet bridges (`wk help`): a phone is not a fleet machine,
# so a bridge profile names a *device* (the pmOS codename) instead of a
# machine, and `wk bridge provision <bridge>` builds this and writes it to
# the card the bridge's conf names.

# One directory per image id under a root the build host owns, so a build
# that is interrupted leaves rubble that names itself. PMO_BUILD_HOST is the
# profile's own answer (image/profiles.sh), not repeated here as a second
# "rpi5" that could drift from it. WK_PMOS_HOST overrides which machine
# builds, e.g. to point a profile at a second aarch64 build host.
pmos_host() {
    [ -n "${PMO_BUILD_HOST:-}" ] || die "this pmos profile sets no PMO_BUILD_HOST (image/profiles.sh)"
    echo "${WK_PMOS_HOST:-$PMO_BUILD_HOST}"
}
# WK_PMOS_ROOT overrides the build root on that host (default: ~/wk-pmos).
pmos_root()      { echo "${WK_PMOS_ROOT:-\$HOME/wk-pmos}"; }
pmos_out()       { echo "$(pmos_root)/out/$1"; }
pmos_log()       { echo "$(pmos_root)/out/$1/build.log"; }
pmos_rc()        { echo "$(pmos_root)/out/$1/build.rc"; }

# ssh to the build host: a fleet machine's ssh destination when the name is
# one (boot/machines.sh), otherwise the name itself -- a build host is a
# smaller claim than fleet membership.
pmos_ssh() {
    local h; h=$(pmos_host)
    if machine_load "$h" 2>/dev/null && [ -n "${MACH_SSH:-}" ]; then
        printf '%s' "$MACH_SSH"
    else
        printf '%s' "$h"
    fi
}

# The fleet's own rule (`m_ssh_opts`, boot/machines.sh) plus keepalives:
# the build host is on WiFi and roams, and ConnectTimeout only covers the
# handshake -- a connection that stalls *after* connecting hangs until TCP
# gives up, silently, unless the keepalive probes kill it first.
# `machine_load` runs here, not inside `pmos_ssh`, so MACH_ROLE is set
# before `m_ssh_opts` reads it.
pmos_ssh_opts() {
    machine_load "$(pmos_host)" >/dev/null 2>&1
    printf '%s' "-o BatchMode=yes -o ConnectTimeout=$(wk_ssh_timeout) $(m_ssh_opts) \
-o ServerAliveInterval=15 -o ServerAliveCountMax=4"
}

_pmos_sh() {
    # shellcheck disable=SC2046
    ssh $(pmos_ssh_opts) "$(pmos_ssh)" "$@"
}

# Ask the build host something whose answer may legitimately be "nothing":
# whether a file exists yet, how big a log is, what is in a directory.
# `set -o pipefail` makes a plain pipe take ssh's non-zero exit status, and
# `set -e` then kills the command with no message at all -- often for just
# an rc file that does not exist yet. So the remote side is made to succeed,
# and nothing here treats an empty answer as an error.
_pmos_ask() {
    _pmos_sh "$* 2>/dev/null || true" 2>/dev/null | tr -d '\r' || true
}

# --- what a pmos build leaves on its build host -------------------------------
# Two directories under ~/wk-pmos:
#
#   work/  pmbootstrap's chroots and apk cache -- ~8 GB, rebuildable from the
#          network. `wk gc --purge-pmos` erases it.
#   out/   half a gigabyte per build: the compressed image, its block map
#          and the log. Ordinary `wk gc` prunes these.
#
# Reported and pruned from here, not cmd/disk and cmd/gc, so "what does a
# pmos build cost" has one answer, next to the code that creates it.

# Every build host a pmos profile names, once each. A subshell per profile:
# image_profile_load sets globals neither `wk disk` nor `wk gc` should keep.
pmos_build_hosts() {
    local p
    for p in $(image_profile_list | awk '/^[a-z0-9-]+$/ { print $1 }'); do
        ( image_profile_load "$p" >/dev/null 2>&1 || exit 0
          [ "${IMG_BUILDER:-}" = pmos ] || exit 0
          pmos_host )
    done | sed '/^$/d' | sort -u
}

# `kb<TAB>label<TAB>note` for one build host, the shape cmd/disk renders.
# `??` when unreachable, not "0": "0" would be a measurement.
pmos_cache_probe() { # <host>
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

# Erase the chroots on one build host. The expensive, deliberate half.
pmos_purge_work() { # <host>
    local h="$1" kb
    kb=$(WK_PMOS_HOST="$h" _pmos_ask "du -sk \$HOME/wk-pmos/work | cut -f1" | tr -d ' \n')
    case "$kb" in ''|*[!0-9]*) kb=0 ;; esac
    [ "$kb" -gt 0 ] || { debug "no pmbootstrap work folder on $h"; return 0; }

    # Unmount before removing, or the chroots' bind mounts follow the rm into
    # the host's own /proc and /dev. pmbootstrap's own shutdown is the only
    # thing that knows what it mounted.
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

# The newest finished build for a profile: a result block is what "finished"
# means. Newest first, like pmos_find_build, but only one that actually
# finished -- --resume's own build (an rc but no result yet) is not an image
# to hand somebody.
pmos_newest_out() { # <profile>
    local d
    for d in $(_pmos_ask "ls -1t $(pmos_root)/out" | grep "^$1-"); do
        _pmos_sh "test -f $(pmos_out "$d")/result" >/dev/null 2>&1 \
            && { printf '%s' "$d"; return 0; }
    done
    return 1
}

# Copy a finished build's image off its build host to a local path,
# decompressing on the way and checking the hash the build host itself
# computed -- the only form of that check that works from a Mac, which has
# no sfdisk. The image lands wherever the caller put it, ready for
# 'wk sysimage write --from'.
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

# The build itself. Launched under nohup on the build host, followed by
# tailing its log rather than run as a foreground ssh command: a foreground
# build dies with the connection, having left a chroot mounted. `--detach`
# is then just "do not tail", not a second mechanism.
pmos_spawn() {
    local id="$1" out; out=$(pmos_out "$id")

    pmos_prune

    info "copying the builder to $(pmos_host)"
    _pmos_sh "mkdir -p $out" || die "cannot write $out on $(pmos_host)"
    _pmos_sh "cat > $(pmos_root)/pmos-build.sh && chmod +x $(pmos_root)/pmos-build.sh" \
        < "$WK_ROOT/image/pmos-build.sh" \
        || die "could not copy image/pmos-build.sh to $(pmos_host)"

    # The key the image will accept, named rather than guessed: the key that
    # reaches the *build host* is not evidence of the key the phone needs.
    local key; key="${WK_IMAGE_KEY:-$HOME/.ssh/id_ed25519.pub}"
    [ -r "$key" ] || die "no public key at $key
    The image has to accept an ssh key on first boot. Set WK_IMAGE_KEY."
    _pmos_sh "cat > $(pmos_root)/driving-key.pub" < "$key" \
        || die "could not copy the public key to $(pmos_host)"

    pmos_ensure_packages

    # One build at a time on a build host: the first thing a build does is
    # `pmbootstrap shutdown`, unmounting chroots and detaching the loop
    # device out from under any build still using them. A check, not a lock,
    # since nothing coordinates this over ssh. Pattern is `pmos-build[.]sh`,
    # brackets and all: `pgrep -f` matches every process's full command
    # line, and the ssh carrying this check itself contains the plain
    # spelling, so an unbracketed pattern matches itself.
    local running
    running=$(_pmos_ask "pgrep -f 'pmos-build[.]sh' >/dev/null && echo yes" | tr -d ' \n')
    [ "$running" = yes ] && die "a pmos build is already running on $(pmos_host).
    Two at once would fight over the same chroots and loop devices. Wait for it,
    or watch it:  ssh $(pmos_ssh) tail -f \$HOME/wk-pmos/out/*/build.log"

    info "starting the build on $(pmos_host) (it survives this connection)"
    # detach_remote (lib/detach.sh) owns the nohup/disown spelling and the
    # log/rc-file convention. Each PMO_* value is its own argv item, not a
    # pre-quoted string, so detach_remote quotes them.
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
    # The launcher returns immediately; give the far side a moment to create
    # the log.
    sleep 3
}

# The build host's own prerequisites (host/linux/apt.txt; `./setup`
# installs them). This is the fallback for a build host `./setup` has not
# run against. Asks before installing: this is somebody's machine and apt
# is not this command's business.
pmos_ensure_packages() {
    local missing="" pkgs=""
    # command -> package: pmbootstrap names the program it cannot find, and
    # apt wants the package that carries it. kpartx is the one that differs.
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

# Two kinds of leftover, and only one is rubble. A directory with no rc file
# is a build that was interrupted and cannot be resumed, so it goes. A
# finished one is kept -- `wk bridge provision` and `--resume` both find it
# there -- but only the newest per profile, or a build host quietly fills up.
pmos_prune() {
    local d rc profile seen_profiles="" keep=1
    for d in $(_pmos_ask "ls -1t $(pmos_root)/out"); do
        case "$d" in ''|probe-*) continue ;; esac
        rc=$(_pmos_ask "cat $(pmos_root)/out/$d/build.rc" | tr -d ' \n')
        if [ -z "$rc" ]; then
            # No rc and nothing running (checked by the caller, which refuses
            # while a build is live) means it died partway.
            info "removing an interrupted build on $(pmos_host): $d"
            _pmos_sh "rm -rf $(pmos_root)/out/$d" || true
            continue
        fi
        # Newest per *profile*, not newest overall: a build host can hold
        # builds for more than one device, and a run of one profile's builds
        # must not age out another's only copy.
        profile="${d%-*}"
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

# Follow the remote log until the build ends. A poll, not `ssh tail -f`:
# `tail -f` has no idea when the build is over. Polling ends when the rc
# file appears, the same evidence the exit status is read from.
# detach_wait_remote (lib/detach.sh) owns the poll, in its streaming mode:
# 5s, not the shared default of 30, since a human is watching the log scroll.
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

# The newest build of this profile the build host has, finished or not. What
# `--resume` attaches to, and what makes a dropped connection cost nothing.
pmos_find_build() {
    _pmos_ask "ls -1t $(pmos_root)/out" | grep "^$IMG_PROFILE-" | head -1 || true
}

# --- can the phone actually reach the network the credential names? ----------
# The uplink credential is copied from the build host's own association,
# which keeps the PSK off every wire and log -- at the cost that the *band*
# comes along implicitly, so something has to notice when it is a band the
# target has no radio for. A credential valid for a network the phone's
# hardware cannot see boots the phone into isolation while everything else
# about the image verifies correctly. Deliberately not "which band is the
# build host on", which goes wrong the moment the SSID gains a band the
# build host is not associated on: what matters is whether the SSID is *on
# the air* in a band the phone supports, scanned from the machine about to
# copy the credential. Nothing here reads or transports the PSK.

# The SSID the credential will name, read the same way image/pmos-build.sh
# reads the whole credential, so the two can never disagree.
pmos_uplink_ssid() {
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

# Every frequency, in MHz, that SSID is broadcast on, as seen from the build
# host. Both a fresh scan and the cached `scan dump`, unioned: the dump is
# the fallback for a host where scanning is not permitted, and unioning
# rather than preferring one means a stale cache can only be over-generous,
# never blocking.
pmos_ssid_freqs() { # <ssid>
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

# Refuse a build whose credential names a network the phone has no radio to see.
pmos_check_uplink_band() {
    local bands="${PMO_WIFI_BANDS:-}" ssid freqs f reach="" seen="" want24="" want5=""
    [ -n "$bands" ] || { debug "profile declares no PMO_WIFI_BANDS -- not checking the uplink band"; return 0; }
    case " $bands " in *" 2.4 "*) want24=1 ;; esac
    case " $bands " in *" 5 "*)   want5=1  ;; esac

    ssid=$(pmos_uplink_ssid)
    [ -n "$ssid" ] || { debug "could not read the uplink SSID from $(pmos_host) -- leaving the band unchecked"; return 0; }

    freqs=$(pmos_ssid_freqs "$ssid")
    if [ -z "$freqs" ]; then
        # Not proof of anything: a scan can miss an AP, and the phone will be
        # somewhere else in the house anyway.
        warn "$(pmos_host) cannot currently see '$ssid' on the air, so which bands it"
        warn "  offers could not be checked. $PMO_DEVICE's radio is ${bands} GHz."
        return 0
    fi

    for f in $freqs; do
        # `iw` prints "freq: 2412.0", and `[ 2412.0 -lt 3000 ]` is not a failed
        # comparison -- it is a syntax error that `[` reports as false, silently
        # sending every frequency to the 5 GHz branch. Truncate first.
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

    # Before anything is built: a twenty-minute build that produces an
    # unreachable phone is the expensive way to learn this.
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
    # `wk bridge provision` is the rest of this path done right, in order:
    # it finds this build, copies it down, writes it, and prompts through
    # the steps that are a person.
    log  "  the rest of the way:  wk bridge provision ${PMO_BRIDGE:-<bridge>}"
    log  "  ...or by hand: copy disk.wic.xz off $(pmos_host), then"
    log  "             wk sysimage write --from <path> --disk <machine>:<device>"
    log  "             ('wk sysimage disks <machine>' lists what is attached where)"
    log  "             then the card goes into the phone, and:"
    log  "             wk bridge setup ${PMO_BRIDGE:-<bridge>}"
}
