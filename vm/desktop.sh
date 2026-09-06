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

wk_quiet_desktop_user || echo "warning: the guest's desktop is not fully quiet (above); 'wk vm check <name>' says which settings" >&2

for _k in DidSeeCloudSetup DidSeeSiriSetup DidSeeAppearanceSetup \
          DidSeePrivacy DidSeeTrueTone DidSeeAccessibility DidSeeSyncSetup \
          DidSeeApplePaySetup DidSeeAvatarSetup DidSeeTouchIDSetup \
          DidSeeScreenTime DidSeeiCloudLoginForStorageServices \
          DidSeeAppleIDSetup DidSeeSafariImport DidSeeSiriSetupPromptCount \
          DidSeeDevicesSetup DidSeeUpdateSetup DidSeeWelcome; do
    defaults write com.apple.SetupAssistant "$_k" -bool true || true
done
defaults write com.apple.SetupAssistant LastSeenCloudProductVersion "$(sw_vers -productVersion)"
defaults write com.apple.SetupAssistant LastSeenBuddyBuildVersion "$(sw_vers -buildVersion)"

# Measured 2026-09-06 on clones of a freshly provisioned base: the keys above are not enough on their own -- a clone comes up with Setup Assistant frontmost, and it is this file's absence that lets it. On a live guest neither this file alone nor dismissing the pane alone stops it coming back; both, then a reboot, do, twice reproduced. Provisioning already dismisses the pane, so a base that also carries this is a base whose clones come up clear.
touch "$HOME/.skipbuddy"

defaults write com.apple.SoftwareUpdate AutomaticCheckEnabled -bool false
defaults write com.apple.SoftwareUpdate AutomaticDownload -bool false
defaults write com.apple.SoftwareUpdate AutomaticallyInstallMacOSUpdates -bool false
defaults write com.apple.SoftwareUpdate CriticalUpdateInstall -bool false
defaults write com.apple.SoftwareUpdate ConfigDataInstall -bool false
defaults write com.apple.commerce AutoUpdate -bool false

sudo -n bash -c "$(declare -f wk_quiet_desktop_system wk_quiet_desktop_power _wk_qd_pmset); wk_quiet_desktop_system" \
    || echo "warning: the machine-wide half did not fully take; 'wk vm check <name>' says which" >&2

_say "desktop settled"
