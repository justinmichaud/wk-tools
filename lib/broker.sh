# /run/wk, the one host directory a container bind-mounts, is where wk-broker.py listens.
WK_BROKER_SOCKET="${WK_BROKER_SOCKET:-/run/wk/broker.sock}"

broker_present() { [ -S "$WK_BROKER_SOCKET" ]; }

broker_call() {
    local client="$WK_ROOT/container/broker/wk-broker-client.py"
    [ -f "$client" ] || die "this workspace's copy of wk-tools has no broker client
    ($client). Refresh it:  wk sync --tools container   on the workstation."
    WK_BROKER_SOCKET="$WK_BROKER_SOCKET" python3 "$client" "$@"
}

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
