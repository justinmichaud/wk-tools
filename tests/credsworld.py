"""What the credential tests share: a World whose controller names a model endpoint, a scripted HTTP function, and a
claude.ai login that is built here and belongs to nobody."""

import json
from unittest import mock

from tests.world import CONFS, World
from wk.creds import injector, net

URL = "https://llm.example/v1"
TOKEN = "ghp_" + "x" * 12


def login(expires_ms=NotImplemented, access="access-one", refresh="refresh-one"):
    return json.dumps({"claudeAiOauth": {"accessToken": access, "refreshToken": refresh, "scopes": ["user:inference"],
                                         "expiresAt": injector.NEVER_MS if expires_ms is NotImplemented else expires_ms}})


def world(**secrets):
    w = World(here="vault", confs=dict(CONFS, vault="kind=controller\nlitellm_url=%s" % URL))
    w.secrets.values.update({name.replace("_", "-"): value for name, value in secrets.items()})
    return w


class Http:
    """A scripted `net.request`: answers[(method, url)] is (status, body), or an Exception to raise. `asked` records
    every request as (method, url, headers, body); an answer that is an exception, Killed too, is raised."""

    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def __call__(self, method, url, headers=None, body=None, timeout=None):
        self.asked.append((method, url, headers or {}, body))
        if (method, url) not in self.answers:
            raise AssertionError("an unscripted request: %s %s" % (method, url))
        answer = self.answers[(method, url)]
        if isinstance(answer, BaseException):
            raise answer
        status, body = answer
        return status, body if isinstance(body, bytes) else json.dumps(body).encode()

    def patch(self):
        return mock.patch.object(net, "request", self)


MODELS = ("GET", URL + "/models")
