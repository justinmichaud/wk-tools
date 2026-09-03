# What state a macOS guest's desktop is actually in, as key=value lines.
# Runs *in* the guest, over ssh, and reads only -- nothing here may change a
# setting or start anything: `wk vm check` is a report.
#
# A guest is the only workspace kind with a real GPU and a window meant to be
# looked at, and four separate mechanisms can put something in front of that
# window -- the screen lock, the screen saver, display sleep, and a modal panel
# (Setup Assistant, a Software Update offer). Each occludes any drawing window,
# which throttles its timers: a benchmark measuring the wrong thing rather than
# failing. vm/provision-base.sh disables all four in the golden base; this says
# whether that is still true of the guest in front of you.
#
# Self-contained: it sources nothing, so it answers about a guest provisioned by
# an older base too.

set -euo pipefail

# Who owns the console is who is logged in at the window. Empty or `root` means
# the login window, i.e. nobody -- and a guest at a login window has no desktop
# for anything to draw on.
printf 'console_user=%s\n' "$(stat -f%Su /dev/console 2>/dev/null || echo '?')"

# The screen lock, which is the one that actually blocks. Independent of the
# screen saver and of display sleep, and settable only through sysadminctl.
_lock=$(sudo -n sysadminctl -screenLock status 2>&1 | tr -d '\r' | tail -1) || true
case "$_lock" in
    *"screenLock is off"*|*"is off"*) printf 'screenlock=off\n' ;;
    *"screenLock delay"*|*"is on"*)   printf 'screenlock=on\n' ;;
    *)                                printf 'screenlock=unknown\n' ;;
esac

printf 'idletime=%s\n' "$(defaults -currentHost read com.apple.screensaver idleTime 2>/dev/null || echo '?')"
printf 'askforpassword=%s\n' "$(defaults read com.apple.screensaver askForPassword 2>/dev/null || echo 0)"
printf 'displaysleep=%s\n' "$(pmset -g 2>/dev/null | awk '$1 == "displaysleep" { print $2 }' | tail -1)"

# Setup Assistant's post-login panes: each `DidSee*` key is what the assistant
# sets when a person clicks that pane through. A clone is a new install by
# macOS's own reckoning, so these come back on every clone unless the base
# carries them.
_pending=""
for _k in DidSeeCloudSetup DidSeeSiriSetup DidSeeAppearanceSetup \
          DidSeePrivacy DidSeeTrueTone DidSeeAccessibility DidSeeSyncSetup; do
    [ "$(defaults read com.apple.SetupAssistant "$_k" 2>/dev/null)" = 1 ] \
        || _pending="$_pending $_k"
done
printf 'setupassistant_pending=%s\n' "${_pending# }"

# The Software Update offer, which arrives as a modal panel on the desktop. A
# guest is a clone of a pinned image; upgrading it is meaningless.
#
# Two domains, because they are two different facts (vm/desktop.sh says which
# key is for what). The per-user domain is what System Settings shows a person;
# /Library/Preferences is the one softwareupdated acts on, and it is root's --
# so a guest can perfectly well read 0 in the first and 1 in the second, and
# only the second puts a panel on the desktop. Both are reported raw: `?` is
# "no such key here", which is not the same as off, and the report says so.
printf 'update_check=%s\n' "$(defaults read com.apple.SoftwareUpdate AutomaticCheckEnabled 2>/dev/null || echo '?')"
printf 'update_download=%s\n' "$(defaults read com.apple.SoftwareUpdate AutomaticDownload 2>/dev/null || echo '?')"
printf 'update_check_system=%s\n' "$(defaults read /Library/Preferences/com.apple.SoftwareUpdate AutomaticCheckEnabled 2>/dev/null || echo '?')"
printf 'update_autoinstall_system=%s\n' "$(defaults read /Library/Preferences/com.apple.SoftwareUpdate AutomaticallyInstallMacOSUpdates 2>/dev/null || echo '?')"

# The scheduled check as softwareupdate itself reports it: "Automatic check is
# on" / "off", no domain and no root needed, and the one reading that cannot
# disagree with itself across macOS versions.
printf 'update_schedule=%s\n' "$(softwareupdate --schedule 2>/dev/null \
    | sed -n 's/.*[Aa]utomatic check is *\([a-z]*\).*/\1/p' | tail -1)"

# Setup Assistant's "what is new in macOS" pane, which is a Software Update
# screen by another name: Buddy shows it when these do not already name the
# running system, and a clone is a new install by macOS's own reckoning.
printf 'setupassistant_seen_product=%s\n' "$(defaults read com.apple.SetupAssistant LastSeenCloudProductVersion 2>/dev/null || echo '?')"
printf 'os_product=%s\n' "$(sw_vers -productVersion 2>/dev/null || echo '?')"

# A panel actually on screen right now, which is the fact the settings above
# only predict. Named processes rather than a window list: there is no
# unprivileged way to enumerate other applications' windows.
printf 'panels=%s\n' "$(pgrep -fl 'Setup Assistant|Software Update|SUUIAgent' 2>/dev/null \
    | awk '{ $1 = ""; print }' | tr '\n' ';' | sed 's/^ //')"

# What a person needs to type at that window, if it ever asks.
printf 'user=%s\n' "$(id -un)"
