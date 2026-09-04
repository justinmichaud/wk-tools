#!/usr/bin/env bash
#
# wk bench mac-volume -- make this Mac's benchmark install, on a second APFS
# volume in the internal container.
#
#   wk bench mac-volume                    report: the container, the room, the plan
#   wk bench mac-volume --create           add the volume (does not install macOS)
#   wk bench mac-volume --fetch [--version V]   download the macOS installer
#   wk bench mac-volume --install          hand off to startosinstall; reboots
#   wk bench mac-volume --provision        in bench mode: marker, quiet, python
#   wk bench mac-volume --repair           re-arm first-boot on an installed volume
#   wk bench mac-volume --build-pkg        the provisioning package, on its own
#   wk bench mac-volume --all              --create, --fetch if needed, --install
#   ... any of the above with --dry-run    say what it would do, change nothing
#
# Runs *on the Mac*, unlike `wk bench mac`: every step acts on the machine it
# runs on, needs real sudo, and one of them reboots.
#
# A second APFS volume in the internal container, not an external SSD: the
# internal disk is the storage host mode measures on, so a second volume
# removes disk hardware as a variable. It costs shared free space (hence the
# headroom refusal below) and shared wear and thermals with the workstation.
# `diskutil apfs deleteVolume` is the way back.
#
# Not in the `wk quiesce` privileged helper: creating a volume and running
# startosinstall as root are once-per-machine, not the small reversible
# per-run actions that allowlist grants.
#
# Environment overrides, each defaulted below where it is read. WK_BENCH_VOLUME
# is also read by bench/mac-ab.sh and boot/machines/mbp.conf's NODE_VOLUME, so
# the three have to agree; WK_BENCH_NO_PKG skips the provisioning package, which
# leaves Setup Assistant needing a person, so it is for debugging --install
# itself.

set -euo pipefail
WK_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$WK_ROOT/lib/common.sh"

VOLUME="${WK_BENCH_VOLUME:-WK Bench}"
PROFILE=perf-macos-tolken

# How much the host install still has to work in afterwards, the container
# being shared. Fatal rather than a warning: the failure mode is a full disk
# discovered mid-build.
NEED_GB="${WK_BENCH_NEED_GB:-120}"

DRY=""

# Every mutating command goes through here; nothing calls sudo directly.
run() {
    if [ -n "$DRY" ]; then
        local q="" a
        for a in "$@"; do q="$q $(sh_quote "$a")"; done
        log "  would run:$q"
        return 0
    fi
    "$@"
}

usage() { sed -n '3,13p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 1; }

# The guard is here, not at the top: a question about the command has to be
# answerable from a machine that is not the Mac.
case "${1:-}" in
    -h|--help) usage ;;
esac
is_macos || die "this runs on the Mac itself -- it acts on the machine's own disk.
  From another machine, the lane that drives it is: wk bench mac <ws>"

# --- reading the disk --------------------------------------------------------
# plutil -extract against `diskutil info -plist`, not awk against the human
# output, whose labels differ between macOS versions and disk kinds.

dk() { diskutil info -plist "$1" 2>/dev/null; }

dk_field() {  # $1 target, $2 key; empty (not an error) when the key is absent
    dk "$1" | plutil -extract "$2" raw - 2>/dev/null || true
}

container_of_root() {
    local c
    c=$(dk_field / APFSContainerReference)
    [ -n "$c" ] || die "the boot volume is not on an APFS container -- this shape does not apply"
    printf '%s' "$c"
}

free_bytes_of_container() {
    dk_field / APFSContainerFree
}

volume_exists() { diskutil info "$VOLUME" >/dev/null 2>&1; }

# Mounted and actually a macOS system volume: an empty formatted volume with
# the right name mounts perfectly and boots nothing.
volume_is_system() {
    [ -d "/Volumes/$VOLUME/System/Library/CoreServices" ] \
        && [ -f "/Volumes/$VOLUME/System/Library/CoreServices/SystemVersion.plist" ]
}

gb() { printf '%s' "$(( ${1:-0} / 1000000000 ))"; }

report() {
    local cont free
    cont=$(container_of_root)
    free=$(free_bytes_of_container)
    info "the benchmark volume on this Mac"
    log  "  container:      $cont  (the same one the running system is on)"
    log  "  free in it:     $(gb "$free") GB   (need $NEED_GB GB to proceed)"
    log  "  volume name:    $VOLUME"
    local _k="${WK_TS_AUTHKEY:-$HOME/.config/wk/tailscale-authkey}"
    if [ -s "$_k" ]; then
        log  "  tailscale key:  present ($_k)"
    else
        log  "  tailscale key:  MISSING -- the bench install will have no tailnet"
        log  "                  identity, so 'wk bench mac-ab' cannot watch a run"
        log  "                  or collect it without the startup manager."
        log  "                  './setup --stage benchkey' asks for one."
    fi
    if volume_exists; then
        if volume_is_system; then
            local v
            v=$(/usr/libexec/PlistBuddy -c 'Print :ProductUserVisibleVersion' \
                    "/Volumes/$VOLUME/System/Library/CoreServices/SystemVersion.plist" 2>/dev/null || echo '?')
            log "  state:          a macOS $v system volume -- installed"
            if [ -f "/Volumes/$VOLUME/etc/wk-image" ]; then
                log "  marker:         $(kv_field "/Volumes/$VOLUME/etc/wk-image" id)"
            else
                warn "  marker:         MISSING -- bench mode would report itself as host mode"
                log  "                  --provision writes it (run it in bench mode)"
            fi
        else
            warn "  state:          the volume exists but has no macOS on it"
            log  "                  --fetch then --install, or delete it and start again:"
            log  "                    sudo diskutil apfs deleteVolume '$VOLUME'"
        fi
    else
        log  "  state:          absent -- --create makes it"
    fi
    log  ""
    log  "  the way back, whole:  sudo diskutil apfs deleteVolume '$VOLUME'"
}


do_create() {
    local cont free
    cont=$(container_of_root)
    free=$(free_bytes_of_container)

    volume_exists && { info "'$VOLUME' already exists -- nothing to create"; return 0; }

    [ -n "$free" ] || die "could not read the container's free space -- refusing to add a volume blind"
    if [ "$free" -lt $((NEED_GB * 1000000000)) ]; then
        die "only $(gb "$free") GB free in $cont, and this needs $NEED_GB GB.
  Both installs share this container, so filling it stops the machine you work
  on, not just the one you measure on. Free space first, or set
  WK_BENCH_NEED_GB deliberately lower if you have costed it."
    fi

    # No size argument: a macOS upgrade that outgrows a quota fails looking
    # like a disk fault.
    info "adding APFS volume '$VOLUME' to $cont"
    [ -n "$DRY" ] || log "  sudo will ask for your password"
    run sudo diskutil apfs addVolume "$cont" APFS "$VOLUME"
    [ -n "$DRY" ] && return 0
    info "created. It is empty and boots nothing yet -- --fetch, then --install"
}

do_fetch() {
    local want="${1:-}"
    # softwareupdate, not a downloaded .app: the one source Apple personalises
    # correctly for the machine asking.
    if [ -z "$want" ]; then
        info "full installers this Mac is offered"
        softwareupdate --list-full-installers 2>&1 | sed 's/^/  /' >&2
        log ""
        log "  pick one:  wk bench mac-volume --fetch --version <version>"
        log "  match the host install's major version unless you mean not to --"
        log "  two different macOS versions is a second variable in every number."
        return 0
    fi
    info "fetching the macOS $want installer (this is tens of GB)"
    run softwareupdate --fetch-full-installer --full-installer-version "$want"
}

find_installer() {
    local app
    for app in /Applications/Install\ macOS*.app; do
        [ -x "$app/Contents/Resources/startosinstall" ] && printf '%s\n' "$app"  # one per line
    done | tail -1
}

do_install() {
    local app target="/Volumes/$VOLUME"
    volume_exists || die "'$VOLUME' does not exist yet -- --create first"
    volume_is_system && { info "'$VOLUME' already has macOS on it -- nothing to install"; return 0; }

    app=$(find_installer)
    [ -n "$app" ] || die "no 'Install macOS *.app' in /Applications -- --fetch first"

    cat >&2 <<EOF

  ------------------------------------------------------------------
   NEXT IS THE PART THAT NEEDS YOU, AND IT REBOOTS THIS MACHINE.

   startosinstall will:
     - ask for your password (sudo), and then
     - ask for it again, as --passprompt, for the volume-owner
       authorisation. On Apple Silicon the installer cannot
       personalise a volume without a credential from an account
       that owns it; sudo is not enough and root is not enough.
       Without this it fails at the licence banner with
       "failed to authorize for installation".
     - reboot into the installer and install macOS onto '$VOLUME'.
       That takes a half hour or so and the machine is unusable.

   When it comes back it boots the NEW install and runs Setup
   Assistant, which is also you:

     - ONE user, named  bench  -- not your own name, and not
       'justinmichaud'. A benchmark install is cattle: whose Mac
       it is attached to is not a property of the measurement, and
       an operator's account name baked into the artifact makes the
       rebuild recipe depend on who is running it. It is also what
       dotfiles/ssh/config's 'tolken-bench' stanza expects.
       Give it a password you are willing to type at a console.
     - skip Apple ID
     - skip FileVault: decryption is CPU work in the middle of a
       measurement
     - skip Siri and analytics

   Then, in that install:
     wk bench mac-volume --provision

   Installer: $app
   Target:    $target
  ------------------------------------------------------------------

EOF
    if [ -z "$DRY" ]; then
        confirm "start the install onto '$VOLUME' now?" || { log "nothing done"; return 0; }
    fi

    # --user/--passprompt are not optional: on Apple Silicon the installer
    # cannot personalise a volume without a credential from a volume owner,
    # and omitting them dies at the licence banner. --nointeraction is dropped
    # deliberately: it suppresses the prompt --passprompt needs.
    local admin="${WK_BENCH_ADMIN:-$(id -un)}"
    log "  authorising as '$admin' -- it must be a volume owner on this Mac"

    local pkg=""
    if [ -z "${WK_BENCH_NO_PKG:-}" ]; then
        info "building the provisioning package (so Setup Assistant never runs)"
        pkg=$(do_build_pkg) || die "could not build the provisioning package"
    else
        warn "WK_BENCH_NO_PKG set: Setup Assistant WILL run and will need a person"
    fi

    local sargs=(--volume "$target" --agreetolicense --user "$admin" --passprompt)
    [ -n "$pkg" ] && sargs+=(--installpackage "$pkg")
    run sudo "$app/Contents/Resources/startosinstall" "${sargs[@]}"
    return 0
}

# --- the package that makes Setup Assistant unnecessary ----------------------
# `startosinstall --installpackage` lays a package down before first boot, the
# only hook early enough to answer Setup Assistant.
#
#   /private/var/db/.AppleSetupDone   marks setup done, safe to write on an
#                                     offline volume
#   /Library/LaunchDaemons/…plist     runs the script below at first boot
#   /usr/local/libexec/…firstboot.sh  everything needing a *live* system,
#                                     including account creation: writing
#                                     dslocal records by hand on an offline
#                                     volume risks an account that exists
#                                     and cannot log in
#   /usr/local/share/wk-bench/        its payload: authorized_keys, wk-tools
#                                     (travels with the package -- the bench
#                                     install has no network route yet)

pkg_default_out() { echo "${TMPDIR:-/tmp}/wk-bench-provision.pkg"; }

do_build_pkg() {
    local out="${1:-$(pkg_default_out)}" root comp
    command -v pkgbuild >/dev/null 2>&1 || die "pkgbuild is missing (install the Command Line Tools)"
    command -v productbuild >/dev/null 2>&1 || die "productbuild is missing (install the Command Line Tools)"

    root=$(mktemp -d "${TMPDIR:-/tmp}/wk-bench-pkgroot.XXXXXX") || die "could not make a staging directory"
    comp="${TMPDIR:-/tmp}/wk-bench-component.pkg"

    install -d "$root/Library/LaunchDaemons" "$root/usr/local/libexec" \
               "$root/usr/local/share/wk-bench" "$root/private/var/db"

    # Two forms of Setup Assistant: .AppleSetupDone (the system assistant) and
    # .skipbuddy in the User Template (the per-user one). `defaults` keys
    # written into a live account do not survive, since auto-login builds a new
    # session on the next boot before those keys mean anything.
    : > "$root/private/var/db/.AppleSetupDone"
    for _lproj in English.lproj Non_localized; do
        install -d "$root/System/Library/User Template/$_lproj" 2>/dev/null || true
        : > "$root/System/Library/User Template/$_lproj/.skipbuddy"
    done
    install -d "$root/Library/User Template/English.lproj" 2>/dev/null || true
    : > "$root/Library/User Template/English.lproj/.skipbuddy"

    install -m 0755 "$WK_ROOT/bench/mac-bench-firstboot.sh" \
                    "$root/usr/local/libexec/wk-bench-firstboot.sh"
    # `--installpackage` lands the root tree as real files, so firstboot
    # sources this at the same relative path.
    install -m 0644 "$WK_ROOT/bench/mac-quiet-hosts.sh" \
                    "$root/usr/local/libexec/wk-bench-quiet-hosts.sh"

    # RunAtLoad with no KeepAlive, which would resurrect a job that has
    # deleted its own script. Not a LaunchAgent: creating the user is what this
    # does, so there is no session yet.
    cat > "$root/Library/LaunchDaemons/com.wk.bench-firstboot.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>              <string>com.wk.bench-firstboot</string>
  <key>ProgramArguments</key>   <array>
      <string>/bin/bash</string>
      <string>/usr/local/libexec/wk-bench-firstboot.sh</string>
  </array>
  <key>RunAtLoad</key>          <true/>
  <key>StandardOutPath</key>    <string>/var/log/wk-bench-firstboot.log</string>
  <key>StandardErrorPath</key>  <string>/var/log/wk-bench-firstboot.log</string>
</dict>
</plist>
PLIST

    # A known constant, not random and discarded: this repository is public,
    # so this password is public, and anyone who reaches this machine's
    # console or ssh gets passwordless sudo -- defensible only because the
    # machine is disposable and holds no user data. WK_BENCH_PASSWORD
    # overrides it.
    local pw="${WK_BENCH_PASSWORD:-benchbench}"
    local pwfile; pwfile="$(wk_state_dir)/bench-password"
    ensure_dir "$(wk_state_dir)" >/dev/null
    printf '%s' "$pw" > "$pwfile"; chmod 0600 "$pwfile"
    printf '%s' "$pw" > "$root/usr/local/share/wk-bench/password"
    chmod 0600 "$root/usr/local/share/wk-bench/password"
    log "  bench account password: '$pw' (constant; also at $pwfile)"

    # Tailscale, so the bench install has an identity that does not move: its
    # LAN address is a DHCP lease that changes between reboots. The auth key is
    # never in this repository -- `wk_tailscale_authkey` asks for it.
    local akey
    if akey=$(wk_tailscale_authkey); then
        install -m 0600 "$akey" "$root/usr/local/share/wk-bench/tailscale-authkey"
        log "  tailscale: auth key staged into the package"
    else
        warn "  no tailscale auth key, so the bench install will have no tailnet"
        warn "  identity: reachable only at whatever DHCP address it gets, and not"
        warn "  at all from a driver that reaches this Mac over the tailnet."
    fi

    # The Tailscale package, fetched rather than built. macOS ships as
    # `Tailscale-<ver>-macos.pkg`, the macsys/standalone variant with its own
    # LaunchDaemon, installed non-interactively by `installer -pkg … -target /`.
    # Kept next to the auth key so provisioning does not need the network on
    # the day the network is what is being fixed.
    local tspkg_cache="$HOME/.config/wk/Tailscale-macos.pkg"
    if [ ! -s "$tspkg_cache" ]; then
        log "  tailscale: fetching the macOS package (once)"
        # Scraped off the index rather than assembled from the JSON
        # `TarballsVersion`, which names the Linux tarball artefacts and has no
        # macOS version field.
        local tsname tsver
        tsname=$(curl -fsS 'https://pkgs.tailscale.com/stable/' 2>/dev/null \
                 | grep -oE 'Tailscale-[0-9.]+-macos\.pkg' | sort -u | tail -1)
        if [ -z "$tsname" ]; then
            tsver=$(curl -fsS 'https://pkgs.tailscale.com/stable/?mode=json' 2>/dev/null \
                    | sed -n 's/.*"TarballsVersion": *"\([^"]*\)".*/\1/p' | head -1)
            [ -n "$tsver" ] && tsname="Tailscale-$tsver-macos.pkg"
        fi
        if [ -n "$tsname" ]; then
            mkdir -p "$(dirname "$tspkg_cache")"
            curl -fsSL -o "$tspkg_cache.part" \
                "https://pkgs.tailscale.com/stable/$tsname" \
                && mv "$tspkg_cache.part" "$tspkg_cache" \
                || { rm -f "$tspkg_cache.part"; warn "  tailscale: download failed"; }
        else
            warn "  tailscale: could not determine which package to fetch"
        fi
    fi
    if [ -s "$tspkg_cache" ]; then
        install -m 0644 "$tspkg_cache" \
            "$root/usr/local/share/wk-bench/Tailscale-macos.pkg"
        log "  tailscale: package staged ($(du -h "$tspkg_cache" | awk '{print $1}'))"
    else
        warn "  tailscale: no package staged; firstboot will say so and carry on"
    fi

    if [ -f "$HOME/.ssh/authorized_keys" ]; then
        install -m 0644 "$HOME/.ssh/authorized_keys" "$root/usr/local/share/wk-bench/authorized_keys"
        log "  authorized_keys: $(grep -c . "$HOME/.ssh/authorized_keys" 2>/dev/null || echo 0) key(s) from this install"
    else
        warn "  no ~/.ssh/authorized_keys here, so the bench install will have none"
        warn "  -- it will boot, and nothing will be able to drive it"
    fi

    rsync -a --delete --exclude '.git/' --exclude '__pycache__/' --exclude '*.pyc' \
          "$WK_ROOT/" "$root/usr/local/share/wk-bench/wk-tools/" \
        || die "could not stage wk-tools into the package"

    # A postinstall starts the daemon on the boot it was installed on:
    # `--installpackage` packages land after launchd has scanned
    # /Library/LaunchDaemons, so a RunAtLoad daemon dropped then waits for the
    # next boot -- which, with Setup Assistant suppressed and no account
    # created, nothing triggers. `|| true` throughout: a failure here must not
    # fail the OS install.
    local scripts="${TMPDIR:-/tmp}/wk-bench-pkgscripts"
    rm -rf "$scripts"; install -d "$scripts"
    cat > "$scripts/postinstall" <<'POST'
#!/bin/bash
PLIST=/Library/LaunchDaemons/com.wk.bench-firstboot.plist
[ -f "$PLIST" ] || exit 0
launchctl bootstrap system "$PLIST" 2>/dev/null \
  || launchctl load -w "$PLIST" 2>/dev/null \
  || true
exit 0
POST
    chmod 0755 "$scripts/postinstall"

    rm -f "$comp" "$out"
    pkgbuild --root "$root" --scripts "$scripts" \
             --identifier com.wk.bench-provision --version 1 \
             --install-location / "$comp" >/dev/null || die "pkgbuild failed"
    # --installpackage documents "a package built with productbuild(1)"; a
    # bare component package is refused.
    productbuild --package "$comp" "$out" >/dev/null || die "productbuild failed"
    rm -rf "$root" "$comp" "$scripts"

    info "built $out ($(human_bytes "$(file_bytes "$out")"))"
    log  "  signature: $(pkgutil --check-signature "$out" 2>&1 | sed -n 's/^ *Status: *//p' | head -1)"
    printf '%s' "$out"
}

# The Wi-Fi identity the bench install needs to exist on the network, read from
# this Mac's own configuration and System keychain rather than stored in the
# repository. A passphrase that cannot be read is reported, never worked
# around.
write_wifi_conf() {
    local dest="$1" dev ssid psk
    dev=$(networksetup -listallhardwareports 2>/dev/null \
            | awk '/Hardware Port: Wi-Fi/{getline; print $2; exit}')
    [ -n "$dev" ] || { warn "  no Wi-Fi interface here; assuming the bench install has wired network"; return 0; }

    ssid=$(networksetup -getairportnetwork "$dev" 2>/dev/null | sed -n 's/^Current Wi-Fi Network: //p')
    if [ -z "$ssid" ]; then
        warn "  this Mac is not on a Wi-Fi network, so there is nothing to copy"
        return 0
    fi

    psk=$(sudo security find-generic-password -D "AirPort network password" \
              -a "$ssid" -w /Library/Keychains/System.keychain 2>/dev/null) || psk=""
    if [ -z "$psk" ]; then
        warn "  could not read the passphrase for '$ssid' from the System keychain"
        warn "  the bench install will have no network unless it is on ethernet"
        return 0
    fi

    printf 'WIFI_SSID=%s\nWIFI_PSK=%s\n' "$(sh_quote "$ssid")" "$(sh_quote "$psk")" \
        | sudo tee "$dest" >/dev/null
    sudo chmod 0600 "$dest"
    info "  wifi: '$ssid' written into the bench payload"
}

# --- everything this needs from a person, asked before anything starts --------
# The tailscale key is used inside `do_install` -> `do_build_pkg`, which on
# `--all` is after creating an APFS volume and downloading a ~15 GB installer.
gather_secrets() {
    info "what this needs from you, before anything is created or downloaded"

    if [ -s "${WK_TS_AUTHKEY:-$HOME/.config/wk/tailscale-authkey}" ]; then
        log "  tailscale auth key: already stored"
    else
        log "  The benchmark install needs its own tailnet identity. It is a"
        log "  different OS from this one, so it does not inherit this Mac's."
        log "  Without it the install is reachable only at a DHCP address on this"
        log "  LAN -- and not at all from a driver that reaches this Mac by its"
        log "  tailnet name, which is how 'wk bench mac-ab' is driven. That is the"
        log "  difference between an A/B you can watch and one you have to walk"
        log "  over and collect."
        wk_tailscale_authkey >/dev/null \
            || warn "  continuing without a tailnet identity for the bench install"
    fi

    log "  sudo: startosinstall needs a volume owner's password (once, at --install)"
}

# --- re-arming an install that is already there -------------------------------
# `--install` refuses once the volume carries macOS, so this re-arms first boot
# for provisioning that half-landed -- the account, marker and wk-tools arrived
# but Remote Login or autologin did not, and the daemon has already removed
# itself. Written from host mode onto the mounted volume.
do_repair() {
    local S="/Volumes/$VOLUME" D="/Volumes/$VOLUME - Data"
    volume_exists   || die "'$VOLUME' is not attached"
    volume_is_system || die "'$VOLUME' has no macOS on it -- --install first, not --repair"
    [ -d "$D" ] || die "no data volume at '$D'; is this a volume group?"

    info "re-arming first-boot provisioning on '$VOLUME'"

    # Remote Login, set offline as the launchd override it is; without ssh
    # every other repair needs a person. `false` means "not disabled".
    # Add-then-Set because PlistBuddy's Add fails when the key exists and Set
    # fails when it does not.
    #
    # The stderr redirects are on PlistBuddy only: wrapping the statement in
    # `run ... 2>/dev/null` would swallow `run`'s own "would run" line.
    local dis="$D/private/var/db/com.apple.xpc.launchd/disabled.plist"
    if [ -f "$dis" ]; then
        if [ -n "$DRY" ]; then
            log "  would set com.openssh.sshd=false (i.e. enabled) in $dis"
        else
            sudo /usr/libexec/PlistBuddy -c 'Add :com.openssh.sshd bool false' "$dis" >/dev/null 2>&1 \
              || sudo /usr/libexec/PlistBuddy -c 'Set :com.openssh.sshd false' "$dis" >/dev/null 2>&1 \
              || warn "  could not write the sshd override"
            if plutil -p "$dis" 2>/dev/null | grep -q '"com.openssh.sshd" => false'; then
                changed "remote login enabled on the bench volume"
            else
                warn "  the sshd override did not take -- the volume will have no ssh"
            fi
        fi
    else
        warn "  no launchd override file at $dis -- leaving ssh to the first-boot script"
    fi

    run sudo install -m 0755 "$WK_ROOT/bench/mac-bench-firstboot.sh" \
        "$S/usr/local/libexec/wk-bench-firstboot.sh"
    run sudo install -m 0644 "$WK_ROOT/bench/mac-quiet-hosts.sh" \
        "$S/usr/local/libexec/wk-bench-quiet-hosts.sh"

    local pw="${WK_BENCH_PASSWORD:-benchbench}"
    local pwfile; pwfile="$(wk_state_dir)/bench-password"
    if [ -n "$DRY" ]; then
        log "  would write the bench password ('$pw') into the volume's payload"
    else
        ensure_dir "$(wk_state_dir)" >/dev/null
        printf '%s' "$pw" > "$pwfile"; chmod 0600 "$pwfile"
        printf '%s' "$pw" | sudo tee "$S/usr/local/share/wk-bench/password" >/dev/null
        sudo chmod 0600 "$S/usr/local/share/wk-bench/password"
        info "  bench account password: '$pw'"
    fi

    local sroot="/Volumes/$VOLUME - Data/private/var/wk"
    [ -d "/Volumes/$VOLUME - Data" ] || sroot="/Volumes/$VOLUME/var/wk"
    if [ -n "$DRY" ]; then
        log "  would create $sroot owned by uid 501 (the first account on both installs)"
    else
        sudo install -d -o 501 -g 20 -m 0755 "$sroot" \
            && changed "staging root ready: $sroot" \
            || warn "  could not create $sroot -- 'wk bench stage' will fail"
    fi

    if [ -n "$DRY" ]; then
        log "  would copy this Mac's Wi-Fi identity into the bench payload"
    else
        write_wifi_conf "$S/usr/local/share/wk-bench/wifi.conf"
    fi

    # The only route to a tailnet identity once --install refuses to run twice.
    local akey
    if akey=$(wk_tailscale_authkey); then
        if [ -n "$DRY" ]; then
            log "  would stage the tailscale auth key into the volume payload"
        else
            sudo install -m 0600 "$akey" "$S/usr/local/share/wk-bench/tailscale-authkey" \
                && changed "tailscale auth key staged" \
                || warn "  could not stage the tailscale auth key"
        fi
    else
        warn "  no tailscale auth key: this install will stay unobservable"
    fi
    local tspkg="$HOME/.config/wk/Tailscale-macos.pkg"
    if [ -s "$tspkg" ]; then
        if [ -n "$DRY" ]; then
            log "  would stage $(basename "$tspkg") into the volume payload"
        else
            sudo install -m 0644 "$tspkg" \
                "$S/usr/local/share/wk-bench/Tailscale-macos.pkg" \
                && changed "tailscale package staged" \
                || warn "  could not stage the tailscale package"
        fi
    elif [ -s "${WK_TS_AUTHKEY:-$HOME/.config/wk/tailscale-authkey}" ]; then
        warn "  a key is configured but $tspkg is not cached."
        warn "  './setup --stage benchkey' fetches it (no compiler involved)."
    fi

    if [ -n "$DRY" ]; then
        log "  would write $S/Library/LaunchDaemons/com.wk.bench-firstboot.plist"
    else
        sudo tee "$S/Library/LaunchDaemons/com.wk.bench-firstboot.plist" >/dev/null <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>              <string>com.wk.bench-firstboot</string>
  <key>ProgramArguments</key>   <array>
      <string>/bin/bash</string>
      <string>/usr/local/libexec/wk-bench-firstboot.sh</string>
  </array>
  <key>RunAtLoad</key>          <true/>
  <key>StandardOutPath</key>    <string>/var/log/wk-bench-firstboot.log</string>
  <key>StandardErrorPath</key>  <string>/var/log/wk-bench-firstboot.log</string>
</dict>
</plist>
PLIST
        sudo chown root:wheel "$S/Library/LaunchDaemons/com.wk.bench-firstboot.plist"
        sudo chmod 0644       "$S/Library/LaunchDaemons/com.wk.bench-firstboot.plist"
        changed "re-armed the first-boot daemon"
    fi

    log ""
    info "boot '$VOLUME' once more; it will finish provisioning and reboot itself"
    log  "  the account password, if it is ever needed at the console:"
    log  "    $pwfile"
}


do_provision() {
    # Run in host mode this would write a bench-mode marker onto the
    # workstation, which `wk bench staged` would measure with a desktop
    # underneath and label bench_host=image.
    if [ ! -d /System/Volumes/Data ] || [ "$(diskutil info -plist / | plutil -extract VolumeName raw - 2>/dev/null || true)" != "$VOLUME" ]; then
        local here
        here=$(diskutil info -plist / | plutil -extract VolumeName raw - 2>/dev/null || echo '?')
        die "this is running on '$here', not on '$VOLUME'.
  --provision writes the bench-mode marker, and writing it on the workstation
  would make host mode claim to be bench mode. Boot '$VOLUME' first."
    fi

    info "provisioning '$VOLUME' as the benchmark install"

    # 1. The id= line is the only thing that tells wk bench mode answered.
    local marker=/etc/wk-image
    if [ -f "$marker" ]; then
        unchanged "marker $marker: $(kv_field "$marker" id)"
    else
        if [ -n "$DRY" ]; then
            log "  would write $marker:  id=$PROFILE-$(date -u +%Y-%m) / profile=$PROFILE"
        else
            printf 'id=%s-%s\nprofile=%s\n' "$PROFILE" "$(date -u +%Y-%m)" "$PROFILE" \
                | sudo tee "$marker" >/dev/null
            changed "wrote $marker"
        fi
    fi

    # 2. The permanent half of quieting; `wk quiesce` does the per-run half.
    #    Read back rather than trusted: `softwareupdate --schedule off` does
    #    not stick in a guest.
    info "quieting the install permanently"
    run sudo mdutil -i off -a          >/dev/null 2>&1 || warn "  spotlight: could not turn indexing off"
    run sudo softwareupdate --schedule off >/dev/null 2>&1 || warn "  updates: could not turn the schedule off"
    run sudo defaults write /Library/Preferences/com.apple.SoftwareUpdate AutomaticCheckEnabled -bool false 2>/dev/null || true
    run sudo pmset -a lowpowermode 0 sleep 0 displaysleep 0 disksleep 0 >/dev/null 2>&1 || true
    run sudo tmutil disablelocal      >/dev/null 2>&1 || true
    run defaults -currentHost write com.apple.screensaver idleTime 0 2>/dev/null || true

    log "  read back:"
    log "    spotlight: $(mdutil -a -s 2>/dev/null | tr '\n' ' ' | sed 's/  */ /g')"
    log "    updates:   $(softwareupdate --schedule 2>&1 | tail -1)"
    log "    lowpower:  $(pmset -g 2>/dev/null | awk '/lowpowermode/{print $2}')"
    log "    filevault: $(fdesetup status 2>/dev/null | head -1)"
    # `grep -c` prints 0 and exits 1 when it counts nothing, so `|| echo 0`
    # would append a second zero on its own line.
    log "    timemachine destinations: $(tmutil destinationinfo 2>/dev/null | grep -c '^Name' || true)"

    # 2b. The scanner check above can only report; this is the fix. The same
    #     function mac-bench-firstboot.sh calls at first boot. It never calls
    #     sudo itself, so it also runs under a plain test as a non-root user
    #     against a temp file.
    . "$WK_ROOT/bench/mac-quiet-hosts.sh"
    if [ -n "$DRY" ]; then
        info "software-update endpoint denial in /etc/hosts"
        wk_bench_hosts_apply /etc/hosts 1
    elif wk_bench_hosts_present /etc/hosts; then
        unchanged "hosts: update endpoints already denied"
    else
        if sudo bash -c ". $(sh_quote "$WK_ROOT/bench/mac-quiet-hosts.sh"); wk_bench_hosts_apply /etc/hosts"; then
            changed "hosts: update endpoints denied"
        else
            warn "  hosts: could not deny update endpoints in /etc/hosts (see above)"
        fi
    fi

    # 3. Apple's /usr/bin/python3 has pyobjc; a Homebrew python3 does not.
    if /usr/bin/python3 -c 'import objc' 2>/dev/null; then
        unchanged "/usr/bin/python3 has pyobjc"
    else
        warn "/usr/bin/python3 cannot 'import objc' -- run-benchmark's prepare_env will fail"
        log  "  xcode-select --install   (Command Line Tools; it is a GUI prompt)"
    fi

    # 4. scipy is only for `wk bench compare` here; host mode can compare too.
    if /usr/bin/python3 -c 'import scipy' 2>/dev/null; then
        unchanged "scipy present (wk bench compare works here)"
    else
        log "scipy absent -- optional: /usr/bin/python3 -m pip install --user scipy"
        log "  (only needed to run 'wk bench compare' in bench mode)"
    fi

    log ""
    info "still yours to check, and each is a run that otherwise looks like a hang:"
    log "  * one user, logged in AT THE CONSOLE. A browser driven over ssh with"
    log "    nobody at the screen has nowhere to draw."
    log "  * this install's own ~/.ssh/authorized_keys -- two installs, two files,"
    log "    and it is the second that gets forgotten."
    log "  * no login items, Siri and analytics off."
    log ""
    log "  then, from the driving machine:  wk bench mac <ws> --preflight"
}

# --- dispatch ----------------------------------------------------------------
# --dry-run is parsed out independently of the action, so it composes with all.

ACTION=""
VER=""
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)   DRY=1; shift ;;
        --version)   VER="${2:-}"; shift 2 ;;
        --report|--status|--create|--fetch|--install|--provision|--all|--build-pkg|--repair)
                     [ -z "$ACTION" ] || die "one action at a time (got $ACTION and $1)"
                     ACTION="$1"; shift ;;
        -h|--help)   usage ;;
        *)           die "unknown option: $1" ;;
    esac
done

[ -n "$DRY" ] && info "--dry-run: nothing on this machine will be changed"

case "${ACTION:---report}" in
    --report|--status) report ;;
    --create)    do_create ;;
    --fetch)     do_fetch "$VER" ;;
    --install)   gather_secrets; do_install ;;
    --provision) do_provision ;;
    --build-pkg) do_build_pkg >/dev/null ;;
    --repair)    do_repair ;;
    --all)
        gather_secrets
        do_create
        if [ -z "$(find_installer)" ]; then
            [ -n "$VER" ] || die "no installer downloaded and no --version given.
  'wk bench mac-volume --fetch' lists what this Mac is offered; then
  'wk bench mac-volume --all --version <v>' does the rest in one go."
            do_fetch "$VER"
        else
            info "installer already downloaded: $(find_installer)"
        fi
        do_install

        # Not a no-op when everything exists: three steps that each say
        # "nothing to do" can leave a machine with no tailnet identity.
        # Re-arming first boot is the one route left that can still lay down
        # the tailscale payload; --install refuses to run twice.
        if volume_exists && volume_is_system; then
            info "the volume is installed; completing provisioning"
            log  "  (--install cannot run twice, so anything still missing is"
            log  "   re-armed through first boot instead)"
            do_repair
        fi
        ;;
esac
