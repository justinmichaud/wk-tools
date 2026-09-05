# Idempotent, and re-run on every boot: `defaults -currentHost` keys are per hardware UUID, and `tart clone` mints a new one.

set -euo pipefail

_say() { printf '==> %s\n' "$*" >&2; }

if [ -n "${WK_VM_PASSWORD:-}" ] \
   && dscl . -authonly "$(id -un)" "$WK_VM_PASSWORD" >/dev/null 2>&1; then
    sudo -n sysadminctl -screenLock off -password "$WK_VM_PASSWORD" 2>/dev/null \
        || echo "warning: could not turn off the screen lock; the guest may come up locked" >&2
else
    echo "warning: the screen lock is left as it is: $(id -un)'s password here is not the
  one this run was given, so offering it to sysadminctl would be a guess.
  'wk vm check <name>' reports the lock; a rebuilt base sets the password." >&2
fi

defaults -currentHost write com.apple.screensaver idleTime -int 0
defaults write com.apple.screensaver askForPassword -int 0
defaults write com.apple.screensaver askForPasswordDelay -int 0

sudo -n pmset -a displaysleep 0 sleep 0 disablesleep 1 2>/dev/null || true

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

# TODO: measure on a tahoe (macOS 26) guest which of these keys the update offer obeys.
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
