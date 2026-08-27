# Only Remote Login: the one thing on a fresh macOS install that blocks
# *everything else* and can't be discovered remotely -- macOS ships it off,
# so a new workstation answers nothing on port 22, and every fleet verb
# aimed at it (`wk status`, `wk sync --target`, the macOS benchmark lane)
# fails identically and unhelpfully until somebody turns it on by hand.
#
# A stage of its own, not a line in settings.sh (user-level `defaults`, no
# sudo) or machine.sh (the podman VM): this needs root and is about the
# host's identity on the network.
#
# Deliberately *not* here: authorized_keys. Which keys may log in is
# `re-authable` machine-local state in `wk doctor`'s ledger, decided per
# machine by a person -- a repo that wrote it would hand out access on every
# pull. `wk bench mac --preflight` prints the key to paste instead.

# `setup` has no machine name to look MACH_ROLE up with (boot/machines/<name>.conf),
# so the question asked here is the one it can actually answer: does this
# host workspaces? On macOS that's always yes -- the only reason to run
# ./setup on a Mac is to build on it.
_want_remote_login=1

# `systemsetup -getremotelogin` is unusable here: it requires admin, and
# **exits 0** even while refusing ("You need administrator access..."), so
# even the status code lies. Trusting it would make ./setup ask for a
# password on every run of an already-configured machine, breaking the one
# contract this script has: a second run reports no changes.
#
# launchctl's disabled-override table is readable by anyone and is exactly
# what the Remote Login toggle writes, so it is the authority here.
_rl_state() {
    local out
    out=$(launchctl print-disabled system 2>/dev/null | grep -F '"com.openssh.sshd"') || true
    case "$out" in
        *"=> enabled"*)  echo on ;;
        *"=> disabled"*) echo off ;;
        # Absent means no override was ever written -- same as off on a
        # fresh install, but reported unknown rather than assumed: wrong in
        # the "off" direction is a spurious sudo prompt forever.
        *) echo unknown ;;
    esac
}

if [ "$_want_remote_login" = 1 ]; then
    _rl=$(_rl_state)
    case "$_rl" in
        on)
            unchanged "remote login is on" ;;
        off)
            # Turning on a listener should cost a deliberate authentication;
            # a non-interactive ./setup reports and skips rather than
            # silently leaving the machine unreachable.
            if [ -n "${WK_DRY_RUN:-}" ]; then
                changed "would enable remote login (sudo systemsetup -setremotelogin on)"
            elif sudo -n true 2>/dev/null || [ -t 0 ]; then
                if sudo systemsetup -setremotelogin on >/dev/null 2>&1; then
                    changed "enabled remote login"
                    # Read back: -setremotelogin is silent on failure under
                    # some privacy configs, where the real blocker is Full
                    # Disk Access for the calling binary.
                    [ "$(_rl_state)" = on ] || warn "  ...but it still reports off -- check
    System Settings -> General -> Sharing -> Remote Login. A terminal without
    Full Disk Access can be refused here without saying so."
                else
                    warn "could not enable remote login (sudo refused or was cancelled)"
                fi
            else
                warn "remote login is off and there is no terminal to ask for a password on"
                log  "  this machine cannot be driven from any other until it is on:"
                log  "    sudo systemsetup -setremotelogin on"
            fi ;;
        unknown|*)
            # Not treated as off (see _rl_state): a machine answering this
            # very ssh session is plainly on.
            warn "could not read the remote login state; leaving it alone"
            log  "  if this machine is not reachable from another, turn it on:"
            log  "    sudo systemsetup -setremotelogin on" ;;
    esac
fi
