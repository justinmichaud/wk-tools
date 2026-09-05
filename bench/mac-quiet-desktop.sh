# Sourced, never run: defines functions and touches nothing until one is
# called, and sources nothing itself, so it can be streamed into a guest that
# has no copy of wk-tools on disk yet.

# <name> <domain> <key> <type> <value>; the name is what the probe prints and
# the report reads. `@` on the domain means the key is per hardware UUID
# (`defaults -currentHost`), which `tart clone` remints for every guest.
wk_quiet_desktop_rows() {
    cat <<'ROWS'
widgets_desktop com.apple.WindowManager StandardHideWidgets bool true
widgets_stage com.apple.WindowManager StageManagerHideWidgets bool true
reduce_motion com.apple.universalaccess reduceMotion bool true
reduce_transparency com.apple.universalaccess reduceTransparency bool true
appnap NSGlobalDomain NSAppSleepDisabled bool true
askforpassword com.apple.screensaver askForPassword int 0
askforpassworddelay com.apple.screensaver askForPasswordDelay int 0
idletime @com.apple.screensaver idleTime int 0
ROWS
}

# chronod hosts the desktop and Notification Centre widgets, which animate and
# refresh on timers of their own; NotificationCenter draws a banner over
# whatever is being measured. Setup Assistant's MiniBuddy is not here: it is
# submitted by runningboardd on behalf of loginwindow and has no launchd label
# to disable (measured on a Tahoe 26.4 clone, 2026-09-05).
wk_quiet_desktop_agents() {
    cat <<'ROWS'
widgets_agent com.apple.chronod NotificationCenterNone
notifications com.apple.notificationcenterui NotificationCenter
ROWS
}

# launchd's own Label, which is not always the plist's name: com.apple.notificationcenterui.plist declares com.apple.notificationcenterui.agent, and disabling the filename is recorded happily while the agent keeps running.
_wk_qd_label() { # <plist basename>
    /usr/libexec/PlistBuddy -c 'Print :Label' \
        "/System/Library/LaunchAgents/$1.plist" 2>/dev/null || printf '%s' "$1"
}

_wk_qd_uid() { id -u "${1:-$(id -un)}" 2>/dev/null; }

# Run as root at a first boot, an unqualified `defaults write` lands in root's own domain and the account that gets measured never sees it.
_wk_qd_defaults() { # <user> <args...>
    local u="$1"; shift
    if [ "$u" = "$(id -un)" ]; then
        defaults "$@"
    else
        sudo -u "$u" defaults "$@"
    fi
}

wk_quiet_desktop_user() { # [user] -- 0 when every setting above took
    local u="${1:-$(id -un)}" bad=0 name domain key type value host uid label
    uid=$(_wk_qd_uid "$u") || uid=""

    while read -r name domain key type value; do
        [ -n "$domain" ] || continue
        host=""
        case "$domain" in @*) host=-currentHost; domain="${domain#@}" ;; esac
        # shellcheck disable=SC2086 -- $host is one flag or nothing.
        _wk_qd_defaults "$u" $host write "$domain" "$key" "-$type" "$value" \
            || { echo "wk: $u could not be given $domain $key" >&2; bad=1; }
    done <<ROWS
$(wk_quiet_desktop_rows)
ROWS

    [ -n "$uid" ] || { echo "wk: no such account '$u', so no agent was turned off" >&2; return 1; }
    local plist proc
    while read -r name plist proc; do
        [ -n "$plist" ] || continue
        label=$(_wk_qd_label "$plist")
        # `disable` is recorded in launchd's own per-user store and holds across logins; `bootout` needs a live GUI domain, and a first boot has none.
        launchctl disable "gui/$uid/$label" 2>/dev/null \
            || { echo "wk: could not disable $label for $u" >&2; bad=1; }
        launchctl bootout "gui/$uid/$label" >/dev/null 2>&1 || true
    done <<ROWS
$(wk_quiet_desktop_agents)
ROWS
    return "$bad"
}

# Automatic update *checking* is not here: measured on Tahoe 26.4, softwareupdated erases AutomaticCheckEnabled from this domain within seconds and `softwareupdate --schedule off` exits 0 having changed nothing, so the only thing that stops a scan is not reaching Apple.
wk_quiet_desktop_system() {
    local bad=0 key
    [ "$(id -u)" -eq 0 ] || { echo "wk: wk_quiet_desktop_system needs root" >&2; return 1; }

    mdutil -i off -a >/dev/null 2>&1 || { echo "wk: spotlight is still indexing" >&2; bad=1; }
    # Setup Assistant's own flow turns this on, so it is turned off after every pass through one as well as on principle: a measured Mac does not upload crash reports, and the upload is egress under a run.
    defaults write "/Library/Application Support/CrashReporter/DiagnosticMessagesHistory" \
        AutoSubmit -bool false 2>/dev/null \
        || { echo "wk: could not turn off diagnostic submission" >&2; bad=1; }
    tmutil disablelocal >/dev/null 2>&1 || true
    pmset -a displaysleep 0 sleep 0 disksleep 0 disablesleep 1 lowpowermode 0 >/dev/null 2>&1 \
        || { echo "wk: could not hold the display and the disk awake" >&2; bad=1; }

    for key in AutomaticDownload AutomaticallyInstallMacOSUpdates \
               CriticalUpdateInstall ConfigDataInstall; do
        defaults write /Library/Preferences/com.apple.SoftwareUpdate "$key" -bool false \
            || { echo "wk: could not turn off SoftwareUpdate $key" >&2; bad=1; }
    done
    return "$bad"
}

wk_quiet_desktop_probe() { # [user] -- `<name>=<value>`; `?` is "no such key", which is not off
    local u="${1:-$(id -un)}" name domain key type value host uid label

    while read -r name domain key type value; do
        [ -n "$domain" ] || continue
        host=""
        case "$domain" in @*) host=-currentHost; domain="${domain#@}" ;; esac
        # shellcheck disable=SC2086 -- $host is one flag or nothing.
        printf '%s=%s\n' "$name" \
            "$(_wk_qd_defaults "$u" $host read "$domain" "$key" 2>/dev/null || echo '?')"
    done <<ROWS
$(wk_quiet_desktop_rows)
ROWS

    uid=$(_wk_qd_uid "$u") || uid=""
    # By the process, not by launchd's disabled list: disabling the wrong label is recorded there as cheerfully as the right one, and the agent goes on running.
    local plist proc
    while read -r name plist proc; do
        [ -n "$plist" ] || continue
        if [ -z "$uid" ]; then
            printf '%s=?\n' "$name"
        elif pgrep -x "$proc" >/dev/null 2>&1; then
            printf '%s=on\n' "$name"
        else
            printf '%s=off\n' "$name"
        fi
    done <<ROWS
$(wk_quiet_desktop_agents)
ROWS

    printf 'spotlight=%s\n' "$(mdutil -s / 2>/dev/null | sed -n '2s/^[[:space:]]*//p')"
    printf 'analytics=%s\n' "$(defaults read \
        "/Library/Application Support/CrashReporter/DiagnosticMessagesHistory" AutoSubmit 2>/dev/null || echo '?')"
}
