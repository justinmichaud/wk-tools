"""How a program here says it is up: sd_notify to systemd (a no-op under nohup), and `send`, the ntfy publish to a person."""

import json
import os
import re
import secrets
import socket
import urllib.error
import urllib.request

TIMEOUT = 5
TOPIC = re.compile(r"^[-_A-Za-z0-9]{1,64}$")
GUESSABLE = 16
MINT_BYTES = 16   # twice GUESSABLE in hex, so `check` can never call a minted topic guessable


def api():
    return os.environ.get("WK_NTFY_API", "https://ntfy.sh")


def sd_notify(state):
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.connect(addr)
        sock.sendall(state.encode())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A reserved topic name (`docs`, `app`) answers 302 to the ntfy web site, which followed would read as 200."""

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
                "most 64 of them; %s answers 404 for anything else." % api())
    return ""


def hidden(topic, text):
    return text.replace(topic, "<the topic>") if topic and text else text


def check(topic):
    why = topic_reject(topic)
    if why:
        return 4, why
    ntfy = api()
    status, text = _http("GET", "%s/%s/json?poll=1" % (ntfy, topic))
    if status == 200 and len(topic) < GUESSABLE:
        return 3, ("%s serves it, and the name is %d characters -- under %d, so "
                   "it is a name someone could arrive at by guessing." % (ntfy, len(topic), GUESSABLE))
    if status == 200:
        return 0, "%s serves it, and the name is %d characters, so guessing it is not a way in." % (ntfy, len(topic))
    if status is None:
        return 6, hidden(topic, "could not reach %s (%s)" % (ntfy, text))
    if status in (301, 302, 303, 307, 308, 404):
        return 5, ("%s carries no topic by that name (HTTP %d): a reserved name "
                   "redirects to its web site and one it will not take is 404." % (ntfy, status))
    return 6, ("%s answered HTTP %d rather than 200, 404 or a redirect, so "
               "nothing about the topic was established." % (ntfy, status))


def publish(topic, headline, detail="", tag=""):
    why = topic_reject(topic)
    if why:
        return 4, why
    doc = {"topic": topic, "message": detail or headline}
    if detail:
        doc["title"] = headline
    if tag:
        doc["tags"] = [tag]
    ntfy = api()
    status, text = _http("POST", ntfy + "/", json.dumps(doc).encode())
    if status == 200:
        return 0, ""
    if status is None:
        return 6, hidden(topic, "could not reach %s (%s), so nothing was notified" % (ntfy, text))
    return 5, hidden(topic, "%s refused the publish (HTTP %d): %s" % (ntfy, status, text.strip()[:200]))


def send(root, headline, detail="", tag="", env=None, machine=None):
    """False, with the reason warned, whenever it did not go out: a caller carries on rather than ending a run."""
    from wk import act
    from wk.secrets import Secrets
    sec = Secrets(root, env=env, machine=machine)
    if not sec.cred_stored("ntfy"):
        act.warn("no ntfy topic on this machine: wk key set ntfy")
        return False
    code, why = publish((sec.cred_read("ntfy") or "").strip(), headline, detail, tag)
    if code:
        act.warn("wk-notify: " + why)
    return code == 0
