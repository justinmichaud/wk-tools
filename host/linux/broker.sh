# The socket the unit binds in %t/wk is what workspaces mount at /run/wk.

wk_eval wk.store paths
. "$WK_ROOT/host/units.sh"

_unit_journal=""

unit_start wk-broker.service "$WK_ROOT" "$WK_STORE" \
    "workspaces will have no way to ask for a bench machine" "$_unit_journal" sh -c

unset _unit_journal
