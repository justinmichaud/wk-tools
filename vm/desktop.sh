# Put a macOS guest on an empty desktop, and keep it there. Runs *in* the
# guest, over ssh; idempotent, so running it on every start costs nothing.
#
# Two callers, one implementation: vm/provision-base.sh, so the golden base
# carries this, and t_start (targets/vm.sh), so every guest cloned from that
# base gets it again on every boot. Both, and not just the base, for a measured
# reason -- `defaults -currentHost` writes to
# ~/Library/Preferences/ByHost/<domain>.<hardware UUID>.plist, and `tart clone`
# gives the clone a new hardware UUID. The base's ByHost settings therefore do
# not apply to any guest cloned from it: `wk vm check` found the screen saver
# armed on a guest whose base had turned it off. Anything read back by
# `-currentHost` has to be written per guest.
#
# What is being prevented, in every case, is a window in front of the one being
# measured: an occluded window is a throttled window, so a benchmark in there
# measures something else rather than failing.
#
# `wk vm check` (vm/desktop-probe.sh) is the report that says whether this
# worked; this file is the only thing that changes anything.

set -euo pipefail

_say() { printf '==> %s\n' "$*" >&2; }

# --- 1. the screen lock ------------------------------------------------------
# Independent of the screen saver and of display sleep, settable only through
# sysadminctl, and the one that actually blocks a login.
#
# The password is checked with `dscl -authonly` before it is offered, and the
# call is skipped rather than attempted with a wrong one: `sysadminctl
# -screenLock` takes the account's *current* password, and what it does when
# given a wrong one is not documented and was observed to leave the lock armed.
# A guest whose password is not the one this run expects is reported, not
# guessed at -- `wk vm check` says so too.
if [ -n "${WK_VM_PASSWORD:-}" ] \
   && dscl . -authonly "$(id -un)" "$WK_VM_PASSWORD" >/dev/null 2>&1; then
    sudo -n sysadminctl -screenLock off -password "$WK_VM_PASSWORD" 2>/dev/null \
        || echo "warning: could not turn off the screen lock; the guest may come up locked" >&2
else
    echo "warning: the screen lock is left as it is: $(id -un)'s password here is not the
  one this run was given, so offering it to sysadminctl would be a guess.
  'wk vm check <name>' reports the lock; a rebuilt base sets the password." >&2
fi

# --- 2. the screen saver, per guest ------------------------------------------
# -currentHost, hence per hardware UUID, hence per clone: see the note above.
defaults -currentHost write com.apple.screensaver idleTime -int 0
defaults write com.apple.screensaver askForPassword -int 0
defaults write com.apple.screensaver askForPasswordDelay -int 0

# --- 3. display sleep --------------------------------------------------------
sudo -n pmset -a displaysleep 0 sleep 0 disablesleep 1 2>/dev/null || true

# --- 4. Setup Assistant ------------------------------------------------------
# Its post-login panes run on every clone: a clone is a new install by macOS's
# own reckoning, even with auto-login and .AppleSetupDone already satisfied.
# Each `DidSee*` key is what the assistant sets when a person clicks that pane
# through, and the list grows with each macOS release -- an unknown key here is
# harmless, a missing one is a modal panel.
for _k in DidSeeCloudSetup DidSeeSiriSetup DidSeeAppearanceSetup \
          DidSeePrivacy DidSeeTrueTone DidSeeAccessibility DidSeeSyncSetup \
          DidSeeApplePaySetup DidSeeAvatarSetup DidSeeTouchIDSetup \
          DidSeeScreenTime DidSeeiCloudLoginForStorageServices \
          DidSeeAppleIDSetup DidSeeSafariImport DidSeeSiriSetupPromptCount \
          DidSeeDevicesSetup DidSeeUpdateSetup DidSeeWelcome; do
    defaults write com.apple.SetupAssistant "$_k" -bool true 2>/dev/null || true
done
defaults write com.apple.SetupAssistant LastSeenCloudProductVersion "$(sw_vers -productVersion)"
defaults write com.apple.SetupAssistant LastSeenBuddyBuildVersion "$(sw_vers -buildVersion)"

# What is already on screen is deliberately left alone. Killing Setup Assistant
# ends the desktop session with it -- it is the login-time agent, not a window
# on top of a session -- and the guest lands back at the login window, which is
# strictly worse than a panel in front of a desktop. The keys above stop the
# *next* one, so the remedy for one already up is a restart of the guest:
#     wk vm stop <name> && wk vm start <name>
# `wk vm check` reports it and says so.

# --- 5. Software Update ------------------------------------------------------
# A guest is a disposable clone of a pinned image (WK_VM_IMAGE): upgrading it is
# meaningless, and rebuilding the base is how it changes (CLAUDE.md, "No
# in-place upgrades"). macOS offers the upgrade on the desktop anyway.
defaults write com.apple.SoftwareUpdate AutomaticCheckEnabled -bool false
defaults write com.apple.SoftwareUpdate AutomaticDownload -bool false
defaults write com.apple.SoftwareUpdate AutomaticallyInstallMacOSUpdates -bool false
defaults write com.apple.SoftwareUpdate CriticalUpdateInstall -bool false
defaults write com.apple.commerce AutoUpdate -bool false
sudo -n defaults write /Library/Preferences/com.apple.SoftwareUpdate \
    AutomaticCheckEnabled -bool false 2>/dev/null || true
sudo -n softwareupdate --schedule off >/dev/null 2>&1 || true

_say "desktop settled"
