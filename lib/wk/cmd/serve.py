"""Serve the controller's API on its tailnet address, until killed.

It answers for the fleet's workspace names, gives a person's session its credentials and revokes a server's push keys (DESIGN.md, Shape). The controller role of `wk machine setup` runs it as the systemd user unit wk-api. The port is wk_api_port
(ansible/group_vars/all.json), or the controller's conf's api_port=."""

from wk.api import server
from wk.core.decl import CONTROLLER, Command

COMMAND = Command(on=CONTROLLER)


def main(args, inv):
    server.serve(inv)
