# The pmos builder: a postmarketOS system for a phone, built by pmbootstrap on
# a Linux aarch64 machine and imported into the image store like any other.
#
# Sourced by cmd/sysimage. The third builder, and the reason it is a builder
# rather than a command of its own is the same reason yocto is one: what comes
# out is an image in the store, and everything after that -- `wk sysimage ls`,
# `show`, `write`, `rm`, the refusals, the block-map write path -- is shared.
#
#   distro   download a pinned distro image and seed it (minutes, unprivileged,
#            on the workstation)
#   yocto    bitbake a distribution from source in a workspace (hours)
#   pmos     pmbootstrap on a Linux machine over ssh (tens of minutes)
#
# Why over ssh: pmbootstrap is Linux-only and needs root -- loop devices,
# chroots, kpartx -- and the workstation driving this is a Mac. So the privileged
# half runs where the capability is, which is the split `wk sysimage write` has
# always had. The build host must be aarch64, because both phones are: on the
# target's own architecture pmbootstrap never starts qemu, and image/pmos-build.sh
# refuses anything else rather than quietly taking ten times as long.
#
# What this is for: the two tailnet bridges (`wk help bridge`). A phone is not a
# fleet machine -- `wk boot` cannot boot one and nothing here pretends it can --
# so a bridge profile names a *device* (the pmOS codename) instead of a machine,
# and the card it lands on is written by the machine that happens to hold the
# card reader:
#
#   wk bridge provision tailnet-bridge-generic
#
# ...which builds this, writes it to the card the bridge's conf names, and then
# prompts through the step that is a person. By hand, the same thing is:
#
#   wk sysimage build bridge-pinephone
#   wk sysimage write <id> --disk rpi5:/dev/mmcblk0
#
# Then the card goes into the phone. That is the one hands-on step left, and it
# is the recoverable one: both phones boot a card in preference to their internal
# storage, so removing it puts the phone back exactly as it was.

# Where the build happens, and where its artifacts wait to be imported.
#
# One directory per image id under a root the build host owns, so a build that
# is interrupted leaves rubble that names itself and the next one does not
# inherit it.
# PMO_BUILD_HOST is the profile's own answer (image/profiles.sh) -- not
# repeated here as a second "rpi5" that could drift from it.
pmos_host() {
    [ -n "${PMO_BUILD_HOST:-}" ] || die "this pmos profile sets no PMO_BUILD_HOST (image/profiles.sh)"
    echo "${WK_PMOS_HOST:-$PMO_BUILD_HOST}"
}
pmos_root()      { echo "${WK_PMOS_ROOT:-\$HOME/wk-pmos}"; }
pmos_out()       { echo "$(pmos_root)/out/$1"; }
pmos_log()       { echo "$(pmos_root)/out/$1/build.log"; }
pmos_rc()        { echo "$(pmos_root)/out/$1/build.rc"; }

# ssh to the build host. A fleet machine's ssh destination when the name is one
# (boot/machines.sh), and otherwise the name itself -- a build host is a machine
# that can run pmbootstrap, which is a smaller claim than fleet membership.
pmos_ssh() {
    local h; h=$(pmos_host)
    if machine_load "$h" 2>/dev/null && [ -n "${MACH_SSH:-}" ]; then
        printf '%s' "$MACH_SSH"
    else
        printf '%s' "$h"
    fi
}

# The options for reaching the build host: the fleet's own rule (`m_ssh_opts`,
# boot/machines.sh) when it names a fleet machine -- so a build host that is
# also a bench-device gets the same root-login and unpinned-key handling any
# other connection to it would -- plus keepalives, which every pmos build
# needs and a plain fleet connection does not: the build host is on WiFi and
# it roams. ConnectTimeout only covers the handshake -- a connection that
# stalls *after* connecting hangs until TCP gives up, which for a poll loop
# means it stops reporting and never notices. Four missed 15s probes and the
# ssh dies, the poll retries, and the build -- which is detached on the far
# side -- carries on regardless.
#
# `machine_load` runs here, not inside `pmos_ssh`, so MACH_ROLE is set before
# `m_ssh_opts` reads it regardless of the order ssh(1)'s argv is built in.
pmos_ssh_opts() {
    machine_load "$(pmos_host)" >/dev/null 2>&1
    printf '%s' "-o BatchMode=yes -o ConnectTimeout=${WK_SSH_TIMEOUT:-10} $(m_ssh_opts) \
-o ServerAliveInterval=15 -o ServerAliveCountMax=4"
}

_pmos_sh() {
    # shellcheck disable=SC2046
    ssh $(pmos_ssh_opts) "$(pmos_ssh)" "$@"
}

# Ask the build host something whose answer may legitimately be "nothing":
# whether a file exists yet, how big a log is, what is in a directory.
#
# This exists because of `set -o pipefail`, and it cost most of an afternoon.
# `x=$(_pmos_sh "cat missing" | tr -d ...)` looks defensive -- the failure is
# piped into tr, whose status is the pipeline's -- but pipefail makes the
# pipeline take ssh's non-zero status, and `set -e` then kills the command
# **with no message at all**. Every "the build died silently after one poll" in
# this file's history was this: the poll asked for an rc file that did not exist
# yet, which is the normal state of a build that is still running.
#
# So: the remote side is made to succeed, and the local pipeline is guarded too.
# Nothing here treats an empty answer as an error, because for every caller an
# empty answer is information.
_pmos_ask() {
    _pmos_sh "$* 2>/dev/null || true" 2>/dev/null | tr -d '\r' || true
}

# --- what a pmos build leaves on its build host -------------------------------
#
# Two directories under ~/wk-pmos, and they are the largest thing this repo
# stores on a machine that is not the workstation:
#
#   work/  pmbootstrap's chroots and its apk cache -- about 8 GB, and what makes
#          a rebuild four minutes instead of twenty. Rebuildable from the
#          network, so it is a cache in the strict sense: `wk gc --purge-pmos`
#          erases it and the next build refills it.
#   out/   half a gigabyte per build: the compressed image, its block map and
#          the log. Ordinary `wk gc` prunes these the same way it prunes images.
#
# They are reported and pruned from here rather than from cmd/disk and cmd/gc so
# that "what does a pmos build cost" has one answer, next to the code that
# creates it.

# Every build host a pmos profile names, once each. A subshell per profile
# because image_profile_load sets globals, and neither `wk disk` nor `wk gc` has
# any business having a profile loaded afterwards.
pmos_build_hosts() {
    local p
    for p in $(image_profile_list | awk '/^[a-z0-9-]+$/ { print $1 }'); do
        ( image_profile_load "$p" >/dev/null 2>&1 || exit 0
          [ "${IMG_BUILDER:-}" = pmos ] || exit 0
          pmos_host )
    done | sed '/^$/d' | sort -u
}

# `kb<TAB>label<TAB>note` for one build host, the shape cmd/disk renders.
# Silent when the host has nothing of ours; `??` when it cannot be reached,
# because "0" would be a measurement and this is an absence.
pmos_cache_probe() { # <host>
    local out
    out=$(WK_PMOS_HOST="$1" _pmos_ask "du -sk \$HOME/wk-pmos/work \$HOME/wk-pmos/out")
    if [ -z "$out" ]; then
        # Reachable-but-empty and unreachable are different answers, and the
        # difference matters to somebody deciding whether to go looking.
        if WK_PMOS_HOST="$1" _pmos_sh true >/dev/null 2>&1; then return 0; fi
        printf '%s\t%s\t%s\n' '??' "$1" "did not answer; nothing measured"
        return 0
    fi
    printf '%s\n' "$out" | while read -r kb path; do
        [ -n "${kb:-}" ] || continue
        case "$path" in
            */work) printf '%s\t%s\t%s\n' "$kb" "$1: pmbootstrap chroots" \
                        "rebuildable; wk gc --purge-pmos" ;;
            */out)  printf '%s\t%s\t%s\n' "$kb" "$1: builds awaiting import" \
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
  into        $(image_dir "$id")
  writes to   a card, with 'wk sysimage write $id --disk <machine>:<device>'
EOF
    log "dry run -- nothing was built."
}

# When the build actually started, out of the image id.
#
# Not $BUILT, which is "now": with --resume that is the moment of the *import*,
# which can be an hour after the build and on a different day. The id carries
# the real answer (`<profile>-20260821T132857Z`) because it is what named the
# directory the build wrote into, so the manifest can be honest about both --
# `built` from the id, `imported` from the clock.
_pmos_built_at() {
    printf '%s' "${1##*-}" \
        | sed -n 's/^\(....\)\(..\)\(..\)T\(..\)\(..\)\(..\)Z$/\1-\2-\3T\4:\5:\6Z/p'
}

# Import what the build host produced. The compressed image comes over and
# disk.img is made here by decompressing: one artifact, one hash, and every
# reader downstream sees the same bytes. The compressed copy is a wire format
# and is deleted once it is unpacked.
pmos_import() {
    local id="$1" out dir
    out=$(pmos_out "$id"); dir=$(image_dir "$id")

    _pmos_sh "test -f $out/result" \
        || die "the build on $(pmos_host) left no result block at $out.
    It did not get as far as producing an image. The log is at $(pmos_log "$id")
    on that machine."

    mkdir -p "$dir"

    info "importing disk.wic.xz from $(pmos_host)"
    _pmos_sh "cat $out/disk.wic.xz" > "$dir/disk.wic.xz" \
        || die "could not copy the image out of $(pmos_host)"

    # Decompressed here rather than sent raw: the compressed file is a fraction
    # of the size over a WiFi link, and the raw image is derivable from it.
    info "decompressing (the store holds the raw image, and hashes it)"
    xz -dc "$dir/disk.wic.xz" > "$dir/disk.img" \
        || die "what came out of $(pmos_host) is not valid xz"
    rm -f "$dir/disk.wic.xz"

    local result; result=$(_pmos_sh "cat $out/result") \
        || die "could not read the build's result block from $(pmos_host)"
    _pmos_field() { printf '%s\n' "$result" | sed -n "s/^$1=//p" | head -1; }

    # The raw hash the build host computed, against the one here. This is what
    # the yocto importer's partition-table read is for -- catching a login
    # shell's banner printed ahead of the bytes -- and it catches strictly more:
    # a contaminated stream cannot match, wherever the extra bytes landed. It is
    # also the only form of the check that works from a Mac, which has no
    # sfdisk.
    local there here
    there=$(_pmos_field raw_sha256)
    here=$(sha256sum "$dir/disk.img" | cut -d' ' -f1)
    [ "$there" = "$here" ] || die "the image does not survive the trip:
    on $(pmos_host): $there
    here:            $here"

    cat > "$(image_manifest "$id")" <<EOF
id=$id
profile=$IMG_PROFILE
builder=pmos
device=$(_pmos_field device)
arch=$IMG_ARCH
hostname=$(_pmos_field hostname)
channel=$(_pmos_field channel)
pmaports_rev=$(_pmos_field pmaports_rev)
pmbootstrap=$(_pmos_field pmbootstrap)
ui=$(_pmos_field ui)
console_user=$(_pmos_field user)
uplink_ssid=$(_pmos_field uplink_ssid)
bridge=${PMO_BRIDGE:-}
built_on=$(pmos_host)
built=$(_pmos_built_at "$id")
imported=$BUILT
built_by=$(hostname)
wk_tools=$(git -C "$WK_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)
disk_bytes=$(file_bytes "$dir/disk.img")
disk_sha256=$here
EOF
    unset -f _pmos_field
}

# The build itself.
#
# Launched under nohup on the build host and followed by tailing its log, rather
# than run as a foreground ssh command. The difference is what happens when this
# connection dies: a foreground build dies with it, half an hour in, having left
# a chroot mounted. Detached, the machine that builds it keeps building -- and
# `--detach` is then just "do not tail", not a second mechanism.
pmos_spawn() {
    local id="$1" out; out=$(pmos_out "$id")

    pmos_prune

    info "copying the builder to $(pmos_host)"
    _pmos_sh "mkdir -p $out" || die "cannot write $out on $(pmos_host)"
    _pmos_sh "cat > $(pmos_root)/pmos-build.sh && chmod +x $(pmos_root)/pmos-build.sh" \
        < "$WK_ROOT/image/pmos-build.sh" \
        || die "could not copy image/pmos-build.sh to $(pmos_host)"

    # The key the image will accept. Named rather than guessed -- the image has
    # to accept a key on first boot or the phone comes up unreachable, and the
    # key that reaches the *build host* is not evidence of the key the phone
    # needs.
    local key; key="${WK_IMAGE_KEY:-$HOME/.ssh/id_ed25519.pub}"
    [ -r "$key" ] || die "no public key at $key
    The image has to accept an ssh key on first boot. Set WK_IMAGE_KEY."
    _pmos_sh "cat > $(pmos_root)/driving-key.pub" < "$key" \
        || die "could not copy the public key to $(pmos_host)"

    pmos_ensure_packages

    # One build at a time on a build host, and this is why it matters more than
    # it looks: the first thing a build does is `pmbootstrap shutdown`, which
    # unmounts the chroots and detaches the loop device -- underneath a build
    # that is using them. The two would produce one corrupt image and one
    # inscrutable mkfs failure.
    #
    # A check rather than a lock, deliberately. The store lock this command
    # already holds serialises builds driven from *this* machine, which is the
    # realistic case; this catches the other one -- a build somebody else
    # started over there -- and refuses instead of racing it. (A lock would
    # want flock, and nothing in this tree calls flock.)
    # `pmos-build[.]sh`, and the brackets are the whole trick: `pgrep -f` matches
    # against every process's full command line, and the ssh that carries this
    # check *contains the pattern*, so the plain spelling matches itself and
    # reports a build running on an idle machine. It did, immediately.
    local running
    running=$(_pmos_ask "pgrep -f 'pmos-build[.]sh' >/dev/null && echo yes" | tr -d ' \n')
    [ "$running" = yes ] && die "a pmos build is already running on $(pmos_host).
    Two at once would fight over the same chroots and loop devices. Wait for it,
    or watch it:  ssh $(pmos_ssh) tail -f \$HOME/wk-pmos/out/*/build.log"

    info "starting the build on $(pmos_host) (it survives this connection)"
    # detach_remote (lib/detach.sh) owns the nohup/disown spelling and the
    # log/rc-file convention now -- the same shape targets/vm.sh's base
    # prebuild uses. Each PMO_* value is its own argv item rather than a
    # pre-quoted string, so detach_remote quotes them the same way this used
    # to by hand.
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
    # The launcher returns immediately; give the far side a moment to create the
    # log, so the follow below has something to read.
    sleep 3
}

# The build host's own prerequisites.
#
# `./setup` installs these now -- they are in host/linux/apt.txt -- so this is
# the fallback for a build host that has not had setup run against it, or one
# provisioned before they were added. It still asks before installing, because
# this is somebody's machine and apt is not this command's business; what it
# does not do is make you find the package names.
pmos_ensure_packages() {
    local missing="" pkgs=""
    # command -> package, because pmbootstrap names the program it cannot find
    # and apt wants the package that carries it. kpartx is the one where those
    # differ enough to cost you a search.
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

    warn "$(pmos_host) is missing what pmbootstrap needs:$missing"
    log  "  './setup' on that machine installs them (host/linux/apt.txt); this is"
    log  "  the fallback for a build host that has not had it run."
    confirm "install$pkgs on $(pmos_host)?" \
        || die "not installed -- the build cannot run without them.
    By hand:  ssh $(pmos_ssh) sudo apt install -y$pkgs"
    # shellcheck disable=SC2086
    _pmos_sh "sudo -n DEBIAN_FRONTEND=noninteractive apt-get install -y -q$pkgs" \
        || die "could not install$pkgs on $(pmos_host)"
    changed "installed$pkgs on $(pmos_host)"
}

# What earlier builds left on the build host: half a gigabyte of compressed
# image each, plus the log.
#
# Two kinds, and only one is rubble. A directory with no rc file is a build that
# was interrupted -- nothing can be imported from it and the next build cannot
# resume it -- so it goes. A finished one is kept, because `--resume` imports
# from it and re-importing after a failed transfer is the cheap path; but only
# the newest two, or a build host quietly fills up. Said out loud rather than
# silently: a command that deletes half a gigabyte should name it.
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
        # Newest per *profile*, not newest overall. Overall was the first
        # version and it was wrong in a way that only shows up with two devices:
        # two PinePhone builds in a row made the Librem 5's artifacts "old" and
        # deleted them, on a build host where they were the only copy that had
        # not been imported yet.
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

# Follow the remote log until the build ends.
#
# A poll, not `ssh tail -f`, and both halves of that are deliberate. `tail -f`
# has no idea when the build is over, so it needs a process to watch -- and
# `--pid=$(pgrep ...)` with nothing to match becomes `--pid=` and then a plain
# `tail -f` that never returns -- hanging for as long as it is left, on a build
# that has already failed, holding the image-store lock the whole time. Polling
# ends when the rc file appears, which is the same evidence the exit status is
# read from, so the follow cannot outlive the build or end before it.
#
# detach_wait_remote (lib/detach.sh) owns the poll now, in its streaming mode:
# 5s, not the shared default of 30, because this is the attended path -- a
# human is watching the log scroll, and a build that finished inside 30s would
# otherwise report nothing at all until the next tick.
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

# An unfinished or unimported build for this profile, newest first. What
# `--resume` attaches to, and what makes a dropped connection cost nothing.
pmos_find_build() {
    _pmos_ask "ls -1t $(pmos_root)/out" | grep "^$IMG_PROFILE-" | head -1 || true
}

# --- can the phone actually reach the network the credential names? ----------
#
# The uplink credential is copied from the build host's own association, which
# is what keeps the PSK off every wire and out of every log. The cost of that
# shortcut is that the *band* comes along implicitly, so something has to notice
# when it is a band the target has no radio for.
#
# A dual-band board associated with the house SSID on channel 52 hands over a
# 5 GHz network; the PinePhone's radio is an RTL8723CS, 802.11 b/g/n and
# single-band. The image is then built with a perfectly valid
# credential for a network the phone's hardware cannot see, booted, and showed
# every neighbour's 2.4 GHz network except the one it wanted. Everything else
# about that image was correct, which is what made it expensive: the card, the
# SPL, the write and the read-back all verified, and the fault was one number
# nobody had compared.
#
# The question this asks is deliberately *not* "which band is the build host
# on", which goes wrong the moment the SSID gains a 2.4 GHz radio while the
# build host stays associated on 5 GHz: a build that works and a check that
# refuses it. What matters is
# whether the SSID the credential names is *on the air* in a band the phone
# supports -- so that is what gets looked at, with a scan from the machine that
# is about to copy the credential.
#
# Nothing here reads or transports the PSK. The SSID is not the secret; it is
# already recorded in the image manifest.

# The SSID the credential will name, read out of the build host's netplan the
# same way image/pmos-build.sh reads the whole credential -- so the two can
# never disagree about which network is meant.
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

# Every frequency, in MHz, that SSID is being broadcast on, as seen from the
# build host.
#
# Both a fresh scan and the cached dump, unioned, and the reason is a bug this
# had on its first outing: `scan dump` alone returned only the 5 GHz BSSID of an
# SSID that had just gained a 2.4 GHz radio, because the cache predated the
# change -- and a band missing from the answer is a build refused for no reason.
# A fresh scan is what sees the current truth; the dump is the fallback for a
# host where scanning is not permitted. Unioned rather than preferred, so a
# stale cache can only ever be over-generous, never blocking.
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
        # comparison -- it is a syntax error that `[` reports as false. Every
        # frequency therefore fell through to the 5 GHz branch, which made this
        # refuse a network it had just correctly found on 2.4 GHz. Truncate to
        # an integer before comparing, and never compare a string `iw` printed.
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
            *) die "unknown option: $1
    The pmos builder takes --dry-run, --detach and --resume." ;;
        esac
        shift
    done

    BUILT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    local id="$profile-$(date -u +%Y%m%dT%H%M%SZ)"

    [ -n "$dry" ] && { pmos_dry_run "$id"; return 0; }

    _pmos_sh true >/dev/null 2>&1 \
        || die "cannot ssh to $(pmos_ssh) -- that is the build host for this profile.
    pmbootstrap is Linux-only and needs root, so the build happens there.
    Another machine: WK_PMOS_HOST=<name> wk sysimage build $profile"

    # Before the lock and before anything is built: a twenty-minute build that
    # produces an unreachable phone is the expensive way to learn this.
    pmos_check_uplink_band

    image_lock

    local r
    for r in $(image_rubble); do
        warn "destroying rubble from an interrupted build: $r"
        rm -rf "$(image_dir "$r")"
    done

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
            info "it has already finished -- importing"
        fi
    else
        pmos_spawn "$id"
        if [ -n "$detach" ]; then
            info "building $id on $(pmos_host); this connection is not involved"
            log  "  follow:  ssh $(pmos_ssh) tail -f $(pmos_log "$id")"
            log  "  finish:  wk sysimage build $profile --resume   (imports it into the store)"
            return 0
        fi
        pmos_follow "$id"
    fi

    pmos_import "$id"

    info "built $id  ($(du -h "$(image_disk "$id")" | cut -f1))"
    # One line first, because the rest of this path is a chain somebody has to
    # get right in order, and `wk bridge provision` is that chain -- it picks up
    # exactly this image, writes it, and prompts through the two steps that are
    # a person.
    log  "  the rest of the way:  wk bridge provision ${PMO_BRIDGE:-<bridge>}"
    log  "  ...or by hand:"
    log  "             wk sysimage write $id --disk <machine>:<device>"
    log  "             ('wk sysimage disks <machine>' lists what is attached where)"
    log  "             then the card goes into the phone, and:"
    log  "             wk bridge setup ${PMO_BRIDGE:-<bridge>}"
}
