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
# Runs *on the Mac*, unlike `wk bench mac`. Every step here acts on the machine
# it runs on, needs real sudo, and one of them reboots -- so there is nothing to
# be gained by driving it from elsewhere and a live ssh session to lose.
#
# WHY A SECOND APFS VOLUME AND NOT AN EXTERNAL SSD
#
# docs/HANDOFF-mac-perf-mode.md left this open as a measurement decision rather
# than a convenience one, and it is: an external Thunderbolt SSD is simple and
# removable but is *not the storage the machine normally runs on*, so anything
# the benchmark touches on disk is measured against different hardware than host
# mode uses. A second volume in the internal container removes that variable,
# which is the one that cannot be corrected for afterwards. Chosen 2026-08-22.
#
# What it costs, recorded because it is the half that is easy to forget:
#
#   * the two installs share a container, so they share free space. APFS
#     volumes have no fixed size -- the benchmark install growing is the host
#     install shrinking, and a container that fills affects the machine you
#     work on, not just the one you measure on. Hence the headroom refusal
#     below rather than a warning.
#   * they share the SSD's wear and its thermal behaviour. That is the point
#     (it is what makes them comparable) but it also means a long benchmark run
#     is heating the disk the workstation lives on.
#   * `diskutil apfs deleteVolume` is the way back and it is one command, which
#     is the main practical argument for this shape over an install that has to
#     be re-made from scratch on a disk you have to find.
#
# WHAT THIS DELIBERATELY DOES NOT DO
#
# It does not go into the `wk quiesce` privileged helper. That helper is a fixed
# allowlist granted NOPASSWD forever (admin/wk-quiesce-priv), and it stays
# auditable precisely because everything in it is a small, reversible, per-run
# action. "Create a volume" and "run startosinstall as root" are neither: they
# are once-per-machine provisioning, and granting them unconditionally would
# trade a real root escalation for saving one password prompt on a command that
# is run once. So this asks for sudo like any other administrative script.

set -euo pipefail
WK_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$WK_ROOT/lib/common.sh"

VOLUME="${WK_BENCH_VOLUME:-WK Bench}"
PROFILE=perf-macos-tolken

# Room to leave. A macOS install is ~25 GB and the staged products are ~2 GB,
# but the figure that matters is not the sum: it is how much the *host* install
# still has to work in afterwards, because the container is shared. 120 GB is
# "the benchmark install fits and the workstation does not notice", and the
# refusal is fatal rather than a warning because the failure mode is a full
# disk on the machine you were using, discovered mid-build.
NEED_GB="${WK_BENCH_NEED_GB:-120}"

# --dry-run, on every action, because the actions here are a disk being
# repartitioned and an operating system being installed: "show me what you would
# do" is not a convenience on a command like that, it is the only way to read it
# before it is too late to. The read-only checks still run under --dry-run --
# a plan that does not tell you it would refuse on free space is not a plan.
DRY=""

# Every mutating command in this file goes through here and nothing calls sudo
# directly, so there is exactly one place that decides whether an action
# happens. A second decision point is how a --dry-run grows a command that
# runs anyway.
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

# The guard is here and not at the top so that --help and --explain answer
# anywhere: a question *about* a command has to be answerable from the machine
# you are reading the docs on, which for this repository is usually not the Mac.
case "${1:-}" in
    -h|--help) usage ;;
esac
is_macos || die "this runs on the Mac itself -- it acts on the machine's own disk.
  From another machine, the lane that drives it is: wk bench mac <ws>"

# --- reading the disk --------------------------------------------------------
#
# plutil -extract against `diskutil info -plist`, not awk against the human
# output: the labels in the human form differ between macOS versions and
# between disk kinds, and this has to be right about which container it is
# about to add a volume to.

dk() { diskutil info -plist "$1" 2>/dev/null; }

dk_field() {
    # $1 target, $2 key. Empty (not an error) when the key is absent, so a
    # caller can distinguish "no such key on this disk" from a failed command.
    dk "$1" | plutil -extract "$2" raw - 2>/dev/null || true
}

container_of_root() {
    local c
    c=$(dk_field / APFSContainerReference)
    [ -n "$c" ] || die "the boot volume is not on an APFS container -- this shape does not apply"
    printf '%s' "$c"
}

free_bytes_of_container() {
    # The container's free space, which is the number both installs draw on.
    # APFSContainerFree on the *root volume* is that figure; a volume's own
    # FreeSpace is the same pool seen from one volume and is not independent.
    dk_field / APFSContainerFree
}

# Both of these read the real disk, under --dry-run exactly as otherwise.
#
# An earlier version had --dry-run *simulate* the volume its own --create step
# would have made, so that `--all --dry-run` could walk the whole chain. That
# was wrong and is worth recording as wrong: it made the dry run a second code
# path with its own model of the disk, and a dry run that reasons about a
# fictional machine can pass while the real path fails -- which is precisely
# the evidence it was supposed to provide. --dry-run is the real path with
# mutations suppressed, nothing else, so a precondition that is not met is
# reported the same way in both.
volume_exists() { diskutil info "$VOLUME" >/dev/null 2>&1; }

# Mounted and actually a macOS *system* volume, which is the distinction
# boot/mac-volume.sh makes for the same reason: an empty formatted volume with
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
    if volume_exists; then
        if volume_is_system; then
            local v
            v=$(/usr/libexec/PlistBuddy -c 'Print :ProductUserVisibleVersion' \
                    "/Volumes/$VOLUME/System/Library/CoreServices/SystemVersion.plist" 2>/dev/null || echo '?')
            log "  state:          a macOS $v system volume -- installed"
            if [ -f "/Volumes/$VOLUME/etc/wk-image" ]; then
                log "  marker:         $(sed -n 's/^id=//p' "/Volumes/$VOLUME/etc/wk-image")"
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

# --- the steps ---------------------------------------------------------------

do_create() {
    local cont free
    cont=$(container_of_root)
    free=$(free_bytes_of_container)

    volume_exists && { info "'$VOLUME' already exists -- nothing to create"; return 0; }

    # Fatal, not a warning: see NEED_GB above. The disk this would fill is the
    # one the workstation runs on.
    [ -n "$free" ] || die "could not read the container's free space -- refusing to add a volume blind"
    if [ "$free" -lt $((NEED_GB * 1000000000)) ]; then
        die "only $(gb "$free") GB free in $cont, and this needs $NEED_GB GB.
  Both installs share this container, so filling it stops the machine you work
  on, not just the one you measure on. Free space first, or set
  WK_BENCH_NEED_GB deliberately lower if you have costed it."
    fi

    # No size argument on purpose. APFS volumes in a container share its space,
    # so a quota here would be a limit on the benchmark install with no benefit
    # to the host install -- and a macOS upgrade that outgrows the quota fails
    # in a way that reads as a disk fault.
    info "adding APFS volume '$VOLUME' to $cont"
    [ -n "$DRY" ] || log "  sudo will ask for your password"
    run sudo diskutil apfs addVolume "$cont" APFS "$VOLUME"
    [ -n "$DRY" ] && return 0
    info "created. It is empty and boots nothing yet -- --fetch, then --install"
}

do_fetch() {
    local want="${1:-}"
    # softwareupdate, not a downloaded .app or an installer package: this is
    # the one source Apple personalises correctly for the machine asking, which
    # for Apple Silicon is the whole game.
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
    # Newest first, so a machine with two installer apps uses the later one
    # rather than whichever the shell globbed first.
    for app in /Applications/Install\ macOS*.app; do
        # One per line: without the newline two installer apps would come back
        # concatenated into a single unusable path.
        [ -x "$app/Contents/Resources/startosinstall" ] && printf '%s\n' "$app"
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
    # No confirmation under --dry-run: the flag is already the answer to "are
    # you sure", and prompting would make the one form of this command that
    # changes nothing the one form that cannot be scripted.
    if [ -z "$DRY" ]; then
        confirm "start the install onto '$VOLUME' now?" || { log "nothing done"; return 0; }
    fi

    # --agreetolicense and --nointeraction cover the prompts that are Apple's
    # boilerplate. They do NOT cover the credential above, which is the point
    # of the banner: a run that looks stuck is usually waiting for it.
    # --user and --passprompt are not optional, and leaving them off is what
    # this command did first: it reached the licence banner and then died with
    # "failed to authorize for installation. Provide a password with
    # --stdinpass or --passprompt."
    #
    # On Apple Silicon the installer cannot personalise a volume without a
    # credential from a *volume owner* -- an account that was present when the
    # machine's LocalPolicy was set. sudo is not enough and root is not enough;
    # this is the Secure Enclave's question, not the filesystem's.
    #
    # --nointeraction is dropped here on purpose. It exists to suppress prompts
    # for scripted runs, which is the opposite of what --passprompt needs: the
    # whole point is that a person types the password once, at this terminal.
    # Keeping both would be asking for a prompt and forbidding it in the same
    # command line.
    #
    # WK_BENCH_ADMIN overrides the account, for a machine whose volume owner is
    # not the account driving the install.
    local admin="${WK_BENCH_ADMIN:-$(id -un)}"
    log "  authorising as '$admin' -- it must be a volume owner on this Mac"

    # The provisioning package, built fresh every time rather than cached: it
    # carries this machine's authorized_keys and a copy of wk-tools, and a stale
    # one would install a stale tree onto a volume nobody looks inside.
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
#
# `startosinstall --installpackage` lays a package down on the target volume as
# part of the install, before its first boot. That is the only hook that exists
# early enough to answer Setup Assistant, because Setup Assistant is the *first*
# thing the new system runs.
#
# What the package carries, and why it is split the way it is:
#
#   /private/var/db/.AppleSetupDone   the marker whose presence means "this
#                                     install has been through setup". A plain
#                                     empty file, and the only thing here that
#                                     is genuinely safe to do to a volume that
#                                     is not running.
#   /Library/LaunchDaemons/…plist     runs the script below at first boot
#   /usr/local/libexec/…firstboot.sh  everything that needs a *live* system
#   /usr/local/share/wk-bench/        its payload: authorized_keys, and wk-tools
#
# The split is the point. Creating a user on an offline volume means writing
# dslocal records by hand and hoping they match what opendirectoryd would have
# written -- the classic way to get an account that exists and cannot log in. So
# the package does the one offline-safe thing and defers the rest to a daemon
# that runs once, on a booted system, with sysadminctl and systemsetup available.
#
# wk-tools travels *in* the package rather than being fetched, because the bench
# install has no reason to have a network route or git, and `wk bench staged` is
# the thing that runs the measurement -- it cannot be the thing that needs
# provisioning first.

pkg_default_out() { echo "${TMPDIR:-/tmp}/wk-bench-provision.pkg"; }

do_build_pkg() {
    local out="${1:-$(pkg_default_out)}" root comp
    command -v pkgbuild >/dev/null 2>&1 || die "pkgbuild is missing (install the Command Line Tools)"
    command -v productbuild >/dev/null 2>&1 || die "productbuild is missing (install the Command Line Tools)"

    root=$(mktemp -d "${TMPDIR:-/tmp}/wk-bench-pkgroot.XXXXXX") || die "could not make a staging directory"
    comp="${TMPDIR:-/tmp}/wk-bench-component.pkg"

    install -d "$root/Library/LaunchDaemons" "$root/usr/local/libexec" \
               "$root/usr/local/share/wk-bench" "$root/private/var/db"

    # Setup Assistant, answered by not being asked -- in both of its forms,
    # which is the part this got wrong twice.
    #
    #   .AppleSetupDone   suppresses the *system* assistant (language, region,
    #                     account creation). One file, well known.
    #   .skipbuddy        suppresses the *per-user* assistant -- Apple ID,
    #                     Screen Time, analytics, "Update Mac automatically" --
    #                     for every user created from the template afterwards.
    #
    # The second one is the fix for the pane that kept coming back. Writing
    # `defaults` keys into the live account's ~/Library/Preferences does not
    # survive, because auto-login builds a *new* session on the next boot and
    # the assistant runs before those keys mean anything. Seeding the User
    # Template means the account never asks in the first place.
    # (toru173/Skipping-the-macOS-First-Run-Setup-Assistant; macadmins docs.)
    : > "$root/private/var/db/.AppleSetupDone"
    for _lproj in English.lproj Non_localized; do
        install -d "$root/System/Library/User Template/$_lproj" 2>/dev/null || true
        : > "$root/System/Library/User Template/$_lproj/.skipbuddy"
    done
    install -d "$root/Library/User Template/English.lproj" 2>/dev/null || true
    : > "$root/Library/User Template/English.lproj/.skipbuddy"

    install -m 0755 "$WK_ROOT/bench/mac-bench-firstboot.sh" \
                    "$root/usr/local/libexec/wk-bench-firstboot.sh"

    # RunAtLoad with no KeepAlive: it runs once per boot and removes itself on
    # success, so a KeepAlive here would resurrect a job that has deleted its own
    # script. Not LaunchAgent: there is no user session yet -- creating the user
    # is what this does.
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

    # The bench account's password: constant, simple, and written down.
    #
    # It was random and discarded, on the theory that autologin meant nobody
    # would ever need it. Autologin then failed and the machine sat at a login
    # window whose password existed nowhere -- twice. A recovery path that only
    # works when nothing has gone wrong is not a recovery path.
    #
    # So it is a known constant. What that costs, stated plainly rather than
    # hidden: this repository is public, so this password is public, and anyone
    # who reaches this machine's console or ssh gets an account with passwordless
    # sudo. It is defensible only because of what the machine is -- a disposable
    # benchmark install holding a staged build and a result file, no user data,
    # no keys, deletable with one `diskutil apfs deleteVolume`. It would be
    # indefensible on a workstation, and nothing here should be copied to one.
    # WK_BENCH_PASSWORD overrides it for anyone who wants otherwise.
    local pw="${WK_BENCH_PASSWORD:-benchbench}"
    local pwfile; pwfile="$(wk_state_dir)/bench-password"
    ensure_dir "$(wk_state_dir)" >/dev/null
    printf '%s' "$pw" > "$pwfile"; chmod 0600 "$pwfile"
    printf '%s' "$pw" > "$root/usr/local/share/wk-bench/password"
    chmod 0600 "$root/usr/local/share/wk-bench/password"
    log "  bench account password: '$pw' (constant; also at $pwfile)"

    # Tailscale, so the bench install has an identity that does not move.
    #
    # This is the fix for the single largest time sink in bringing this volume
    # up: the install had no stable address. It is a different operating system
    # from the host, so it has no tailnet identity of its own, and its LAN
    # address is a DHCP lease that changed between reboots -- which left the ssh
    # alias pointing at the *host* install and every probe answering for the
    # wrong machine.
    #
    # `tailscaled` rather than the GUI variants, and that is the whole point:
    # Tailscale's own comparison lists it as the only macOS variant that can
    # "run before login". A benchmark machine that is only reachable once
    # somebody has logged in is not reachable.
    #
    # The auth key is a secret and therefore never in this repository: it comes
    # from ~/.config/wk/tailscale-authkey on the driving Mac, which is
    # machine-local state exactly like the Wi-Fi passphrase.
    local akey="$HOME/.config/wk/tailscale-authkey"
    if [ -s "$akey" ]; then
        install -m 0600 "$akey" "$root/usr/local/share/wk-bench/tailscale-authkey"
        log "  tailscale: auth key staged into the package"
    else
        warn "  no $akey -- the bench install will have no tailnet identity,"
        warn "  so it will only be reachable at whatever DHCP address it gets"
    fi

    # Whoever can already reach host mode should be able to reach bench mode.
    # Taken from this install rather than named here, so the bench volume
    # inherits the machine's existing answer instead of a second list that has
    # to be kept in step -- and so nothing in the repository hands out access.
    if [ -f "$HOME/.ssh/authorized_keys" ]; then
        install -m 0644 "$HOME/.ssh/authorized_keys" "$root/usr/local/share/wk-bench/authorized_keys"
        log "  authorized_keys: $(grep -c . "$HOME/.ssh/authorized_keys" 2>/dev/null || echo 0) key(s) from this install"
    else
        warn "  no ~/.ssh/authorized_keys here, so the bench install will have none"
        warn "  -- it will boot, and nothing will be able to drive it"
    fi

    # --exclude .git for the same reason `wk version` hashes without it: it is
    # not part of what runs, and it is most of the size.
    rsync -a --delete --exclude '.git/' --exclude '__pycache__/' --exclude '*.pyc' \
          "$WK_ROOT/" "$root/usr/local/share/wk-bench/wk-tools/" \
        || die "could not stage wk-tools into the package"

    # A postinstall that starts the daemon on the boot it was installed on.
    #
    # This is the difference between provisioning and a machine that sits at a
    # login window with no accounts. `--installpackage` packages are laid down
    # during the *boot-install* phase of the first boot -- install.log shows the
    # source as `.com.apple.templatemigration.boot-install/` -- which is after
    # launchd has already scanned /Library/LaunchDaemons. So a RunAtLoad daemon
    # dropped then does not run until the *next* boot, and with Setup Assistant
    # suppressed and no account created there is nothing to trigger a next boot.
    # Measured 2026-08-22: the volume booted, ran for minutes (wifi.log, asl),
    # and never opened the daemon's StandardOutPath.
    #
    # The postinstall runs while that system is up, so it can bootstrap the job
    # itself. `|| true` throughout: a failure here must not fail the OS install,
    # because the daemon still exists and the next boot would run it anyway --
    # this makes the common case need one less reboot, it is not load-bearing.
    local scripts="${TMPDIR:-/tmp}/wk-bench-pkgscripts"
    rm -rf "$scripts"; install -d "$scripts"
    cat > "$scripts/postinstall" <<'POST'
#!/bin/bash
# Installed into a *running* system during boot-install; start the first-boot
# job now rather than waiting for a reboot that may never come.
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
    # productbuild, because --installpackage documents "a package built with
    # productbuild(1)" -- a bare component package is not a distribution and is
    # refused.
    productbuild --package "$comp" "$out" >/dev/null || die "productbuild failed"
    rm -rf "$root" "$comp" "$scripts"

    info "built $out ($(human_bytes "$(file_bytes "$out")"))"
    log  "  signature: $(pkgutil --check-signature "$out" 2>&1 | sed -n 's/^ *Status: *//p' | head -1)"
    printf '%s' "$out"
}

# The Wi-Fi identity the bench install needs to exist on the network.
#
# Not in the repository, and not invented here: it is read from this Mac's own
# configuration and its System keychain, which is the only place it legitimately
# lives. The same reasoning as host/linux/rpi5/rpi5.conf being gitignored -- a
# site's Wi-Fi credentials are machine-local state, and a public repository is
# the wrong place for them.
#
# Reading the passphrase needs root (it is in /Library/Keychains/System.keychain)
# and therefore the sudo the caller is already providing. If it cannot be read,
# that is reported rather than worked around: an install with no network is the
# failure this whole function exists to prevent, and a silent one wastes a boot.
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

# --- re-arming an install that is already there -------------------------------
#
# `--install` refuses once the volume carries macOS, which is right: reinstalling
# to fix provisioning would be half an hour to re-run a script. This puts the
# first-boot machinery back on a volume that already booted, so the next boot
# re-provisions it.
#
# Needed because the first attempt half-worked: the account, sudoers, marker,
# quieting and wk-tools all landed, but Remote Login and autologin did not -- and
# the daemon had already removed itself, so nothing would retry. Everything here
# is written from host mode onto the mounted volume, which is the one moment the
# bench install is editable without being running.
do_repair() {
    local S="/Volumes/$VOLUME" D="/Volumes/$VOLUME - Data"
    volume_exists   || die "'$VOLUME' is not attached"
    volume_is_system || die "'$VOLUME' has no macOS on it -- --install first, not --repair"
    [ -d "$D" ] || die "no data volume at '$D'; is this a volume group?"

    info "re-arming first-boot provisioning on '$VOLUME'"

    # Remote Login, set offline as the launchd override it actually is. This is
    # the one that matters: without ssh every other repair needs a person.
    local dis="$D/private/var/db/com.apple.xpc.launchd/disabled.plist"
    # `false` means "not disabled". The running host has exactly this entry, and
    # the bench volume has none at all, which is macOS's default: Remote Login
    # off. Add-then-Set because PlistBuddy's Add fails when the key exists and
    # Set fails when it does not.
    #
    # The stderr redirects are on PlistBuddy only, not on the whole statement:
    # an earlier version wrapped these in `run ... 2>/dev/null`, which swallowed
    # `run`'s own "would run" line and made --dry-run silently skip printing the
    # single most important step in this function.
    if [ -f "$dis" ]; then
        if [ -n "$DRY" ]; then
            log "  would set com.openssh.sshd=false (i.e. enabled) in $dis"
        else
            sudo /usr/libexec/PlistBuddy -c 'Add :com.openssh.sshd bool false' "$dis" >/dev/null 2>&1 \
              || sudo /usr/libexec/PlistBuddy -c 'Set :com.openssh.sshd false' "$dis" >/dev/null 2>&1 \
              || warn "  could not write the sshd override"
            # Read back: this is the one change that decides whether the machine
            # can be reached at all afterwards.
            if plutil -p "$dis" 2>/dev/null | grep -q '"com.openssh.sshd" => false'; then
                changed "remote login enabled on the bench volume"
            else
                warn "  the sshd override did not take -- the volume will have no ssh"
            fi
        fi
    else
        warn "  no launchd override file at $dis -- leaving ssh to the first-boot script"
    fi

    # The script and its daemon, fresh, plus the payload bits the script reads.
    run sudo install -m 0755 "$WK_ROOT/bench/mac-bench-firstboot.sh" \
        "$S/usr/local/libexec/wk-bench-firstboot.sh"

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

    # The staging root, created from this side because the volume is not running.
    # `wk bench stage` writes here unattended over ssh, so it cannot be the
    # thing that asks for a password -- and /private/var is root-owned, so
    # somebody has to. That somebody is this command, once, rather than a line
    # in a runbook that a person has to remember before every re-provision.
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

    # Re-create the daemon the last run deleted. RunAtLoad only; it removes
    # itself again when it finishes.
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

# --- provisioning the install, from inside it --------------------------------

do_provision() {
    # Refuse to provision the wrong install. Running this in host mode would
    # write a bench-mode marker onto the workstation, and `wk bench staged`
    # would then happily measure the machine with a desktop underneath it and
    # label the result bench_host=image -- a wrong number that looks right,
    # which is the only kind worth refusing outright.
    if [ ! -d /System/Volumes/Data ] || [ "$(diskutil info -plist / | plutil -extract VolumeName raw - 2>/dev/null || true)" != "$VOLUME" ]; then
        local here
        here=$(diskutil info -plist / | plutil -extract VolumeName raw - 2>/dev/null || echo '?')
        # Refused under --dry-run too. Where this runs is the one thing the
        # command is *about*, so letting a dry run past it would be simulating
        # the machine rather than describing it.
        die "this is running on '$here', not on '$VOLUME'.
  --provision writes the bench-mode marker, and writing it on the workstation
  would make host mode claim to be bench mode. Boot '$VOLUME' first."
    fi

    info "provisioning '$VOLUME' as the benchmark install"

    # 1. The marker. The id= line is the only thing that tells wk bench mode
    #    answered; without it `wk bench staged` refuses, correctly.
    local marker=/etc/wk-image
    if [ -f "$marker" ]; then
        unchanged "marker $marker: $(sed -n 's/^id=//p' "$marker")"
    else
        if [ -n "$DRY" ]; then
            log "  would write $marker:  id=$PROFILE-$(date -u +%Y-%m) / profile=$PROFILE"
        else
            printf 'id=%s-%s\nprofile=%s\n' "$PROFILE" "$(date -u +%Y-%m)" "$PROFILE" \
                | sudo tee "$marker" >/dev/null
            changed "wrote $marker"
        fi
    fi

    # 2. The permanent half of quieting. `wk quiesce` does the per-run half and
    #    measures the result; these are the settings that have to be true of the
    #    install itself, and the note in the handoff is that
    #    `softwareupdate --schedule off` did not stick in a guest -- so it is
    #    set here and then read back rather than trusted.
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
    # `grep -c` prints 0 *and* exits 1 when it counts nothing, so the old
    # `|| echo 0` appended a second zero on its own line.
    log "    timemachine destinations: $(tmutil destinationinfo 2>/dev/null | grep -c '^Name' || true)"

    # 3. The python the benchmark driver needs. Apple's /usr/bin/python3 has
    #    pyobjc; a Homebrew python3 does not, and the failure is a bare
    #    `import objc` deep inside run-benchmark's prepare_env.
    if /usr/bin/python3 -c 'import objc' 2>/dev/null; then
        unchanged "/usr/bin/python3 has pyobjc"
    else
        warn "/usr/bin/python3 cannot 'import objc' -- run-benchmark's prepare_env will fail"
        log  "  xcode-select --install   (Command Line Tools; it is a GUI prompt)"
    fi

    # 4. scipy, only for `wk bench compare` over here. Not fatal: comparing can
    #    happen in host mode, which is where the results are read anyway.
    if /usr/bin/python3 -c 'import scipy' 2>/dev/null; then
        unchanged "scipy present (wk bench compare works here)"
    else
        log "scipy absent -- optional: /usr/bin/python3 -m pip install --user scipy"
        log "  (only needed to run 'wk bench compare' in bench mode)"
    fi

    # 5. The thing that is only discoverable after a reboot, so it is said here
    #    rather than found later.
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
#
# --dry-run is parsed out first and independently of the action, so it composes
# with all of them rather than each action having to remember to accept it.

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
    --install)   do_install ;;
    --provision) do_provision ;;
    --build-pkg) do_build_pkg >/dev/null ;;
    --repair)    do_repair ;;
    # One invocation for the whole provisioning run, because the alternative is
    # sitting at the machine typing three commands that each have exactly one
    # sensible successor. It stops where the machine reboots -- everything after
    # that belongs to Setup Assistant and then to --provision.
    --all)
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
        ;;
esac
