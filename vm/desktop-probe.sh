# What state a macOS guest's desktop is actually in, as key=value lines. Runs in
# the guest, over ssh, and reads only. Four mechanisms can occlude the window
# being measured -- the screen lock, the screen saver, display sleep, and a
# modal panel -- and vm/desktop.sh disables all four; this says whether that is
# still true. Sources nothing, so it answers about an older base's guest too.

set -euo pipefail

# Who owns the console is who is logged in at the window; empty or `root` is the
# login window, and a guest there has no desktop to draw on.
printf 'console_user=%s\n' "$(stat -f%Su /dev/console 2>/dev/null || echo '?')"

# The screen lock: independent of the screen saver and of display sleep, and
# settable only through sysadminctl.
_lock=$(sudo -n sysadminctl -screenLock status 2>&1 | tr -d '\r' | tail -1) || true
case "$_lock" in
    *"screenLock is off"*|*"is off"*) printf 'screenlock=off\n' ;;
    *"screenLock delay"*|*"is on"*)   printf 'screenlock=on\n' ;;
    *)                                printf 'screenlock=unknown\n' ;;
esac

printf 'idletime=%s\n' "$(defaults -currentHost read com.apple.screensaver idleTime 2>/dev/null || echo '?')"
printf 'askforpassword=%s\n' "$(defaults read com.apple.screensaver askForPassword 2>/dev/null || echo 0)"
printf 'displaysleep=%s\n' "$(pmset -g 2>/dev/null | awk '$1 == "displaysleep" { print $2 }' | tail -1)"

# Each `DidSee*` key is what the assistant sets when a person clicks that pane
# through. A clone is a new install by macOS's own reckoning, so these come back
# on every clone.
_pending=""
for _k in DidSeeCloudSetup DidSeeSiriSetup DidSeeAppearanceSetup \
          DidSeePrivacy DidSeeTrueTone DidSeeAccessibility DidSeeSyncSetup; do
    [ "$(defaults read com.apple.SetupAssistant "$_k" 2>/dev/null)" = 1 ] \
        || _pending="$_pending $_k"
done
printf 'setupassistant_pending=%s\n' "${_pending# }"

# Two domains, because they are two different facts: the per-user one is what
# System Settings shows a person, /Library/Preferences the root-owned one
# softwareupdated acts on, so a guest can read 0 in the first and 1 in the
# second. Reported raw, since `?` is "no such key here" rather than off.
printf 'update_check=%s\n' "$(defaults read com.apple.SoftwareUpdate AutomaticCheckEnabled 2>/dev/null || echo '?')"
printf 'update_download=%s\n' "$(defaults read com.apple.SoftwareUpdate AutomaticDownload 2>/dev/null || echo '?')"
printf 'update_check_system=%s\n' "$(defaults read /Library/Preferences/com.apple.SoftwareUpdate AutomaticCheckEnabled 2>/dev/null || echo '?')"
printf 'update_autoinstall_system=%s\n' "$(defaults read /Library/Preferences/com.apple.SoftwareUpdate AutomaticallyInstallMacOSUpdates 2>/dev/null || echo '?')"

# The scheduled check as softwareupdate reports it ("Automatic check is on" /
# "off"): no domain, no root, and no disagreement across macOS versions.
printf 'update_schedule=%s\n' "$(softwareupdate --schedule 2>/dev/null \
    | sed -n 's/.*[Aa]utomatic check is *\([a-z]*\).*/\1/p' | tail -1)"

# Setup Assistant's "what is new in macOS" pane: Buddy shows it when these do
# not already name the running system.
printf 'setupassistant_seen_product=%s\n' "$(defaults read com.apple.SetupAssistant LastSeenCloudProductVersion 2>/dev/null || echo '?')"
printf 'os_product=%s\n' "$(sw_vers -productVersion 2>/dev/null || echo '?')"

# A panel on screen right now, which the settings above only predict. Named
# processes: there is no unprivileged way to list other applications' windows.
printf 'panels=%s\n' "$(pgrep -fl 'Setup Assistant|Software Update|SUUIAgent' 2>/dev/null \
    | awk '{ $1 = ""; print }' | tr '\n' ';' | sed 's/^ //')"

# What a person needs to type at that window, if it ever asks.
printf 'user=%s\n' "$(id -un)"
