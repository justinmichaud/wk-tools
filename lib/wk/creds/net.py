"""The one function that speaks HTTP for wk: every credential's probe, the rotation and the controller's API go through
it, and a test replaces it, so no test reaches the network."""

import http.client
import urllib.error
import urllib.request

TIMEOUT = 20


class NoAnswer(Exception):
    """No HTTP answer came. What the far end would have said is unknown."""


def request(method, url, headers=None, body=None, timeout=TIMEOUT):
    """(status, body) of one request; NoAnswer when none came within `timeout` seconds. `body` is bytes."""
    req = urllib.request.Request(url, method=method, data=body, headers=dict({"User-Agent": "wk"}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.read()
    except (OSError, http.client.HTTPException) as e:
        raise NoAnswer("%s: %s" % (type(e).__name__, e))
