"""Credentials: the store, each credential's rule, the injector, and what an agent session takes from them.

    wk.creds.secrets(inv)              the Secrets seam: OpenBao, as the person, on the client
    wk.creds.agent_env(inv)            the environment an agent session is started with to use the injector

Delivery to a server is the playbook's: the credentials, openbao and injector roles."""

from wk.creds.agent import agent_env  # noqa: F401
from wk.creds.bao import connect as secrets  # noqa: F401
