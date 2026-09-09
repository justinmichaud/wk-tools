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

# The DidSee keys Setup Assistant's binary reads (what `strings` finds in it), less the ones measured not to work -- the key the update pane is gated on is deliberately absent, tried and beaten, and tests/test_vm_desktop.py holds this file to that. Measured 2026-09-09: a clone carrying every key here plus .skipbuddy still drew the AutoUpdate pane, so each of these only shortens Buddy's flow and none of them ends it. Ending it is vm/desktop-unblock.py, on the base.
for _k in DidSeeAccessibility DidSeeActivationLock DidSeeAppearance \
          DidSeeAppearanceSetup DidSeeApplePaySetup DidSeeAppStore \
          DidSeeCloudSetup DidSeeLockdownMode DidSeePrivacy DidSeeScreenTime \
          DidSeeSiriSetup DidSeeSyncSetup DidSeeSyncSetup2 \
          DidSeeTermsOfAddress DidSeeTouchIDSetup \
          DidSeeiCloudLoginForStorageServices; do
    defaults write com.apple.SetupAssistant "$_k" -bool true || true
done
defaults write com.apple.SetupAssistant LastSeenCloudProductVersion "$(sw_vers -productVersion)"
defaults write com.apple.SetupAssistant LastSeenBuddyBuildVersion "$(sw_vers -buildVersion)"

touch "$HOME/.skipbuddy"

defaults write com.apple.SoftwareUpdate AutomaticCheckEnabled -bool false
defaults write com.apple.SoftwareUpdate AutomaticDownload -bool false
defaults write com.apple.SoftwareUpdate AutomaticallyInstallMacOSUpdates -bool false
defaults write com.apple.SoftwareUpdate CriticalUpdateInstall -bool false
defaults write com.apple.SoftwareUpdate ConfigDataInstall -bool false
defaults write com.apple.commerce AutoUpdate -bool false

sudo -n bash -c "$(declare -f wk_quiet_desktop_system wk_quiet_desktop_power _wk_qd_pmset); wk_quiet_desktop_system" \
    || echo "warning: the machine-wide half did not fully take; 'wk vm check <name>' says which" >&2

wk_pyobjc_install || echo "warning: pyobjc did not install; run-benchmark cannot drive a
  browser here and nothing can keep it frontmost. 'wk vm check <name>' says so." >&2

_say "desktop settled"
