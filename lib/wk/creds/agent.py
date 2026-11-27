"""What an agent session takes from the credentials lane: the environment that points it at the injector."""

from wk.core.vars import VARS

CA = "injector-ca.pem"
LOGIN_DIR = "claude-login"


def injector(inv):
    """The injector's "host:port" on the tailnet."""
    controller = inv.fleet.controller
    return "%s:%s" % (controller.name, controller.get("injector_port", VARS["wk_injector_port"]))


def agent_env(inv):
    """The environment an agent session adds: the injector as its proxy, the injector's CA added to its trust, and
    the directory holding the placeholder login."""
    at = injector(inv)
    return {"HTTPS_PROXY": "http://" + at, "https_proxy": "http://" + at, "NODE_EXTRA_CA_CERTS": "%s/%s" % (inv.wk_home, CA),
            "CLAUDE_SECURESTORAGE_CONFIG_DIR": "%s/%s" % (inv.wk_home, LOGIN_DIR)}
