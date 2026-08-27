# Only Remote Login: a fresh macOS install ships it off, so a new workstation
# answers nothing on port 22 and every fleet verb aimed at it fails identically
# until somebody turns it on by hand. Needs root, hence its own stage.
#
# Deliberately *not* here: authorized_keys, which is `re-authable`
# machine-local state in `wk doctor`'s ledger, decided per machine by a
# person -- `wk bench mac --preflight` prints the key to paste instead.

# No machine name to look MACH_ROLE up with, so: does this host workspaces?
# Always yes on macOS -- the only reason to run ./setup on a Mac is to build on it.
_want_remote_login=1

# `systemsetup -getremotelogin` needs admin and exits 0 even while refusing, so
# launchctl's disabled-override table -- readable by anyone, and what the
# toggle actually writes -- is the authority here instead.
_rl_state() {
    local out
    out=$(launchctl print-disabled system 2>/dev/null | grep -F '"com.openssh.sshd"') || true
    case "$out" in
        *"=> enabled"*)  echo on ;;
        *"=> disabled"*) echo off ;;
        # Absent: reported unknown, not assumed off (wrong-off means a sudo prompt forever).
        *) echo unknown ;;
    esac
}

if [ "$_want_remote_login" = 1 ]; then
    _rl=$(_rl_state)
    case "$_rl" in
        on)
            unchanged "remote login is on" ;;
        off)
            # A non-interactive ./setup reports and skips rather than forcing authentication.
            if [ -n "${WK_DRY_RUN:-}" ]; then
                changed "would enable remote login (sudo systemsetup -setremotelogin on)"
            elif sudo -n true 2>/dev/null || [ -t 0 ]; then
                if sudo systemsetup -setremotelogin on >/dev/null 2>&1; then
                    changed "enabled remote login"
                    # Read back: silent on failure when the real blocker is Full Disk Access.
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
            # Not off: a machine answering this ssh session is plainly on.
            warn "could not read the remote login state; leaving it alone"
            log  "  if this machine is not reachable from another, turn it on:"
            log  "    sudo systemsetup -setremotelogin on" ;;
    esac
fi
