"""Credentials: the store, each credential's rule, the rotation of the push keys, the injector, and what an agent
session takes from them.

    wk.creds.secrets(inv)              the Secrets seam: files under the controller's <wk home>/secrets
    wk.creds.agent_env(inv)            the environment an agent session is started with to use the injector

Delivery to a server is the playbook's: the credentials and controller roles."""

from wk.creds.agent import agent_env  # noqa: F401
from wk.creds.store import files as secrets  # noqa: F401
