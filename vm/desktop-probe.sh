# Read-only key=value readings of what vm/desktop.sh set; sources nothing, so an older base answers too.

set -euo pipefail

printf 'console_user=%s\n' "$(stat -f%Su /dev/console 2>/dev/null || echo '?')"

_lock=$(sudo -n sysadminctl -screenLock status 2>&1 | tr -d '\r' | tail -1) || true
case "$_lock" in
    *"screenLock is off"*|*"is off"*) printf 'screenlock=off\n' ;;
    *"screenLock delay"*|*"is on"*)   printf 'screenlock=on\n' ;;
    *)                                printf 'screenlock=unknown\n' ;;
esac

printf 'idletime=%s\n' "$(defaults -currentHost read com.apple.screensaver idleTime 2>/dev/null || echo '?')"
printf 'askforpassword=%s\n' "$(defaults read com.apple.screensaver askForPassword 2>/dev/null || echo 0)"
printf 'displaysleep=%s\n' "$(pmset -g 2>/dev/null | awk '$1 == "displaysleep" { print $2 }' | tail -1)"

_pending=""
for _k in DidSeeCloudSetup DidSeeSiriSetup DidSeeAppearanceSetup \
          DidSeePrivacy DidSeeTrueTone DidSeeAccessibility DidSeeSyncSetup; do
    [ "$(defaults read com.apple.SetupAssistant "$_k" 2>/dev/null)" = 1 ] \
        || _pending="$_pending $_k"
done
printf 'setupassistant_pending=%s\n' "${_pending# }"

printf 'update_check=%s\n' "$(defaults read com.apple.SoftwareUpdate AutomaticCheckEnabled 2>/dev/null || echo '?')"
printf 'update_download=%s\n' "$(defaults read com.apple.SoftwareUpdate AutomaticDownload 2>/dev/null || echo '?')"
printf 'update_check_system=%s\n' "$(defaults read /Library/Preferences/com.apple.SoftwareUpdate AutomaticCheckEnabled 2>/dev/null || echo '?')"
printf 'update_autoinstall_system=%s\n' "$(defaults read /Library/Preferences/com.apple.SoftwareUpdate AutomaticallyInstallMacOSUpdates 2>/dev/null || echo '?')"

printf 'update_schedule=%s\n' "$(softwareupdate --schedule 2>/dev/null \
    | sed -n 's/.*[Aa]utomatic check is *\([a-z]*\).*/\1/p' | tail -1)"

printf 'setupassistant_seen_product=%s\n' "$(defaults read com.apple.SetupAssistant LastSeenCloudProductVersion 2>/dev/null || echo '?')"
printf 'os_product=%s\n' "$(sw_vers -productVersion 2>/dev/null || echo '?')"

printf 'panels=%s\n' "$(pgrep -fl 'Setup Assistant|Software Update|SUUIAgent' 2>/dev/null \
    | awk '{ $1 = ""; print }' | tr '\n' ';' | sed 's/^ //')"

printf 'user=%s\n' "$(id -un)"
