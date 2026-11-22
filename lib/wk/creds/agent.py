"""What an agent session takes from the credentials lane: the environment that points it at the injector, the forward
that brings the injector to a borrowed box, and the directories the wrapper hides."""

from wk.core import route
from wk.core.act import die
from wk.core.errors import NEEDS_PERSON
from wk.core.vars import VARS

CA = "injector-ca.pem"
LOGIN_DIR = "claude-login"


def injector(inv):
    """The injector's "host:port" on the tailnet."""
    vault = inv.fleet.credential
    return "%s:%s" % (vault.name, vault.get("injector_port", VARS["wk_injector_port"]))


def forward(inv, conf):
    """A borrowed box is off the tailnet, the one network the injector listens on: the hand-over carries it there."""
    return injector(inv) if conf.kind == "borrowed" else None


def agent_env(inv):
    """The environment an agent session, and nothing else, is started with: the injector as its proxy, the injector's
    CA added to its trust, and the directory holding the placeholder login."""
    at = injector(inv)
    if inv.fleet.here.kind == "borrowed":
        port = inv.env.get(route.FORWARD)
        if not port:
            die("%s is a borrowed box, off the tailnet: a session there reaches the injector through the ssh session that starts it\n"
                "    start it from the client: wk %s" % (inv.fleet.hostname, " ".join(inv.words)), NEEDS_PERSON)
        at = "127.0.0.1:" + port
    return {"HTTPS_PROXY": "http://" + at, "https_proxy": "http://" + at, "NODE_EXTRA_CA_CERTS": "%s/%s" % (inv.wk_home, CA),
            "CLAUDE_SECURESTORAGE_CONFIG_DIR": "%s/%s" % (inv.wk_home, LOGIN_DIR)}


def hidden(conf, wk_home):
    """The delivered credentials; on the credential server also the injector's directory (the real login) and
    OpenBao's (the store and its unseal key)."""
    return [wk_home + "/" + name for name in ("secrets",) + (("injector", "openbao") if conf.role else ())]
