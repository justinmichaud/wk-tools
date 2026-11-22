"""Credentials: the OpenBao client, each credential's rule, delivery to servers, and the injector.

    wk.creds.secrets(inv)       the Secrets seam: OpenBao, as the person, on the client
    wk.creds.agent_env(inv)     the environment an agent session is started with to use the injector"""

from wk.creds.bao import connect as secrets  # noqa: F401
from wk.creds.service import agent_env  # noqa: F401
