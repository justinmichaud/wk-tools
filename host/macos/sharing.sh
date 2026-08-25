# What makes this Mac reachable as part of the fleet, rather than a laptop that
# happens to have the tools on it.
#
# Only Remote Login, and it is here because it is the one thing on a fresh macOS
# install that blocks *everything else* and cannot be discovered from anywhere:
# macOS ships it off, so a new workstation answers nothing on port 22, and every
# fleet verb aimed at it -- `wk status`, `wk sync --target`, the whole macOS
# benchmark lane -- fails identically and unhelpfully. It cost this exact
# session: without it a machine cannot be inspected at all until somebody turns
# it on by hand.
#
# Why a stage of its own rather than a line in settings.sh or machine.sh:
# settings.sh is user-level `defaults` with no sudo anywhere in it, and
# machine.sh is about the podman VM. This needs root and is about the host's
# identity on the network, which is neither of those.
#
# Deliberately *not* here: authorized_keys. Which keys may log in is
# `re-authable` machine-local state in `wk doctor`'s ledger -- a person decides
# it, per machine, and a repo that writes it would be handing out access on
# every pull. `wk bench mac --preflight` prints the key to paste and that is the
# right division.

# Roles, because this is not true of every machine wk-tools sets up. A
# workstation is driven from elsewhere and must answer; a machine that is only
# ever measured has its own image and its own rules. MACH_ROLE lives in
# boot/machines/<name>.conf, and `setup` has no machine name to look one up
# with -- so the question asked here is the one setup can actually answer:
# is this host a workstation in the fleet's sense, i.e. does it host workspaces?
# On macOS that is always yes, because the only reason to run ./setup on a Mac
# is to build on it.
_want_remote_login=1

# Read without root, and never guess.
#
# `systemsetup -getremotelogin` is the obvious reader and is unusable here: it
# requires admin, answers "You need administrator access to run this tool...
# exiting!" -- and **exits 0 while doing so**, so even the status code lies.
# Trusting it and treating the unreadable answer as "off" makes ./setup ask for
# a password on every single run of an already-configured machine. That breaks this script's one contract: a second
# run must report no changes.
#
# launchctl's disabled-override table is readable by anyone and is exactly what
# the Remote Login toggle writes, so it is the authority here. `=> enabled`
# means "not disabled", which is the question being asked.
_rl_state() {
    local out
    out=$(launchctl print-disabled system 2>/dev/null | grep -F '"com.openssh.sshd"') || true
    case "$out" in
        *"=> enabled"*)  echo on ;;
        *"=> disabled"*) echo off ;;
        # Absent from the table at all means no override has ever been written.
        # On a fresh macOS install that is the same as off, but it is reported
        # as unknown rather than assumed: the cost of being wrong in the "off"
        # direction is a spurious sudo prompt forever, and in the "on"
        # direction it is one clear warning.
        *) echo unknown ;;
    esac
}

if [ "$_want_remote_login" = 1 ]; then
    _rl=$(_rl_state)
    case "$_rl" in
        on)
            unchanged "remote login is on" ;;
        off)
            # Needs root, and asking for a password is correct here rather than
            # something to route around: turning on a listener is exactly the
            # kind of change that should cost a deliberate authentication. If
            # there is no way to ask -- a non-interactive ./setup -- it is
            # reported and skipped rather than silently left, because "the
            # machine is unreachable" is the most expensive failure in this
            # repository to diagnose from the far end.
            if [ -n "${WK_DRY_RUN:-}" ]; then
                changed "would enable remote login (sudo systemsetup -setremotelogin on)"
            elif sudo -n true 2>/dev/null || [ -t 0 ]; then
                if sudo systemsetup -setremotelogin on >/dev/null 2>&1; then
                    changed "enabled remote login"
                    # Read back rather than trust: -setremotelogin is silent on
                    # failure under some privacy configurations, where the real
                    # blocker is Full Disk Access for the calling binary.
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
            # Not treated as off: see _rl_state. A machine that is answering
            # this very ssh session is plainly on, and guessing "off" here is
            # how an idempotent script grows a password prompt it never needs.
            warn "could not read the remote login state; leaving it alone"
            log  "  if this machine is not reachable from another, turn it on:"
            log  "    sudo systemsetup -setremotelogin on" ;;
    esac
fi
