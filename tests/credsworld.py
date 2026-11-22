"""What the credential tests share: a World whose client names a model endpoint, a scripted HTTP function, and
credentials that are built here and belong to nobody."""

import base64
import json
import struct
from unittest import mock

from tests.world import CONFS, World
from wk.creds import injector, net

URL = "https://llm.example/v1"
TOKEN = "ghp_" + "x" * 12


def string(data):
    return struct.pack(">I", len(data)) + data


def private_key(cipher=b"none", fill=b"\1"):
    """An OpenSSH private key file in shape only: its key material is one repeated byte."""
    public = string(b"ssh-ed25519") + string(fill * 32)
    blob = b"openssh-key-v1\0" + string(cipher) + string(b"none") + string(b"") + struct.pack(">I", 1) + string(public) + string(fill * 64)
    return "-----BEGIN OPENSSH PRIVATE KEY-----\n%s\n-----END OPENSSH PRIVATE KEY-----" % base64.b64encode(blob).decode()


def public_key(fill=b"\1"):
    return "ssh-ed25519 " + base64.b64encode(string(b"ssh-ed25519") + string(fill * 32)).decode()


def login(expires_ms=NotImplemented, access="access-one", refresh="refresh-one"):
    return json.dumps({"claudeAiOauth": {"accessToken": access, "refreshToken": refresh, "scopes": ["user:inference"],
                                         "expiresAt": injector.NEVER_MS if expires_ms is NotImplemented else expires_ms}})


def world(**secrets):
    w = World(confs=dict(CONFS, desk="kind=client\nlitellm_url=%s" % URL))
    w.secrets.values.update({name.replace("_", "-"): value for name, value in secrets.items()})
    return w


class Http:
    """A scripted `net.request`: answers[(method, url)] is (status, body), or an Exception to raise. `asked` records
    every request as (method, url, headers, body)."""

    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def __call__(self, method, url, headers=None, body=None):
        self.asked.append((method, url, headers or {}, body))
        if (method, url) not in self.answers:
            raise AssertionError("an unscripted request: %s %s" % (method, url))
        answer = self.answers[(method, url)]
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return status, body if isinstance(body, bytes) else json.dumps(body).encode()

    def patch(self):
        return mock.patch.object(net, "request", self)


MODELS = ("GET", URL + "/models")
