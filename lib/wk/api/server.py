"""`wk serve`: the controller's API, an HTTP server on its tailnet address.

    PUT /ws/<name>      reserves the name for the asker: 200; 409 naming the holder when another server holds it
    DELETE /ws/<name>   releases it when the asker holds it: 200; 404 when no server does; 409 naming the holder
    GET /ws             200 {"names": {"<name>": "<server>"}}
    POST /credentials   a person's session's: the asker's push keys made anew and the logins git-webkit and gh read,
                        200 {"deploy": {"<owner>/<repo>": "<private key>"}, "env": {"<variable>": "<value>"}}
    POST /revoke        before an agent session: every push key of the asker's revoked, 200 {}; 502 when that fails

The asker is the node `tailscale whois` names for the request's address; a node with no conf is answered 403, and only
a server reserves, releases, asks for credentials and revokes them. Every answer is a JSON object, and every refusal's says why in "message"."""

import http.server
import json

from wk.api import client, names
from wk.core.act import die, info
from wk.core.conf import THE
from wk.core.errors import NEEDS_PERSON, Refused
from wk.creds import human, rotation
from wk.workspace.lifecycle import NAME

WHOIS_TIMEOUT = 10


def caller(inv, address):
    """The asker's conf, or why there is none."""
    r = inv.here.run(["tailscale", "whois", "--json", address], timeout=WHOIS_TIMEOUT)
    try:
        node = json.loads(r.out)["Node"]["Name"].split(".")[0]
    except (ValueError, KeyError, TypeError, AttributeError):
        return None, "the tailnet names no node at %s" % address
    confs = inv.fleet.load()
    if node not in confs:
        return None, "%s has no conf on the controller" % node
    return confs[node], None


def route(inv, method, path, address):
    who, why = caller(inv, address)
    if who is None:
        return 403, {"message": why}
    home = inv.wk_home
    if (method, path) == ("GET", "/ws"):
        return 200, {"names": names.holders(inv.here, home)}
    if method in ("PUT", "DELETE", "POST") and not who.holds_workspaces:
        return 403, {"message": "%s holds no workspace: it is %s" % (who.name, THE[who.kind])}
    if (method, path) == ("POST", "/credentials"):
        return 200, human.made(inv, who.name)
    if (method, path) == ("POST", "/revoke"):
        rotation.revoke(inv, who.name)
        return 200, {}
    name = path[len("/ws/"):]
    if method not in ("PUT", "DELETE") or not path.startswith("/ws/") or not NAME.fullmatch(name):
        return 404, {"message": "no such request: %s %s" % (method, path)}
    held = names.reserve(inv.here, home, name, who.name) if method == "PUT" else names.holder(inv.here, home, name)
    if held is None:
        return 404, {"message": "no server holds %s" % name}
    if held != who.name:
        return 409, {"message": "%s is %s's" % (name, held), "holder": held}
    if method == "DELETE":
        names.release(inv.here, home, name)
    return 200, {"name": name, "server": who.name}


def answer(inv, method, path, address):
    """(status, the JSON object answered) for one request from `address`."""
    try:
        return route(inv, method, path, address)
    except Refused as e:
        return 502, {"message": str(e)}


class Handler(http.server.BaseHTTPRequestHandler):
    def reply(self):
        status, doc = self.server.answer(self.command, self.path, self.client_address[0])
        body = (json.dumps(doc, sort_keys=True) + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_PUT = do_DELETE = do_POST = reply


class Server(http.server.ThreadingHTTPServer):
    def __init__(self, address, answer):
        super().__init__(address, Handler)
        self.answer = answer


def serve(inv):
    r = inv.here.run(["tailscale", "ip", "-4"], timeout=WHOIS_TIMEOUT)
    if not r.ok or not r.out.split():
        die("this machine has no tailnet address to serve on: `tailscale ip -4` said %s\n    check `tailscale status`"
            % (r.err or r.out).strip(), NEEDS_PERSON)
    address = (r.out.split()[0], client.port(inv.fleet.controller))
    with Server(address, lambda method, path, at: answer(inv, method, path, at)) as server:
        info("serving the controller's API on %s:%d" % address)
        server.serve_forever()
