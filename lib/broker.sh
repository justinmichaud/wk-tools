# Reaching the fleet from inside a workspace, when there is no route to it.
#
# A workspace has no network interface and no host filesystem, so every command
# that acts on hardware is refused in there -- `wk boot`, `wk pi`, `wk sysimage`
# are all on `wk`'s is_host_only list, and that refusal is correct and stays.
#
# What this adds is the one exception the sandbox was always missing: a
# *request*. Three of those verbs describe outcomes a bench run genuinely needs
# and that nothing in the workspace can produce -- put this build on the board,
# reboot it into a testing system, run this plan, give it back. The broker
# (container/broker/wk-broker.py) performs them on the workstation and hands
# back the words; the workspace never holds a key, a route or a shell.
#
# So the refusal is now conditional on there being no broker, rather than
# unconditional, and the shape of the message is the same either way: what was
# asked for, where it would have to happen, and what to do about it.
#
# Nothing here validates a request. Every check lives in the broker, outside the
# sandbox, because a check inside the sandbox is a check the sandbox can edit.

# The socket a workspace sees. `/run/wk` is the one directory the container
# bind-mounts from the host's runtime directory (targets/container.sh), and it
# already carries the egress proxy's socket -- so this needs no container
# change and reaches workspaces that already exist. WK_BROKER_SOCKET
# overrides the path; tests/test_dispatcher.py points it at a path with
# nothing listening, so a host-only refusal is testable from inside a fake
# workspace without a real broker.
WK_BROKER_SOCKET="${WK_BROKER_SOCKET:-/run/wk/broker.sock}"

broker_present() { [ -S "$WK_BROKER_SOCKET" ]; }

# broker_call <verb> [key=value ...]
#
# Exits with the brokered command's own status. Never returns a value: the
# broker's own words are the output, and paraphrasing them here would be a
# second copy of a refusal.
broker_call() {
    local client="$WK_ROOT/container/broker/wk-broker-client.py"
    [ -f "$client" ] || die "this workspace's copy of wk-tools has no broker client
    ($client). Refresh it:  wk sync --target container   on the workstation."
    WK_BROKER_SOCKET="$WK_BROKER_SOCKET" python3 "$client" "$@"
}

# The refusal for a host-only command that the broker does not serve, spoken
# once so that every caller says the same thing. Named after what it is: the
# sandbox holding, with the one door named.
broker_no_route() { # <what was asked for> <what a person would run>
    die "$1 acts on a host and its hardware, and this is workspace '$(wk_self)'.
    There is no route from here and there is not meant to be one.
    $([ -S "$WK_BROKER_SOCKET" ] \
        && printf '%s' "The request broker is listening, but it does not serve this --
    its vocabulary is: wk boot, wk pi deploy, wk pi bench." \
        || printf '%s' "No request broker is listening at $WK_BROKER_SOCKET either, so
    there is no door for a request like this one. Somebody with the workstation
    opens it with:  ./setup --stage broker   ('wk doctor' then says it is
    reachable from in here).")
    Run it on the workstation:  $2"
}
