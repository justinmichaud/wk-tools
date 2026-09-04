# The fleet-request broker, as a systemd --user service. Containers and broker
# share this machine, so the socket it binds in %t/wk is the directory every
# workspace bind-mounts at /run/wk (targets/container.sh).

. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/host/units.sh"

_unit_journal=""

unit_start wk-broker.service "$WK_ROOT" "$WK_STORE" \
    "workspaces will have no way to ask for a bench device" "$_unit_journal" sh -c

unset _unit_journal
