"""The claude.ai login document, and the placeholder a workspace holds: it never expires, so no CLI asks the token endpoint."""

import contextlib
import json
import os

PLACEHOLDER = "wk-injects-this"
SCOPES = ("user:inference", "user:profile", "user:sessions:claude_code")
NEVER_MS = 4102444800000


def placeholder():
    return json.dumps({"claudeAiOauth": {"accessToken": PLACEHOLDER, "refreshToken": PLACEHOLDER,
                                         "expiresAt": NEVER_MS, "scopes": list(SCOPES)}}) + "\n"


def parse(text):
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise ValueError("it is not JSON (%s)" % e)
    oauth = doc.get("claudeAiOauth") if isinstance(doc, dict) else None
    if not isinstance(oauth, dict):
        raise ValueError("it has no claudeAiOauth object, which the Claude CLI's /login writes")
    for key in ("accessToken", "refreshToken"):
        if not isinstance(oauth.get(key), str) or not oauth[key]:
            raise ValueError("its claudeAiOauth has no %s" % key)
    if PLACEHOLDER in (oauth["accessToken"], oauth["refreshToken"]):
        raise ValueError("it is a workspace's placeholder, not a login")
    if not isinstance(oauth.get("expiresAt"), (int, float)):
        raise ValueError("its claudeAiOauth has no expiresAt")
    return oauth


@contextlib.contextmanager
def locked(path, wait=60):
    from wk.act import Refused
    from wk.clock import Clock
    from wk.lock import Lock
    from wk.machine import Local
    from wk.store import Store
    lock = Lock(Store(dict(os.environ, WK_LOCK_DIR=os.path.dirname(path))), Local(), Clock())
    try:
        lock.hold("claude-login", wait)
    except Refused:
        raise TimeoutError("another holder kept the lock on %s for %ds" % (path, wait))
    try:
        yield
    finally:
        lock.release("claude-login")
