# Put a macOS guest on an empty desktop, and keep it there. Runs in the guest,
# over ssh; idempotent. An occluded window is a throttled window.
#
# From vm/provision-base.sh for the golden base and from t_start (targets/vm.sh)
# on every boot: `defaults -currentHost` writes to
# ~/Library/Preferences/ByHost/<domain>.<hardware UUID>.plist and `tart clone`
# gives the clone a new hardware UUID.

set -euo pipefail

_say() { printf '==> %s\n' "$*" >&2; }

# --- 1. the screen lock ------------------------------------------------------
# Independent of the screen saver and of display sleep, settable only through
# sysadminctl. It takes the account's current password and leaves the lock armed
# when given a wrong one, so `dscl -authonly` checks before it is offered.
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
# reckoning, even with auto-login and .AppleSetupDone satisfied. Each `DidSee*`
# key is what the assistant sets when a pane is clicked through.
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

# What is on screen is left alone: Setup Assistant is the login-time agent, so
# killing it ends the desktop session. The keys above stop the next one.

# --- 5. Software Update ------------------------------------------------------
# `com.apple.SoftwareUpdate` in the user domain is what System Settings shows;
# the settings softwareupdated acts on live in root's
# /Library/Preferences/com.apple.SoftwareUpdate. Both are written.
#
# Key by key, and which macOS each is for:
#   AutomaticCheckEnabled            10.8+   check at all; off stops the panel
#   AutomaticDownload                10.8+   background download, tens of GB
#   AutomaticallyInstallMacOSUpdates 10.14+  installs a macOS update and reboots
#   CriticalUpdateInstall            10.8+   XProtect/MRT security responses
#   ConfigDataInstall                10.8+   the data files those come with
#   com.apple.commerce AutoUpdate    10.11+  the App Store's own auto-update
#   softwareupdate --schedule off    10.4+   the scheduled check, and the only
#                                            one reading back as a yes/no, which
#                                            is what vm/desktop-probe.sh asks
#
# TODO: measure on a running tahoe (macOS 26) guest which of these the offer
# obeys; `wk vm start` prints vm/desktop-probe.sh's readings after settling.
# Apple gave the major-upgrade deferral to MDM only (10.15+), so if the offer
# survives these the answer is an image with no newer major to offer.
defaults write com.apple.SoftwareUpdate AutomaticCheckEnabled -bool false
defaults write com.apple.SoftwareUpdate AutomaticDownload -bool false
defaults write com.apple.SoftwareUpdate AutomaticallyInstallMacOSUpdates -bool false
defaults write com.apple.SoftwareUpdate CriticalUpdateInstall -bool false
defaults write com.apple.SoftwareUpdate ConfigDataInstall -bool false
defaults write com.apple.commerce AutoUpdate -bool false
for _k in AutomaticCheckEnabled AutomaticDownload \
          AutomaticallyInstallMacOSUpdates CriticalUpdateInstall ConfigDataInstall; do
    sudo -n defaults write /Library/Preferences/com.apple.SoftwareUpdate \
        "$_k" -bool false 2>/dev/null || true
done
sudo -n softwareupdate --schedule off >/dev/null 2>&1 || true

_say "desktop settled"
