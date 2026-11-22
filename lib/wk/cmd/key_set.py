"""Store one credential in OpenBao, after checking it.

The value is read from standard input, or asked for without echo at a terminal; it is never an argument. A value of
several lines (a deploy key, the claude.ai login) is piped in: `wk key set deploy-key/<owner>/<repo> < keyfile`.
It is stored unless its shape is wrong or its issuer refuses it (`--force` stores what an issuer refuses); an issuer
that does not answer is said so.

The names: github-user, github-token, bugzilla-user, bugzilla-password, litellm-key, claude-login, tailnet-auth-key,
tailnet-api-key, wifi-ssid, wifi-psk, deploy-key/<owner>/<repo> and bmc/<machine>/user, bmc/<machine>/password.
`wk key check` says what each is for. litellm-key is the one an agent session can read."""

import getpass

from wk.core.act import barrier, die, info, warn
from wk.core.decl import Command
from wk.creds import rules

COMMAND = Command(where="client", takes=("name",))


def main(args, inv):
    rule = rules.find(args.name)
    store = inv.secrets()
    value = (getpass.getpass("%s (%s): " % (rule.name, rule.what)) if inv.stdin.isatty() else inv.stdin.read()).strip()
    why = rule.check(value)
    if why:
        die("that is not %s: %s\n    nothing is stored" % (rule.what, why))
    verdict, detail = rule.probe(value, rules.Context(store.get, inv.clock.now(), inv.fleet.here.get("litellm_url")))
    if verdict == rules.BAD:
        barrier("%s: %s\n    nothing is stored" % (rule.name, detail))
    if verdict == rules.UNKNOWN:
        warn("%s is stored unverified: %s" % (rule.name, detail))
    store.put(rule.name, value)
    info("stored %s%s; `wk key sync` delivers it" % (rule.name, ": " + detail if verdict == rules.OK else ""))
