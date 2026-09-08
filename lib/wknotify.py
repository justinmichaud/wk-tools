"""sd_notify for the Type=notify services here -- a no-op without NOTIFY_SOCKET, since the same programs run under nohup on the macOS host, whose python3 has no python3-systemd -- and the ntfy.sh publish `wk notify` reaches a person with."""

import json
import os
import re
import secrets
import socket
import sys
import urllib.error
import urllib.request

# WK_NTFY_API points this at another ntfy: a self-hosted one, or the local
# stub tests/test_notify.py answers with so no test reaches ntfy.sh.
NTFY = os.environ.get("WK_NTFY_API", "https://ntfy.sh")
TIMEOUT = 5

TOPIC = re.compile(r"^[-_A-Za-z0-9]{1,64}$")
GUESSABLE = 16
# Twice GUESSABLE in hex, so `check` can never call a minted topic guessable.
MINT_BYTES = 16

USAGE = (
    "usage: wknotify.py publish <headline> [--detail <text>] [--tag <name>]\n"
    "       wknotify.py check\n"
    "       wknotify.py mint\n"
    "The topic arrives on stdin, always: an argument is visible in `ps`.\n"
    "publish exits 0 when %s took it and never anything else: a caller warns\n"
    "on every non-zero code, so a message that landed has one answer.\n"
    "check exits 3 for a topic it serves whose name is under %d characters,\n"
    "which is a verdict and not a publish.\n"
    "Both: 4 no topic, 5 refused, 6 nothing established. One attempt, %ds,\n"
    "no retry.\n"
    "mint prints a fresh topic of %d characters on stdout and reads nothing:\n"
    "the topic is a secret wk makes, not one a person invents.\n"
    % (NTFY, GUESSABLE, TIMEOUT, MINT_BYTES * 2))


def sd_notify(state):
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.connect(addr)
        sock.sendall(state.encode())


# A reserved topic name (`docs`, `app`) answers 302 to the ntfy web site, and a
# followed redirect would read as 200 for a name that carries no topic.
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _http(method, url, body=None):
    req = urllib.request.Request(url, method=method, data=body)
    req.add_header("User-Agent", "wk-notify")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with _OPENER.open(req, timeout=TIMEOUT) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, "%s: %s" % (e.__class__.__name__, e)


def mint():
    return secrets.token_hex(MINT_BYTES)


def topic_reject(topic):
    if not topic:
        return "there is nothing there."
    if not TOPIC.match(topic):
        return ("an ntfy topic is one word of letters, digits, '-' and '_', at "
                "most 64 of them; %s answers 404 for anything else." % NTFY)
    return ""


def check(topic):
    status, text = _http("GET", "%s/%s/json?poll=1" % (NTFY, topic))
    if status == 200 and len(topic) < GUESSABLE:
        return 3, ("%s serves it, and the name is %d characters -- under %d, so "
                   "it is a name someone could arrive at by guessing."
                   % (NTFY, len(topic), GUESSABLE))
    if status == 200:
        return 0, ("%s serves it, and the name is %d characters, so guessing it "
                   "is not a way in." % (NTFY, len(topic)))
    if status is None:
        return 6, "could not reach %s (%s)" % (NTFY, text)
    if status in (301, 302, 303, 307, 308, 404):
        return 5, ("%s carries no topic by that name (HTTP %d): a reserved name "
                   "redirects to its web site and one it will not take is 404."
                   % (NTFY, status))
    return 6, ("%s answered HTTP %d rather than 200, 404 or a redirect, so "
               "nothing about the topic was established." % (NTFY, status))


def publish(topic, headline, detail, tag):
    doc = {"topic": topic, "message": detail or headline}
    if detail:
        doc["title"] = headline
    if tag:
        doc["tags"] = [tag]
    status, text = _http("POST", NTFY + "/", json.dumps(doc).encode())
    if status == 200:
        return 0, ""
    if status is None:
        return 6, ("could not reach %s (%s), so nothing was notified"
                   % (NTFY, text))
    return 5, ("%s refused the publish (HTTP %d): %s"
               % (NTFY, status, text.strip()[:200]))


def _out(code, msg, topic):
    if msg:
        if topic:
            msg = msg.replace(topic, "<the topic>")
        stream = sys.stdout if code == 0 else sys.stderr
        stream.write("wk-notify: %s\n" % msg)
    return code


def _publish_args(rest):
    headline, detail, tag = "", "", ""
    while rest:
        arg, rest = rest[0], rest[1:]
        if arg in ("--detail", "--tag"):
            if not rest:
                return None
            if arg == "--detail":
                detail = rest[0]
            else:
                tag = rest[0]
            rest = rest[1:]
        elif headline or arg.startswith("-"):
            return None
        else:
            headline = arg
    return (headline, detail, tag) if headline else None


def main(argv):
    verb = argv[1] if len(argv) > 1 else ""
    if verb == "mint" and len(argv) == 2:
        sys.stdout.write(mint() + "\n")
        return 0
    if verb not in ("publish", "check"):
        sys.stderr.write(USAGE)
        return 2
    topic = sys.stdin.read().strip()
    why = topic_reject(topic)
    if why:
        return _out(4, why, topic)
    if verb == "check":
        if len(argv) != 2:
            sys.stderr.write(USAGE)
            return 2
        return _out(*check(topic), topic=topic)
    parsed = _publish_args(argv[2:])
    if parsed is None:
        sys.stderr.write(USAGE)
        return 2
    return _out(*publish(topic, *parsed), topic=topic)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
