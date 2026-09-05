set -euo pipefail

printf 'console_user=%s\n' "$(stat -f%Su /dev/console 2>/dev/null || echo '?')"

_lock=$(sudo -n sysadminctl -screenLock status 2>&1 | tr -d '\r' | tail -1) || true
case "$_lock" in
    *"screenLock is off"*|*"is off"*) printf 'screenlock=off\n' ;;
    *"screenLock delay"*|*"is on"*)   printf 'screenlock=on\n' ;;
    *)                                printf 'screenlock=unknown\n' ;;
esac

wk_quiet_desktop_probe
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
printf 'update_download_system=%s\n' "$(defaults read /Library/Preferences/com.apple.SoftwareUpdate AutomaticDownload 2>/dev/null || echo '?')"
printf 'update_autoinstall_system=%s\n' "$(defaults read /Library/Preferences/com.apple.SoftwareUpdate AutomaticallyInstallMacOSUpdates 2>/dev/null || echo '?')"

printf 'setupassistant_seen_product=%s\n' "$(defaults read com.apple.SetupAssistant LastSeenCloudProductVersion 2>/dev/null || echo '?')"
printf 'os_product=%s\n' "$(sw_vers -productVersion 2>/dev/null || echo '?')"

_front=$(lsappinfo front 2>/dev/null) || true
printf 'frontapp=%s\n' "$([ -z "$_front" ] || lsappinfo info -only bundleID "$_front" 2>/dev/null \
    | sed -n 's/.*"CFBundleIdentifier"="\([^"]*\)".*/\1/p')"
wk_window_probe

# The one panel the reading above cannot see: SecurityAgent draws a modal authentication sheet without ever becoming the frontmost *application* (bench/mac-bench-autorun.sh dismisses it for the same reason).
printf 'securityagent=%s\n' "$(pgrep -x SecurityAgent >/dev/null 2>&1 && echo up || echo down)"

printf 'user=%s\n' "$(id -un)"
